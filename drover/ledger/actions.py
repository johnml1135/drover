"""Shared durable action identities. Effects run only after processing commits."""
import json

from . import ops
from .db import tx


KINDS = frozenset(('task.retry', 'task.comment', 'issue.resolve'))
SUPERVISED_KINDS = frozenset(('director.launch', 'director.stop', 'run.stop'))


def _action(conn, action_id):
    row = conn.execute('SELECT * FROM action_outbox WHERE action_id=?', (action_id,)).fetchone()
    if not row:
        raise ops.NotFound(f'action {action_id}')
    result = dict(row)
    result['payload'] = json.loads(result['payload'])
    result['result'] = json.loads(result['result']) if result['result'] is not None else None
    return result


def enqueue(conn, event_id, action_index, kind, payload, *, supervised=False):
    """One immutable ordered intent, joining the source transaction when present."""
    from ..notifications.core import Conflict
    from ..notifications.roles import _artifact, canonical, hashed
    if not isinstance(kind, str) or kind not in (KINDS | SUPERVISED_KINDS if supervised else KINDS):
        raise ValueError('unsupported action kind')
    if isinstance(event_id, bool) or not isinstance(event_id, int) or not 0 < event_id < 2**63:
        raise ValueError('source event identity must be a positive integer')
    if not isinstance(payload, dict) or isinstance(action_index, bool) or not isinstance(action_index, int) or action_index < 0:
        raise ValueError('action object and nonnegative action index required')
    with tx(conn):
        encoded = _artifact(conn, payload)
        digest = hashed(canonical([kind, payload]))
        action_id = f'{event_id}:{action_index}'
        row = conn.execute('SELECT content_hash FROM action_outbox WHERE action_id=?', (action_id,)).fetchone()
        if row and row['content_hash'] != digest:
            raise Conflict('action identity reused with different content')
        conn.execute('INSERT OR IGNORE INTO action_outbox(action_id,event_id,action_index,kind,payload,content_hash) '
                     'VALUES(?,?,?,?,?,?)', (action_id, event_id, action_index, kind, encoded, digest))
        return _action(conn, action_id)


def pending(conn, limit=32):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError('action batch limit must be between 1 and 100')
    return [_action(conn, row['action_id']) for row in conn.execute(
        "SELECT action_id FROM action_outbox WHERE state='pending' AND kind IN ('task.retry','task.comment','issue.resolve') "
        "ORDER BY event_id,action_index LIMIT ?", (limit,))]


def complete(conn, action_id, result):
    """Trusted destination receipt, stored in its local effect transaction."""
    from ..notifications.core import Conflict
    from ..notifications.roles import _artifact, canonical
    with tx(conn):
        prior = _action(conn, action_id)
        encoded = _artifact(conn, result)
        if prior['state'] != 'pending':
            if prior['state'] != 'complete' or canonical(prior['result']) != encoded:
                raise Conflict('action already has a different durable outcome')
            return prior
        conn.execute("UPDATE action_outbox SET state='complete',result=? WHERE action_id=?", (encoded, action_id))
        return _action(conn, action_id)


def _validate_destination(service, kind, payload, authority):
    from ..notifications.core import Conflict, EVENT_CLASSES, _role, effective_owner
    from ..notifications.roles import admission
    conn = service.conn
    role_id = authority['role_id']
    admission(service, role_id)
    role = _role(conn, role_id)
    if role['generation'] != authority['generation']:
        raise PermissionError('director generation changed')
    control = service.admission(role_id=role_id, epoch=authority.get('control_epoch', 0))
    if not control['allowed']:
        raise Conflict(control['reason'])
    if kind not in json.loads(role['policy']).get('action_allowlist', []):
        raise PermissionError('action is outside director allowlist')
    event_class = payload.get('event_class')
    if event_class not in EVENT_CLASSES:
        raise ValueError('action must name its owned event class')
    task = ops.get_task(conn, payload.get('task_id'))
    owner = effective_owner(conn, task['id'], event_class)
    if not owner or owner['role_id'] != role_id or owner['revision'] != authority['ownership_revision']:
        raise PermissionError('action ownership changed')
    if not task['approved_at']:
        raise ops.NotApproved('director actions require owner-approved work')
    if task['status'] in ('cancelled', 'landed'):
        raise Conflict('task is cancelled or complete')
    revision = conn.execute('SELECT COALESCE(MAX(id),0) FROM task_events WHERE task_id=?', (task['id'],)).fetchone()[0]
    if payload.get('expected_revision') != revision or payload.get('expected_status') != task['status']:
        raise Conflict('task revision or state changed')
    if kind in ('task.retry', 'issue.resolve'):
        from ..notifications.roles import validate_issue
        issue = validate_issue(service, role_id, payload.get('issue_id'), authority.get('issue_hash'), hashed_token=True)
        if issue['event_class'] != event_class or issue['task_id'] != task['id']:
            raise PermissionError('issue does not authorize this task partition')
        if kind == 'task.retry':
            if task['status'] not in ('failed', 'needs-owner', 'blocked'):
                raise Conflict('task is not eligible for recovery')
            if issue['attempts'] >= issue['attempt_limit']:
                raise Conflict('issue recovery budget exhausted')
    return task


def authorize(service, role_id, lease, request):
    """Accept a constrained intent; mutable fences are checked again by execute."""
    from ..notifications.core import EVENT_CLASSES, effective_owner
    from ..notifications.roles import _artifact, hashed
    if not isinstance(request, dict) or set(request) != {'kind', 'payload'} or not isinstance(request['kind'], str) or request['kind'] not in KINDS:
        raise ValueError('unsupported action request')
    payload = request['payload']
    if not isinstance(payload, dict):
        raise ValueError('action payload must be an object')
    required = {'task_id', 'event_class', 'expected_revision', 'expected_status'}
    extra = {'text'} if request['kind'] == 'task.comment' else {'issue_id', 'issue_token'}
    if set(payload) != required | extra:
        raise ValueError('action payload has missing or unsupported fields')
    if not isinstance(payload['task_id'], str) or not 0 < len(payload['task_id']) <= 256 or \
            not isinstance(payload['event_class'], str) or payload['event_class'] not in EVENT_CLASSES or \
            not isinstance(payload['expected_status'], str):
        raise ValueError('task identity, known event class and expected state required')
    if isinstance(payload['expected_revision'], bool) or not isinstance(payload['expected_revision'], int):
        raise ValueError('expected_revision must be an event position')
    if request['kind'] == 'task.comment' and (not isinstance(payload['text'], str) or not 0 < len(payload['text']) <= 8192):
        raise ValueError('bounded comment text required')
    owner = effective_owner(service.conn, payload['task_id'], payload['event_class'])
    authority = dict(role_id=role_id, generation=lease['generation'], control_epoch=lease.get('control_epoch', 0),
                     ownership_revision=owner['revision'] if owner else None)
    saved = dict(payload)
    if 'issue_token' in saved:
        token = saved.pop('issue_token')
        if not isinstance(token, str):
            raise PermissionError('issue handling token required')
        authority['issue_hash'] = hashed(token)
    _validate_destination(service, request['kind'], saved, authority)
    saved['_authority'] = authority
    _artifact(service.conn, saved)
    return saved


def execute(conn, action_id, now=None):
    """Run a supported local task effect and its receipt in one ledger transaction.

    Process launch, network effects and unknown outcomes belong to supervised
    adapters. They must use their own destination identity before calling complete.
    """
    from ..notifications import Notifications
    from ..notifications.core import Conflict
    from ..notifications.roles import canonical
    service = Notifications(conn, now=now)
    with tx(conn):
        action = _action(conn, action_id)
        if action['kind'] in SUPERVISED_KINDS:
            return action  # Only the supervising destination can reconcile external effects.
        if action['state'] != 'pending':
            return action
        payload = action['payload']
        try:
            authority = payload.get('_authority')
            if not isinstance(authority, dict) or not {'role_id', 'generation', 'ownership_revision'} <= set(authority):
                raise PermissionError('action lacks scoped destination authority')
            acceptance = conn.execute('SELECT receipt FROM director_processing WHERE event_id=? AND role_id=?',
                                       (action['event_id'], authority['role_id'])).fetchone()
            if not acceptance or not any(accepted['action_id'] == action_id and accepted['content_hash'] == action['content_hash']
                                          for accepted in json.loads(acceptance['receipt'])['actions']):
                raise PermissionError('action lacks committed processing acceptance')
            _validate_destination(service, action['kind'], payload, payload['_authority'])
            # A failed effect cannot leave half-applied ledger changes behind.
            conn.execute('SAVEPOINT action_effect')
            try:
                if action['kind'] == 'task.comment':
                    comment = ops.add_comment(conn, 'task', payload['task_id'], 'director:' + payload['_authority']['role_id'], payload['text'])
                    result = dict(comment_id=comment['id'], task_id=payload['task_id'])
                elif action['kind'] == 'task.retry':
                    task = ops.set_status(conn, payload['task_id'], 'ready', actor='system',
                                          reason='Scoped director recovery')
                    result = dict(task_id=task['id'], status=task['status'])
                    conn.execute('UPDATE director_issue_leases SET attempts=attempts+1 WHERE issue_id=?', (payload['issue_id'],))
                else:
                    issue = conn.execute('SELECT envelope FROM notification_inbox WHERE id=?',
                                          (int(payload['issue_id'].split(':')[1]),)).fetchone()
                    envelope = json.loads(issue['envelope'])
                    if envelope['cause_id'] is None:
                        raise Conflict('issue has no explicit resolvable cause')
                    result = dict(event_id=ops.event(conn, 'recovery.resolved', payload['_authority']['role_id'],
                                  dict(cause=envelope['cause_id']), task_id=payload['task_id']))
                conn.execute('RELEASE action_effect')
            except BaseException:
                conn.execute('ROLLBACK TO action_effect')
                conn.execute('RELEASE action_effect')
                raise
        except (ValueError, PermissionError, LookupError) as error:
            conn.execute("UPDATE action_outbox SET state='held',result=? WHERE action_id=?",
                         (canonical(dict(reason=str(error))), action_id))
            return _action(conn, action_id)
        return complete(conn, action_id, result)
