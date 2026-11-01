"""A director hands off its session before it fills: asked once, and given no input a pane is not safe to take.

Each tick reads the context. Unknown changes nothing. Under `[director] handoff_tokens` a request or a file on hand is
void, because the owner compacted: the file goes with it, so the next climb starts clean. At the level the director is
asked once, through `pane_send`, and a refusal means no input and another try later. The state is set before the send,
so a hub that dies inside it can only lose the request, never make a second; the forced level then still applies.

`handoff_state` moves `requested`, then `written` when the file holds every section. At `[watch] context_tokens` Drover
stops waiting and writes its own file, at the first moment `pane_send.check` passes. Forced never interrupts: until
then the context notice in `watch` tells the owner, so this module publishes none. Drover never sends `/compact`.

It stops at `written`. Rotation is not built: where the new agent goes while the old CLI sits in its pane is unruled.
"""
from . import handoff, pane_context, pane_send
from .ledger import ops_panes
from .pane_send import PaneRefused

REQUESTED, WRITTEN = 'requested', 'written'


def _open_tasks(conn):
    rows = conn.execute("SELECT id,title,status FROM tasks WHERE internal=0 AND archived=0 AND status NOT IN ('landed','cancelled') "
                        'ORDER BY rank LIMIT ?', (handoff.MAX_TASKS,))
    return [dict(row) for row in rows]


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


def _settle(service, run, row, path, on_hand):
    """Stop waiting for the director: its file if it wrote one, else Drover's own, once the pane is safe."""
    if on_hand and row['handoff_state'] != WRITTEN:
        row = ops_panes.update_agent(service.conn, row['id'], handoff_state=WRITTEN)
    try:
        pane_send.check(service.herdr, row)
    except PaneRefused:
        return
    if not on_hand:
        facts = dict(open_tasks=_open_tasks(service.conn))
        handoff.write(path, handoff.fallback(run['role_id'], service.notifications.role_state(run['role_id']), facts))
        ops_panes.update_agent(service.conn, row['id'], handoff_state=WRITTEN)


def maintain(service, run) -> None:
    """Called for a live run of the current generation whose pane shows the recorded agent."""
    row = ops_panes.by_run(service.conn, run['run_id'])
    if row is None or row['state'] != 'live':
        return
    tokens = pane_context.read(row, service.herdr, service.cfg)
    if tokens is None:
        return
    if tokens != row['context_tokens']:
        row = ops_panes.update_agent(service.conn, row['id'], context_tokens=tokens)
    path = handoff.path_for(service.cfg, run['role_id'], run['generation'])
    if tokens < service.cfg['director']['handoff_tokens']:
        if row['handoff_state']:
            _cancel(service, row, path)
        return
    on_hand = bool(row['handoff_state']) and handoff.valid(path)
    if on_hand or tokens >= service.cfg['watch']['context_tokens']:
        _settle(service, run, row, path, on_hand)
    elif row['handoff_state'] is None:
        _request(service, row, path)
