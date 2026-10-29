"""Durable roles, exclusive ownership and event-time subscription decisions."""
import hashlib
import json
import time
from datetime import datetime

from ..ledger import ops
from ..ledger.db import tx
from .core_scope import (Conflict, EVENT_CLASSES, PROFILE_VERSION, classify, _position, _role,
                         effective_owner, recipient_owner, scope_task, event_scope)
from .core_subscriptions import (SCOPE_KEYS, FILTER_KEYS, _selectors, _scope_values, _matches,
                                 _preferences, _subscription, _specificity, _matching_subscription,
                                 _mode, _delivery_mode)
from .core_evidence import (serialized_size, _text_excerpt, _cause_identity, _bounded_evidence,
                            bound_envelope, bound_digest, issue_key, coalesce_issue)
from .core_ownership import OwnershipMixin
from .core_replay import ReplayMixin


LANDING_OWNER_WAKE_S = 30 * 60
LANDING_OWNER_WAKES = 'landing_owner_wakes'


def _resolve_issue(conn, event, event_class):
    if event_class == 'landed':
        matches = ("actionable=1 AND json_extract(envelope,'$.task_id')=? "
                   "AND json_extract(envelope,'$.evidence.to')='needs-owner'")
        parameters = (event['task_id'],)
    else:
        if event['kind'] not in ('progress.recovered', 'recovery.resolved', 'question-answered'):
            return
        cause, question = _cause_identity(event['data'].get('cause')), event['data'].get('question')
        if cause is None and question is None:
            return
        # Resolve using the immutable source, independently of display excerpts.
        source_question = ("(SELECT json_extract(e.data,'$.question') FROM task_events e WHERE "
                           "e.id=COALESCE(json_extract(envelope,'$.latest_event_id'),json_extract(envelope,'$.event_id')))")
        matches = ("actionable=1 AND json_extract(envelope,'$.task_id') IS ? "
                   "AND ((? IS NOT NULL AND json_extract(envelope,'$.cause_id')=?) OR "
                   f"(? IS NOT NULL AND {source_question}=?))")
        parameters = (event['task_id'], cause, cause, question, question)
    affected_roles = set()
    for table in ('notification_intents', 'notification_inbox'):
        affected_roles.update(row['role_id'] for row in conn.execute(
            f"SELECT DISTINCT role_id FROM {table} WHERE resolved=0 AND {matches}",
            parameters) if row['role_id'])
        conn.execute(f"UPDATE {table} SET resolved=1 WHERE {matches}", parameters)
    for role in conn.execute('SELECT role_id FROM notification_escalations').fetchall():
        reconcile_storage_hold(conn, role['role_id'])
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='director_runtime'").fetchone():
        from .roles import refresh_wake
        for role_id in affected_roles:
            refresh_wake(Notifications(conn), role_id)


def reconcile_storage_hold(conn, role_id):
    """Release pressure and its owner escalation when current issues leave scope."""
    role = _role(conn, role_id)
    count = conn.execute("SELECT COUNT(*) FROM notification_inbox WHERE role_id=? AND ((actionable=1 AND resolved=0) "
                         "OR (acknowledged_at IS NULL AND mode='immediate' AND json_extract(envelope,'$.notice_id') IS NOT NULL))",
                         (role_id,)).fetchone()[0]
    if count < json.loads(role['policy']).get('max_pending_actionable', 10000):
        escalation = conn.execute('SELECT question_id FROM notification_escalations WHERE role_id=?', (role_id,)).fetchone()
        if escalation:
            conn.execute("UPDATE questions SET status='answered',answer_text=?,answered_at=? WHERE id=? AND status='open'",
                         ('Notification storage pressure cleared.', ops.stamp(), escalation['question_id']))
            conn.execute('DELETE FROM notification_escalations WHERE role_id=?', (role_id,))
            ops.event(conn, 'notification.owner-escalation-cleared', 'system',
                      {'role_id': role_id, 'question': escalation['question_id']})
    return count


def capture_event(conn, event, *, subscription=None, replay=False):
    """Called inside the source event transaction, including nested waiting events."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='notification_intents'").fetchone():
        return  # Waiting's historical migrations precede V9.
    event_class = classify(conn, event)
    if event_class is None:
        return
    group_id = event['data'].get('group_id') if event['task_id'] is None else None
    if group_id:
        group = conn.execute('SELECT role_id FROM task_groups WHERE group_id=?', (group_id,)).fetchone()
        if not group:
            return
        task = dict(id=None, kind='research', epic_id=None, repo=None, _groups=[group_id])
    elif event['task_id'] is not None:
        task = scope_task(conn, ops.get_task(conn, event['task_id']))
    else:
        return
    if not replay:
        _resolve_issue(conn, event, event_class)
    ownership = dict(role_id=group['role_id'], revision=1) if group_id else effective_owner(conn, task['id'], event_class)
    role_id = ownership['role_id'] if ownership else None
    task['_joined'] = bool(role_id and task['id'] and conn.execute(
        'SELECT 1 FROM task_group_members m JOIN task_groups g USING(group_id) WHERE m.child_id=? AND g.role_id=?',
        (task['id'], role_id)).fetchone())
    sub = subscription or _matching_subscription(conn, role_id, task, event)
    preferences = sub['preferences'] if sub else _preferences(mode='muted')
    mode = _delivery_mode(task, event_class, preferences)
    severity = 'warning' if event_class in ('decision-needed', 'issue', 'role-unavailable') else 'info'
    envelope = dict(schema_version=1, event_id=event['id'], event_class=event_class,
                    task_id=task['id'], group_id=group_id,
                    cause_id=_cause_identity(event['data'].get('cause')),
                    at=_text_excerpt(event['at'], 64), actor=_text_excerpt(event['actor'], 256), severity=severity,
                    reason=_text_excerpt(event['data']['reason'], 512) if event['data'].get('reason') is not None else None,
                    expected_revision=event['id'], evidence=_bounded_evidence(event['data'], event['id']), profile_version=PROFILE_VERSION, mode=mode,
                    subscription_id=sub['id'] if sub else None, subscription_revision=sub['revision'] if sub else None)
    bound_envelope(envelope)
    actionable = event_class in ('decision-needed', 'issue', 'role-unavailable')
    conflict = (' ON CONFLICT(event_id) DO UPDATE SET role_id=excluded.role_id,ownership_revision=excluded.ownership_revision,'
                'subscription_id=excluded.subscription_id,subscription_revision=excluded.subscription_revision,'
                'preferences=excluded.preferences,envelope=excluded.envelope,projected=0') if replay else ''
    conn.execute('INSERT INTO notification_intents(event_id,schema_version,event_class,role_id,ownership_revision,'
                 'subscription_id,subscription_revision,preferences,envelope,actionable) VALUES(?,1,?,?,?,?,?,?,?,?)' + conflict,
                 (event['id'], event_class, role_id, ownership['revision'] if ownership else None,
                  sub['id'] if sub else None, sub['revision'] if sub else None,
                  ops._j(preferences), ops._j(envelope), actionable))
    if not replay and role_id and mode == 'immediate':
        from .roles import signal
        # Identical observations of one unresolved episode do not create wakes.
        prior = None
        if actionable:
            episode = issue_key(conn, envelope)
            for row in conn.execute('SELECT envelope FROM notification_intents WHERE event_id<? AND role_id=? '
                                     "AND event_class=? AND json_extract(envelope,'$.task_id') IS ? "
                                     'AND resolved=0 ORDER BY event_id DESC', (event['id'], role_id, event_class, task['id'])):
                candidate = json.loads(row['envelope'])
                if issue_key(conn, candidate) == episode:
                    prior = candidate
                    break
        if prior is None or prior['evidence'] != envelope['evidence']:
            signal(conn, role_id, event['id'])
    group_following_child = bool(sub and sub['selectors'].get('groups') and not group_id and
                                 task['id'] not in sub['selectors'].get('tasks', []))
    if not replay and sub and not group_following_child and preferences['stop_on_completion'] and (event_class in ('landed', 'group-ready') or
            (event_class == 'result-accepted' and task['kind'] in ('research', 'investigation')) or
            (event['kind'] == 'status' and event['data'].get('to') == 'cancelled')):
        Notifications(conn).tune(sub['id'], sub['revision'], enabled=False)


def forget_task(conn, task_id):
    """Owner deletion revokes future offers while preserving existing audit facts."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='notification_intents'").fetchone():
        return
    affected_roles = [row['role_id'] for row in conn.execute(
        "SELECT DISTINCT role_id FROM notification_inbox WHERE json_extract(envelope,'$.task_id')=?", (task_id,))]
    for table in ('notification_intents', 'notification_inbox'):
        conn.execute(f"UPDATE {table} SET resolved=1,envelope=json_set(envelope,'$.mode','muted') "
                     "WHERE json_extract(envelope,'$.task_id')=?", (task_id,))
    conn.execute("UPDATE notification_inbox SET mode='muted' WHERE json_extract(envelope,'$.task_id')=?", (task_id,))
    conn.execute('UPDATE notification_compactions SET count=summarized_count WHERE task_id=?', (task_id,))
    for role_id in affected_roles:
        reconcile_storage_hold(conn, role_id)
        from .roles import refresh_wake
        refresh_wake(Notifications(conn), role_id)


class Notifications(OwnershipMixin, ReplayMixin):
    """One small trusted service shared by delivery, supervision and HTTP adapters."""
    def __init__(self, conn, now=None):
        self.conn = conn
        self.now = now or time.time

    def _landing_questions(self):
        """Only current landing decisions with surviving consumers and no owner action.

        Waiting owns reconciliation/supersession. Use its current question view,
        rather than session state or an old question-asked notification intent.
        """
        from ..ledger import states
        from ..ledger.waiting import OPEN, Waiting
        waiting = Waiting(self.conn)
        landing_ids = {r[0] for r in self.conn.execute(
            "SELECT json_extract(data,'$.question') FROM task_events WHERE kind='question-asked' AND actor='landing'")}
        landing_ids.update(r['question_id'] for r in waiting.pending() if r['subject_kind'] == 'merge_queue')
        out = []
        for question in waiting.questions():
            if question['id'] not in landing_ids or not question['visible'] or question['proposal']:
                continue
            if not any(kind != 'task' or (
                    (task := ops.get_task(self.conn, key))['status'] not in states.TERMINAL and not task['archived'])
                       for kind, key in question['subjects']):
                continue
            if question['cause'] and waiting.state(question['cause']) != OPEN:
                continue
            # Keep holding is an owner action even though Waiting reopens the
            # question. Consult its answer history without reading Waiting tables.
            if self.conn.execute("SELECT 1 FROM task_events WHERE kind IN ('question-answered','question-superseded') "
                                 "AND json_extract(data,'$.question')=? LIMIT 1", (question['id'],)).fetchone():
                continue
            out.append(question)
        return out

    def wake_landing_questions(self):
        """Publish one retractable owner notice per unanswered landing question.

        The deadline is derived from the ledger's asked time, including questions
        first observed after a Hub outage. Deadline, notice and woken marker commit
        together; neither a session replacement nor a Hub restart resets them.
        """
        with tx(self.conn):
            saved = ops.get_setting(self.conn, LANDING_OWNER_WAKES, '{}')
            state = json.loads(saved)
            for entry in state.values():
                entry['active'] = False
            raised, now = [], self.now()
            for question in self._landing_questions():
                qid = question['id']
                if str(qid) not in state:
                    state[str(qid)] = dict(
                        deadline=datetime.fromisoformat(question['asked']).timestamp() + LANDING_OWNER_WAKE_S,
                        woken_at=None)
                entry = state[str(qid)]
                entry['active'] = True
                if entry['woken_at'] is not None or now < entry['deadline']:
                    continue
                option = next((o for o in question['options'] if o.get('recommended')), None)
                option = option or next(iter(question['options']), None)
                action = (f'Open Needs me and answer question #{qid}' +
                          (f' by choosing "{option["label"]}".' if option else ' with your decision.'))
                notice = dict(key=f'landing-question:{qid}', question=qid,
                              target_kind=question['target_kind'], target_id=question['target_id'],
                              title='Landing question still needs an answer: ' + question['title'],
                              body=f'Question #{qid} has waited 30 minutes without owner action. {action}')
                event_id = ops.event(self.conn, 'notification.landing-owner-wake', 'hub',
                                     dict(notice=notice, deadline=entry['deadline']))
                entry.update(woken_at=now, notice=notice, event_id=event_id)
                raised.append(notice)
            encoded = ops._j(state)
            if encoded != saved:
                ops.set_setting(self.conn, LANDING_OWNER_WAKES, encoded)
            return raised

    def owner_attention(self):
        out = super().owner_attention()
        state = json.loads(ops.get_setting(self.conn, LANDING_OWNER_WAKES, '{}'))
        # Recheck current truth on reads too: an answer retracts the notice even
        # before the next monitor tick. Retain the marker to prevent a second wake.
        current = {str(q['id']) for q in self._landing_questions()} if state else set()
        out.extend(entry['notice'] for qid, entry in state.items()
                   if qid in current and entry.get('active') and entry['woken_at'] is not None)
        return out

    def register(self, role_id, **policy):
        if not isinstance(role_id, str) or not role_id.strip() or len(role_id) > 256:
            raise ValueError('role_id must be nonempty')
        _preferences(**policy.get('notifications', {}))
        from .roles import validate_policy
        validate_policy(policy)
        for key, default in (('max_pending_routine', 1000), ('max_pending_actionable', 10000)):
            if not isinstance(policy.get(key, default), int) or not 1 <= policy.get(key, default) <= 100000:
                raise ValueError(f'{key} must be between 1 and 100000')
        with tx(self.conn):
            from .roles import _artifact
            saved_policy = _artifact(self.conn, policy)
            self.conn.execute('INSERT INTO director_roles(role_id,policy,created,last_digest) VALUES(?,?,?,?) '
                              'ON CONFLICT(role_id) DO NOTHING',
                              (role_id, saved_policy, self.now(), self.now()))
            from .controls import apply_persistent
            apply_persistent(self, role_id)
            return self._role_envelope(role_id)

    def issue_session(self, role_id, session_id, ttl=3600, capabilities=None):
        from .roles import issue_session
        return issue_session(self, role_id, session_id, ttl, capabilities)

    def authenticate(self, credential):
        from .roles import authenticate
        return authenticate(self, credential)

    def acquire(self, role_id, session_id, ttl=60):
        from .roles import acquire
        return acquire(self, role_id, session_id, ttl)

    def commit(self, role_id, lease_token, checkpoint, delivery_ids, actions, request_key, release=False, delivery_versions=None):
        from .roles import commit
        return commit(self, role_id, lease_token, checkpoint, delivery_ids, actions, request_key, release, delivery_versions)

    def role_state(self, role_id):
        from .roles import role_state
        return role_state(self, role_id)

    def get_checkpoint(self, role_id, revision=None):
        from .roles import get_checkpoint
        return get_checkpoint(self, role_id, revision)

    def claim_issue(self, role_id, issue_id, lease_token, **policy):
        from .roles import claim_issue
        return claim_issue(self, role_id, issue_id, lease_token, **policy)

    def hold_issue(self, role_id, issue_id, lease_token, reason):
        from .roles import hold_issue
        return hold_issue(self, role_id, issue_id, lease_token, reason)

    def reserve_wake(self, role_id, request_key, tokens=0, model=None):
        from .roles import reserve_wake
        return reserve_wake(self, role_id, request_key, tokens, model)

    def replace(self, role_id, expected_generation, *, confirmed_dead=False, reason='consumer-confirmed-gone'):
        from .roles import replace
        return replace(self, role_id, expected_generation, confirmed_dead=confirmed_dead, reason=reason)

    def create_group(self, group_id, role_id, parent_id=None, mode='synthesis'):
        from .groups import create_group
        return create_group(self, group_id, role_id, parent_id, mode)

    def get_group(self, group_id):
        from .groups import get_group
        return get_group(self, group_id)

    def enroll(self, group_id, child_id, expected_revision, request_key):
        from .groups import enroll
        return enroll(self, group_id, child_id, expected_revision, request_key)

    def seal(self, group_id, expected_revision, request_key):
        from .groups import seal
        return seal(self, group_id, expected_revision, request_key)

    def broadcast(self, issuer, targets, body, urgency='summary', control=None, request_key=None):
        from .controls import broadcast
        return broadcast(self, issuer, targets, body, urgency, control, request_key)

    def stop(self, issuer, request_key):
        from .controls import stop
        return stop(self, issuer, request_key)

    def resume(self, issuer, expected_epoch, request_key):
        from .controls import resume
        return resume(self, issuer, expected_epoch, request_key)

    def require_skill(self, issuer, skill, digest, targets, request_key):
        from .controls import require_skill
        return require_skill(self, issuer, skill, digest, targets, request_key)

    def ack_control(self, role_id, lease_token, control_id, digest=None):
        from .controls import ack_control
        return ack_control(self, role_id, lease_token, control_id, digest)

    def admission(self, role_id=None, generation=None, epoch=None):
        from .controls import admission
        return admission(self.conn, role_id, generation, epoch)

    def control_state(self, role_id=None):
        from .controls import control_state
        return control_state(self, role_id)


    def project(self, limit=100):
        from .delivery import project
        return project(self, limit)

    def receive(self, role_id, limit=50):
        from .delivery import receive
        return receive(self, role_id, limit)


    def digest(self, role_id):
        from .delivery import digest
        return digest(self, role_id)

    def check_admission(self, role_id):
        from .controls import admission
        result = admission(self.conn, role_id=role_id)
        if not result['allowed']:
            raise Conflict(result['reason'])
        if self.conn.execute('SELECT 1 FROM notification_escalations WHERE role_id=?', (role_id,)).fetchone():
            raise Conflict('notification storage high-water mark holds admission for this director')

    def _follow_result(self, sub_id, replay_after):
        sub = _subscription(self.conn, sub_id)
        if replay_after is None:
            return self._subscription_envelope(sub_id)
        from .delivery import compaction_coverage
        compacted = compaction_coverage(self.conn, sub['role_id'])
        if compacted and replay_after < compacted['last_event']:
            result = self._subscription_envelope(sub_id)
            result.update(resync_required=True, snapshot=self.scoped_snapshot(sub['role_id']))
            return result
        events = ops.events(self.conn, after=replay_after, limit=1000)
        first = None
        for event in events:
            target = self.conn.execute('SELECT * FROM notification_targets WHERE event_id=? AND role_id=?',
                                       (event['id'], sub['role_id'])).fetchone()
            if target:
                if not target['projected']:
                    first = event['id'] if first is None else first
                continue  # Explicit targeting retains its original delivery preferences.
            event_class = classify(self.conn, event)
            task = event_scope(self.conn, event)
            if not event_class or task is None:
                continue
            envelope = dict(task_id=event['task_id'], group_id=event['data'].get('group_id'), event_class=event_class)
            owner = recipient_owner(self.conn, envelope)
            if not owner or owner['role_id'] != sub['role_id']:
                continue
            if not _matches(task, event_class, event['data'].get('reason'), sub['selectors']):
                continue
            if self.conn.execute('SELECT 1 FROM notification_inbox WHERE event_id=? AND role_id=?', (event['id'], sub['role_id'])).fetchone():
                continue
            capture_event(self.conn, event, subscription=sub, replay=True)
            first = event['id'] if first is None else first
        if first is not None:
            self.conn.execute("UPDATE notification_positions SET event_id=MIN(event_id,?) WHERE name='inbox'", (first - 1,))
        result = self._subscription_envelope(sub_id)
        result['replay_through'] = events[-1]['id'] if events else replay_after
        result['replay_more'] = len(events) == 1000
        return result

    def scoped_snapshot(self, role_id):
        _role(self.conn, role_id)
        return [dict(task_id=row['task_id'], event_class=row['event_class'], revision=row['revision'],
                     status=ops.get_task(self.conn, row['task_id'])['status']) for row in self.conn.execute(
                         'SELECT * FROM task_ownership WHERE role_id=? ORDER BY task_id,event_class', (role_id,))]

    def authorize_task(self, role_id, task_id):
        """Transport ownership check; group references confer no task authority."""
        ops.get_task(self.conn, task_id)
        owner = effective_owner(self.conn, task_id, '*')
        if not owner or owner['role_id'] != role_id:
            raise PermissionError('task belongs to another director')

    def authorize_group(self, role_id, group_id):
        group = self.get_group(group_id)
        if group['role_id'] != role_id:
            raise PermissionError('group belongs to another director')
        return group

    def subscription(self, subscription_id, role_id):
        sub = _subscription(self.conn, subscription_id)
        if sub['role_id'] != role_id:
            raise PermissionError('subscription belongs to another director')
        return self._subscription_envelope(subscription_id)
