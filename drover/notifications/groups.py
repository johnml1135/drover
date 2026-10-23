"""Sealed accepted-result joins; navigation never approves or completes a parent."""
import hashlib
import json

from ..ledger import ops
from ..ledger.db import tx
from .core import Conflict, _role, effective_owner


def get_group(service, group_id):
    row = service.conn.execute('SELECT * FROM task_groups WHERE group_id=?', (group_id,)).fetchone()
    if row is None:
        raise ops.NotFound(f'group {group_id}')
    result = dict(row)
    result['members'] = [dict(r) for r in service.conn.execute(
        'SELECT child_id,lifecycle FROM task_group_members WHERE group_id=? ORDER BY child_id', (group_id,))]
    result['manifest'] = json.loads(result['manifest']) if result['manifest'] is not None else None
    result['ready'], result['sealed'] = bool(result['ready']), bool(result['sealed'])
    result['readiness_reason'] = None if result['ready'] else result['failure_reason'] or (
        'membership-open' if not result['sealed'] else 'awaiting-accepted-results')
    return result


def create_group(service, group_id, role_id, parent_id=None, mode='synthesis'):
    if not isinstance(group_id, str) or not group_id.strip() or len(group_id) > 256:
        raise ValueError('bounded group identity required')
    if mode != 'synthesis':
        raise ValueError('unsupported group lifecycle: only synthesis is declared')
    conn = service.conn
    with tx(conn):
        from .roles import _artifact
        _artifact(conn, dict(group_id=group_id, role_id=role_id, parent_id=parent_id))
        _role(conn, role_id)
        service.check_admission(role_id)
        if parent_id is not None:
            ops.get_task(conn, parent_id)
            owner = effective_owner(conn, parent_id, 'group-ready')
            if not owner or owner['role_id'] != role_id:
                raise Conflict('group parent belongs to another director')
        old = conn.execute('SELECT * FROM task_groups WHERE group_id=?', (group_id,)).fetchone()
        if old:
            if (old['role_id'], old['parent_id'], old['mode']) != (role_id, parent_id, mode):
                raise Conflict('group identity already has different content')
            return get_group(service, group_id)
        conn.execute('INSERT INTO task_groups(group_id,role_id,parent_id,mode) VALUES(?,?,?,?)',
                     (group_id, role_id, parent_id, mode))
        service.follow(role_id, {'groups': [group_id]})
        ops.event(conn, 'notification.group-created', 'system', dict(group_id=group_id, role_id=role_id))
        return get_group(service, group_id)


def reaches(conn, start, target):
    """Follow dependencies and the parent's required group children together."""
    seen, stack = set(), [start]
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(r[0] for r in conn.execute('SELECT depends_on FROM task_deps WHERE task_id=?', (node,)))
        stack.extend(r[0] for r in conn.execute('SELECT m.child_id FROM task_group_members m JOIN task_groups g '
                                               'USING(group_id) WHERE g.parent_id=?', (node,)))
    return False


def _request(service, group_id, expected_revision, request_key, operation, apply):
    if not isinstance(request_key, str) or not request_key or len(request_key) > 256:
        raise ValueError('bounded request_key required')
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
        raise ValueError('positive expected_revision required')
    conn = service.conn
    with tx(conn):
        from .roles import _artifact, hashed, canonical
        _artifact(conn, dict(request_key=request_key, operation=operation))
        request_hash = hashed(canonical(dict(expected_revision=expected_revision, operation=operation)))
        group = get_group(service, group_id)
        prior = conn.execute('SELECT * FROM task_group_requests WHERE group_id=? AND request_key=?',
                             (group_id, request_key)).fetchone()
        if prior:
            if prior['request_hash'] != request_hash:
                raise Conflict('group request key reused with different content')
            return json.loads(prior['receipt'])
        service.check_admission(group['role_id'])
        if group['revision'] != expected_revision:
            raise Conflict('group revision changed')
        apply(group)
        conn.execute('UPDATE task_groups SET revision=revision+1 WHERE group_id=?', (group_id,))
        evaluate(service, group_id)
        receipt = get_group(service, group_id)
        conn.execute('INSERT INTO task_group_requests VALUES(?,?,?,?)',
                     (group_id, request_key, request_hash, ops._j(receipt)))
        return receipt


def enroll(service, group_id, child_id, expected_revision, request_key):
    def apply(group):
        if group['sealed']:
            raise Conflict('group membership is sealed')
        child = ops.get_task(service.conn, child_id)
        if group['parent_id'] is not None and reaches(service.conn, child_id, group['parent_id']):
            raise ValueError('group enrollment would make a combined dependency cycle')
        lifecycle = 'legacy-merged' if child['write'] else 'legacy-read-only'
        service.conn.execute('INSERT OR IGNORE INTO task_group_members VALUES(?,?,?)', (group_id, child_id, lifecycle))
        # Group scope follows only children in the owning director's partition.
        # Existing explicit task preferences keep their higher precedence.
        ops.event(service.conn, 'notification.group-enrolled', 'system', dict(group_id=group_id, child_id=child_id))
    return _request(service, group_id, expected_revision, request_key, dict(enroll=child_id), apply)


def seal(service, group_id, expected_revision, request_key):
    def apply(group):
        if group['sealed']:
            raise Conflict('group membership is already sealed')
        if not group['members']:
            raise ValueError('cannot seal an empty group')
        service.conn.execute('UPDATE task_groups SET sealed=1 WHERE group_id=?', (group_id,))
        ops.event(service.conn, 'notification.group-sealed', 'system', dict(group_id=group_id))
    return _request(service, group_id, expected_revision, request_key, dict(seal=True), apply)


def accepted_result(conn, child_id, lifecycle):
    """Legacy receipt adapters. Future lifecycles must add an explicit proof adapter."""
    try:
        task = ops.get_task(conn, child_id)
    except ops.NotFound:
        return None
    if task['status'] != 'landed':
        return None
    if lifecycle == 'legacy-merged':
        receipt = conn.execute('SELECT * FROM task_landed WHERE task_id=?', (child_id,)).fetchone()
        if not receipt or not receipt['land_sha'] or not receipt['tip']:
            return None
        status = conn.execute("SELECT id FROM task_events WHERE task_id=? AND kind='status' "
                               "AND json_extract(data,'$.to')='landed' ORDER BY id DESC LIMIT 1", (child_id,)).fetchone()
        if not status:
            return None
        return dict(task_id=child_id, revision=status['id'], kind='merged', commit=receipt['land_sha'],
                    tip=receipt['tip'], receipt=receipt['receipt'])
    if lifecycle == 'legacy-read-only':
        accepted = conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind='status' AND actor='owner' "
                                 "AND json_extract(data,'$.from')='needs-owner' AND json_extract(data,'$.to')='landed' "
                                 "AND json_extract(data,'$.read_only')=1 ORDER BY id DESC LIMIT 1", (child_id,)).fetchone()
        if not accepted:
            return None
        run_id = json.loads(accepted['data']).get('run')
        run = conn.execute("SELECT * FROM runs WHERE id=? AND task_id=? AND role='worker' "
                            "AND ended IS NOT NULL AND outcome='read-only-done'", (run_id, child_id)).fetchone()
        if run is None:
            return None
        result_hash = hashlib.sha256(ops._j(dict(run)).encode()).hexdigest()
        return dict(task_id=child_id, revision=accepted['id'], kind='read-only', run_id=run_id, result_hash=result_hash)
    raise ValueError('unsupported accepted-result lifecycle')


def evaluate(service, group_id):
    group = get_group(service, group_id)
    if group['ready']:
        return
    conn = service.conn
    manifest, failure, attention = [], None, None
    for member in group['members']:
        result = accepted_result(conn, member['child_id'], member['lifecycle'])
        if result is not None:
            manifest.append(result)
        else:
            task = conn.execute('SELECT status FROM tasks WHERE id=?', (member['child_id'],)).fetchone()
            if task is None or task['status'] in ('cancelled', 'failed', 'needs-owner'):
                failure = f"child-{task['status'] if task else 'deleted'}:{member['child_id']}"
                if task is None or task['status'] != 'failed':
                    attention = failure
            refusal = conn.execute("SELECT e.kind FROM notification_intents i JOIN task_events e ON e.id=i.event_id "
                                   "WHERE e.task_id=? AND i.resolved=0 AND e.kind IN ('recovery.refused','recovery.exhausted') "
                                   'ORDER BY e.id DESC LIMIT 1', (member['child_id'],)).fetchone()
            if refusal:
                attention = f"child-{refusal['kind']}:{member['child_id']}"
    failure = attention or failure
    conn.execute('UPDATE task_groups SET failure_reason=? WHERE group_id=?', (failure, group_id))
    prior_issue = conn.execute('SELECT data FROM task_events WHERE id=?', (group['issue_event'],)).fetchone()
    prior_reason = json.loads(prior_issue['data']).get('reason') if prior_issue else None
    if attention and group['sealed'] and (group['issue_event'] is None or attention != prior_reason):
        ident = ops.event(conn, 'group.issue', 'system', dict(group_id=group_id, reason=attention, cause=f'group:{group_id}'))
        conn.execute('UPDATE task_groups SET issue_event=? WHERE group_id=?', (ident, group_id))
    if attention is None and group['issue_event'] is not None:
        ops.event(conn, 'recovery.resolved', 'system', dict(group_id=group_id, cause=f'group:{group_id}'))
        conn.execute('UPDATE task_groups SET issue_event=NULL WHERE group_id=?', (group_id,))
    if not group['sealed'] or not manifest or len(manifest) != len(group['members']):
        return
    conn.execute('UPDATE task_groups SET ready=1,manifest=?,failure_reason=NULL WHERE group_id=?', (ops._j(manifest), group_id))
    ident = ops.event(conn, 'group.ready', 'system', dict(group_id=group_id, manifest=manifest, parent_id=group['parent_id']))
    conn.execute('UPDATE task_groups SET completion_event=? WHERE group_id=?', (ident, group_id))


def changed(conn, task_id):
    """Called inside accepted receipt and predicate-changing source transactions."""
    from .core import Notifications
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_groups'").fetchone():
        return
    for row in conn.execute('SELECT group_id FROM task_group_members WHERE child_id=?', (task_id,)).fetchall():
        evaluate(Notifications(conn), row['group_id'])
