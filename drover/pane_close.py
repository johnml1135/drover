"""Close a pane Drover started once its agent is retired and the pane has sat unchanged for an hour.

A pane closes only when every gate holds. The pane is Drover's own: a `pane_agents` row that Drover started, whose state
is `retired`, and the newest open row for that pane. Herdr reports the agent `done`, and its terminal and session are
the recorded ones. Its state_change_seq, revision, terminal and visible screen stay unchanged for MIN_IDLE_S; the agent
list has no timestamp, so the clock starts at the first read and any change restarts it. The composer holds no draft,
nothing is queued against it, no dialog is open and the layout is recognised. It is not focused. Its checkout has no
uncommitted or untracked changes, and no process outside the pane's own tree uses it. Whether the branch is merged or
pushed is reported but does not block a close, because nothing here removes a checkout or branch.

Anything unknown keeps the pane: Herdr closed, an unreadable screen, a checkout git cannot read, processes that cannot be
listed. Another program's pane, an owner-started pane and a live agent are never candidates at all. Panes close by id
through `pane close`; nothing is killed. The pane is re-read right before the close, and each close is a `pane-closed`
ledger event naming the evidence and the reason, with the row moving to `gone`. The intent is saved before `pane close`;
a close that Herdr never confirms is settled on the next tick from whether the pane is gone, with exactly one event.
"""
import hashlib
import json
import logging

from . import config, git_client, pane_fault, pane_guard, pane_screen, pane_send, placement
from .git_client import QUICK, Answer
from .herdr_client import HerdrRefused, HerdrUnavailable
from .ledger import ops_panes
from .ledger.db import tx
from .pane_read import PaneRefused

MIN_IDLE_S = 3600
SETTING = 'pane_close_observed'
PENDING = 'pane_close_pending'
ACTOR = 'pane_close'
log = logging.getLogger(__name__)


class _Unknown(Exception):
    """git did not say, as a timeout did before: stop reading this checkout."""


def worktree_facts(cwd, run=None):
    """Facts about the checkout holding cwd, or None when it is not inside a readable git checkout. `run` is shaped
    like git_client.ask, which is the default."""
    try:
        return _facts(cwd, run or git_client.ask)
    except (_Unknown, *pane_fault.EXPECTED, ValueError):      # git timing out, or an answer that is not text
        return None     # a stuck or unreadable checkout is unknown, and unknown keeps the pane


def _facts(cwd, ask):
    def read(path, *args, no):
        reply = ask(path, *args, timeout_s=QUICK, no=no)
        if reply.answer is Answer.UNKNOWN:
            raise _Unknown(reply.why)
        return reply.answer is Answer.YES, reply.out.strip()
    found, top = read(cwd, 'rev-parse', '--show-toplevel', no=(128,))
    if not found or not top:
        return None
    clean, status = read(top, 'status', '--porcelain', '--untracked-files=all', no=())
    # A base that does not exist (verify -q exits 1) is a no: nothing to be merged into, as rc 128 read before.
    base = next((ref for ref in ('origin/main', 'main') if read(top, 'rev-parse', '--verify', '-q', ref, no=(1,))[0]), None)
    tracked, upstream = read(top, 'rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}', no=(128,))
    return dict(path=top, branch=read(top, 'rev-parse', '--abbrev-ref', 'HEAD', no=(128,))[1] or None,
                clean=clean and status == '',
                merged=base is not None and read(top, 'merge-base', '--is-ancestor', 'HEAD', base, no=(1,))[0],
                pushed=tracked and read(top, 'rev-list', '--count', upstream + '..HEAD', no=())[1] == '0')


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
    session = agent.get('agent_session')
    session = session.get('value') if isinstance(session, dict) else None
    if agent.get('pane_id') != pane or not row.get('terminal_id') or agent.get('terminal_id') != row['terminal_id'] \
            or agent.get('agent') != row.get('kind') or not row.get('session_id') or session != row['session_id'] \
            or agent.get('state_change_seq') is None or agent.get('revision') is None:
        return say('keep', 'the occupant is not the recorded agent session, or it cannot be told')
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
        queued = pane_guard.queued_input_range(screen)
    except ValueError as error:
        return say('keep', 'dialog open, or the screen cannot be read: ' + str(error))
    if queued is not None:
        return say('keep', 'follow-up input is queued against the composer')
    if pane_screen.status(screen, row['kind']) != 'idle' or not pane_screen.composer_empty(screen, row['kind']):
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


def _snapshot(row, cfg=None):
    """Processes on the side the hub runs on. The other side cannot be listed from here, so it is unknown."""
    if row.get('placement') != placement.cleanup_side():
        return None
    ignore = list((config.get() if cfg is None else cfg)['disk_cleanup']['process_ignore'])
    return placement.cleanup_processes(paths={'process_ignore': ignore}, clear_ignored=True)


def _busy(herdr, row, pane, path, processes):
    """Whether a process outside the pane's own tree uses `path`; None when that cannot be known."""
    try:
        info = herdr.call('pane', 'process-info', '--pane', pane).get('process_info')
    except HerdrRefused:
        return None
    listed = info.get('foreground_processes', []) if isinstance(info, dict) else None
    if not isinstance(listed, list) or 'shell_pid' not in info or not all(isinstance(p, dict) and 'pid' in p for p in listed):
        return None
    own = {info['shell_pid'], *(p['pid'] for p in listed)}
    try:
        proof = processes(row)
    except pane_fault.EXPECTED:
        return None
    if proof is None or proof.unknown:
        return None
    return proof.busy_outside(path, own)


def _kept(row, reason):
    return dict(pane=row['pane_id'], agent_id=row['id'], name=row.get('name'), status=None, idle_s=None,
                fingerprint=None, worktree=None, verdict='keep', reason=reason)


def assess(herdr, row, *, now, observed, run=None, processes=None):
    """Read the pane and judge it. Herdr closed raises HerdrUnavailable; a pane Herdr will not show is kept."""
    pane = row['pane_id']
    try:
        agent = herdr.call('agent', 'get', pane).get('agent')
        if not isinstance(agent, dict):
            return _kept(row, 'agent unreadable')
        screen = herdr.text('pane', 'read', pane, '--source', 'visible') if agent.get('agent_status') == 'done' else ''
    except HerdrRefused:
        return _kept(row, 'pane or agent not readable')
    facts = None
    cwd = agent.get('foreground_cwd') or agent.get('cwd')
    if cwd:
        facts = worktree_facts(cwd, run)
        if facts:
            facts['busy'] = _busy(herdr, row, pane, facts['path'], processes or _snapshot)
    return judge(row, agent, screen, facts, now=now, observed=observed)


def _event(port, kind, entry, row, reason):
    data = dict(pane=entry['pane'], agent_id=row['id'], name=row.get('name'), seat=row['seat'],
                placement=row['placement'], evidence=entry.get('fingerprint'), idle_s=entry.get('idle_s'),
                worktree=entry.get('worktree'), reason=reason)
    port.append_event(kind, data, actor=ACTOR)


def _load(port, key):
    """A dict of dicts from a ledger setting; anything else restarts empty, which only delays closes."""
    try:
        data = json.loads(port.get_setting(key) or '{}')
    except ValueError:
        return {}
    return data if isinstance(data, dict) and all(isinstance(v, dict) for v in data.values()) else {}


def _save(port, key, data):
    port.set_setting(key, json.dumps(data, sort_keys=True))


def _settle(port, pending, row):
    pending.pop(str(row['id']), None)
    _save(port, PENDING, pending)


def _gone(port, row, pending, entry, reason):
    """The pane is not there: one pane-closed event, the row to `gone`, the pending mark cleared."""
    with tx(port.conn):     # one commit: a failure between the steps leaves no event and the pending mark
        _event(port, 'pane-closed', entry, row, reason)
        ops_panes.update_agent(port.conn, row['id'], state='gone')
        _settle(port, pending, row)


def reconcile(port, herdr, row, pending):
    """A close that was not confirmed: ask Herdr whether the pane is gone and write exactly one event for it."""
    entry = pending[str(row['id'])]
    try:
        herdr.call('pane', 'get', row['pane_id'])
    except HerdrRefused as error:
        if error.code != 'pane_not_found':
            return _kept(row, 'close unconfirmed and the pane cannot be read: ' + error.code)
        _gone(port, row, pending, entry, 'gone after an unconfirmed close; ' + str(entry.get('reason')))
        return dict(_kept(row, entry.get('reason')), verdict='closed', fingerprint=entry.get('fingerprint'))
    _event(port, 'pane-close-failed', entry, row, 'close_unconfirmed_and_the_pane_remains')
    _settle(port, pending, row)
    return dict(_kept(row, 'the close was not confirmed and the pane remains'), verdict='close-failed')


def close_if_still_eligible(port, herdr, row, result, *, now, observed, pending, run=None, processes=None):
    """Re-read the row and the pane right before the mutation; only a fresh 'close' verdict reaches `pane close`."""
    if result['verdict'] != 'close':
        return result
    current = ops_panes.by_pane(port.conn, row['pane_id'])
    if current is None or current['id'] != row['id']:
        return dict(result, verdict='keep', reason='not closed: the pane has another claim now')
    checked = assess(herdr, current, now=now, observed=observed, run=run, processes=processes)
    if checked['verdict'] != 'close':
        return dict(checked, reason='rechecked before close: ' + checked['reason'])
    entry = dict(pane=checked['pane'], fingerprint=checked['fingerprint'], idle_s=checked['idle_s'],
                 worktree=checked['worktree'], reason=checked['reason'])
    pending[str(current['id'])] = entry     # written first: a crash or a late answer is settled by the next tick
    _save(port, PENDING, pending)
    try:
        pane_send.close_pane(port, herdr, current)
    except PaneRefused as refused:      # nothing went out, so nothing is left to settle later
        _settle(port, pending, current)
        return dict(checked, verdict='keep', reason='not closed: ' + refused.reason)
    except HerdrRefused as error:
        if error.code == 'pane_not_found':
            _gone(port, current, pending, entry, 'the pane was already gone; ' + checked['reason'])
            return dict(checked, verdict='closed')
        _event(port, 'pane-close-failed', entry, current, error.code)
        _settle(port, pending, current)
        return dict(checked, verdict='close-failed', reason=error.code)
    except HerdrUnavailable:
        return dict(checked, verdict='keep', reason='close unconfirmed: the next tick settles it')
    _gone(port, current, pending, entry, checked['reason'])
    return dict(checked, verdict='closed')


def tick(port, herdr, now, *, apply=False, run=None, processes=None, cfg=None) -> list[dict]:
    """Judge every retired Drover pane; a dry run unless `apply`, which closes those that pass twice. No pane's failure skips the rest."""
    processes = processes or (lambda row: _snapshot(row, cfg))
    rows = [r for r in ops_panes.open_agents(port.conn) if r['state'] == 'retired']
    observed, pending = _load(port, SETTING), _load(port, PENDING)
    results = []
    for row in rows:
        try:
            owner = ops_panes.by_pane(port.conn, row['pane_id'])
            if owner is None or owner['id'] != row['id']:
                continue    # a newer open row owns this pane: it is not retired
            if str(row['id']) in pending:
                results.append(reconcile(port, herdr, row, pending))
                continue
            result = assess(herdr, row, now=now, observed=observed, run=run, processes=processes)
            if apply:
                result = close_if_still_eligible(port, herdr, row, result, now=now, observed=observed,
                                                 pending=pending, run=run, processes=processes)
            results.append(result)
        except HerdrUnavailable:
            results.append(_kept(row, 'herdr unavailable: nothing is known about the pane'))
        except Exception as error:      # one pane's failure is that pane's keep, never the rest's
            pane_fault.note(port, log, f"Judging pane {row.get('pane_id')}", error)
            results.append(_kept(row, 'not judged: %s' % type(error).__name__))
    present = {r['pane_id'] for r in rows} - {r['pane'] for r in results if r['verdict'] == 'closed'}
    _save(port, SETTING, {p: v for p, v in observed.items() if p in present})
    return results
