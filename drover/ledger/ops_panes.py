"""The agents in panes and what Drover wrote to them. Nothing here reaches Herdr.

A row's pane may still exist until the row is `gone`: `retired` panes wait for their close. A `gone` row is history, so
it grants nothing and never comes back. Seat, placement and who started the agent are written once.

A write keeps a digest and a short head, never the text. The digest hashes the text with its whitespace collapsed,
exactly as `pane_screen.prompt_fingerprint` does, because that is what a composer is compared with.
"""
import hashlib
from datetime import datetime, timezone

from .db import tx

AGENT_STATES = ('starting', 'live', 'rotating', 'compaction', 'retired', 'gone')
HEAD_MAX = 60
_REQUIRED = ('pane_id', 'kind', 'seat', 'placement', 'started_by')
_UPDATABLE = ('role_id', 'run_id', 'terminal_id', 'name', 'session_id', 'cwd', 'state', 'handoff_state', 'context_tokens')
_CHOICES = {'kind': ('claude', 'codex'), 'placement': ('windows', 'wsl'), 'started_by': ('drover', 'owner'),
            'state': AGENT_STATES}


def _stamp():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _check(fields, allowed):
    unknown = set(fields) - set(allowed)
    if unknown:
        raise ValueError(f'not a pane agent field that can be set here: {sorted(unknown)}')
    for name, choices in _CHOICES.items():
        if name in fields and fields[name] not in choices:
            raise ValueError(f'{name} must be one of {choices}, not {fields[name]!r}')


def _agent(conn, agent_id):
    row = conn.execute('SELECT * FROM pane_agents WHERE id=?', (agent_id,)).fetchone()
    if row is None:
        raise LookupError(f'no pane agent {agent_id}')
    return dict(row)


def record_agent(conn, **fields) -> dict:
    fields.setdefault('state', 'starting')
    _check(fields, _REQUIRED + _UPDATABLE)
    missing = [name for name in _REQUIRED if not fields.get(name)]
    if missing:
        raise ValueError(f'a pane agent needs {missing}')
    fields['created'] = fields['updated'] = _stamp()
    names = ','.join(fields)
    marks = ','.join('?' * len(fields))
    with tx(conn):
        return dict(conn.execute(f'INSERT INTO pane_agents({names}) VALUES({marks}) RETURNING *',
                                 tuple(fields.values())).fetchone())


def update_agent(conn, agent_id, **fields) -> dict:
    _check(fields, _UPDATABLE)
    with tx(conn):
        row = _agent(conn, agent_id)
        if row['state'] == 'gone' and fields.get('state', 'gone') != 'gone':
            raise ValueError(f'pane agent {agent_id} is gone and stays gone')
        if not fields:
            return row
        fields['updated'] = _stamp()
        names = ','.join(f'{name}=?' for name in fields)
        return dict(conn.execute(f'UPDATE pane_agents SET {names} WHERE id=? RETURNING *',
                                 (*fields.values(), agent_id)).fetchone())


def open_agents(conn, *, placement=None) -> list[dict]:
    """Every agent whose pane may still exist, retired ones included; `state` tells them apart."""
    where, params = ("state<>'gone'", ()) if placement is None else ("state<>'gone' AND placement=?", (placement,))
    return [dict(row) for row in conn.execute(f'SELECT * FROM pane_agents WHERE {where} ORDER BY id', params)]


def by_pane(conn, pane_id) -> dict | None:
    """The newest row that is not gone. None means Drover has no claim on this pane."""
    row = conn.execute("SELECT * FROM pane_agents WHERE pane_id=? AND state<>'gone' ORDER BY id DESC LIMIT 1",
                       (pane_id,)).fetchone()
    return dict(row) if row else None


def note_write(conn, agent_id, kind, text) -> dict:
    """Record the intent to write, before the write."""
    text = ' '.join(text.split())
    with tx(conn):
        _agent(conn, agent_id)
        row = conn.execute("INSERT INTO pane_writes(agent_id,kind,digest,head,state,at) VALUES(?,?,?,?,'pending',?) "
                           'RETURNING *', (agent_id, kind, hashlib.sha256(text.encode('utf-8')).hexdigest(),
                                           text[:HEAD_MAX], _stamp())).fetchone()
        return dict(row)


def finish_write(conn, write_id, state, reason=None) -> dict:
    if not state:
        raise ValueError('a write needs a state')
    with tx(conn):
        row = conn.execute('UPDATE pane_writes SET state=?, reason=? WHERE id=? RETURNING *',
                           (state, reason, write_id)).fetchone()
        if row is None:
            raise LookupError(f'no pane write {write_id}')
        return dict(row)


def last_write(conn, agent_id, kind=None) -> dict | None:
    where, params = ('agent_id=?', (agent_id,)) if kind is None else ('agent_id=? AND kind=?', (agent_id, kind))
    row = conn.execute(f'SELECT * FROM pane_writes WHERE {where} ORDER BY id DESC LIMIT 1', params).fetchone()
    return dict(row) if row else None
