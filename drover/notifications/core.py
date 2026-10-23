"""Durable roles, exclusive ownership and event-time subscription decisions."""
import hashlib
import json
import time

from ..ledger import ops
from ..ledger.db import tx


class Conflict(ValueError):
    """The caller's expected revision no longer owns the requested change."""


EVENT_CLASSES = frozenset(('progress', 'stage', 'result-accepted', 'landed', 'group-ready',
                           'decision-needed', 'decision-recorded', 'issue', 'recovered',
                           'role-unavailable'))
PROFILE_VERSION = 1


def classify(event):
    """Map ledger facts, never process exit, to semantic notification classes (v1)."""
    kind, data = event['kind'], event['data']
    if kind in ('deps-set', 'related-set') and not any(data.values()):
        return None
    if kind.startswith('director.') or kind.startswith('notification.'):
        return None
    if kind in ('task.result-accepted', 'result.accepted'):
        return 'result-accepted'
    if kind == 'group.ready':
        return 'group-ready'
    if kind in ('question-asked', 'decision-needed'):
        return 'decision-needed'
    if kind in ('question-answered', 'decision-recorded', 'decision.recorded'):
        return 'decision-recorded'
    if kind in ('recovery.refused', 'recovery.exhausted', 'progress.stalled', 'replan-owner'):
        return 'issue'
    if kind in ('progress.recovered', 'recovery.resolved'):
        return 'recovered'
    if kind == 'role.unavailable':
        return 'role-unavailable'
    if kind == 'status':
        if data.get('to') == 'landed':
            return 'landed'
        if data.get('to') in ('needs-owner', 'stuck'):
            return 'issue'
        return 'stage'
    if kind == 'comment' and data.get('urgent'):
        return 'decision-needed'
    return 'progress'


def _position(conn):
    return conn.execute('SELECT COALESCE(MAX(id),0) FROM task_events').fetchone()[0]


def _role(conn, role_id):
    row = conn.execute('SELECT * FROM director_roles WHERE role_id=?', (role_id,)).fetchone()
    if row is None:
        raise ops.NotFound(f'director {role_id}')
    return dict(row)


def effective_owner(conn, task_id, event_class):
    row = conn.execute("SELECT * FROM task_ownership WHERE task_id=? AND event_class IN (?, '*') "
                       "ORDER BY (event_class='*') LIMIT 1", (task_id, event_class)).fetchone()
    return dict(row) if row else None


SCOPE_KEYS = frozenset(('tasks', 'epics', 'repos'))
FILTER_KEYS = frozenset(('types', 'events', 'reasons'))


def _selectors(selectors):
    if not isinstance(selectors, dict):
        raise ValueError('selectors must be an object')
    if set(selectors) - SCOPE_KEYS - FILTER_KEYS - {'exclude'}:
        raise ValueError('unknown or unsupported selector keys')
    result = {}
    for key, values in selectors.items():
        if key == 'exclude':
            if not isinstance(values, dict) or set(values) - SCOPE_KEYS - FILTER_KEYS:
                raise ValueError('unknown exclusion keys')
            result[key] = _selectors(values)
        else:
            if not isinstance(values, (list, tuple)) or any(not isinstance(v, str) for v in values):
                raise ValueError(f'{key} must be a list of strings')
            result[key] = sorted(set(values))
    for key in ('events',):
        if set(result.get(key, [])) - EVENT_CLASSES:
            raise ValueError('unknown event class')
    if set(result.get('types', [])) - set(ops.task_context.KINDS):
        raise ValueError('unknown task type')
    return result


def _matches(task, event_class, reason, selectors):
    scope_values = {'tasks': task['id'], 'epics': task['epic_id'], 'repos': task['repo']}
    scopes = [scope_values[key] in selectors[key] for key in SCOPE_KEYS if key in selectors]
    if scopes and not any(scopes):
        return False
    values = {'types': task['kind'], 'events': event_class, 'reasons': reason}
    if any(key in selectors and value not in selectors[key] for key, value in values.items()):
        return False
    exclusions = selectors.get('exclude', {})
    return not any(value in exclusions.get(key, []) for key, value in {**scope_values, **values}.items())


def _preferences(**values):
    defaults = dict(preset='normal', mode='profile', class_modes={}, digest_interval=900,
                    digest_wake=False, detail_limit=50, minimum_severity='info', enabled=True,
                    expires_at=None, stop_on_completion=False)
    if set(values) - set(defaults):
        raise ValueError('unknown notification preferences')
    defaults.update(values)
    if defaults['preset'] not in ('quiet', 'normal', 'verbose'):
        raise ValueError('unknown preset')
    if defaults['mode'] not in ('profile', 'immediate', 'digest', 'muted'):
        raise ValueError('unknown mode')
    modes = defaults['class_modes']
    if not isinstance(modes, dict) or set(modes) - EVENT_CLASSES or any(
            mode not in ('immediate', 'digest', 'muted') for mode in modes.values()):
        raise ValueError('invalid class_modes')
    if not isinstance(defaults['digest_interval'], (int, float)) or not 1 <= defaults['digest_interval'] <= 604800:
        raise ValueError('digest_interval must be between 1 second and 7 days')
    if not isinstance(defaults['detail_limit'], int) or not 1 <= defaults['detail_limit'] <= 100:
        raise ValueError('detail_limit must be between 1 and 100')
    if defaults['minimum_severity'] not in ('info', 'warning', 'critical'):
        raise ValueError('unknown minimum_severity')
    for key in ('enabled', 'digest_wake', 'stop_on_completion'):
        if not isinstance(defaults[key], bool):
            raise ValueError(f'{key} must be a boolean')
    if defaults['expires_at'] is not None and not isinstance(defaults['expires_at'], (int, float)):
        raise ValueError('expires_at must be an epoch timestamp')
    return defaults


def _subscription(conn, sub_id):
    row = conn.execute('SELECT s.*,v.selectors,v.preferences FROM subscriptions s '
                       'JOIN subscription_versions v ON v.subscription_id=s.id AND v.revision=s.revision '
                       'WHERE s.id=?', (sub_id,)).fetchone()
    if not row:
        raise ops.NotFound(f'subscription {sub_id}')
    result = dict(row)
    for key in ('selectors', 'preferences'):
        result[key] = json.loads(result[key])
    return result


def _specificity(task, selectors):
    """Scope unions take the specificity of the arm matching this task."""
    for key, value, rank in (('tasks', task['id'], 3), ('epics', task['epic_id'], 2), ('repos', task['repo'], 1)):
        if value in selectors.get(key, []):
            return rank
    return 0


def _matching_subscription(conn, role_id, task, event):
    """Union matching facts; resolve preferences without creation-order priority.

    Matching task scope beats epic, repo, then owned-all scope. At equal scope,
    class overrides beat explicit modes, which beat profiles. Equal explicit
    preferences merge with muted > immediate > digest; ties use the lowest stable
    subscription ID. Thus an explicit scoped mute is as effective as immediate,
    while disjoint event/reason filters continue to select their union of facts.
    """
    candidates = []
    event_class = classify(event)
    for row in conn.execute('SELECT id FROM subscriptions WHERE role_id=? ORDER BY id', (role_id,)):
        sub = _subscription(conn, row['id'])
        prefs = sub['preferences']
        if not prefs['enabled'] or (prefs['expires_at'] is not None and
                                   ops._parse(event['at']).timestamp() >= prefs['expires_at']):
            continue
        if _matches(task, event_class, event['data'].get('reason'), sub['selectors']):
            candidates.append(sub)
    def precedence(sub):
        prefs = sub['preferences']
        explicit = 2 if event_class in prefs['class_modes'] else int(prefs['mode'] != 'profile')
        mode = _delivery_mode(task, event_class, prefs)
        return (_specificity(task, sub['selectors']), explicit, {'digest': 0, 'immediate': 1, 'muted': 2}[mode])
    return max(candidates, key=precedence) if candidates else None


def _mode(task, event_class, preferences):
    if event_class in preferences['class_modes']:
        return preferences['class_modes'][event_class]
    if preferences['mode'] != 'profile':
        return preferences['mode']
    if event_class in ('decision-needed', 'issue', 'role-unavailable'):
        return 'immediate'
    preset = preferences['preset']
    if preset == 'verbose':
        return 'immediate'
    if preset == 'normal' and (event_class == 'group-ready' or
                              (event_class == 'landed' and task['kind'] not in ('review', 'mechanical'))):
        return 'immediate'
    if preset == 'normal' and event_class == 'result-accepted' and task['kind'] in ('research', 'investigation'):
        return 'immediate'
    return 'digest'


def _delivery_mode(task, event_class, preferences):
    mode = _mode(task, event_class, preferences)
    severity = 'warning' if event_class in ('decision-needed', 'issue', 'role-unavailable') else 'info'
    levels = {'info': 0, 'warning': 1, 'critical': 2}
    return 'muted' if levels[severity] < levels[preferences['minimum_severity']] else mode


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


def _resolve_issue(conn, event):
    if event['kind'] not in ('progress.recovered', 'recovery.resolved', 'question-answered'):
        return
    cause, question = _cause_identity(event['data'].get('cause')), event['data'].get('question')
    if cause is None and question is None:
        return
    affected_roles = set()
    for table in ('notification_intents', 'notification_inbox'):
        affected_roles.update(row['role_id'] for row in conn.execute(
            f"SELECT DISTINCT role_id FROM {table} WHERE actionable=1 AND resolved=0 AND json_extract(envelope,'$.task_id')=? "
            "AND ((? IS NOT NULL AND json_extract(envelope,'$.cause_id')=?) OR "
            "(? IS NOT NULL AND json_extract(envelope,'$.evidence.question')=?))",
            (event['task_id'], cause, cause, question, question)) if row['role_id'])
        conn.execute(f"UPDATE {table} SET resolved=1 WHERE actionable=1 AND json_extract(envelope,'$.task_id')=? "
                     "AND ((? IS NOT NULL AND json_extract(envelope,'$.cause_id')=?) OR "
                     "(? IS NOT NULL AND json_extract(envelope,'$.evidence.question')=?))",
                     (event['task_id'], cause, cause, question, question))
    for role in conn.execute('SELECT role_id FROM notification_escalations').fetchall():
        reconcile_storage_hold(conn, role['role_id'])
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='director_runtime'").fetchone():
        from .roles import refresh_wake
        for role_id in affected_roles:
            refresh_wake(Notifications(conn), role_id)


def reconcile_storage_hold(conn, role_id):
    """Release pressure and its owner escalation when current issues leave scope."""
    role = _role(conn, role_id)
    count = conn.execute('SELECT COUNT(*) FROM notification_inbox WHERE role_id=? AND actionable=1 AND resolved=0',
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
    event_class = classify(event)
    if event_class is None or event['task_id'] is None:
        return
    task = ops.get_task(conn, event['task_id'])
    if not replay:
        _resolve_issue(conn, event)
    ownership = effective_owner(conn, task['id'], event_class)
    role_id = ownership['role_id'] if ownership else None
    sub = subscription or _matching_subscription(conn, role_id, task, event)
    preferences = sub['preferences'] if sub else _preferences(mode='muted')
    mode = _delivery_mode(task, event_class, preferences)
    severity = 'warning' if event_class in ('decision-needed', 'issue', 'role-unavailable') else 'info'
    envelope = dict(schema_version=1, event_id=event['id'], event_class=event_class,
                    task_id=task['id'], group_id=None,
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
        prior = conn.execute('SELECT envelope FROM notification_intents WHERE event_id<? AND role_id=? '
                             "AND event_class=? AND json_extract(envelope,'$.task_id')=? "
                             "AND json_extract(envelope,'$.cause_id') IS ? AND resolved=0 ORDER BY event_id DESC LIMIT 1",
                             (event['id'], role_id, event_class, task['id'], envelope['cause_id'])).fetchone() if actionable else None
        if not prior or json.loads(prior['envelope'])['evidence'] != envelope['evidence']:
            signal(conn, role_id, event['id'])
    if not replay and sub and preferences['stop_on_completion'] and (event_class in ('landed', 'group-ready') or
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


class Notifications:
    """One small trusted service shared by delivery, supervision and HTTP adapters."""
    def __init__(self, conn, now=None):
        self.conn = conn
        self.now = now or time.time

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
                if owner['role_id'] != role_id:
                    continue
                self.conn.execute('UPDATE notification_intents SET role_id=?,ownership_revision=? WHERE event_id=?',
                                  (role_id, owner['revision'], intent['event_id']))
                self.conn.execute('UPDATE notification_inbox SET role_id=?,ownership_revision=?,offered_at=NULL,digest_id=NULL '
                                  'WHERE event_id=? AND acknowledged_at IS NULL',
                                  (role_id, owner['revision'], intent['event_id']))
            from .delivery import transfer_compactions, compact
            transfer_compactions(self.conn, task_id, role_id)
            for affected_role in {role_id} | {intent['role_id'] for intent in intents if intent['role_id']}:
                compact(self, affected_role)
                from .roles import refresh_wake
                refresh_wake(self, affected_role)
            ops.event(self.conn, 'notification.ownership-assigned', 'system',
                      {'from': old['role_id'] if old else None, 'to': role_id,
                       'event_class': event_class, 'revision': revision}, task_id=task_id)
            return dict(task_id=task_id, role_id=role_id, event_class=event_class, revision=revision)

    def _validate_scope(self, role_id, selectors):
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

    def project(self, limit=100):
        from .delivery import project
        return project(self, limit)

    def receive(self, role_id, limit=50):
        from .delivery import receive
        return receive(self, role_id, limit)

    def _subscription_envelope(self, sub_id):
        sub = _subscription(self.conn, sub_id)
        sub['next_digest_at'] = _role(self.conn, sub['role_id'])['last_digest'] + sub['preferences']['digest_interval']
        sub['preview'] = []
        for row in self.conn.execute('SELECT * FROM task_events ORDER BY id DESC LIMIT 50'):
            event = ops._jrow(row, 'data')
            if event['task_id'] is None:
                continue
            event_class = classify(event)
            if event_class is None:
                continue
            owner = effective_owner(self.conn, event['task_id'], event_class)
            if owner and owner['role_id'] == sub['role_id'] and _matches(
                    ops.get_task(self.conn, event['task_id']), event_class, event['data'].get('reason'), sub['selectors']):
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
            return dict(roles=[self.health(row['role_id']) for row in self.conn.execute(
                'SELECT role_id FROM director_roles ORDER BY role_id')],
                unassigned=self.conn.execute('SELECT COUNT(*) FROM notification_intents WHERE role_id IS NULL').fetchone()[0],
                projection_position=self.conn.execute("SELECT event_id FROM notification_positions WHERE name='inbox'").fetchone()[0])
        role = self._role_envelope(role_id)
        role['runtime'] = self.role_state(role_id)
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

    def digest(self, role_id):
        from .delivery import digest
        return digest(self, role_id)

    def check_admission(self, role_id):
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
            event_class = classify(event)
            if not event_class or not event['task_id']:
                continue
            owner = effective_owner(self.conn, event['task_id'], event_class)
            if not owner or owner['role_id'] != sub['role_id']:
                continue
            task = ops.get_task(self.conn, event['task_id'])
            if not _matches(task, event_class, event['data'].get('reason'), sub['selectors']):
                continue
            if self.conn.execute('SELECT 1 FROM notification_inbox WHERE event_id=?', (event['id'],)).fetchone():
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
