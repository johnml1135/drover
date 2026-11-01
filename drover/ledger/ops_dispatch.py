"""Who picks the next task: Drover (the claim order) or a director (explicit picks).

An epic is `drover` or `director` dispatched; a task with no epic follows the `dispatch_default` setting. Switching
changes only who picks next: claimed and running work, checks, review and the merge queue carry on. `dispatch_enabled`,
`dispatch_repos` and STOP bind a picked task exactly as they bind any other (claim_next applies them first).
"""
from .ops_common import IllegalTransition, NotFound, event, get_epic, get_setting, set_setting, stamp, tx

DROVER = 'drover'
DIRECTOR = 'director'
MODES = (DROVER, DIRECTOR)
DEFAULT_KEY = 'dispatch_default'


def _mode(value):
    if value not in MODES:
        raise ValueError(f"dispatch mode must be one of {MODES}, not {value!r}")
    return value


def _owner(actor):
    if actor != 'owner':
        raise PermissionError('only the owner switches who dispatches')


def default_mode(conn):
    value = get_setting(conn, DEFAULT_KEY, DROVER)
    return value if value in MODES else DROVER


def mode_of(conn, task):
    """The dispatch mode that governs `task`: its epic's, or the global default for a task with no epic."""
    if task.get('epic_id') is None:
        return default_mode(conn)
    row = conn.execute("SELECT dispatch FROM epics WHERE id=?", (task['epic_id'],)).fetchone()
    return row[0] if row else default_mode(conn)


def set_epic_mode(conn, epic_id, mode, actor='owner'):
    _owner(actor)
    _mode(mode)
    with tx(conn):
        old = get_epic(conn, epic_id)
        if old['dispatch'] != mode:
            conn.execute("UPDATE epics SET dispatch=?, updated=? WHERE id=?", (mode, stamp(), epic_id))
            event(conn, 'dispatch-mode', actor, {'from': old['dispatch'], 'to': mode}, epic_id=epic_id)
        return get_epic(conn, epic_id)


def set_default_mode(conn, mode, actor='owner'):
    _owner(actor)
    _mode(mode)
    with tx(conn):
        old = default_mode(conn)
        set_setting(conn, DEFAULT_KEY, mode)
        if old != mode:
            event(conn, 'dispatch-mode', actor, {'scope': 'default', 'from': old, 'to': mode})
    return mode


def pick(conn, task_id, actor):
    """Release a ready task to the claim order. Only the owner and the director's API token (dispatcher) may."""
    if actor not in ('owner', 'dispatcher'):
        raise PermissionError('only the owner or a director may pick a task')
    with tx(conn):
        row = conn.execute("SELECT status, epic_id, picked_at FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise NotFound(f"task {task_id}")
        if row['status'] != 'ready':
            raise IllegalTransition(f"cannot pick a {row['status']} task; only ready tasks are picked")
        from .ops_tasks import get_task
        if row['picked_at']:                         # the first pick stands, and the log records only that one
            return get_task(conn, task_id)
        conn.execute("UPDATE tasks SET picked_at=?, picked_by=?, updated=? WHERE id=?",
                     (stamp(), actor, stamp(), task_id))
        event(conn, 'picked', actor, {}, task_id=task_id, epic_id=row['epic_id'])
        return get_task(conn, task_id)


def awaiting_pick(conn):
    """Ready tasks a director has not picked yet (the ones Drover is leaving alone)."""
    n = 0
    for r in conn.execute("SELECT id, epic_id FROM tasks WHERE status='ready' AND archived=0 AND picked_at IS NULL"):
        if mode_of(conn, {'epic_id': r['epic_id']}) == DIRECTOR:
            n += 1
    return n
