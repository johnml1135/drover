"""Notification evidence and issue identity."""
import hashlib
import json

from ..ledger import ops

def serialized_size(value):
    """Count complete JSON bytes, including non-ASCII escapes and separators."""
    return len(json.dumps(value).encode('utf-8'))

def _text_excerpt(value, byte_limit):
    text = value if isinstance(value, str) else str(value)
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if serialized_size(text[:middle]) <= byte_limit:
            low = middle
        else:
            high = middle - 1
    return text[:low]

def _cause_identity(value):
    if value is None:
        return None
    text = str(value)
    return text if serialized_size(text) <= 512 else 'sha256:' + hashlib.sha256(text.encode('utf-8')).hexdigest()

def _bounded_evidence(data, event_id):
    if serialized_size(data) <= 8192:
        return data
    evidence = {'truncated': True, 'source_event_id': event_id}
    for key in ('text', 'decision', 'state', 'result', 'reason', 'cause', 'question', 'commit', 'sha', 'report'):
        if key in data:
            evidence[key] = _text_excerpt(data[key], 512)
    return evidence

def bound_envelope(envelope):
    if serialized_size(envelope) > 16384:
        envelope['evidence'] = {'truncated': True, 'source_event_id': envelope['event_id']}
    if serialized_size(envelope) > 16384:
        raise ValueError('notification metadata exceeds the serialized envelope byte limit')
    return envelope

def bound_digest(message):
    """Validate the complete digest without changing its retained body or identity."""
    if serialized_size(message) > 32768:
        raise ValueError('digest exceeds the serialized message byte limit')
    return message

def issue_key(conn, envelope):
    """Use typed source identity, independently of bounded display evidence."""
    source = conn.execute('SELECT data FROM task_events WHERE id=?',
                           (envelope.get('latest_event_id', envelope['event_id']),)).fetchone()
    if source is None:
        raise ops.NotFound('issue source event')
    data = json.loads(source['data'])
    cause = _cause_identity(data.get('cause'))
    reason = _text_excerpt(data['reason'], 512) if data.get('reason') is not None else None
    return ops._j([envelope['task_id'], envelope['event_class'],
                   cause or reason or data.get('question')])

def coalesce_issue(previous, following):
    """Return the newer envelope and whether it makes processed evidence unread."""
    latest = previous.get('latest_event_id', previous['event_id'])
    if following['event_id'] <= latest:
        return None
    updated = dict(following, event_id=previous['event_id'], latest_event_id=following['event_id'])
    return updated, previous['evidence'] != following['evidence']
