"""A director hands off its session before it fills: asked once, and given no input a pane is not safe to take.

Each tick reads the context. Unknown changes nothing. Under `[director] handoff_tokens` a request or a file on hand is
void, because the owner compacted: the file goes with it, so the next climb starts clean. At the level the director is
asked once, through `pane_send`, and a refusal means no input and another try later. The state is set before the send,
so a hub that dies inside it can only lose the request, never make a second; the forced level then still applies.

`handoff_state` moves `requested`, then `written` when the file holds every section, then `rotated`. At `[watch]
context_tokens` Drover stops waiting and writes its own file, at the first moment `pane_send.check` passes. Forced never
interrupts: until then the context notice in `watch` tells the owner. Drover never sends `/compact`.

Then the old CLI leaves first. With the file written and the pane safe, the row turns `rotating` with the pending file,
in one transaction, and only then does one guarded `/exit` go out. A hub that dies between them leaves a row that recovery
reads: the shell alone carries on, and an agent with no exit on record rolls back to `live`. A refusal rolls back too and
waits with one notice, and the session goes on. At the forced level the reads that prove the pane idle, empty and
unfocused close an open wake as superseded, so a wake that never settled cannot pin a full session.

Nothing is fenced and nothing new starts while the old CLI lives: only a read showing the shell alone ends the run as
`rotated`, retires the row and signals the role, whose next run is a fresh session with the handoff. No replacement is
spent. That run must write `restated.md` beside the file; the pending handoff stands until it does, so a launch that
fails hands the same file to the one after it. `handoff.owed` says how long that goes on with no wake.

A resume is the same session, so its row keeps `handoff_state`, and the director's file stands on a resumed row at any
reading: no request clears it, a low reading does not void it and no fallback replaces it. A resumed CLI may show an empty
context until its first turn, so there a low reading is not proof that the owner compacted.
"""
from . import compaction_hold, config, handoff, pane_context, pane_launch, pane_send
from .directors import DirectorRuntime
from .ledger import actions, disk_notices, ops_panes
from .ledger.db import tx
from .notifications import roles
from .pane_send import PaneRefused

REQUESTED, WRITTEN, ROTATED = 'requested', 'written', handoff.ROTATED
ROTATING = 'rotating'
EXIT_S = 120
RESTATE_S = 600
UNKNOWN_S = 1800
UNKNOWN_SINCE = 'pane_context_unknown:'     # a row's setting: since when its context has read unknown


def _open_tasks(conn):
    rows = conn.execute("SELECT id,title,status FROM tasks WHERE internal=0 AND archived=0 AND "
                        "status NOT IN ('landed','cancelled') ORDER BY rank LIMIT ?", (handoff.MAX_TASKS,))
    return [dict(row) for row in rows]


def _where(row):
    return f"Seat {row['seat']} ({row['placement']}), pane {row['pane_id']}"


def _request(service, row, path):
    try:
        handoff.clear(path)         # the agent has not been told yet, so anything here is from before
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    ops_panes.update_agent(service.conn, row['id'], handoff_state=REQUESTED)
    text = handoff.request_text(handoff.agent_path(path, row['placement']))
    try:
        pane_send.send(service.port, service.herdr, row, text, 'handoff')
    except PaneRefused:             # no input went out, so a later tick may ask
        ops_panes.update_agent(service.conn, row['id'], handoff_state=None)


def _cancel(service, row, path):
    handoff.clear(path)
    ops_panes.update_agent(service.conn, row['id'], handoff_state=None)


def _exit_waits_id(row):
    return f"pane-rotation-waits:{row['id']}"


def _roll_back(service, run, row):
    """No `/exit` went out, so the session goes on and the role owes no handoff."""
    with tx(service.conn):
        ops_panes.update_agent(service.conn, row['id'], state='live')
        handoff.set_pending(service.port, run['role_id'], None)


def _exit(service, run, row, path, forced):
    """One `/exit`, after the row that says so is committed. A compaction refuses every launch, so no session is ended while
    one is on.
    """
    if compaction_hold.active(config.state_dir(service.cfg)):
        return
    with tx(service.conn):
        ops_panes.update_agent(service.conn, row['id'], state=ROTATING)
        handoff.set_pending(service.port, run['role_id'], path)
    try:
        pane_send.exit_agent(service.port, service.herdr, row, forced=forced)
    except PaneRefused as refused:
        _roll_back(service, run, row)
        pane_launch.publish(service.port, _exit_waits_id(row), 'A director cannot rotate yet',
                            f'{_where(row)}: {refused.reason}. Its handoff is written, and it keeps its session until this clears.')


def _settle(service, run, row, path, on_hand, forced):
    """Stop waiting for the director: its file if it wrote one, else Drover's own, once the pane is safe."""
    if on_hand:
        for write in ops_panes.open_writes(service.conn, row['id']):
            if write['kind'] == 'handoff':      # the file is what that request asked for, so it landed
                ops_panes.finish_write(service.conn, write['id'], 'observed', 'file-written')
        if row['handoff_state'] != WRITTEN:
            row = ops_panes.update_agent(service.conn, row['id'], handoff_state=WRITTEN)
    try:
        pane_send.check(service.herdr, row)
    except PaneRefused:
        return
    if not on_hand:
        facts = dict(open_tasks=_open_tasks(service.conn))
        handoff.write(path, handoff.fallback(run['role_id'], service.notifications.role_state(run['role_id']), facts))
        row = ops_panes.update_agent(service.conn, row['id'], handoff_state=WRITTEN)
    _exit(service, run, row, path, forced)


def _restate_id(run):
    return f"pane-restate:{run['run_id']}"


def _restatement(service, run, row):
    """A session that took up a handoff writes its restatement first. Its absence is one notice; nothing is typed about it."""
    path = run['spec'].get('handoff') and handoff.pending(service.port, run['role_id'])
    if not path:
        return
    if handoff.restated_path(path).exists():
        handoff.set_pending(service.port, run['role_id'], None)
        disk_notices.retract(service.conn, _restate_id(run), kind='pane.notice', actor='panes')
        return
    brief = ops_panes.run_brief(service.conn, row['run_id'])
    if brief and pane_launch.age(brief['at'], service.now()) >= RESTATE_S:
        pane_launch.publish(service.port, _restate_id(run), 'A new director has not restated its handoff',
                            f'{_where(row)}: no {handoff.RESTATED} beside {path}. The session goes on, and may have missed the rules.')


def _unknown_id(row):
    return f"pane-context:{row['id']}"


def _reading(service, row):
    """The context, or None. None changes nothing but this clock: one notice if it lasts, closed by the next real reading."""
    tokens = pane_context.read(row, service.herdr, service.cfg)
    key, now = UNKNOWN_SINCE + str(row['id']), service.now()
    since = service.port.get_setting(key)
    if tokens is not None:
        if since:
            service.port.set_setting(key, '')
            disk_notices.retract(service.conn, _unknown_id(row), kind='pane.notice', actor='panes')
    elif not since:
        service.port.set_setting(key, str(now))
    elif now - float(since) >= UNKNOWN_S:
        pane_launch.publish(service.port, _unknown_id(row), "A director's context cannot be read",
                            f'{_where(row)}: Drover cannot say how full its session is, so it will not ask for a handoff or rotate it.')
    return tokens


def maintain(service, run) -> None:
    """Called for a live run of the current generation whose pane shows the recorded agent."""
    row = ops_panes.by_run(service.conn, run['run_id'])
    if row is None or row['state'] != 'live':
        return
    _restatement(service, run, row)
    tokens = _reading(service, row)
    if tokens is None:
        return
    if tokens != row['context_tokens']:
        row = ops_panes.update_agent(service.conn, row['id'], context_tokens=tokens)
    path = handoff.path_for(service.cfg, run['role_id'], run['generation'])
    resumed = ops_panes.reused_pane(service.conn, row) and handoff.valid(path)
    if tokens < service.cfg['director']['handoff_tokens']:
        if row['handoff_state'] and not resumed:
            _cancel(service, row, path)
        return
    on_hand = resumed or (bool(row['handoff_state']) and handoff.valid(path))
    forced = tokens >= service.cfg['watch']['context_tokens']
    if on_hand or forced:
        _settle(service, run, row, path, on_hand, forced)
    elif row['handoff_state'] is None:
        _request(service, row, path)


def _rotate(service, run, row):
    """The old session ends here, after its CLI has. The row and the run move together, so no tick sees one without the other."""
    try:
        handoff.clear(handoff.restated_path(handoff.pending(service.port, run['role_id'])))     # the last rotation's proves nothing
    except OSError:
        return
    runtime = DirectorRuntime(service.port, service, now=service.now)
    with tx(service.conn):
        for write in ops_panes.open_writes(service.conn, row['id']):
            ops_panes.finish_write(service.conn, write['id'], 'observed', ROTATED)
        ops_panes.update_agent(service.conn, row['id'], state='retired', handoff_state=ROTATED)
        runtime.finish(run['run_id'], dict(status=ROTATED, usage={}))
        if run['stop_action_id'] and actions._action(service.conn, run['stop_action_id'])['state'] == 'pending':
            actions.complete(service.conn, run['stop_action_id'], dict(run_id=run['run_id'], reconciled=ROTATED))
        event = service.port.append_event('director.rotated', dict(role_id=run['role_id'], run_id=run['run_id']), actor='panes')
        roles.signal(service.conn, run['role_id'], event)
    disk_notices.retract(service.conn, _exit_waits_id(row), kind='pane.notice', actor='panes')


def follow_exit(service, run) -> None:
    """A `rotating` row's tick. A read that cannot say waits. The shell alone carries on, whatever the record says. An agent
    with no `/exit` on record is a hub that died first, so the row goes back; past EXIT_S after one it is one notice and no
    more input.
    """
    row = ops_panes.by_run(service.conn, run['run_id'])
    if row is None or row['state'] != ROTATING:
        return
    try:
        alone = pane_send.shell_alone(service.herdr, row)
    except PaneRefused:
        return
    if alone:
        _rotate(service, run, row)
        return
    written = ops_panes.last_write(service.conn, row['id'], 'exit')
    if written is None or written['state'] == 'unsent':
        _roll_back(service, run, row)
    elif pane_launch.age(written['at'], service.now()) >= EXIT_S:
        pane_launch.publish(service.port, f"pane-exit:{row['id']}", 'A director did not leave its pane',
                            f'{_where(row)}: it was told to exit and is still there, so its successor waits. It gets no more input, '
                            'and the next session starts once it has exited.')
