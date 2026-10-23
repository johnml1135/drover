"""Bounded durable projection and deterministic personal delivery; no external I/O."""
import json

from ..ledger import ops
from ..ledger.db import tx
from .core import _role, _preferences, effective_owner


def project(service, limit):
    if not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError('projection limit must be between 1 and 1000')
    conn = service.conn
    with tx(conn):
        position = conn.execute("SELECT event_id FROM notification_positions WHERE name='inbox'").fetchone()[0]
        events = ops.events(conn, after=position, limit=limit)
        for event in events:
            intent = conn.execute('SELECT * FROM notification_intents WHERE event_id=?', (event['id'],)).fetchone()
            if intent and not intent['projected'] and intent['role_id']:
                envelope = json.loads(intent['envelope'])
                if envelope['mode'] != 'muted' or intent['actionable']:
                    issue_key = None
                    if intent['actionable']:
                        issue_key = ops._j([envelope['task_id'], intent['event_class'],
                                           envelope['cause_id'] or envelope['reason'] or event['data'].get('question')])
                    existing = conn.execute('SELECT * FROM notification_inbox WHERE issue_key=? AND resolved=0',
                                            (issue_key,)).fetchone() if issue_key else None
                    if existing:
                        envelope['event_id'] = existing['event_id']
                        envelope['latest_event_id'] = event['id']
                        conn.execute('UPDATE notification_inbox SET envelope=?,digest_id=NULL WHERE id=?',
                                     (ops._j(envelope), existing['id']))
                    else:
                        conn.execute('INSERT OR IGNORE INTO notification_inbox(event_id,role_id,event_class,ownership_revision,'
                                     'subscription_id,subscription_revision,mode,actionable,resolved,issue_key,envelope,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                                     (intent['event_id'], intent['role_id'], intent['event_class'], intent['ownership_revision'],
                                      intent['subscription_id'], intent['subscription_revision'], envelope['mode'],
                                      intent['actionable'], intent['resolved'], issue_key, intent['envelope'], service.now()))
                    compact(service, intent['role_id'])
            if intent:
                conn.execute('UPDATE notification_intents SET projected=1 WHERE event_id=?', (event['id'],))
        if events:
            conn.execute("UPDATE notification_positions SET event_id=? WHERE name='inbox'", (events[-1]['id'],))
        return len(events)


def receive(service, role_id, limit):
    if not isinstance(limit, int) or not 0 <= limit <= 100:
        raise ValueError('receive limit must be between 0 and 100')
    conn = service.conn
    with tx(conn):
        _role(conn, role_id)
        rows = conn.execute("SELECT * FROM notification_inbox WHERE role_id=? AND acknowledged_at IS NULL "
                            "AND (mode='immediate' OR actionable=1) AND (resolved=0 OR actionable=0) ORDER BY id LIMIT ?", (role_id, limit)).fetchall()
        result = []
        for row in rows:
            envelope = json.loads(row['envelope'])
            owner = effective_owner(conn, envelope['task_id'], row['event_class'])
            if not owner or owner['role_id'] != role_id:
                continue
            envelope.update(delivery_id=f"notification:{row['id']}", subscriber=role_id,
                            ownership_revision=row['ownership_revision'])
            conn.execute('UPDATE notification_inbox SET offered_at=? WHERE id=?', (service.now(), row['id']))
            result.append(envelope)
        digests = conn.execute('SELECT * FROM notification_digests WHERE role_id=? AND acknowledged_at IS NULL ORDER BY id LIMIT ?',
                               (role_id, limit - len(result))).fetchall()
        for row in digests:
            body = json.loads(row['body'])
            # Ownership may have changed since summary construction. Historical
            # records stay intact, while a new offer respects current read scope.
            body['entries'] = [entry for entry in body['entries'] if
                               (owner := effective_owner(conn, entry['task_id'], entry['event_class'])) and
                               owner['role_id'] == role_id]
            body.update(delivery_id=f"digest:{row['id']}", digest_id=f"digest:{row['id']}")
            result.append(body)
            conn.execute('UPDATE notification_digests SET offered_at=? WHERE id=?', (service.now(), row['id']))
        return result


def coverage(conn, role_id, kind, count, first, last):
    conn.execute('INSERT INTO notification_coverage(role_id,kind,count,first_event,last_event) VALUES(?,?,?,?,?) '
                 'ON CONFLICT(role_id,kind) DO UPDATE SET count=count+excluded.count,'
                 'first_event=MIN(first_event,excluded.first_event),last_event=MAX(last_event,excluded.last_event)',
                 (role_id, kind, count, first, last))


def digest(service, role_id):
    conn = service.conn
    with tx(conn):
        role = _role(conn, role_id)
        settings = _preferences(**json.loads(role['policy']).get('notifications', {}))
        now = service.now()
        if now < role['last_digest'] + settings['digest_interval']:
            return None
        rows = conn.execute("SELECT * FROM notification_inbox WHERE role_id=? AND digest_id IS NULL "
                            "AND mode<>'muted' ORDER BY event_id", (role_id,)).fetchall()
        compacted = compaction_coverage(conn, role_id)
        unsummarized = compacted['count'] - compacted['summarized_count'] if compacted else 0
        if not rows and not unsummarized:
            return None
        counts = {}
        for row in rows:
            counts[row['event_class']] = counts.get(row['event_class'], 0) + 1
        entries = [json.loads(row['envelope']) for row in rows[-settings['detail_limit']:]]
        body = dict(schema_version=1, subscriber=role_id, created=now, counts=counts,
                    entries=entries, omitted=len(rows) - len(entries), wake=settings['digest_wake'],
                    first_event=rows[0]['event_id'] if rows else compacted['first_event'],
                    last_event=rows[-1]['event_id'] if rows else compacted['last_event'])
        if unsummarized:
            body['compacted'] = dict(count=unsummarized, first_event=compacted['first_event'],
                                     last_event=compacted['last_event'], resync_required=True)
        body['details'] = dict(after=body['first_event'] - 1, through=body['last_event'])
        while len(ops._j(body).encode()) > 32768 and body['entries']:
            body['entries'].pop(0)
            body['omitted'] += 1
        cur = conn.execute('INSERT INTO notification_digests(role_id,created,body,offered_at) VALUES(?,?,?,?)',
                           (role_id, now, ops._j(body), now))
        conn.execute("UPDATE notification_inbox SET digest_id=? WHERE role_id=? AND digest_id IS NULL AND mode<>'muted'",
                     (cur.lastrowid, role_id))
        coverage(conn, role_id, 'digest', len(rows) + unsummarized, body['first_event'], body['last_event'])
        conn.execute('UPDATE notification_compactions SET summarized_count=count WHERE role_id=?', (role_id,))
        conn.execute('UPDATE director_roles SET last_digest=? WHERE role_id=?', (now, role_id))
        body['digest_id'] = f'digest:{cur.lastrowid}'
        return body


def compact(service, role_id):
    """Bound routine delivery state, preserving source audit and issue responsibility."""
    conn = service.conn
    role = _role(conn, role_id)
    policy = json.loads(role['policy'])
    cap = policy.get('max_pending_routine', 1000)
    rows = conn.execute('SELECT id,event_id,digest_id,event_class,envelope FROM notification_inbox WHERE role_id=? AND actionable=0 '
                        'AND acknowledged_at IS NULL ORDER BY id DESC LIMIT -1 OFFSET ?', (role_id, cap)).fetchall()
    if rows:
        for row in rows:
            task_id = json.loads(row['envelope'])['task_id']
            conn.execute('INSERT INTO notification_compactions(role_id,task_id,event_class,count,first_event,last_event,summarized_count) '
                         'VALUES(?,?,?,1,?,?,?) ON CONFLICT(role_id,task_id,event_class) DO UPDATE SET '
                         'count=count+1,first_event=MIN(first_event,excluded.first_event),last_event=MAX(last_event,excluded.last_event),'
                         'summarized_count=summarized_count+excluded.summarized_count',
                         (role_id, task_id, row['event_class'], row['event_id'], row['event_id'], int(row['digest_id'] is not None)))
        conn.executemany('DELETE FROM notification_inbox WHERE id=?', [(r['id'],) for r in rows])
    actionable = conn.execute('SELECT COUNT(*) FROM notification_inbox WHERE role_id=? AND actionable=1 AND resolved=0',
                              (role_id,)).fetchone()[0]
    if actionable >= policy.get('max_pending_actionable', 10000) and not conn.execute(
            'SELECT 1 FROM notification_escalations WHERE role_id=?', (role_id,)).fetchone():
        task_id = conn.execute("SELECT json_extract(envelope,'$.task_id') FROM notification_inbox "
                               'WHERE role_id=? AND actionable=1 LIMIT 1', (role_id,)).fetchone()[0]
        # This is an owner question, not another director issue contributing to
        # the same storage pressure. The existing owner Inbox reads this table.
        question = conn.execute('INSERT INTO questions(target_kind,target_id,title,body,asked) VALUES(?,?,?,?,?)',
                                ('task', task_id, f'Notification storage needs attention: {role_id}',
                                 'Unresolved actionable notifications reached the configured storage limit. '
                                 'New work for this director is held.', ops.stamp())).lastrowid
        ops.event(conn, 'notification.owner-escalation', 'system', {'role_id': role_id, 'question': question}, task_id=task_id)
        conn.execute('INSERT INTO notification_escalations(role_id,reason,created,question_id) VALUES(?,?,?,?)',
                     (role_id, 'actionable-storage-high-water', service.now(), question))


def compaction_coverage(conn, role_id):
    row = conn.execute('SELECT SUM(count) AS count,MIN(first_event) AS first_event,MAX(last_event) AS last_event,'
                       'SUM(summarized_count) AS summarized_count FROM notification_compactions WHERE role_id=?',
                       (role_id,)).fetchone()
    return dict(row) if row and row['count'] else None


def transfer_compactions(conn, task_id, role_id):
    rows = conn.execute('SELECT * FROM notification_compactions WHERE task_id=? AND role_id<>? AND count>summarized_count',
                        (task_id, role_id)).fetchall()
    for row in rows:
        owner = effective_owner(conn, task_id, row['event_class'])
        if not owner or owner['role_id'] != role_id:
            continue
        pending = row['count'] - row['summarized_count']
        conn.execute('INSERT INTO notification_compactions(role_id,task_id,event_class,count,first_event,last_event) VALUES(?,?,?,?,?,?) '
                     'ON CONFLICT(role_id,task_id,event_class) DO UPDATE SET count=count+excluded.count,'
                     'first_event=MIN(first_event,excluded.first_event),last_event=MAX(last_event,excluded.last_event)',
                     (role_id, task_id, row['event_class'], pending, row['first_event'], row['last_event']))
        conn.execute('UPDATE notification_compactions SET count=summarized_count WHERE role_id=? AND task_id=? AND event_class=?',
                     (row['role_id'], task_id, row['event_class']))
