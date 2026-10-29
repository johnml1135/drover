"""Director sessions and leases."""
import json
import secrets

from ..ledger import ops
from ..ledger.db import tx
from .core_scope import Conflict, _role
from .roles_base import CAPABILITIES, _artifact, _runtime, _ttl, canonical, hashed

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
        control_epoch = service.admission(role_id=role_id)['epoch']
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
