"""Replay and owner attention operations."""
import json

from ..ledger.db import tx
from .core_scope import _position, _role, recipient_owner
from .core_evidence import bound_digest, bound_envelope, issue_key


class ReplayMixin:

    def replay(self, role_id, after=None, limit=50):
        """Bounded durable replay plus pending offers, using retained recipients.

        Cursors contain source and digest positions, never in-memory bus IDs.
        Pending offers intentionally repeat until acknowledged, including source
        facts older than the cursor that arrive by transfer or late projection.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError('replay limit must be between 1 and 100')
        event_after, digest_after = 0, 0
        if after is not None:
            parts = after.split(':')
            if len(parts) != 3 or parts[0] != 'v1' or any(not p.isascii() or not p.isdigit() for p in parts[1:]):
                raise ValueError('invalid replay cursor')
            event_after, digest_after = map(int, parts[1:])
        from .delivery import compaction_coverage
        with tx(self.conn):
            _role(self.conn, role_id)
            self.project(1000)
            compacted = compaction_coverage(self.conn, role_id)
            digest_compacted = self.conn.execute("SELECT 1 FROM notification_coverage WHERE role_id=? AND kind='digest-compacted'", (role_id,)).fetchone()
            if after is not None and ((compacted and event_after < compacted['last_event']) or
                    (digest_compacted and digest_after < self.conn.execute('SELECT COALESCE(MIN(id),0) FROM notification_digests WHERE role_id=?', (role_id,)).fetchone()[0])):
                high = self.conn.execute('SELECT COALESCE(MAX(id),0) FROM notification_digests WHERE role_id=?', (role_id,)).fetchone()[0]
                return dict(items=[], cursor=f'v1:{_position(self.conn)}:{high}', more=False,
                            resync_required=True, snapshot=self.scoped_snapshot(role_id), health=self.health(role_id))
            # Filter current recipients before LIMIT so stale/superseded rows do
            # not hide later authorized history. Keep the shared Python authority
            # recheck below. Source order prevents a pending reoffer from jumping
            # the durable historical frontier past an older retained delivery.
            rows = self.conn.execute("""
                SELECT n.* FROM notification_inbox n WHERE n.role_id=?
                AND (n.mode='immediate' OR n.actionable=1) AND (n.resolved=0 OR n.actionable=0)
                AND (n.acknowledged_at IS NULL OR COALESCE(json_extract(n.envelope,'$.latest_event_id'),n.event_id)>?)
                AND CASE
                  WHEN json_extract(n.envelope,'$.notice_id') IS NOT NULL THEN EXISTS (
                    SELECT 1 FROM notification_targets t JOIN notification_notices b USING(event_id)
                    LEFT JOIN notification_controls c ON c.control_id=b.control_id
                    WHERE t.event_id=json_extract(n.envelope,'$.event_id') AND t.role_id=n.role_id
                    AND t.role_id=json_extract(n.envelope,'$.target_role')
                    AND (b.control_id IS NULL OR (t.superseded=0 AND c.active=1)))
                  WHEN json_extract(n.envelope,'$.group_id') IS NOT NULL
                       AND json_extract(n.envelope,'$.task_id') IS NULL THEN EXISTS (
                    SELECT 1 FROM task_groups g WHERE g.group_id=json_extract(n.envelope,'$.group_id')
                    AND g.role_id=n.role_id)
                  ELSE (SELECT o.role_id FROM task_ownership o
                    WHERE o.task_id=json_extract(n.envelope,'$.task_id')
                    AND o.event_class IN (json_extract(n.envelope,'$.event_class'),'*')
                    ORDER BY (o.event_class='*') LIMIT 1)=n.role_id
                END
                ORDER BY COALESCE(json_extract(n.envelope,'$.latest_event_id'),n.event_id),n.id LIMIT ?
                """, (role_id, event_after, limit + 1)).fetchall()
            items = []
            for row in rows:
                message = json.loads(row['envelope'])
                owner = recipient_owner(self.conn, message)
                if not owner or owner['role_id'] != role_id:
                    continue
                message.update(delivery_id=f"notification:{row['id']}", subscriber=role_id,
                    ownership_revision=row['ownership_revision'], message_version=message.get('latest_event_id', row['event_id']))
                items.append(message)
            remaining = max(0, limit + 1 - len(items))
            for row in self.conn.execute('SELECT * FROM notification_digests WHERE role_id=? AND '
                    '(acknowledged_at IS NULL OR id>?) ORDER BY id LIMIT ?',
                    (role_id, digest_after, remaining)):
                message = json.loads(row['body'])
                message['entries'] = [entry for entry in message['entries'] if
                    (owner := recipient_owner(self.conn, entry)) and owner['role_id'] == role_id]
                message.update(delivery_id=f"digest:{row['id']}", digest_id=f"digest:{row['id']}", message_version=row['id'])
                items.append(message)
            more = len(items) > limit
            result = []
            for message in items[:limit]:
                if message['delivery_id'].startswith('digest:'):
                    digest_after = max(digest_after, message['message_version'])
                    message = bound_digest(message)
                else:
                    event_after = max(event_after, message['message_version'])
                    message = bound_envelope(message)
                result.append(dict(cursor=f'v1:{event_after}:{digest_after}', message=message))
            return dict(items=result, cursor=f'v1:{event_after}:{digest_after}', more=more, resync_required=False)

    def owner_attention(self):
        """Current unassigned issues that need a fallback in the owner Inbox.

        Questions and needs-owner transitions already have actionable owner
        cards. Their immutable routing facts must not create duplicate cards or
        reappear after the question or task has moved on.
        """
        out, seen = [], set()
        rows = self.conn.execute("""
            SELECT n.envelope FROM notification_intents n
            JOIN tasks t ON t.id=json_extract(n.envelope,'$.task_id') AND t.archived=0
            JOIN task_events e ON e.id=n.event_id
            WHERE n.role_id IS NULL AND n.actionable=1 AND n.resolved=0
              AND e.kind<>'question-asked'
              AND NOT (e.kind='status' AND json_extract(e.data,'$.to')='needs-owner')
            ORDER BY n.event_id DESC LIMIT 200
            """)
        for row in rows:
            envelope = json.loads(row['envelope'])
            key = issue_key(self.conn, envelope)
            if key in seen or recipient_owner(self.conn, envelope):
                continue
            seen.add(key)
            out.append(dict(key=key, target_kind='task', target_id=envelope['task_id'],
                title='Unassigned notification: ' + envelope['event_class'], body=envelope.get('reason') or 'Assign an owning director to handle this issue.'))
        return out

    def task_groups(self, task_id):
        return [self.get_group(row[0]) for row in self.conn.execute('SELECT group_id FROM task_groups WHERE parent_id=? ORDER BY group_id', (task_id,))]
