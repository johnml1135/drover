"""Subscription selection and preferences."""
import json

from ..ledger import ops
from .core_scope import EVENT_CLASSES, classify

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
