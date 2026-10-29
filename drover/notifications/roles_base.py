"""Director role primitives and artifact identities."""
import hashlib
import json
import re

from ..ledger import ops
from .core_scope import _role

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
