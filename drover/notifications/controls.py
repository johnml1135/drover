"""Deterministic retained controls; runtime safe points consume admission epochs."""
from .core import _role


def admission(conn, role_id=None, generation=None, epoch=None):
    state = conn.execute('SELECT * FROM notification_control_state WHERE id=1').fetchone()
    result = dict(allowed=True, reason=None, epoch=state['epoch'])
    if state['stopped']:
        result.update(allowed=False, reason='stop-work')
    elif epoch is not None and epoch != state['epoch']:
        result.update(allowed=False, reason='control-epoch-changed')
    elif role_id is not None:
        role = _role(conn, role_id)
        if generation is not None and generation != role['generation']:
            result.update(allowed=False, reason='director-generation-changed')
        else:
            runtime = conn.execute('SELECT session_id FROM director_runtime WHERE role_id=?', (role_id,)).fetchone()
            for control in conn.execute("SELECT c.* FROM notification_controls c JOIN notification_targets t ON t.event_id=c.event_id "
                                        "WHERE t.role_id=? AND t.superseded=0 AND c.active=1 AND c.kind='skill' ORDER BY c.epoch", (role_id,)):
                applied = conn.execute('SELECT 1 FROM notification_control_applications WHERE control_id=? AND role_id=? '
                                       'AND generation=? AND session_id=? AND digest=? AND status=?',
                                       (control['control_id'], role_id, role['generation'], runtime['session_id'] if runtime else None,
                                        control['digest'], 'applied')).fetchone()
                if not applied:
                    result.update(allowed=False, reason=f"skill-required:{control['skill']}:{control['digest']}")
                    break
    return result


def _issuer(conn, issuer, global_control=False):
    """Trusted owner/service strings and explicit director principals are distinct.

    Transports must construct the director dictionary from authenticated identity;
    they must never forward a director's role text as trusted service authority.
    """
    if isinstance(issuer, str) and issuer in ('owner', 'service'):
        return issuer
    if global_control:
        raise PermissionError('global controls require trusted owner or service authority')
    if isinstance(issuer, dict) and set(issuer) == {'kind', 'role_id'} and issuer['kind'] == 'director':
        role_id = issuer['role_id']
    elif isinstance(issuer, str):
        role_id = issuer  # Existing unreserved in-process director summary calls.
    else:
        raise PermissionError('authenticated director principal required')
    if not isinstance(role_id, str):
        raise PermissionError('authenticated director principal required')
    role = _role(conn, role_id)
    import json
    if not json.loads(role['policy']).get('broadcast_summary', False):
        raise PermissionError('summary broadcast capability required')
    return 'director:' + role_id


def _targets(conn, targets):
    if targets == 'all':
        return [r[0] for r in conn.execute("SELECT role_id FROM director_roles WHERE state<>'retired' ORDER BY role_id")]
    if not isinstance(targets, list) or not targets or len(targets) > 100 or any(not isinstance(t, str) for t in targets):
        raise ValueError('targets must be all or a nonempty bounded role list')
    result = sorted(set(targets))
    for role in result:
        _role(conn, role)
    return result


def _idempotent(service, issuer, request_key, request, apply):
    import json
    from ..ledger.db import tx
    from .roles import _artifact, hashed, canonical
    from .core import Conflict
    if not isinstance(request_key, str) or not request_key or len(request_key) > 256:
        raise ValueError('bounded request_key required')
    with tx(service.conn):
        _artifact(service.conn, dict(request_key=request_key, request=request))
        request_hash = hashed(canonical(request))
        prior = service.conn.execute('SELECT * FROM notification_control_requests WHERE issuer=? AND request_key=?',
                                     (issuer, request_key)).fetchone()
        if prior:
            if prior['request_hash'] != request_hash:
                raise Conflict('notice/control request key reused with different content')
            return json.loads(prior['receipt'])
        receipt = apply()
        service.conn.execute('INSERT INTO notification_control_requests VALUES(?,?,?,?)',
                             (issuer, request_key, request_hash, canonical(receipt)))
        return receipt


def _target(service, notice, role_id):
    from ..ledger import ops
    from .core import bound_envelope
    from .roles import signal
    envelope = dict(schema_version=1, event_id=notice['event_id'], event_class='control' if notice['control_id'] else 'broadcast',
                    task_id=None, group_id=None, notice_id=notice['notice_id'], control_id=notice['control_id'],
                    target_role=role_id, cause_id=None, at=notice['at'], actor=notice['issuer'],
                    severity='critical' if notice['urgency'] == 'urgent' else 'info', reason=None,
                    expected_revision=notice['event_id'], evidence=notice['body'], profile_version=1,
                    mode='immediate' if notice['urgency'] == 'urgent' else 'digest',
                    subscription_id=None, subscription_revision=None)
    bound_envelope(envelope)
    service.conn.execute('INSERT OR IGNORE INTO notification_targets(event_id,role_id,envelope) VALUES(?,?,?)',
                         (notice['event_id'], role_id, ops._j(envelope)))
    # New targets for a persistent control may be inserted after its source cursor.
    service.conn.execute("UPDATE notification_positions SET event_id=MIN(event_id,?) WHERE name='inbox'", (notice['event_id'] - 1,))
    if notice['urgency'] == 'urgent':
        signal(service.conn, role_id, notice['event_id'])


def _publish(service, issuer, targets, body, urgency, control_id=None):
    from ..ledger import ops
    from .roles import _artifact
    if urgency not in ('summary', 'urgent'):
        raise ValueError('unknown urgency')
    if not isinstance(body, (str, dict)):
        raise ValueError('notice body must be text or an object')
    saved = _artifact(service.conn, body)
    if len(saved.encode()) > 8192:
        raise ValueError('notice body exceeds 8192 bytes')
    chosen = _targets(service.conn, targets)
    notice_id = ops._next_id(service.conn, 'B')
    at = ops.stamp()
    source = ops.event(service.conn, 'notification.broadcast', issuer,
                       dict(notice_id=notice_id, targets=chosen, urgency=urgency, body=body, control_id=control_id), at=at)
    service.conn.execute('INSERT INTO notification_notices VALUES(?,?,?,?,?,?,?)',
                         (notice_id, source, issuer, urgency, saved, control_id, targets == 'all'))
    notice = dict(notice_id=notice_id, event_id=source, issuer=issuer, urgency=urgency, body=body,
                  control_id=control_id, at=at)
    for role in chosen:
        _target(service, notice, role)
    return dict(notice_id=notice_id, event_id=source, targets=chosen, urgency=urgency, control_id=control_id)


def broadcast(service, issuer, targets, body, urgency='summary', control=None, request_key=None):
    issuer = _issuer(service.conn, issuer, global_control=urgency == 'urgent' or control is not None)
    if control is not None:
        raise ValueError('structured controls require stop, resume or require_skill')
    # A missing key permits a new explicitly requested notice; callers needing retries supply one.
    if request_key is None:
        from ..ledger.db import tx
        with tx(service.conn):
            return _publish(service, issuer, targets, body, urgency)
    request = dict(operation='broadcast', targets=targets, body=body, urgency=urgency)
    return _idempotent(service, issuer, request_key, request, lambda: _publish(service, issuer, targets, body, urgency))


def _control(service, issuer, kind, targets, skill=None, digest=None):
    from ..ledger import ops
    conn = service.conn
    state = conn.execute('SELECT * FROM notification_control_state WHERE id=1').fetchone()
    epoch = state['epoch'] + 1
    control_id = ops._next_id(conn, 'C')
    if kind == 'stop':
        conn.execute("UPDATE notification_controls SET active=0 WHERE kind='stop'")
        conn.execute('UPDATE notification_control_state SET epoch=?,stopped=1 WHERE id=1', (epoch,))
    elif kind == 'resume':
        conn.execute("UPDATE notification_controls SET active=0 WHERE kind='stop'")
        conn.execute('UPDATE notification_control_state SET epoch=?,stopped=0 WHERE id=1', (epoch,))
    else:
        conn.execute('UPDATE notification_control_state SET epoch=? WHERE id=1', (epoch,))
    conn.execute('INSERT INTO notification_controls(control_id,kind,epoch,skill,digest,targets_all) VALUES(?,?,?,?,?,?)',
                 (control_id, kind, epoch, skill, digest, targets == 'all'))
    body = dict(kind=kind, epoch=epoch, control_id=control_id)
    if kind == 'skill':
        body.update(skill=skill, digest=digest)
        # Requiring a newer version supersedes earlier requirements only for
        # the addressed partition. Historical target/application facts remain.
        for role in _targets(conn, targets):
            conn.execute("UPDATE notification_targets SET superseded=1 WHERE role_id=? AND event_id IN ("
                         "SELECT event_id FROM notification_controls WHERE kind='skill' AND skill=? AND control_id<>?)",
                         (role, skill, control_id))
        if targets == 'all':
            conn.execute("UPDATE notification_controls SET active=0 WHERE kind='skill' AND skill=? AND targets_all=1 AND control_id<>?",
                         (skill, control_id))
    notice = _publish(service, issuer, targets, body, 'urgent', control_id)
    conn.execute('UPDATE notification_controls SET event_id=? WHERE control_id=?', (notice['event_id'], control_id))
    conn.execute("UPDATE notification_inbox SET mode='muted',envelope=json_set(envelope,'$.mode','muted') "
                 "WHERE EXISTS (SELECT 1 FROM notification_targets t JOIN notification_controls c USING(event_id) "
                 "WHERE t.event_id=notification_inbox.event_id AND t.role_id=notification_inbox.role_id "
                 "AND (t.superseded=1 OR c.active=0))")
    from .core import reconcile_storage_hold
    from .roles import refresh_wake
    for role_id in notice['targets']:
        reconcile_storage_hold(conn, role_id)
        refresh_wake(service, role_id)
    return dict(notice, epoch=epoch, kind=kind, skill=skill, digest=digest)


def stop(service, issuer, request_key):
    issuer = _issuer(service.conn, issuer, global_control=True)
    return _idempotent(service, issuer, request_key, dict(operation='stop'),
                       lambda: _control(service, issuer, 'stop', 'all'))


def resume(service, issuer, expected_epoch, request_key):
    from .core import Conflict
    issuer = _issuer(service.conn, issuer, global_control=True)
    if isinstance(expected_epoch, bool) or not isinstance(expected_epoch, int) or expected_epoch < 0:
        raise ValueError('nonnegative expected_epoch required')
    def apply():
        state = service.conn.execute('SELECT * FROM notification_control_state WHERE id=1').fetchone()
        if state['epoch'] != expected_epoch:
            raise Conflict('control epoch changed')
        return _control(service, issuer, 'resume', 'all')
    return _idempotent(service, issuer, request_key, dict(operation='resume', expected_epoch=expected_epoch), apply)


def require_skill(service, issuer, skill, digest, targets, request_key):
    issuer = _issuer(service.conn, issuer, global_control=True)
    if any(not isinstance(value, str) or not value.strip() or len(value) > 256 for value in (skill, digest)):
        raise ValueError('bounded skill and version digest required')
    return _idempotent(service, issuer, request_key, dict(operation='skill', skill=skill, digest=digest, targets=targets),
                       lambda: _control(service, issuer, 'skill', targets, skill, digest))


def ack_control(service, role_id, lease_token, control_id, digest=None):
    from ..ledger.db import tx
    from .core import Conflict
    from .roles import validate_lease
    with tx(service.conn):
        lease = validate_lease(service, role_id, lease_token)
        control = service.conn.execute('SELECT c.* FROM notification_controls c JOIN notification_targets t '
                                        'ON t.event_id=c.event_id WHERE c.control_id=? AND t.role_id=? '
                                        'AND t.superseded=0 AND c.active=1', (control_id, role_id)).fetchone()
        if not control:
            raise Conflict('control is unavailable or superseded for this director')
        if control['kind'] == 'skill' and digest != control['digest']:
            raise Conflict('skill acknowledgement must cite the required version digest')
        if control['kind'] != 'skill' and digest is not None:
            raise ValueError('this control has no skill digest')
        service.conn.execute('INSERT OR IGNORE INTO notification_control_applications VALUES(?,?,?,?,?,?,?)',
                             (control_id, role_id, lease['generation'], lease['session_id'], 'applied', digest, service.now()))
        row = service.conn.execute('SELECT * FROM notification_control_applications WHERE control_id=? AND role_id=? '
                                    'AND generation=? AND session_id=?',
                                    (control_id, role_id, lease['generation'], lease['session_id'])).fetchone()
        return dict(row)


def apply_persistent(service, role_id):
    """Registration/session hook. Only active global controls extend their snapshot."""
    import json
    for row in service.conn.execute('SELECT n.*,e.at FROM notification_notices n JOIN task_events e ON e.id=n.event_id '
                                    'JOIN notification_controls c ON c.control_id=n.control_id '
                                    "WHERE c.active=1 AND c.targets_all=1 AND c.kind IN ('stop','skill') ORDER BY n.event_id").fetchall():
        # Target-specific newer requirements remain authoritative for this role.
        if service.conn.execute('SELECT 1 FROM notification_targets WHERE event_id=? AND role_id=?',
                                 (row['event_id'], role_id)).fetchone():
            continue
        notice = dict(row)
        notice['body'] = json.loads(row['body'])
        _target(service, notice, role_id)


def control_state(service, role_id=None):
    """Safe owner/role view: receipt is distinct from session application."""
    conn = service.conn
    state = dict(conn.execute('SELECT * FROM notification_control_state WHERE id=1').fetchone())
    controls = []
    for row in conn.execute('SELECT c.*,t.role_id,t.superseded FROM notification_controls c JOIN notification_targets t '
                            'ON t.event_id=c.event_id WHERE (? IS NULL OR t.role_id=?) ORDER BY c.epoch,t.role_id', (role_id, role_id)):
        value = dict(row)
        role = _role(conn, row['role_id'])
        runtime = conn.execute('SELECT session_id FROM director_runtime WHERE role_id=?', (row['role_id'],)).fetchone()
        application = conn.execute('SELECT * FROM notification_control_applications WHERE control_id=? AND role_id=? '
                                   'AND generation=? AND session_id=?',
                                   (row['control_id'], row['role_id'], role['generation'], runtime['session_id'] if runtime else None)).fetchone()
        delivery = conn.execute('SELECT offered_at,acknowledged_at FROM notification_inbox WHERE event_id=? AND role_id=?',
                                (row['event_id'], row['role_id'])).fetchone()
        value.update(status='superseded' if row['superseded'] or not row['active'] else application['status'] if application else 'received' if delivery and delivery['offered_at'] is not None else 'pending',
                     generation=role['generation'], session_id=runtime['session_id'] if runtime else None,
                     application=dict(application) if application else None)
        controls.append(value)
    state['controls'] = controls
    return state
