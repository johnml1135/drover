"""Ownership and subscription operations."""
import json

from ..ledger import ops
from ..ledger.db import tx
from .core_scope import Conflict, EVENT_CLASSES, _position, _role, classify, effective_owner, event_scope, recipient_owner, scope_task
from .core_subscriptions import _delivery_mode, _matches, _matching_subscription, _preferences, _selectors, _subscription


class OwnershipMixin:

    def _role_envelope(self, role_id):
        row = _role(self.conn, role_id)
        row['policy'] = json.loads(row['policy'])
        return row

    def assign(self, task_id, role_id, expected_revision=None, event_class='*'):
        if event_class != '*' and event_class not in EVENT_CLASSES:
            raise ValueError('unknown event class')
        with tx(self.conn):
            ops.get_task(self.conn, task_id)
            _role(self.conn, role_id)
            self.check_admission(role_id)
            old = self.conn.execute('SELECT * FROM task_ownership WHERE task_id=? AND event_class=?',
                                    (task_id, event_class)).fetchone()
            if old:
                if old['role_id'] == role_id and expected_revision in (None, old['revision']):
                    return dict(old)
                if expected_revision != old['revision']:
                    raise Conflict('ownership revision required for transfer')
            elif expected_revision not in (None, 0):
                raise Conflict('ownership does not yet exist')
            revision = old['revision'] + 1 if old else 1
            self.conn.execute('INSERT INTO task_ownership(task_id,event_class,role_id,revision) VALUES(?,?,?,?) '
                              'ON CONFLICT(task_id,event_class) DO UPDATE SET role_id=excluded.role_id,revision=excluded.revision',
                              (task_id, event_class, role_id, revision))
            selectors = {'tasks': [task_id]}
            if event_class != '*':
                selectors['events'] = [event_class]
            self.follow(role_id, selectors)
            # Every pending event follows its current effective partition. Processed
            # history is immutable, even when its original owner no longer owns work.
            intents = self.conn.execute('SELECT i.* FROM notification_intents i '
                                        "WHERE json_extract(i.envelope,'$.task_id')=? AND NOT EXISTS ("
                                        'SELECT 1 FROM notification_inbox n WHERE n.event_id=i.event_id '
                                        'AND n.acknowledged_at IS NOT NULL)', (task_id,)).fetchall()
            for intent in intents:
                owner = effective_owner(self.conn, task_id, intent['event_class'])
                if owner is None or owner['role_id'] != role_id:
                    continue
                if intent['role_id'] is None:
                    # First responsibility resolves real recipient preferences;
                    # the unassigned placeholder mute was never a user choice.
                    source = dict(self.conn.execute('SELECT * FROM task_events WHERE id=?', (intent['event_id'],)).fetchone())
                    source['data'] = json.loads(source['data'])
                    task = scope_task(self.conn, ops.get_task(self.conn, task_id))
                    task['_joined'] = bool(self.conn.execute(
                        'SELECT 1 FROM task_group_members m JOIN task_groups g USING(group_id) WHERE m.child_id=? AND g.role_id=?',
                        (task_id, role_id)).fetchone())
                    sub = _matching_subscription(self.conn, role_id, task, source)
                    preferences = sub['preferences'] if sub else _preferences(mode='muted')
                    envelope = json.loads(intent['envelope'])
                    envelope.update(mode=_delivery_mode(task, intent['event_class'], preferences),
                                    subscription_id=sub['id'] if sub else None,
                                    subscription_revision=sub['revision'] if sub else None)
                    self.conn.execute('UPDATE notification_intents SET preferences=?,envelope=?,subscription_id=?,'
                                      'subscription_revision=?,projected=0 WHERE event_id=?',
                                      (ops._j(preferences), ops._j(envelope), envelope['subscription_id'],
                                       envelope['subscription_revision'], intent['event_id']))
                    self.conn.execute('UPDATE notification_inbox SET envelope=?,mode=?,subscription_id=?,subscription_revision=? '
                                      'WHERE event_id=? AND acknowledged_at IS NULL',
                                      (ops._j(envelope), envelope['mode'], envelope['subscription_id'],
                                       envelope['subscription_revision'], intent['event_id']))
                    self.conn.execute("UPDATE notification_positions SET event_id=MIN(event_id,?) WHERE name='inbox'",
                                      (intent['event_id'] - 1,))
                self.conn.execute('UPDATE notification_intents SET role_id=?,ownership_revision=? WHERE event_id=?',
                                  (role_id, owner['revision'], intent['event_id']))
                self.conn.execute('UPDATE notification_inbox SET role_id=?,ownership_revision=?,offered_at=NULL,digest_id=NULL '
                                  'WHERE event_id=? AND acknowledged_at IS NULL',
                                  (role_id, owner['revision'], intent['event_id']))
            from .delivery import transfer_compactions, compact
            transfer_compactions(self.conn, task_id, role_id)
            for affected_role in {role_id} | {intent['role_id'] for intent in intents if intent['role_id']}:
                compact(self, affected_role)
                from .roles_wake import refresh_wake
                refresh_wake(self, affected_role)
            ops.event(self.conn, 'notification.ownership-assigned', 'system',
                      {'from': old['role_id'] if old else None, 'to': role_id,
                       'event_class': event_class, 'revision': revision}, task_id=task_id)
            return dict(task_id=task_id, role_id=role_id, event_class=event_class, revision=revision)

    def _validate_scope(self, role_id, selectors):
        for group_id in selectors.get('groups', []):
            group = self.get_group(group_id)
            if group['role_id'] != role_id:
                raise Conflict('following does not grant another director group authority')
        for parent_id in selectors.get('descendants', []):
            ops.get_task(self.conn, parent_id)
            if not any((owner := effective_owner(self.conn, parent_id, cls)) and owner['role_id'] == role_id
                       for cls in selectors.get('events', EVENT_CLASSES)):
                raise Conflict('descendant scope requires owned parent authority')
        for task_id in selectors.get('tasks', []):
            ops.get_task(self.conn, task_id)
            classes = selectors.get('events', EVENT_CLASSES)
            if not any((owner := effective_owner(self.conn, task_id, cls)) and owner['role_id'] == role_id
                       for cls in classes):
                raise Conflict('following does not grant ownership of another director partition')

    def follow(self, role_id, selectors, **preferences):
        selectors = _selectors(selectors)
        replay_after = preferences.pop('replay_after', None)
        if replay_after is not None and (not isinstance(replay_after, int) or replay_after < 0):
            raise ValueError('replay_after must be a nonnegative event position')
        with tx(self.conn):
            role = _role(self.conn, role_id)
            resolved = _preferences(**{**json.loads(role['policy']).get('notifications', {}), **preferences})
            self._validate_scope(role_id, selectors)
            for row in self.conn.execute('SELECT id FROM subscriptions WHERE role_id=?', (role_id,)):
                existing = _subscription(self.conn, row['id'])
                if existing['selectors'] == selectors and existing['preferences'] == resolved:
                    return self._follow_result(existing['id'], replay_after)
            sub_id = ops._next_id(self.conn, 'S')
            after = _position(self.conn)
            self.conn.execute('INSERT INTO subscriptions(id,role_id,revision,created_position) VALUES(?,?,1,?)',
                              (sub_id, role_id, after))
            self.conn.execute('INSERT INTO subscription_versions VALUES(?,1,?,?,?)',
                              (sub_id, after, ops._j(selectors), ops._j(resolved)))
            self._digest_settings(role_id, preferences)
            return self._follow_result(sub_id, replay_after)

    def _subscription_envelope(self, sub_id):
        sub = _subscription(self.conn, sub_id)
        sub['next_digest_at'] = _role(self.conn, sub['role_id'])['last_digest'] + sub['preferences']['digest_interval']
        sub['preview'] = []
        for row in self.conn.execute('SELECT * FROM task_events ORDER BY id DESC LIMIT 50'):
            event = ops._jrow(row, 'data')
            target = self.conn.execute('SELECT envelope FROM notification_targets WHERE event_id=? AND role_id=?',
                                       (event['id'], sub['role_id'])).fetchone()
            if target and recipient_owner(self.conn, json.loads(target['envelope'])):
                sub['preview'].append(dict(event_id=event['id'], event_class=json.loads(target['envelope'])['event_class']))
                continue
            event_class = classify(self.conn, event)
            task = event_scope(self.conn, event)
            if event_class is None or task is None:
                continue
            envelope = dict(task_id=event['task_id'], group_id=event['data'].get('group_id'), event_class=event_class)
            owner = recipient_owner(self.conn, envelope)
            if owner and owner['role_id'] == sub['role_id'] and _matches(
                    task, event_class, event['data'].get('reason'), sub['selectors']):
                sub['preview'].append(dict(event_id=event['id'], event_class=event_class))
        sub['preview'].reverse()
        return sub

    def tune(self, subscription_id, expected_revision, **preferences):
        selectors = preferences.pop('selectors', None)
        with tx(self.conn):
            sub = _subscription(self.conn, subscription_id)
            resolved = _preferences(**{**sub['preferences'], **preferences})
            selected = _selectors(selectors) if selectors is not None else sub['selectors']
            if resolved == sub['preferences'] and selected == sub['selectors']:
                return self._subscription_envelope(subscription_id)
            if expected_revision != sub['revision']:
                raise Conflict('subscription revision changed')
            self._validate_scope(sub['role_id'], selected)
            revision = sub['revision'] + 1
            self.conn.execute('INSERT INTO subscription_versions VALUES(?,?,?,?,?)',
                              (subscription_id, revision, _position(self.conn), ops._j(selected), ops._j(resolved)))
            self.conn.execute('UPDATE subscriptions SET revision=? WHERE id=?', (revision, subscription_id))
            self._digest_settings(sub['role_id'], preferences)
            return self._subscription_envelope(subscription_id)

    def health(self, role_id=None):
        if role_id is None:
            return dict(controls=self.control_state(), roles=[self.health(row['role_id']) for row in self.conn.execute(
                'SELECT role_id FROM director_roles ORDER BY role_id')],
                unassigned=self.conn.execute('SELECT COUNT(*) FROM notification_intents WHERE role_id IS NULL').fetchone()[0],
                projection_position=self.conn.execute("SELECT event_id FROM notification_positions WHERE name='inbox'").fetchone()[0])
        role = self._role_envelope(role_id)
        role['runtime'] = self.role_state(role_id)
        role['controls'] = self.control_state(role_id)
        role['admission'] = self.admission(role_id=role_id)
        role['groups'] = [self.get_group(r[0]) for r in self.conn.execute('SELECT group_id FROM task_groups WHERE role_id=? ORDER BY group_id', (role_id,))]
        role['subscriptions'] = [self._subscription_envelope(row['id']) for row in self.conn.execute(
            'SELECT id FROM subscriptions WHERE role_id=? ORDER BY rowid', (role_id,))]
        role['coverage'] = {row['kind']: dict(row) for row in self.conn.execute(
            'SELECT * FROM notification_coverage WHERE role_id=?', (role_id,))}
        role['pending_routine'] = self.conn.execute('SELECT COUNT(*) FROM notification_inbox WHERE role_id=? AND acknowledged_at IS NULL AND actionable=0', (role_id,)).fetchone()[0]
        role['pending_actionable'] = self.conn.execute('SELECT COUNT(*) FROM notification_inbox WHERE role_id=? AND resolved=0 AND actionable=1', (role_id,)).fetchone()[0]
        role['acknowledged'] = self.conn.execute('SELECT COUNT(*) FROM notification_inbox WHERE role_id=? AND acknowledged_at IS NOT NULL', (role_id,)).fetchone()[0]
        from .delivery import compaction_coverage
        compacted = compaction_coverage(self.conn, role_id)
        if compacted:
            role['coverage']['compacted'] = compacted
        role['resync_required'] = compacted is not None or 'digest-compacted' in role['coverage']
        role['admission_held'] = self.conn.execute('SELECT 1 FROM notification_escalations WHERE role_id=?', (role_id,)).fetchone() is not None
        role['oldest_actionable_age'] = self.conn.execute('SELECT MAX(?-created) FROM notification_inbox WHERE role_id=? AND actionable=1 AND resolved=0', (self.now(), role_id)).fetchone()[0]
        role['pending_digests'] = self.conn.execute('SELECT COUNT(*) FROM notification_digests WHERE role_id=? AND acknowledged_at IS NULL', (role_id,)).fetchone()[0]
        role['digests'] = self.conn.execute('SELECT COUNT(*) FROM notification_digests WHERE role_id=?', (role_id,)).fetchone()[0]
        role['pending'] = self.conn.execute('SELECT COUNT(*) FROM notification_inbox WHERE role_id=? AND acknowledged_at IS NULL',
                                           (role_id,)).fetchone()[0]
        return role

    def _digest_settings(self, role_id, preferences):
        role = _role(self.conn, role_id)
        policy = json.loads(role['policy'])
        settings = policy.get('notifications', {})
        settings.update({key: preferences[key] for key in ('digest_interval', 'digest_wake', 'detail_limit') if key in preferences})
        policy['notifications'] = settings
        self.conn.execute('UPDATE director_roles SET policy=? WHERE role_id=?', (ops._j(policy), role_id))
