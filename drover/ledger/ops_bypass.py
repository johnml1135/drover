"""The owner's Bypass: a yank closes a task, then the owner abandons it or hands it back.

Nothing here touches files or git. Stopping a live run and cleaning its worktree belong to the dispatcher and cleanup."""
from . import states
from .db import tx
from .ops_common import event
from .ops_tasks import create_task, deps_of, get_task, set_status


def _refuse_unless_owner(actor, action):
    if actor != 'owner':
        raise PermissionError(f'only the owner may {action} (actor {actor!r})')


def _yanked(conn, task_id, action):
    task = get_task(conn, task_id)
    if task['status'] != states.YANKED:
        raise states.IllegalTransition(f"only a yanked task can be {action}; {task_id} is {task['status']}")
    run = conn.execute('SELECT id FROM runs WHERE task_id=? AND ended IS NULL ORDER BY id LIMIT 1', (task_id,)).fetchone()
    if run:
        raise ValueError(f"{task_id} cannot be {action} yet: run {run[0]} is still stopping")
    return task


def yank(conn, task_id, actor='owner', reason=''):
    """Close a task for the owner. A second yank changes nothing and writes nothing."""
    _refuse_unless_owner(actor, 'yank a task')
    with tx(conn):
        task = get_task(conn, task_id)
        if task['status'] == states.YANKED:
            return task
        yanked = set_status(conn, task_id, states.YANKED, actor=actor, reason=reason)
        event(conn, 'bypass', actor, {'from': task['status'], 'reason': reason, 'worktree': task['worktree']},
              task_id=task_id, epic_id=task['epic_id'])
        return yanked


def abandon(conn, task_id, actor='owner'):
    """Settle a yanked task as never landing, which releases whatever waits on its answer."""
    _refuse_unless_owner(actor, 'abandon a task')
    with tx(conn):
        _yanked(conn, task_id, 'abandoned')
        return set_status(conn, task_id, states.CANCELLED, actor=actor, reason='abandoned')


def _retarget_terminal_waits(conn, old_id, new_id, actor):
    """Hold whatever waits on the old task's outcome on the new task's instead, before the cancel releases it."""
    from .waiting import TASK_TERMINAL, Waiting, cause_key
    waiting = Waiting(conn, actor)
    for row in waiting.pending():
        if row['kind'] == TASK_TERMINAL and row['data']['source'] == old_id:
            cause = cause_key('task', new_id) if row['cause'] == cause_key('task', old_id) else row['cause']
            waiting.wait((row['subject_kind'], row['subject_key']), TASK_TERMINAL, cause_key(new_id), cause=cause,
                         data={**row['data'], 'source': new_id})


def _move_group_links(conn, old_id, new_id):
    """The new task takes the old one's place in its groups, sealed ones too, and as the parent a group gates."""
    groups = conn.execute('SELECT group_id FROM task_group_members WHERE child_id=?', (old_id,)).fetchall()
    for (group,) in groups:
        conn.execute('UPDATE task_group_members SET child_id=? WHERE group_id=? AND child_id=?', (new_id, group, old_id))
        conn.execute('UPDATE task_groups SET revision=revision+1 WHERE group_id=?', (group,))
        event(conn, 'notification.group-enrolled', 'system', dict(group_id=group, child_id=new_id))
    conn.execute('UPDATE task_groups SET parent_id=? WHERE parent_id=?', (new_id, old_id))


def handback(conn, task_id, actor='owner'):
    """Turn a yanked task into new work: a draft the owner must approve, which its dependents now wait on.

    The draft may do no more than the old task could: it keeps every field that defines or restricts the work, its
    prerequisites, its director and its place in any group. The old task keeps its ownership rows as history."""
    _refuse_unless_owner(actor, 'hand back a task')
    with tx(conn):
        old = _yanked(conn, task_id, 'handed back')
        owners = conn.execute('SELECT event_class,role_id FROM task_ownership WHERE task_id=?', (task_id,)).fetchall()
        new = create_task(conn, old['title'], old['repo'], epic_id=old['epic_id'], repo_path=old['repo_path'],
                          goal=old['goal'], acceptance=old['acceptance'], scope_files=old['scope_files'],
                          priority=old['priority'], size=old['size'], risk=old['risk'], status=states.DRAFT,
                          actor=actor, kind=old['kind'], write=old['write'], deps=deps_of(conn, task_id),
                          parent_id=old['parent_id'],
                          initiating_role=next((r['role_id'] for r in owners if r['event_class'] == '*'), None))
        for row in owners:
            if row['event_class'] != '*':
                from ..notifications import Notifications
                Notifications(conn).assign(new['id'], row['role_id'], event_class=row['event_class'])
        # Stored values, not revalidated: a repo unregistered since must neither refuse the handback nor widen it.
        conn.execute('UPDATE tasks SET (read_access,model_pin)=(SELECT read_access,model_pin FROM tasks WHERE id=?) '
                     'WHERE id=?', (task_id, new['id']))
        # Before the cancel: dependents must never see the old task settled while still pointing at it.
        conn.execute('UPDATE task_deps SET depends_on=? WHERE depends_on=?', (new['id'], task_id))
        _retarget_terminal_waits(conn, task_id, new['id'], actor)
        _move_group_links(conn, task_id, new['id'])
        set_status(conn, task_id, states.CANCELLED, actor=actor, reason='handed back')
        event(conn, 'handback', actor, {'to': new['id']}, task_id=task_id, epic_id=old['epic_id'])
        event(conn, 'handback', actor, {'from': task_id}, task_id=new['id'], epic_id=old['epic_id'])
        return get_task(conn, new['id'])
