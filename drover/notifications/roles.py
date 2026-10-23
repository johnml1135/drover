"""Durable session identity, fenced processing and bounded recovery; no runner I/O."""
import hashlib
import json
import re
import secrets

from ..ledger import ops
from ..ledger.db import tx
from .core import Conflict, _role, coalesce_issue, issue_key, recipient_owner


CAPABILITIES = frozenset(('notifications.read', 'notifications.process', 'notifications.tune'))


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError('artifact must be finite JSON data') from error


def hashed(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _ttl(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 86400:
        raise ValueError('ttl must be between zero and 86400 seconds')
    return value


def _runtime(conn, role_id):
    _role(conn, role_id)
    conn.execute('INSERT OR IGNORE INTO director_runtime(role_id) VALUES(?)', (role_id,))
    return dict(conn.execute('SELECT * FROM director_runtime WHERE role_id=?', (role_id,)).fetchone())


def issue_session(service, role_id, session_id, ttl=3600, capabilities=None):
    _ttl(ttl)
    if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 256:
        raise ValueError('session_id must be nonempty and bounded')
    if capabilities is not None and (not isinstance(capabilities, list) or
            any(not isinstance(capability, str) or capability not in CAPABILITIES for capability in capabilities)):
        raise ValueError('unsupported director capability')
    capabilities = sorted(CAPABILITIES if capabilities is None else set(capabilities))
    conn = service.conn
    with tx(conn):
        _artifact(conn, {'session_id': session_id})
        role = _role(conn, role_id)
        runtime = _runtime(conn, role_id)
        generation = role['generation'] + int(runtime['session_id'] is not None)
        credential = secrets.token_urlsafe(32)
        expires = service.now() + ttl
        conn.execute('UPDATE director_roles SET generation=? WHERE role_id=?', (generation, role_id))
        conn.execute('UPDATE director_runtime SET session_id=? WHERE role_id=?', (session_id, role_id))
        conn.execute('DELETE FROM director_leases WHERE role_id=?', (role_id,))
        conn.execute('DELETE FROM director_sessions WHERE role_id=?', (role_id,))
        conn.execute('INSERT INTO director_sessions VALUES(?,?,?,?,?,?)',
                     (hashed(credential), role_id, session_id, generation, expires, canonical(capabilities)))
        ops.event(conn, 'director.session-issued', 'system', dict(role_id=role_id, generation=generation))
        return dict(role_id=role_id, session_id=session_id, generation=generation,
                    expires_at=expires, capabilities=capabilities, credential=credential)


def authenticate(service, credential):
    if not isinstance(credential, str):
        raise PermissionError('invalid director credential')
    row = service.conn.execute('SELECT s.* FROM director_sessions s JOIN director_roles r USING(role_id) '
                               'WHERE credential_hash=? AND s.generation=r.generation AND s.expires>? '
                               "AND r.state<>'retired'", (hashed(credential), service.now())).fetchone()
    if not row:
        raise PermissionError('invalid or expired director credential')
    return dict(role_id=row['role_id'], session_id=row['session_id'], generation=row['generation'],
                expires_at=row['expires'], capabilities=json.loads(row['capabilities']))


def admission(service, role_id):
    service.check_admission(role_id)
    if _role(service.conn, role_id)['state'] != 'active':
        raise Conflict('director is paused or retired')
    if str(ops.get_setting(service.conn, 'dispatch_enabled', 'false')).lower() not in ('true', '1', 'yes', 'on'):
        raise Conflict('dispatch is paused')


def acquire(service, role_id, session_id, ttl=60):
    _ttl(ttl)
    conn = service.conn
    with tx(conn):
        role = _role(conn, role_id)
        if role['state'] == 'retired':
            raise Conflict('director is retired')
        session = conn.execute('SELECT * FROM director_sessions WHERE role_id=? AND session_id=? '
                               'AND generation=? AND expires>?',
                               (role_id, session_id, role['generation'], service.now())).fetchone()
        if not session or 'notifications.process' not in json.loads(session['capabilities']):
            raise PermissionError('current processing session required')
        existing = conn.execute('SELECT * FROM director_leases WHERE role_id=?', (role_id,)).fetchone()
        if existing and existing['generation'] == role['generation'] and existing['expires'] > service.now():
            raise Conflict('director already has an active execution lease')
        token = secrets.token_urlsafe(32)
        expires = min(service.now() + ttl, session['expires'])
        control_epoch = service.admission()['epoch']
        conn.execute('INSERT INTO director_leases VALUES(?,?,?,?,?,?) ON CONFLICT(role_id) DO UPDATE SET '
                     'session_id=excluded.session_id,generation=excluded.generation,token_hash=excluded.token_hash,'
                     'expires=excluded.expires,control_epoch=excluded.control_epoch',
                     (role_id, session_id, role['generation'], hashed(token), expires, control_epoch))
        runtime = _runtime(conn, role_id)
        conn.execute('UPDATE director_runtime SET pending_wake=0 WHERE role_id=?', (role_id,))
        return dict(role_id=role_id, session_id=session_id, generation=role['generation'],
                    lease_token=token, expires_at=expires, wake_position=runtime['wake_position'], control_epoch=control_epoch)


def validate_lease(service, role_id, token):
    if not isinstance(token, str):
        raise PermissionError('current director lease required')
    row = service.conn.execute('SELECT l.* FROM director_leases l JOIN director_roles r USING(role_id) '
                               'JOIN director_sessions s ON s.role_id=l.role_id AND s.session_id=l.session_id '
                               'AND s.generation=l.generation WHERE l.role_id=? AND l.token_hash=? '
                               'AND l.generation=r.generation AND l.expires>? AND s.expires>? '
                               "AND r.state<>'retired'", (role_id, hashed(token), service.now(), service.now())).fetchone()
    if not row:
        raise PermissionError('expired or superseded director lease')
    return dict(row)


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


def _artifact(conn, value):
    """Small persisted artifacts cannot contain credentials or authority tokens."""
    encoded = canonical(value)
    if len(encoded.encode()) > 32768:
        raise ValueError('checkpoint/action payload exceeds 32768 bytes')
    def visit(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError('artifact keys must be strings')
                if key.lower() in ('credential', 'authorization', 'access_token', 'lease_token', 'issue_token'):
                    raise ValueError('credentials cannot be persisted in artifacts')
                visit(key)
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, str):
            for table, column in (('director_sessions', 'credential_hash'), ('director_leases', 'token_hash'),
                                  ('director_issue_leases', 'token_hash')):
                candidates = {item, *re.findall(r'(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])', item)}
                for candidate in candidates:
                    if conn.execute(f'SELECT 1 FROM {table} WHERE {column}=?', (hashed(candidate),)).fetchone():
                        raise ValueError('credentials cannot be persisted in artifacts')
    visit(value)
    return encoded


def _delivery_identity(value, prefixes=('notification', 'digest')):
    try:
        prefix, ident = value.split(':')
        ident = int(ident)
        if not 0 < ident < 2**63 or prefix not in prefixes or value != f'{prefix}:{ident}':
            raise ValueError()
    except (ValueError, AttributeError):
        raise ValueError('invalid delivery identity') from None
    return prefix, ident


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


def signal(conn, role_id, event_id):
    """Called in the source transaction; an active consumer never clears arrivals."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='director_runtime'").fetchone():
        return
    _runtime(conn, role_id)
    conn.execute('UPDATE director_runtime SET wake_position=MAX(wake_position,?),pending_wake=1 WHERE role_id=?',
                 (event_id, role_id))


def refresh_wake(service, role_id):
    """Reconcile pending work while holding the same lock as checkpoint/release."""
    conn = service.conn
    runtime = _runtime(conn, role_id)
    # Reconcile the state projection will produce, including already processed
    # issues whose identical observations must not become new work on release.
    deliveries = {}
    for row in conn.execute('SELECT * FROM notification_inbox WHERE role_id=? AND resolved=0 '
                             'AND (actionable=1 OR acknowledged_at IS NULL)', (role_id,)):
        envelope = json.loads(row['envelope'])
        key = ('issue', row['issue_key']) if row['actionable'] else ('event', row['event_id'])
        deliveries[key] = envelope, row['acknowledged_at'] is None
    for row in conn.execute('SELECT i.* FROM notification_intents i LEFT JOIN notification_inbox n ON n.event_id=i.event_id '
                             'WHERE i.role_id=? AND i.resolved=0 AND i.projected=0 AND (i.actionable=1 OR n.id IS NULL) '
                             'ORDER BY i.event_id', (role_id,)):
        envelope = json.loads(row['envelope'])
        if not row['actionable'] and envelope['mode'] == 'muted':
            continue
        key = ('issue', issue_key(conn, envelope)) if row['actionable'] else ('event', row['event_id'])
        previous = deliveries.get(key)
        if previous is not None:
            if not row['actionable']:
                continue  # Replay cannot change an existing immutable delivery.
            updated = coalesce_issue(previous[0], envelope)
            if updated is None:
                continue
            envelope, changed = updated
            unread = previous[1] or changed
        else:
            unread = True
        deliveries[key] = envelope, unread
    for row in conn.execute('SELECT t.* FROM notification_targets t LEFT JOIN notification_inbox n '
                             'ON n.event_id=t.event_id AND n.role_id=t.role_id WHERE t.role_id=? AND t.projected=0 AND n.id IS NULL', (role_id,)):
        envelope = json.loads(row['envelope'])
        deliveries[('event', row['event_id'])] = envelope, True
    positions = []
    for envelope, unread in deliveries.values():
        owner = recipient_owner(conn, envelope)
        if unread and envelope['mode'] == 'immediate' and owner and owner['role_id'] == role_id:
            positions.append(envelope.get('latest_event_id', envelope['event_id']))
    for row in conn.execute('SELECT body FROM notification_digests WHERE role_id=? AND acknowledged_at IS NULL', (role_id,)):
        body = json.loads(row['body'])
        if body.get('wake') and any((owner := recipient_owner(conn, entry)) and
                                    owner['role_id'] == role_id for entry in body['entries']):
            positions.append(body['last_event'])
    continuation = bool(json.loads(runtime['checkpoint']).get('continuation', False))
    conn.execute('UPDATE director_runtime SET pending_wake=?,wake_position=MAX(wake_position,?) WHERE role_id=?',
                 (bool(positions) or continuation, max(positions, default=0), role_id))


def validate_policy(policy):
    from ..ledger.actions import KINDS
    if 'broadcast_summary' in policy and not isinstance(policy['broadcast_summary'], bool):
        raise ValueError('broadcast_summary must be a boolean')
    allowlist = policy.get('action_allowlist', [])
    if not isinstance(allowlist, list) or any(not isinstance(kind, str) or kind not in KINDS for kind in allowlist):
        raise ValueError('action_allowlist must contain only supported safe actions')
    models = policy.get('allowed_models', [])
    if not isinstance(models, list) or any(not isinstance(model, str) or not model or len(model) > 128 for model in models):
        raise ValueError('allowed_models must name explicit models')
    for key, maximum in (('recovery_attempt_limit', 2), ('replacement_limit', 1),
                         ('wake_limit_per_hour', 1000), ('token_budget', 10**12)):
        if key in policy and (isinstance(policy[key], bool) or not isinstance(policy[key], int) or not 0 <= policy[key] <= maximum):
            raise ValueError(f'{key} must be a nonnegative integer no greater than {maximum}')


def _hold_role(service, role_id, reason):
    runtime = _runtime(service.conn, role_id)
    question = runtime['fallback_question']
    if question is None:
        question = service.conn.execute('INSERT INTO questions(target_kind,target_id,title,body,asked) VALUES(?,?,?,?,?)',
                                         ('director', role_id, f'Director needs attention: {role_id}', reason, ops.stamp())).lastrowid
        ops.event(service.conn, 'director.held', 'system', dict(role_id=role_id, reason=reason, question=question))
    service.conn.execute('UPDATE director_runtime SET held_reason=?,fallback_question=? WHERE role_id=?',
                         (reason, question, role_id))
    return dict(role_id=role_id, accepted=False, held=True, reason=reason, question_id=question)


def _clear_role_hold(service, role_id):
    runtime = _runtime(service.conn, role_id)
    if runtime['fallback_question'] is not None:
        service.conn.execute("UPDATE questions SET status='answered',answer_text=?,answered_at=? WHERE id=? AND status='open'",
                             ('Director admission hold cleared.', ops.stamp(), runtime['fallback_question']))
        ops.event(service.conn, 'director.hold-cleared', 'system', dict(role_id=role_id, question=runtime['fallback_question']))
    service.conn.execute('UPDATE director_runtime SET held_reason=NULL,fallback_question=NULL WHERE role_id=?', (role_id,))


def reserve_wake(service, role_id, request_key, tokens=0, model=None):
    """Reserve an invocation's maximum token use, never a model call or launch.

    Reservations are conservative lifetime charges. Runtime adapters must cap a
    run at this reservation; restarting the hub never restores consumed budget.
    """
    if not isinstance(request_key, str) or not 0 < len(request_key) <= 256:
        raise ValueError('bounded wake request_key required')
    if isinstance(tokens, bool) or not isinstance(tokens, int) or not 0 <= tokens <= 10**12:
        raise ValueError('tokens must be a nonnegative bounded integer')
    if model is not None and (not isinstance(model, str) or not 0 < len(model) <= 128):
        raise ValueError('bounded model name required')
    conn = service.conn
    request_hash = hashed(canonical(dict(tokens=tokens, model=model)))
    with tx(conn):
        _artifact(conn, {'request_key': request_key, 'model': model})
        prior = conn.execute('SELECT * FROM director_budget_entries WHERE role_id=? AND request_key=?', (role_id, request_key)).fetchone()
        if prior:
            if prior['request_hash'] != request_hash:
                raise Conflict('wake request key reused with different content')
            return json.loads(prior['receipt'])
        policy = json.loads(_role(conn, role_id)['policy'])
        _runtime(conn, role_id)
        reason = None
        try:
            admission(service, role_id)
        except Conflict as error:
            reason = str(error)
        live = conn.execute('SELECT 1 FROM director_leases l JOIN director_roles r USING(role_id) '
                             'WHERE l.role_id=? AND l.generation=r.generation AND expires>?', (role_id, service.now())).fetchone()
        if reason is None and live:
            receipt = dict(role_id=role_id, accepted=False, coalesced=True, reason='director-already-active')
            conn.execute('INSERT INTO director_budget_entries VALUES(?,?,?,?,0,0,?,?)',
                         (role_id, request_key, request_hash, service.now(), model, canonical(receipt)))
            return receipt
        if reason is None and (model is not None or tokens) and model not in policy.get('allowed_models', []):
            reason = 'model-not-authorized'
        wakes = conn.execute('SELECT COALESCE(SUM(wakes),0) FROM director_budget_entries WHERE role_id=? AND created>?',
                              (role_id, service.now() - 3600)).fetchone()[0]
        total = conn.execute('SELECT COALESCE(SUM(tokens),0) FROM director_budget_entries WHERE role_id=?', (role_id,)).fetchone()[0]
        if reason is None and wakes >= policy.get('wake_limit_per_hour', 4):
            reason = 'wake-budget-exhausted'
        if reason is None and total + tokens > policy.get('token_budget', 0):
            reason = 'token-budget-exhausted'
        if reason:
            receipt = _hold_role(service, role_id, reason)
        else:
            _clear_role_hold(service, role_id)
            receipt = dict(role_id=role_id, accepted=True, tokens=tokens, model=model,
                           generation=_role(conn, role_id)['generation'],
                           wake_position=_runtime(conn, role_id)['wake_position'])
        conn.execute('INSERT INTO director_budget_entries VALUES(?,?,?,?,?,?,?,?)',
                     (role_id, request_key, request_hash, service.now(), int(reason is None),
                      tokens if reason is None else 0, model, canonical(receipt)))
        return receipt


def replace(service, role_id, expected_generation, *, confirmed_dead=False, reason='consumer-confirmed-gone'):
    """One automatic replacement admission; caller supplies verified supervision evidence."""
    if not isinstance(confirmed_dead, bool) or not isinstance(reason, str) or not 0 < len(reason) <= 1024:
        raise ValueError('confirmed_dead boolean and bounded reason required')
    conn = service.conn
    with tx(conn):
        role = _role(conn, role_id)
        if expected_generation != role['generation']:
            raise Conflict('director generation changed')
        runtime = _runtime(conn, role_id)
        try:
            admission(service, role_id)
        except Conflict as error:
            return _hold_role(service, role_id, str(error))
        if not confirmed_dead:
            return _hold_role(service, role_id, 'consumer-liveness-uncertain')
        if runtime['replacements_used'] >= min(1, json.loads(role['policy']).get('replacement_limit', 1)):
            return _hold_role(service, role_id, 'replacement-budget-exhausted')
        _artifact(conn, {'reason': reason})
        _clear_role_hold(service, role_id)
        generation = role['generation'] + 1
        conn.execute('UPDATE director_roles SET generation=? WHERE role_id=?', (generation, role_id))
        conn.execute('UPDATE director_runtime SET session_id=NULL,replacements_used=replacements_used+1,held_reason=NULL WHERE role_id=?', (role_id,))
        conn.execute('DELETE FROM director_leases WHERE role_id=?', (role_id,))
        conn.execute('DELETE FROM director_sessions WHERE role_id=?', (role_id,))
        ops.event(conn, 'director.replaced', 'system', dict(role_id=role_id, generation=generation, reason=reason))
        return dict(role_id=role_id, generation=generation, accepted=True,
                    checkpoint=json.loads(runtime['checkpoint']), checkpoint_revision=runtime['checkpoint_revision'])
