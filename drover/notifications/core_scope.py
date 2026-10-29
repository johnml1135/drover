"""Notification classes, ownership, and event scope."""
from ..ledger import ops

class Conflict(ValueError):
    """The caller's expected revision no longer owns the requested change."""

EVENT_CLASSES = frozenset(('progress', 'stage', 'result-accepted', 'landed', 'group-ready',
                           'decision-needed', 'decision-recorded', 'issue', 'recovered',
                           'role-unavailable', 'broadcast', 'control'))

PROFILE_VERSION = 1

def classify(conn, event):
    """Map ledger facts, never process exit, to semantic notification classes (v1)."""
    kind, data = event['kind'], event['data']
    if kind in ('deps-set', 'related-set') and not any(data.values()):
        return None
    if kind.startswith('director.') or kind.startswith('notification.'):
        return None
    if kind in ('task.result-accepted', 'result.accepted'):
        return 'result-accepted'
    if kind == 'task.landed':
        from .groups import accepted_result
        result = accepted_result(conn, event['task_id'], 'legacy-merged')
        if result and (data.get('land_sha'), data.get('tip'), data.get('receipt')) == (
                result['commit'], result['tip'], result['receipt']):
            return 'landed'
        return 'stage'
    if kind == 'landing.repair-blocked':
        return 'issue'
    if kind == 'landing.outcome' and data.get('outcome') in ('infra-held', 'base-red', 'error', 'repair-blocked'):
        return 'issue'
    if kind == 'group.issue':
        return 'issue'
    if kind == 'group.ready':
        return 'group-ready'
    if kind in ('question-asked', 'decision-needed'):
        return 'decision-needed'
    if kind in ('question-answered', 'decision-recorded', 'decision.recorded'):
        return 'decision-recorded'
    if kind in ('recovery.refused', 'recovery.exhausted', 'progress.stalled', 'replan-owner'):
        return 'issue'
    if kind in ('progress.recovered', 'recovery.resolved'):
        return 'recovered'
    if kind == 'role.unavailable':
        return 'role-unavailable'
    if kind == 'status':
        if data.get('to') == 'landed':
            # A worker can report a legal status transition without a merge.
            # Merged acceptance is emitted by the receipt seam; read-only
            # acceptance must match the owner's frozen result evidence.
            from .groups import accepted_result
            result = accepted_result(conn, event['task_id'], 'legacy-read-only')
            return 'landed' if result and result['revision'] == event['id'] else 'stage'
        if data.get('to') in ('needs-owner', 'stuck'):
            return 'issue'
        return 'stage'
    if kind == 'comment' and data.get('urgent'):
        return 'decision-needed'
    return 'progress'

def _position(conn):
    return conn.execute('SELECT COALESCE(MAX(id),0) FROM task_events').fetchone()[0]

def _role(conn, role_id):
    row = conn.execute('SELECT * FROM director_roles WHERE role_id=?', (role_id,)).fetchone()
    if row is None:
        raise ops.NotFound(f'director {role_id}')
    return dict(row)

def effective_owner(conn, task_id, event_class):
    row = conn.execute("SELECT * FROM task_ownership WHERE task_id=? AND event_class IN (?, '*') "
                       "ORDER BY (event_class='*') LIMIT 1", (task_id, event_class)).fetchone()
    return dict(row) if row else None

def recipient_owner(conn, envelope):
    """Current authority for an ordinary partition, group fact or explicit target."""
    if envelope.get('notice_id'):
        row = conn.execute('SELECT t.role_id FROM notification_targets t JOIN notification_notices n USING(event_id) '
                           'LEFT JOIN notification_controls c ON c.control_id=n.control_id WHERE t.event_id=? AND t.role_id=? '
                           'AND (n.control_id IS NULL OR (t.superseded=0 AND c.active=1))',
                           (envelope['event_id'], envelope.get('target_role'))).fetchone()
        return dict(role_id=row['role_id'], revision=1) if row else None
    if envelope.get('group_id') and envelope.get('task_id') is None:
        row = conn.execute('SELECT role_id FROM task_groups WHERE group_id=?', (envelope['group_id'],)).fetchone()
        return dict(role_id=row['role_id'], revision=1) if row else None
    return effective_owner(conn, envelope.get('task_id'), envelope['event_class'])

def scope_task(conn, task):
    task = dict(task)
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_groups'").fetchone():
        return task
    task['_groups'] = [r[0] for r in conn.execute('SELECT group_id FROM task_group_members WHERE child_id=?', (task['id'],))]
    task['_ancestors'] = []
    parent = task.get('parent_id')
    while parent and parent not in task['_ancestors']:
        task['_ancestors'].append(parent)
        row = conn.execute('SELECT parent_id FROM tasks WHERE id=?', (parent,)).fetchone()
        parent = row[0] if row else None
    return task

def event_scope(conn, event):
    if event['task_id'] is not None:
        try:
            return scope_task(conn, ops.get_task(conn, event['task_id']))
        except ops.NotFound:
            return None
    group_id = event['data'].get('group_id')
    if group_id and conn.execute('SELECT 1 FROM task_groups WHERE group_id=?', (group_id,)).fetchone():
        return dict(id=None, kind='research', epic_id=None, repo=None, _groups=[group_id])
    return None
