"""Close a pane Drover started once its agent is retired and the pane has sat unchanged for an hour.

A pane closes only when every gate holds. The pane is Drover's own: a `pane_agents` row that Drover started, whose state
is `retired`, and the newest open row for that pane. Herdr reports the agent `done`, and its terminal is the recorded
one. Its state_change_seq, revision, terminal and visible screen stay unchanged for MIN_IDLE_S; the agent list has no
timestamp, so the clock starts at the first read and any change restarts it. The composer holds no draft, no dialog is
open and the layout is recognised. It is not focused. Its checkout has no uncommitted or untracked changes, and no
process outside the pane uses it. Whether the branch is merged or pushed is reported but does not block a close,
because nothing here removes a checkout or branch.

Anything unknown keeps the pane: Herdr closed, an unreadable screen, a checkout git cannot read, processes that cannot be
listed. Another program's pane, an owner-started pane and a live agent are never candidates at all. Panes close by id
through `pane close`; nothing is killed. The pane is re-read right before the close, and each close is a `pane-closed`
ledger event naming the evidence and the reason, with the row moving to `gone`.
"""
import hashlib
import json

from . import pane_guard, pane_screen, placement, placement_git
from .herdr_client import HerdrRefused, HerdrUnavailable
from .ledger import ops_panes

MIN_IDLE_S = 3600
SETTING = 'pane_close_observed'
ACTOR = 'pane_close'


def worktree_facts(cwd, run=None):
    """Facts about the checkout holding cwd, or None when it is not inside a readable git checkout."""
    run = run or _git
    rc, top = run(cwd, 'rev-parse', '--show-toplevel')
    if rc or not top:
        return None
    rc, status = run(top, 'status', '--porcelain', '--untracked-files=all')
    base = 'origin/main' if run(top, 'rev-parse', '--verify', '-q', 'origin/main')[0] == 0 else 'main'
    rc_up, upstream = run(top, 'rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}')
    return dict(path=top, branch=run(top, 'rev-parse', '--abbrev-ref', 'HEAD')[1] or None,
                clean=rc == 0 and status == '',
                merged=run(top, 'merge-base', '--is-ancestor', 'HEAD', base)[0] == 0,
                pushed=rc_up == 0 and run(top, 'rev-list', '--count', upstream + '..HEAD')[1] == '0')


def _git(cwd, *args):
    done = placement_git.git(cwd, *args, timeout=30)
    return done.returncode, (done.stdout or '').strip()


def fingerprint(agent, screen):
    """What moves when a pane's work moves; a change in any part restarts the clock."""
    return dict(terminal_id=agent.get('terminal_id'), state_change_seq=agent.get('state_change_seq'),
                revision=agent.get('revision'), screen=hashlib.sha256(screen.encode('utf-8')).hexdigest()[:16])


def observe(observed, pane, mark, now):
    """Epoch seconds when this exact fingerprint was first seen for the pane."""
    prior = observed.get(pane)
    if prior is None or {k: prior.get(k) for k in mark} != mark:
        observed[pane] = dict(mark, first_seen=now)
    return observed[pane]['first_seen']


def judge(row, agent, screen, facts, *, now, observed):
    """One pane's verdict: 'close', 'wait' (all gates hold, not yet unchanged for MIN_IDLE_S) or 'keep', and why.

    `facts` is worktree_facts plus `busy`: True, False, or None when the processes could not be listed.
    """
    pane = row.get('pane_id')
    out = dict(pane=pane, agent_id=row.get('id'), name=row.get('name'), status=agent.get('agent_status'),
               idle_s=None, fingerprint=None, worktree=facts)

    def say(verdict, reason):
        return dict(out, verdict=verdict, reason=reason)

    if row.get('started_by') != 'drover' or row.get('state') != 'retired':
        return say('keep', 'not a retired Drover agent')
    if agent.get('pane_id') != pane or not row.get('terminal_id') or agent.get('terminal_id') != row['terminal_id'] \
            or agent.get('agent') not in (None, row.get('kind')):
        return say('keep', 'the occupant is not the recorded agent, or it cannot be told')
    if agent.get('agent_status') != 'done':
        observed.pop(pane, None)    # leaving done restarts the clock
        return say('keep', 'not done (%s): idle and working panes never close' % agent.get('agent_status'))
    if not isinstance(screen, str) or not screen.strip():
        return say('keep', 'screen unreadable')
    mark = fingerprint(agent, screen)
    idle = now - observe(observed, pane, mark, now)
    out.update(fingerprint=mark, idle_s=round(idle))
    if agent.get('focused') is not False:
        return say('keep', 'focused, or focus unknown')
    try:
        pane_guard.refuse_dialog(screen)
    except ValueError as error:
        return say('keep', 'dialog open: ' + str(error))
    if pane_screen.status(screen, row['kind']) != 'idle':
        return say('keep', 'composer holds a draft, or the layout is not recognised')
    if not isinstance(facts, dict):
        return say('keep', 'worktree unknown: not inside a readable git checkout')
    if not facts.get('clean'):
        return say('keep', 'uncommitted or untracked changes in ' + str(facts.get('path')))
    if facts.get('busy') is None:
        return say('keep', 'process visibility unavailable for ' + str(facts.get('path')))
    if facts['busy']:
        return say('keep', 'live process outside the pane uses ' + str(facts.get('path')))
    if idle < MIN_IDLE_S:
        return say('wait', 'unchanged for %d of %d s' % (idle, MIN_IDLE_S))
    note = '' if facts.get('merged') or facts.get('pushed') else '; branch neither merged nor pushed: checkout kept'
    return say('close', 'retired and unchanged %d s; composer empty, no dialog, unfocused, checkout clean%s'
               % (idle, note))


def _snapshot(row):
    """Processes on the side the hub runs on. The other side cannot be listed from here, so it is unknown."""
    if row.get('placement') != placement.cleanup_side():
        return None
    return placement.cleanup_processes(request={})


def _busy(herdr, row, pane, path, processes):
    """Whether a process outside the pane uses `path`; None when that cannot be known."""
    try:
        info = herdr.call('pane', 'process-info', '--pane', pane)['process_info']
        own = {info['shell_pid'], *(p['pid'] for p in info.get('foreground_processes', []))}
        proof = processes(row)
    except (KeyError, TypeError, HerdrRefused):
        return None
    if proof is None or proof.unknown:
        return None
    return bool(proof.busy_outside(path, own))


def assess(herdr, row, *, now, observed, run=None, processes=None):
    """Read the pane and judge it. Herdr closed raises HerdrUnavailable; a pane Herdr refuses to show is kept."""
    pane = row['pane_id']
    try:
        agent = herdr.call('agent', 'get', pane)['agent']
        screen = herdr.text('pane', 'read', pane, '--source', 'visible') if agent.get('agent_status') == 'done' else ''
    except (KeyError, TypeError):
        return judge(row, {}, '', None, now=now, observed=observed)
    except HerdrRefused:
        out = judge(row, {}, '', None, now=now, observed=observed)
        return dict(out, reason='pane or agent not readable')
    facts = None
    cwd = agent.get('foreground_cwd') or agent.get('cwd')
    if cwd:
        facts = worktree_facts(cwd, run)
        if facts:
            facts['busy'] = _busy(herdr, row, pane, facts['path'], processes or _snapshot)
    return judge(row, agent, screen, facts, now=now, observed=observed)


def _event(port, kind, result, row, reason):
    data = dict(pane=result['pane'], agent_id=row['id'], name=row.get('name'), seat=row['seat'],
                placement=row['placement'], evidence=result['fingerprint'], idle_s=result['idle_s'],
                worktree=result['worktree'], reason=reason)
    port.append_event(kind, data, actor=ACTOR)


def close_if_still_eligible(port, herdr, row, result, *, now, observed, run=None, processes=None):
    """Re-read the row and the pane right before the mutation; only a fresh 'close' verdict reaches `pane close`."""
    if result['verdict'] != 'close':
        return result
    current = ops_panes.by_pane(port.conn, row['pane_id'])
    if current is None or current['id'] != row['id']:
        return dict(result, verdict='keep', reason='not closed: the pane has another claim now')
    checked = assess(herdr, current, now=now, observed=observed, run=run, processes=processes)
    if checked['verdict'] != 'close':
        return dict(checked, reason='rechecked before close: ' + checked['reason'])
    try:
        herdr.call('pane', 'close', current['pane_id'])
    except HerdrRefused as error:
        _event(port, 'pane-close-failed', checked, current, error.code)
        return dict(checked, verdict='close-failed', reason=error.code)
    _event(port, 'pane-closed', checked, current, checked['reason'])
    ops_panes.update_agent(port.conn, current['id'], state='gone')
    return dict(checked, verdict='closed')


def tick(port, herdr, now, *, apply=True, run=None, processes=None) -> list[dict]:
    """Judge every retired Drover pane; with apply, close those that pass twice. Never raises on a closed Herdr."""
    rows = [r for r in ops_panes.open_agents(port.conn) if r['state'] == 'retired']
    try:
        observed = json.loads(port.get_setting(SETTING) or '{}')
    except ValueError:
        observed = {}   # an unreadable baseline restarts every clock, which only delays closes
    results = []
    for row in rows:
        try:
            if ops_panes.by_pane(port.conn, row['pane_id'])['id'] != row['id']:
                continue    # a newer open row owns this pane: it is not retired
            result = assess(herdr, row, now=now, observed=observed, run=run, processes=processes)
            if apply:
                result = close_if_still_eligible(port, herdr, row, result, now=now, observed=observed, run=run,
                                                 processes=processes)
        except HerdrUnavailable:
            results.append(dict(pane=row['pane_id'], agent_id=row['id'], verdict='keep',
                                reason='herdr unavailable: nothing is known about the pane'))
            break
        results.append(result)
    present = {r['pane_id'] for r in rows} - {r['pane'] for r in results if r['verdict'] == 'closed'}
    port.set_setting(SETTING, json.dumps({p: v for p, v in observed.items() if p in present}, sort_keys=True))
    return results
