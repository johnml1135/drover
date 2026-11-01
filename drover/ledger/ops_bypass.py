"""The owner's Bypass: a yank closes a task, then the owner abandons it or hands it back.

Nothing here touches files or git. Stopping a live run and cleaning its worktree belong to the dispatcher and cleanup."""
from . import states
from .db import tx
from .ops_common import event
from .ops_tasks import create_task, get_task, set_status


def _refuse_unless_owner(actor, action):
    if actor != 'owner':
        raise PermissionError(f'only the owner may {action} (actor {actor!r})')


def _yanked(conn, task_id, action):
    task = get_task(conn, task_id)
    if task['status'] != states.YANKED:
        raise states.IllegalTransition(f"only a yanked task can be {action}; {task_id} is {task['status']}")
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


def handback(conn, task_id, actor='owner'):
    """Turn a yanked task into new work: a draft the owner must approve, which its dependents now wait on."""
    _refuse_unless_owner(actor, 'hand back a task')
    with tx(conn):
        old = _yanked(conn, task_id, 'handed back')
        new = create_task(conn, old['title'], old['repo'], epic_id=old['epic_id'], repo_path=old['repo_path'],
                          goal=old['goal'], acceptance=old['acceptance'], scope_files=old['scope_files'],
                          priority=old['priority'], size=old['size'], risk=old['risk'], status=states.DRAFT,
                          actor=actor)
        # Before the cancel: dependents must never see the old task settled while still pointing at it.
        conn.execute('UPDATE task_deps SET depends_on=? WHERE depends_on=?', (new['id'], task_id))
        set_status(conn, task_id, states.CANCELLED, actor=actor, reason='handed back')
        event(conn, 'handback', actor, {'to': new['id']}, task_id=task_id, epic_id=old['epic_id'])
        event(conn, 'handback', actor, {'from': task_id}, task_id=new['id'], epic_id=old['epic_id'])
        return get_task(conn, new['id'])
