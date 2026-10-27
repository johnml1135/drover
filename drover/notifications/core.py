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
                           'role-unavailable', 'broadcast', 'control'))
PROFILE_VERSION = 1


def classify(conn, event):
    """Map ledger facts, never process exit, to semantic notification classes (v1)."""
    kind, data = event['kind'], event['data']
    if kind in ('deps-set', 'related-set') and not any(data.values()):
        return None
    if kind.startswith('director.') or kind.startswith('notification.'):
        return None
    if kind in ('task.result-accepted', 'result.accepted'):
        return 'result-accepted'
    if kind == 'task.landed':
        from .groups import accepted_result
        result = accepted_result(conn, event['task_id'], 'legacy-merged')
        if result and (data.get('land_sha'), data.get('tip'), data.get('receipt')) == (
                result['commit'], result['tip'], result['receipt']):
            return 'landed'
        return 'stage'
    if kind == 'landing.repair-blocked':
        return 'issue'
    if kind == 'landing.outcome' and data.get('outcome') in ('infra-held', 'base-red', 'error', 'repair-blocked'):
        return 'issue'
    if kind == 'group.issue':
        return 'issue'
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
            # A worker can report a legal status transition without a merge.
            # Merged acceptance is emitted by the receipt seam; read-only
            # acceptance must match the owner's frozen result evidence.
            from .groups import accepted_result
            result = accepted_result(conn, event['task_id'], 'legacy-read-only')
            return 'landed' if result and result['revision'] == event['id'] else 'stage'
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


def recipient_owner(conn, envelope):
    """Current authority for an ordinary partition, group fact or explicit target."""
    if envelope.get('notice_id'):
        row = conn.execute('SELECT t.role_id FROM notification_targets t JOIN notification_notices n USING(event_id) '
                           'LEFT JOIN notification_controls c ON c.control_id=n.control_id WHERE t.event_id=? AND t.role_id=? '
                           'AND (n.control_id IS NULL OR (t.superseded=0 AND c.active=1))',
                           (envelope['event_id'], envelope.get('target_role'))).fetchone()
        return dict(role_id=row['role_id'], revision=1) if row else None
    if envelope.get('group_id') and envelope.get('task_id') is None:
        row = conn.execute('SELECT role_id FROM task_groups WHERE group_id=?', (envelope['group_id'],)).fetchone()
        return dict(role_id=row['role_id'], revision=1) if row else None
    return effective_owner(conn, envelope.get('task_id'), envelope['event_class'])


def scope_task(conn, task):
    task = dict(task)
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_groups'").fetchone():
        return task
    task['_groups'] = [r[0] for r in conn.execute('SELECT group_id FROM task_group_members WHERE child_id=?', (task['id'],))]
    task['_ancestors'] = []
    parent = task.get('parent_id')
    while parent and parent not in task['_ancestors']:
        task['_ancestors'].append(parent)
        row = conn.execute('SELECT parent_id FROM tasks WHERE id=?', (parent,)).fetchone()
        parent = row[0] if row else None
    return task


def event_scope(conn, event):
    if event['task_id'] is not None:
        try:
            return scope_task(conn, ops.get_task(conn, event['task_id']))
        except ops.NotFound:
            return None
    group_id = event['data'].get('group_id')
    if group_id and conn.execute('SELECT 1 FROM task_groups WHERE group_id=?', (group_id,)).fetchone():
        return dict(id=None, kind='research', epic_id=None, repo=None, _groups=[group_id])
    return None


SCOPE_KEYS = frozenset(('tasks', 'groups', 'descendants', 'epics', 'repos'))
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


def _scope_values(task):
    return {'tasks': [task['id']], 'groups': task.get('_groups', []),
            'descendants': task.get('_ancestors', []), 'epics': [task['epic_id']], 'repos': [task['repo']]}


def _matches(task, event_class, reason, selectors):
    scope_values = _scope_values(task)
    scopes = [bool(set(scope_values[key]) & set(selectors[key])) for key in SCOPE_KEYS if key in selectors]
    if scopes and not any(scopes):
        return False
    values = {'types': task['kind'], 'events': event_class, 'reasons': reason}
    if any(key in selectors and value not in selectors[key] for key, value in values.items()):
        return False
    exclusions = selectors.get('exclude', {})
    return not (any(set(value) & set(exclusions.get(key, [])) for key, value in scope_values.items()) or
                any(value in exclusions.get(key, []) for key, value in values.items()))


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
    scopes = _scope_values(task)
    for key, rank in (('tasks', 4), ('groups', 3), ('descendants', 3), ('epics', 2), ('repos', 1)):
        if set(scopes[key]) & set(selectors.get(key, [])):
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
    event_class = classify(conn, event)
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
    if task.get('_joined') and event_class in ('result-accepted', 'landed'):
        return 'digest'
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
                from .roles import refresh_wake
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
