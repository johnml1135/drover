"""Director checkpoints, deliveries, and issue claims."""
import json
import secrets

from ..ledger import ops
from ..ledger.db import tx
from .core_scope import Conflict, _role, recipient_owner
from .core_evidence import issue_key
from .roles_base import _artifact, _delivery_identity, _runtime, _ttl, canonical, hashed
from .roles_auth import validate_lease
from .roles_wake import refresh_wake

def role_state(service, role_id):
    with tx(service.conn):
        state = _runtime(service.conn, role_id)
        state['checkpoint'] = json.loads(state['checkpoint'])
        state['generation'] = _role(service.conn, role_id)['generation']
        lease = service.conn.execute('SELECT session_id,generation,expires,control_epoch FROM director_leases WHERE role_id=?',
                                     (role_id,)).fetchone()
        state['lease'] = dict(lease) if lease else None
        budget = service.conn.execute('SELECT COALESCE(SUM(tokens),0) AS tokens,COALESCE(SUM(wakes),0) AS wakes '
                                       'FROM director_budget_entries WHERE role_id=?', (role_id,)).fetchone()
        state['tokens_reserved'] = budget['tokens']
        state['wakes_reserved'] = budget['wakes']
        state['tokens_by_model'] = {row['model']: row['tokens'] for row in service.conn.execute(
            'SELECT model,SUM(tokens) AS tokens FROM director_budget_entries WHERE role_id=? AND model IS NOT NULL GROUP BY model', (role_id,))}
        state['coalesced_wakes'] = service.conn.execute(
            "SELECT COUNT(*) FROM director_budget_entries WHERE role_id=? AND json_extract(receipt,'$.coalesced')=1", (role_id,)).fetchone()[0]
        state['runs'] = [dict(row) for row in service.conn.execute(
            'SELECT d.run_id,d.generation,d.checkpoint_revision,d.control_epoch,d.state,d.held_reason,'
            'd.launch_action_id,d.stop_action_id,r.outcome FROM director_runs d JOIN runs r ON r.id=d.run_id '
            'WHERE role_id=? ORDER BY run_id DESC LIMIT 20', (role_id,))]
        state['issues'] = []
        for row in service.conn.execute('SELECT * FROM director_issue_leases WHERE role_id=?', (role_id,)):
            try:
                _, envelope, _ = _issue(service, role_id, row['issue_id'])
            except (Conflict, PermissionError):
                continue
            state['issues'].append(dict(issue_id=row['issue_id'], task_id=envelope['task_id'], group_id=envelope.get('group_id'), generation=row['generation'],
                                        attempts=row['attempts'], attempt_limit=row['attempt_limit'], expires_at=row['expires'],
                                        held_reason=row['held_reason'], question_id=row['fallback_question']))
        return state

def get_checkpoint(service, role_id, revision=None):
    _role(service.conn, role_id)
    if revision is not None and (isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0):
        raise ValueError('checkpoint revision must be positive')
    row = service.conn.execute('SELECT * FROM director_checkpoints WHERE role_id=? AND (? IS NULL OR revision=?) '
                               'ORDER BY revision DESC LIMIT 1', (role_id, revision, revision)).fetchone()
    if not row:
        raise ops.NotFound('checkpoint revision')
    return dict(checkpoint=json.loads(row['checkpoint']), checkpoint_revision=row['revision'], event_id=row['event_id'])

def _acknowledge(service, role_id, delivery_id, versions):
    conn = service.conn
    prefix, ident = _delivery_identity(delivery_id)
    table = 'notification_inbox' if prefix == 'notification' else 'notification_digests'
    row = conn.execute(f'SELECT * FROM {table} WHERE id=?', (ident,)).fetchone()
    if not row:
        # Compacted identities are never aliases for their covering successors.
        raise Conflict('resync-required: delivery is unavailable; fetch current scoped snapshot')
    if row['role_id'] != role_id:
        raise PermissionError('delivery belongs to another director')
    if prefix == 'notification':
        envelope = json.loads(row['envelope'])
        owner = recipient_owner(conn, envelope)
        if not owner or owner['role_id'] != role_id or owner['revision'] != row['ownership_revision']:
            raise PermissionError('delivery ownership changed')
        version = envelope.get('latest_event_id', row['event_id'])
        if row['actionable']:
            for candidate in conn.execute('SELECT event_id,envelope FROM notification_intents WHERE role_id=? '
                                           'AND event_class=? AND actionable=1 AND resolved=0 AND event_id>? '
                                           "AND json_extract(envelope,'$.task_id') IS ? ORDER BY event_id DESC",
                                           (role_id, row['event_class'], version, envelope['task_id'])):
                if issue_key(conn, json.loads(candidate['envelope'])) == row['issue_key']:
                    version = candidate['event_id']
                    break
            if delivery_id not in versions:
                raise Conflict('actionable delivery requires message_version')
        if delivery_id in versions and versions[delivery_id] != version:
            raise Conflict('delivery message_version changed; receive current evidence before acknowledging')
    elif delivery_id in versions and versions[delivery_id] != ident:
        raise Conflict('digest message_version changed')
    conn.execute(f'UPDATE {table} SET acknowledged_at=COALESCE(acknowledged_at,?) WHERE id=?', (service.now(), ident))

def commit(service, role_id, lease_token, checkpoint, delivery_ids, actions, request_key, release=False, delivery_versions=None):
    if not isinstance(request_key, str) or not request_key or len(request_key) > 256:
        raise ValueError('bounded request_key required')
    if not isinstance(checkpoint, dict) or not isinstance(delivery_ids, list) or not isinstance(actions, list):
        raise ValueError('checkpoint object and delivery/action lists required')
    if any(not isinstance(item, str) for item in delivery_ids) or len(delivery_ids) > 100 or len(actions) > 32 or len(set(delivery_ids)) != len(delivery_ids):
        raise ValueError('processing batch exceeds limits or repeats delivery identities')
    if not isinstance(release, bool):
        raise ValueError('release must be a boolean')
    versions = {} if delivery_versions is None else delivery_versions
    if not isinstance(versions, dict) or any(not isinstance(key, str) or isinstance(value, bool) or
            not isinstance(value, int) or value <= 0 for key, value in versions.items()) or not set(versions) <= set(delivery_ids):
        raise ValueError('delivery_versions must map selected identities to positive message versions')
    request = dict(checkpoint=checkpoint, delivery_ids=delivery_ids, actions=actions, release=release, delivery_versions=versions)
    request_hash = hashed(canonical(request))
    conn = service.conn
    with tx(conn):
        _artifact(conn, {'request_key': request_key})
        prior = conn.execute('SELECT * FROM director_processing WHERE role_id=? AND request_key=?',
                             (role_id, request_key)).fetchone()
        if prior:
            # A released token may read its own immutable receipt while its
            # session is current. It cannot authorize another mutation.
            released_identity = conn.execute('SELECT 1 FROM director_sessions s JOIN director_roles r USING(role_id) '
                                              'WHERE s.role_id=? AND s.session_id=? AND s.generation=? '
                                              'AND r.generation=s.generation AND s.expires>? AND r.state<>?',
                                              (role_id, prior['session_id'], prior['generation'], service.now(), 'retired')).fetchone()
            if not isinstance(lease_token, str) or prior['lease_hash'] != hashed(lease_token) or not released_identity:
                validate_lease(service, role_id, lease_token)
            if prior['request_hash'] != request_hash:
                raise Conflict('processing request key reused with different content')
            return json.loads(prior['receipt'])
        lease = validate_lease(service, role_id, lease_token)
        saved_checkpoint = _artifact(conn, checkpoint)
        runtime = _runtime(conn, role_id)
        revision = runtime['checkpoint_revision'] + 1
        source = ops.event(conn, 'director.checkpoint', 'system', dict(role_id=role_id,
                           generation=lease['generation'], checkpoint_revision=revision))
        conn.execute('UPDATE director_runtime SET checkpoint=?,checkpoint_revision=?,checkpoint_event=? WHERE role_id=?',
                     (saved_checkpoint, revision, source, role_id))
        conn.execute('INSERT INTO director_checkpoints VALUES(?,?,?,?)', (role_id, revision, source, saved_checkpoint))
        for delivery_id in delivery_ids:
            _acknowledge(service, role_id, delivery_id, versions)
        from ..ledger import actions as outbox
        accepted = []
        for index, action in enumerate(actions):
            payload = outbox.authorize(service, role_id, lease, action)
            accepted.append(outbox.enqueue(conn, source, index, action['kind'], payload))
        if release:
            conn.execute('DELETE FROM director_leases WHERE role_id=?', (role_id,))
            refresh_wake(service, role_id)
        receipt = dict(role_id=role_id, event_id=source, checkpoint_revision=revision,
                       delivery_ids=delivery_ids, delivery_versions=versions, actions=accepted, released=bool(release))
        conn.execute('INSERT INTO director_processing VALUES(?,?,?,?,?,?,?,?)',
                     (role_id, request_key, request_hash, source, canonical(receipt),
                      lease['generation'], lease['session_id'], lease['token_hash']))
        return receipt

def _issue(service, role_id, issue_id):
    _, number = _delivery_identity(issue_id, ('notification',))
    row = service.conn.execute('SELECT * FROM notification_inbox WHERE id=? AND actionable=1 AND resolved=0',
                               (number,)).fetchone()
    if not row:
        raise Conflict('issue is no longer unresolved')
    envelope = json.loads(row['envelope'])
    owner = recipient_owner(service.conn, envelope)
    if row['role_id'] != role_id or not owner or owner['role_id'] != role_id:
        raise PermissionError('issue is owned by another director')
    return dict(row), envelope, owner

def claim_issue(service, role_id, issue_id, lease_token, ttl=60, max_attempts=2):
    _ttl(ttl)
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 0 <= max_attempts <= 2:
        raise ValueError('issue attempt limit must be between zero and two')
    conn = service.conn
    with tx(conn):
        lease = validate_lease(service, role_id, lease_token)
        row, envelope, owner = _issue(service, role_id, issue_id)
        prior = conn.execute('SELECT * FROM director_issue_leases WHERE issue_id=?', (issue_id,)).fetchone()
        if prior and prior['role_id'] == role_id and prior['generation'] == lease['generation'] and \
                prior['ownership_revision'] == owner['revision'] and prior['expires'] > service.now():
            raise Conflict('issue already has an active handler')
        policy = json.loads(_role(conn, role_id)['policy'])
        limit = min(2, max_attempts, policy.get('recovery_attempt_limit', 2), prior['attempt_limit'] if prior else 2)
        token = secrets.token_urlsafe(32)
        expires = min(service.now() + ttl, lease['expires'])
        conn.execute('INSERT INTO director_issue_leases(issue_id,role_id,generation,token_hash,expires,ownership_revision,attempt_limit) '
                     'VALUES(?,?,?,?,?,?,?) ON CONFLICT(issue_id) DO UPDATE SET role_id=excluded.role_id,'
                     'generation=excluded.generation,token_hash=excluded.token_hash,expires=excluded.expires,'
                     'ownership_revision=excluded.ownership_revision,attempt_limit=excluded.attempt_limit',
                     (issue_id, role_id, lease['generation'], hashed(token), expires, owner['revision'], limit))
        return dict(issue_id=issue_id, role_id=role_id, generation=lease['generation'], issue_token=token,
                    expires_at=expires, attempts=prior['attempts'] if prior else 0, attempt_limit=limit,
                    task_id=envelope['task_id'], event_class=row['event_class'])

def validate_issue(service, role_id, issue_id, token, *, hashed_token=False):
    row, envelope, owner = _issue(service, role_id, issue_id)
    if not isinstance(token, str):
        raise PermissionError('current issue handling token required')
    claim = service.conn.execute('SELECT * FROM director_issue_leases WHERE issue_id=?', (issue_id,)).fetchone()
    if not claim or claim['token_hash'] != (token if hashed_token else hashed(token)) or \
            claim['role_id'] != role_id or claim['generation'] != _role(service.conn, role_id)['generation'] or \
            claim['ownership_revision'] != owner['revision'] or claim['expires'] <= service.now():
        raise PermissionError('issue handling authority expired or changed')
    return dict(claim, task_id=envelope['task_id'], event_class=row['event_class'])

def _hold_issue(service, role_id, issue_id, reason):
    conn = service.conn
    row, envelope, owner = _issue(service, role_id, issue_id)
    prior = conn.execute('SELECT fallback_question FROM director_issue_leases WHERE issue_id=?', (issue_id,)).fetchone()
    question = prior['fallback_question'] if prior else None
    if question is None:
        question = conn.execute('INSERT INTO questions(target_kind,target_id,title,body,asked) VALUES(?,?,?,?,?)',
                                ('task' if envelope['task_id'] else 'group', envelope['task_id'] or envelope['group_id'], f'Director issue needs attention: {role_id}', reason, ops.stamp())).lastrowid
        ops.event(conn, 'director.issue-held', 'system', dict(role_id=role_id, issue_id=issue_id,
                  reason=reason, question=question), task_id=envelope['task_id'])
    conn.execute('INSERT INTO director_issue_leases(issue_id,role_id,generation,token_hash,expires,ownership_revision,held_reason,fallback_question) '
                 'VALUES(?,?,?,?,0,?,?,?) ON CONFLICT(issue_id) DO UPDATE SET held_reason=excluded.held_reason,fallback_question=excluded.fallback_question',
                 (issue_id, role_id, _role(conn, role_id)['generation'], hashed(secrets.token_urlsafe(32)),
                  owner['revision'], reason, question))
    return dict(issue_id=issue_id, held=True, reason=reason, question_id=question)

def hold_issue(service, role_id, issue_id, lease_token, reason):
    if not isinstance(reason, str) or not 0 < len(reason) <= 1024:
        raise ValueError('bounded held reason required')
    with tx(service.conn):
        validate_lease(service, role_id, lease_token)
        _artifact(service.conn, {'reason': reason})
        return _hold_issue(service, role_id, issue_id, reason)
