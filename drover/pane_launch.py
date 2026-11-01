"""Drover starts an agent in a pane, and proves it before it counts.

The row is recorded before any input, and input goes only through `pane_send`. A launch goes out once. One that stalled
ends its row `failed` and is never resent: the agent may be up, so the next `start` opens a fresh row only after a read
shows that pane holds no agent. A dialog this module may not answer leaves the pane to the owner with one notice.

The row goes `live` with its session before the brief does, as `pane_send` requires, and `start` returns there: about
100 s at most, inside the tick. All that follows is `advance`, one read a tick with no wait, so nothing behind a launch
waits on an agent. The row's brief write is the state: none, `sent`, then `observed` once the first turn shows.

`working` on screen or from Herdr proves a first turn, and so does the run's session acting on the ledger, since a turn
can start and end between two ticks. An idle composer proves nothing, and `blocked` may be a dialog from before the
brief landed. A brief that fails ends a `live` row as `failed`, unless the agent is busy. That is an agent at work, so the row
stays and nothing is resent, but past STARTUP_S it is one notice and its session is no longer renewed, as with an unknown read.

A run's session takes one brief. A compaction resume is the same run on a new row, so it takes one only if none went out.
"""
import logging
import os
import re
import time
from datetime import datetime

from . import config, handoff, pane_blank, pane_fault, pane_files, pane_guard, pane_screen, pane_send, placement, runner
from .config import state_dir
from .herdr_client import HerdrRefused, HerdrUnavailable
from .launch import AdmissionHeld
from .ledger import disk_notices, ops_panes
from .pane_files import NATIVE_COMPACT, repo_trusted, write_private
from .pane_send import PaneRefused
from .pane_tab import (_dialog, _look, _no_agent, _read_alone, _rename, _stall_reason, _terminal, _workspace, open_pane,
                       session_of)

__all__ = ['NATIVE_COMPACT', 'FIRST_TURN_S', 'START_DEADLINE_S', 'advance', 'age', 'blocked', 'brief', 'expire', 'open_pane', 'publish',
           'repo_trusted', 'resume', 'rotate', 'session_of', 'start', 'write_private']

FIRST_TURN_S = 90
STARTUP_S = 60
START_DEADLINE_S = 300      # well past the longest wait a `starting` row sees, slow calls included: only a dead hub gets here
SHELL_PROMPT_S = 30         # a new tab's shell draws its prompt in its own time; Task 15 measures how long that is
SHELL_BACK_S = 10           # the environment line must end before `agent start` finds a free shell
POLL_S = 2
SHELL_READS = 2             # a launch line takes a moment to become a process, so one read of a lone shell proves nothing
RUNNING = ops_panes.RUNNING
RESUMABLE = ('live', 'compaction')
SHELL_BUSY = ('shell-not-alone', 'shell-line-not-empty')
SHELL_NOT_READY = 'shell-not-ready'
SUPERSEDED = ('wake', 'handoff', 'stop', 'interrupt')
OWNERS_NOW = ('dialog', 'suspected-dialog', 'draft', 'occupant-changed', 'herdr-refused:agent_blocked')    # waiting cannot clear these
HERDR_CLOSED = 'herdr-unavailable'
BUSY = 'busy'
BRIEF_STUCK = 'A director has not taken its brief'
STUCK_KEY = 'pane_brief_stuck:'     # a row's setting: its first brief has been held up by a busy agent past the bound
DEFAULT_PROMPT = 'Read your inbox and commit a checkpoint.'
_now = time.monotonic
log = logging.getLogger(__name__)


def age(stamp, now):
    return now - datetime.fromisoformat(stamp).timestamp()


def _ticks(seconds):
    deadline = _now() + seconds
    while _now() < deadline:
        yield
        time.sleep(POLL_S)


def brief(spec, handoff=None) -> str:
    """One short line, so the screen can prove it is Drover's. Anything longer belongs in a file."""
    first = f'First read the handoff at {handoff} and restate its standing rules in restated.md beside it. ' if handoff else ''
    return f"Drover: you are director role {spec['role_id']}. {first}" + ' '.join(str(spec.get('user_prompt') or DEFAULT_PROMPT).split())


def publish(port, notice_id, title, body):
    disk_notices.publish(port.conn, dict(id=notice_id, kind='pane', title=title, body=body), kind='pane.notice',
                         epic_title='Directors', actor='panes')


def _fail(port, herdr, row, reason, *, notice=True, outcome='The pane is left as it is. Closing it frees the role.') -> dict:
    """End the row. Its writes close as `observed`, never `unsent`, so the launch claim stands."""
    for write in ops_panes.open_writes(port.conn, row['id']):
        ops_panes.finish_write(port.conn, write['id'], 'observed', 'launch-failed')
    failed = ops_panes.update_agent(port.conn, row['id'], state='failed')
    port.append_event('pane-launch-failed', dict(row=row['id'], pane_id=row['pane_id'], seat=row['seat'], placement=row['placement'],
                      reason=reason, shell_alone=_read_alone(herdr, row)), actor='panes')
    if notice:
        publish(port, f"pane-launch:{row['id']}", 'An agent did not start in its pane',
                f"Seat {row['seat']} ({row['placement']}), pane {row['pane_id']}: {reason}. {outcome}")
    return failed


def expire(port, herdr, row, reason='launch-deadline', **told) -> dict:
    """End a row whose launch cannot finish. Nothing is resent: the agent may be up, so the row stays to say so."""
    return _fail(port, herdr, row, reason, **told)


def _prepare(spec, cfg, *, resume_id=None):
    """(seat, args). Nothing here reaches a pane or Herdr."""
    seat = config.seat_by_name(spec['seat'], cfg)
    if seat is None:
        raise AdmissionHeld('pane-seat-unknown')
    if spec['backend'] not in ('claude', 'codex'):
        raise AdmissionHeld('pane-kind-unsupported')
    if spec['backend'] == 'codex' and spec['placement'] != 'wsl':
        raise AdmissionHeld('codex-on-windows')
    if spec['placement'] == 'wsl' and not cfg['hub']['api_listen_wsl']:
        raise AdmissionHeld('hub-unreachable-from-wsl')
    settings = None
    if spec['backend'] == 'claude' and spec['placement'] == 'wsl':
        settings = placement.to_wsl_path(_folder(spec, cfg) / 'settings.json')
    roots = [handoff.agent_path(handoff.folder(cfg, spec['role_id']), spec['placement'])]
    try:
        args = pane_files.agent_args(dict(spec, run_config=dict(sandbox_writable_roots=roots)), model=config.model_id(spec['model'], cfg),
                                     effort=_effort(spec, cfg), settings=settings, resume=resume_id)
    except ValueError:
        raise AdmissionHeld('permission-bypass-refused') from None
    return seat, args


def _effort(spec, cfg):
    return spec.get('effort') or cfg['director']['effort']


def _folder(spec, cfg):
    return state_dir(cfg) / 'panes' / str(spec['run_id'])


def _clear_role(port, herdr, spec):
    """Never a second agent for a role: a failed launch may have started one, so its pane must show none."""
    for row in ops_panes.open_agents(port.conn):
        if row['role_id'] != spec['role_id']:
            continue
        if row['state'] in RUNNING:
            raise AdmissionHeld('pane-role-occupied')
        if row['state'] == 'failed' and not _no_agent(herdr, row):
            raise AdmissionHeld('pane-role-unconfirmed')


def _write_credential(spec, credential, cfg):
    name = 'credential' if spec['placement'] == 'windows' else f"run-{int(spec['run_id'])}.credential"
    write_private(_folder(spec, cfg) / name, credential, placement=spec['placement'])


def _write_files(spec, seat, args, cfg):
    folder = _folder(spec, cfg)
    handoff.folder(cfg, spec['role_id']).mkdir(parents=True, exist_ok=True)
    if '--settings' in args:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'settings.json').write_text(pane_files.hook_settings(spec['backend']), encoding='utf-8', newline='\n')
    return pane_files.write_launcher(folder, spec, seat, args, credential=str(folder / 'credential'),
                                     port=cfg['hub']['api_port'], distro=placement.distro(cfg['paths']['wsl_distro']))


def _when_shell_is_free(seconds, write, late):
    """`write` once the shell takes it. A refusal here means nothing went out, so asking again resends nothing."""
    for _ in _ticks(seconds):
        try:
            return write()
        except PaneRefused as refused:
            if refused.reason not in SHELL_BUSY:
                raise
    raise PaneRefused(late)


def _launch(port, herdr, row, launcher, args):
    """The last write that went out. A PaneRefused means that write did not.

    Only a new tab is waited for. A pane being reused shows what it shows: one read, and a refusal goes to a fresh pane.
    """
    line = "& '" + launcher.replace("'", "''") + "'"
    run = lambda: pane_send.start_in_shell(port, herdr, row, line)
    written = run() if ops_panes.reused_pane(port.conn, row) else _when_shell_is_free(SHELL_PROMPT_S, run, SHELL_NOT_READY)
    if row['placement'] == 'wsl' or written['state'] != 'sent':
        return written
    return _when_shell_is_free(SHELL_BACK_S, lambda: pane_send.start_agent(port, herdr, row, row['name'], args), 'shell-not-back')


def _answer_trust(port, herdr, row, cfg):
    """True when the one Enter went out. The row proves fresh and this proves the repo; the rest stays with the owner."""
    if not repo_trusted(row['cwd'], config.seat_by_name(row['seat'], cfg)):
        return False
    try:
        written = pane_send.answer_startup(port, herdr, row, trusted=True)
    except PaneRefused:
        return False
    if written['state'] != 'sent':
        return False
    port.append_event('pane-trust-answered', dict(row=row['id'], pane_id=row['pane_id'], cwd=row['cwd']), actor='panes')
    return True


def _settle(port, herdr, row, cfg) -> dict:
    """Wait for the agent to report, the row live on success and failed on anything else. Types only the trust Enter."""
    shells = 0
    for _ in _ticks(STARTUP_S):
        try:
            alone, screen, agent = _look(herdr, row)
        except PaneRefused:
            continue
        shells = shells + 1 if alone else 0
        if alone:
            if shells >= SHELL_READS:
                return _fail(port, herdr, row, 'agent-not-running')
            continue
        if pane_guard.startup_keys(screen, fresh_pane=True, kind=row['kind']):
            if not _answer_trust(port, herdr, row, cfg):
                return _fail(port, herdr, row, 'trust-left-for-the-owner')
            continue
        if _dialog(screen):
            return _fail(port, herdr, row, 'dialog')
        if session_of(agent):
            live = ops_panes.update_agent(port.conn, row['id'], session_id=session_of(agent), state='live')
            _rename(herdr, live)
            return live
    return _fail(port, herdr, row, 'agent-not-ready')


def _launched(port, herdr, row, launcher, args, cfg) -> dict:
    try:
        written = _launch(port, herdr, row, launcher, args)
    except PaneRefused as refused:
        if refused.reason == SHELL_NOT_READY and pane_blank.close_blank(port, herdr, row):
            _fail(port, herdr, row, refused.reason, outcome='Its blank tab was closed.')
            return ops_panes.update_agent(port.conn, row['id'], state='gone')
        return _fail(port, herdr, row, refused.reason)
    if written['state'] != 'sent':
        return _fail(port, herdr, row, _stall_reason(herdr, row))
    return _settle(port, herdr, row, cfg)


def _plain(text):
    return re.sub(r'\s+', '-', str(text).lower())


def _check_footer(port, row, screen, spec, cfg):
    """A notice, never a restart. A footer nobody can read says nothing."""
    found = pane_screen.footer_model(screen)
    wanted = {_plain(spec['model']), _plain(config.model_id(spec['model'], cfg))}
    if found and (found[0] not in wanted or found[1] != _plain(_effort(spec, cfg))):
        publish(port, f"pane-model:{row['id']}", 'An agent runs on a different model',
                f"Seat {row['seat']} ({row['placement']}), pane {row['pane_id']}: the footer shows {found[0]} {found[1]}, "
                f"not {spec['model']} {_effort(spec, cfg)}.")


def brief_stuck(port, row) -> bool:
    return bool(port.get_setting(f"{STUCK_KEY}{row['id']}"))


def _busy(port, row, since, now):
    """A working agent keeps its row, and nothing goes to it twice. Past the bound the owner hears, and its session may lapse."""
    if age(since, now) >= STARTUP_S and not brief_stuck(port, row):
        port.set_setting(f"{STUCK_KEY}{row['id']}", '1')
        publish(port, f"pane-brief-stuck:{row['id']}", BRIEF_STUCK,
                f"Seat {row['seat']} ({row['placement']}), pane {row['pane_id']}: it has stayed busy since it started, so it does not "
                'know what it is for and gets no wakes. Nothing is resent, and its session is left to expire.')


def _unstuck(port, row):
    if brief_stuck(port, row):
        port.set_setting(f"{STUCK_KEY}{row['id']}", '')
        disk_notices.retract(port.conn, f"pane-brief-stuck:{row['id']}", kind='pane.notice', actor='panes')


def _send_brief(port, herdr, row, spec, guard, now):
    """The brief if the pane takes it now. None if it cannot go yet, or the row has ended."""
    try:
        guard()
    except AdmissionHeld:       # `recover` stops the run, or a pause lifts: either way the brief waits
        return None
    try:
        written = pane_send.send(port, herdr, row, brief(spec, spec.get('handoff')), 'brief')
    except PaneRefused as refused:
        if refused.reason == BUSY:
            _busy(port, row, row['updated'], now)
        elif refused.reason in OWNERS_NOW or (refused.reason != HERDR_CLOSED and age(row['updated'], now) >= STARTUP_S):
            _fail(port, herdr, row, refused.reason)
        return None
    if written['state'] != 'sent':
        _unsure(port, herdr, row, written, now)
        return None
    _unstuck(port, row)
    return written


def _unsure(port, herdr, row, write, now):
    """A brief that may have gone out is never sent again. Only a busy agent keeps its row."""
    if write['reason'] == BUSY:
        _busy(port, row, write['at'], now)
    else:
        _fail(port, herdr, row, 'brief-unverified')


def _brief_owed(port, row, spec):
    """A later row of a run owes one only if none went out, or this row's own is in hand. Only a handoff briefs a new run's reused pane."""
    if ops_panes.continues_run(port.conn, row):
        brief = ops_panes.run_brief(port.conn, row['run_id'])
        return brief is None or brief['agent_id'] == row['id']
    return bool(spec.get('handoff')) or not ops_panes.reused_pane(port.conn, row)


def _first_turn(port, herdr, row, write, spec, now, cfg) -> bool:
    """One read. Only `working` counts from the pane, and its footer is read only then."""
    try:
        seen = pane_send.observe(herdr, row)
    except PaneRefused as refused:
        if refused.reason == HERDR_CLOSED:
            return False
        if refused.reason == 'occupant-changed':
            _fail(port, herdr, row, 'first-turn-unproven')
            return False
        seen = None
    working = seen and (seen['agent'].get('agent_status') == 'working' or pane_screen.status(seen['screen'], row['kind']) == 'working')
    if working or ops_panes.session_acted(port.conn, spec.get('session_id')):
        ops_panes.finish_write(port.conn, write['id'], 'observed', 'turn-seen')
        if working:
            _check_footer(port, row, seen['screen'], spec, cfg)
        return True
    if age(write['at'], now) >= FIRST_TURN_S:
        _fail(port, herdr, row, 'first-turn-unproven')
    return False


def advance(port, herdr, row, spec, guard, now, *, cfg) -> bool:
    """Take a `live` row one step past its launch, with no wait: True once none is left.

    Herdr unreachable ends nothing: a deadline counts only on a read that answered, and a guard that refuses waits.
    """
    if row['state'] != 'live' or not _brief_owed(port, row, spec):
        return True
    write = ops_panes.last_write(port.conn, row['id'], 'brief')
    if write is None or write['state'] == 'unsent':
        write = _send_brief(port, herdr, row, spec, guard, now)
        if write is None:
            return False
    if write['state'] == 'observed':
        return True
    if write['state'] != 'sent':        # a hub that died between the note and the call: it may have landed, so never again
        _unsure(port, herdr, row, write, now)
        return False
    return _first_turn(port, herdr, row, write, spec, now, cfg)


def _preconditions(spec, cfg):
    """(seat, args), or a hold: what a launch needs that no agent can give it. Touches nothing."""
    if not os.path.isdir(placement.to_windows_path(spec['cwd'])):
        raise AdmissionHeld('pane-cwd-missing')
    return _prepare(spec, cfg)


def blocked(port, herdr, role_id, launch, *, cfg, resuming=False) -> str | None:
    """Why this launch could not start now, or None. Reads only, so a role pays nothing for a launch that cannot go.

    A resume keeps its pane, so it needs no workspace, and its `live` row is no occupant.
    """
    seat = config.seat_by_name(launch['seat'], cfg) or {}
    spec = dict(launch, role='director', role_id=role_id, run_id=0, sandbox=seat.get('sandbox'))
    try:
        _preconditions(spec, cfg)
        if not resuming:
            _clear_role(port, herdr, spec)
            _workspace(herdr, cfg)
    except AdmissionHeld as held:
        return str(held)
    except HerdrUnavailable:
        return HERDR_CLOSED
    except HerdrRefused as refused:
        return f'herdr-refused:{refused.code}'
    return None


def _available(herdr):
    if not herdr.available():
        raise AdmissionHeld('herdr-unavailable')


def start(port, herdr, spec, credential, guard, *, cfg) -> dict:
    """The row for a new agent, `live` once it has reported and `failed` otherwise. Holds come before any effect."""
    guard()
    seat, args = _preconditions(spec, cfg)
    _available(herdr)
    _clear_role(port, herdr, spec)
    try:
        pane_id = open_pane(herdr, cfg, placement.to_windows_path(spec['cwd']))
    except HerdrUnavailable:
        raise AdmissionHeld('herdr-unavailable') from None
    except HerdrRefused as refused:
        raise AdmissionHeld(f'herdr-refused:{refused.code}') from None
    row = ops_panes.record_agent(port.conn, pane_id=pane_id, terminal_id=_terminal(herdr, pane_id), kind=spec['backend'],
                                 seat=spec['seat'], placement=spec['placement'], started_by='drover', role_id=spec['role_id'],
                                 run_id=spec['run_id'], cwd=spec['cwd'], name=f"{spec['role']}-{spec['run_id']}")
    if not row['terminal_id']:
        return _fail(port, herdr, row, 'no-terminal')
    try:
        _write_credential(spec, credential, cfg)
        launcher = _write_files(spec, seat, args, cfg)
    except Exception as error:  # no text from here: it may quote a path or a secret
        pane_fault.note(port, log, 'Writing a launch file', error)
        return _fail(port, herdr, row, 'private-file')
    try:
        guard()
    except AdmissionHeld:
        _fail(port, herdr, row, 'admission-held', notice=False)
        raise
    return _launched(port, herdr, row, launcher, args, cfg)


def _close_superseded(port, row):
    """The shell shows alone, so the CLI these went to is gone and none of them can land."""
    for write in ops_panes.open_writes(port.conn, row['id']):
        if write['kind'] in SUPERSEDED:
            ops_panes.finish_write(port.conn, write['id'], 'observed', 'superseded')


def resume(port, herdr, row, spec, *, cfg, credential=None) -> dict:
    """Relaunch the recorded session in its pane, on a new row and with no brief. Seat and placement come from the row.

    With a `credential` the row joins the new run in `spec`, whose session lapsed the old one's; without it the run goes on.
    The session is the same either way, so the row keeps its context reading and whatever its handoff has reached.

    The old row goes `gone`, so a pane's history cannot pass for its present. The new row is not the pane's first, so
    no startup dialog is answered here. A `failed` row comes back on any refusal, and the same pane is not tried again:
    the role's next `start` opens a fresh pane once a read shows this one holds no agent.
    """
    if row.get('state') not in RESUMABLE or row.get('started_by') != 'drover' or not runner.valid_worker_session(row.get('session_id')):
        raise AdmissionHeld('pane-not-resumable')
    return _take_pane(port, herdr, row, spec, cfg, credential, row['session_id'])


def rotate(port, herdr, row, spec, *, cfg, credential) -> dict:
    """A fresh session on the pane a rotated agent left: `resume` with no `--resume`, which would bring the context back."""
    if row.get('state') != 'retired' or row.get('handoff_state') != 'rotated' or row.get('started_by') != 'drover':
        raise AdmissionHeld('pane-not-rotated')
    return _take_pane(port, herdr, row, spec, cfg, credential, None)


def _carried(port, row, spec, cfg, credential):
    """A new generation reads another path, so its file moves with it. With none, the next request there is its first."""
    state = row.get('handoff_state')
    if credential and state:
        was = ops_panes.generation_of(port.conn, row['run_id'])
        state = state if handoff.follow(cfg, row['role_id'], was, spec['generation']) else None
    return dict(handoff_state=state, context_tokens=row.get('context_tokens'))


def _take_pane(port, herdr, row, spec, cfg, credential, resume_id):
    _available(herdr)
    current = ops_panes.by_pane(port.conn, row['pane_id'])
    if current is None or current['id'] != row['id']:
        raise AdmissionHeld('pane-not-resumable')
    if _terminal(herdr, row['pane_id']) != row['terminal_id']:
        raise AdmissionHeld('pane-occupant-changed')
    if _read_alone(herdr, row) is not True:
        raise AdmissionHeld('pane-agent-running')
    _close_superseded(port, row)
    if ops_panes.open_writes(port.conn, row['id']):
        raise AdmissionHeld('pane-open-write')
    spec = dict(spec, backend=row['kind'], placement=row['placement'], seat=row['seat'], cwd=row['cwd'],
                run_id=spec['run_id'] if credential else row['run_id'])
    seat, args = _prepare(spec, cfg, resume_id=resume_id)
    kept = _carried(port, row, spec, cfg, credential) if resume_id else {}
    fresh = ops_panes.record_agent(port.conn, run_id=spec['run_id'], **kept, **{name: row[name] for name in (
        'pane_id', 'terminal_id', 'kind', 'seat', 'placement', 'started_by', 'role_id', 'cwd', 'name')})
    ops_panes.update_agent(port.conn, row['id'], state='gone')
    try:
        if credential:
            _write_credential(spec, credential, cfg)
        launcher = _write_files(spec, seat, args, cfg)
    except Exception as error:  # no text from here: it may quote a path or a secret
        pane_fault.note(port, log, 'Writing a resume file', error)
        return _fail(port, herdr, fresh, 'private-file')
    return _launched(port, herdr, fresh, launcher, args, cfg)
