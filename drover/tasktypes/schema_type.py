"""Task type normalization."""
from copy import deepcopy
import re

from .schema_core import (
    CORE_IDS, FIELD_ID, FIELD_KINDS, ROLES, TASK_INPUTS, TEMPLATE_VALUES, TUNABLE_PATHS,
    _boolean, _choice, _dumps, _fail, _id, _integer, _json, _keys, _mapping, _path,
    _read_ref, _relative, _strings, _text, _unique, _value,
)


def _field(raw, path):
    _keys(raw, {'type', 'required', 'default', 'nullable', 'minimum', 'maximum',
                'min_length', 'max_length', 'enum', 'items', 'shown', 'label', 'description'}, path)
    field = {'required': False, 'nullable': False, 'shown': [], **deepcopy(raw)}
    _choice(field.get('type'), FIELD_KINDS, path + '.type')
    for key in ('required', 'nullable'):
        _boolean(field[key], path + '.' + key)
    _strings(field['shown'], path + '.shown')
    if len(set(field['shown'])) != len(field['shown']) or set(field['shown']) - {'card', 'page'}:
        _fail(path + '.shown', 'must be an ordered list of card/page without duplicates')
    for key in ('label', 'description'):
        if key in field:
            _text(field[key], path + '.' + key, False)
    for key in ('minimum', 'maximum'):
        if key in field:
            if field['type'] not in ('integer', 'number') or type(field[key]) not in (int, float):
                _fail(path + '.' + key, 'numeric bounds require a numeric field and finite number')
    for key in ('min_length', 'max_length'):
        if key in field:
            if field['type'] not in ('string', 'markdown', 'list', 'repo-ref', 'task-ref', 'uri'):
                _fail(path + '.' + key, 'length bounds require a string or list field')
            _integer(field[key], path + '.' + key)
    for lower, upper in (('minimum', 'maximum'), ('min_length', 'max_length')):
        if lower in field and upper in field and field[lower] > field[upper]:
            _fail(path + '.' + upper, 'must be >= ' + lower)
    if field['type'] == 'list':
        if 'items' not in field:
            _fail(path + '.items', 'typed list items are required')
        field['items'] = _field(field['items'], path + '.items')
    elif 'items' in field:
        _fail(path + '.items', 'only list fields have items')
    if field['type'] == 'enum':
        values = field.get('enum')
        if not isinstance(values, list) or not values:
            _fail(path + '.enum', 'must be a nonempty list of scalar values')
        seen = set()
        for i, item in enumerate(values):
            if item is None or type(item) not in (str, bool, int, float):
                _fail(f'{path}.enum.{i}', 'must be a non-null scalar')
            marker = (type(item), _dumps(item))
            if marker in seen:
                _fail(f'{path}.enum.{i}', 'duplicate enum value')
            seen.add(marker)
    elif 'enum' in field:
        _fail(path + '.enum', 'only enum fields have enum values')
    if 'default' in field:
        _value(field['default'], field, path + '.default')
    return field


def _placeholders(value, fields, path=''):
    if isinstance(value, dict):
        for key, item in value.items():
            _placeholders(item, fields, _path(path, key))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _placeholders(item, fields, _path(path, i))
    elif isinstance(value, str):
        allowed = set(TEMPLATE_VALUES)
        allowed.update('fields.' + key for key in fields)
        allowed.update('task.fields.' + key for key in fields)
        rest = value
        for match in re.finditer(r'\$\{([^{}]*)\}', value):
            if match[1] not in allowed:
                _fail(path, f'unknown placeholder {match[0]}')
            rest = rest.replace(match[0], '')
        if '${' in rest:
            _fail(path, 'malformed placeholder')


def _section(raw, name, defaults, path=None):
    value = raw.get(name, {})
    _keys(value, set(defaults), path or name)
    return {**deepcopy(defaults), **deepcopy(value)}


def _forms(raw, fields, write):
    _keys(raw, {'create', 'edit', 'answer'}, 'forms')
    result = {}
    base = ['title', 'repo', 'goal'] + (['scope_files'] if write else [])
    base += [key for key, field in fields.items() if field['required'] and 'default' not in field]
    for action in ('create', 'edit', 'answer'):
        path = 'forms.' + action
        form = _section(raw, action, {'fields': [] if action == 'answer' else base,
                                     'web': {}, 'cli': {}}, path)
        names = _strings(form['fields'], path + '.fields')
        if len(set(names)) != len(names):
            _fail(path + '.fields', 'duplicate form field')
        for i, name in enumerate(names):
            # Reserved storage/policy names are not necessarily action inputs.
            if name not in fields and name not in TASK_INPUTS and not (action == 'answer' and name == 'questions'):
                _fail(f'{path}.fields.{i}', 'unknown form field')
        if action == 'create':
            for required in base:
                # goal has a compatible description transport name.
                if required not in names and not (required == 'goal' and 'description' in names):
                    _fail(path + '.fields', 'missing creation input ' + required)
        web = form['web']
        _keys(web, {'layout', 'widgets'}, path + '.web')
        web.setdefault('layout', 'stacked')
        _choice(web['layout'], ('stacked',), path + '.web.layout')
        widgets = web.get('widgets', {})
        _mapping(widgets, path + '.web.widgets')
        for name, widget in widgets.items():
            if name not in names:
                _fail(path + '.web.widgets.' + name, 'widget field is not in this form')
            _choice(widget, ('text', 'textarea', 'select', 'checkbox', 'list'), path + '.web.widgets.' + name)
        cli = form['cli']
        _keys(cli, {'mode', 'order'}, path + '.cli')
        cli.setdefault('mode', 'sequential')
        cli.setdefault('order', list(names))
        _choice(cli['mode'], ('sequential',), path + '.cli.mode')
        _strings(cli['order'], path + '.cli.order')
        if len(cli['order']) != len(names) or set(cli['order']) != set(names):
            _fail(path + '.cli.order', 'must contain every form field exactly once')
        result[action] = form
    return result


def _delivery(write, workspace, landing, gate, review, assignee, path_prefix=''):
    def fail(path, message):
        _fail(_path(path_prefix, path), message)
    if write:
        if workspace['worktree'] == 'none':
            fail('workspace.worktree', 'repository writes require new or existing isolation')
        if workspace['branch'] == 'none':
            fail('workspace.branch', 'repository writes require a new or existing branch')
        if landing['mode'] == 'no-repo-change':
            fail('landing.mode', 'repository writes require commit+land or spec-only delivery')
        if gate == 'none':
            fail('gate', 'repository writes require scoped/full; exemptions need owner evidence')
        if not review['required']:
            fail('review.required', 'repository writes require independent review')
    elif landing['mode'] != 'no-repo-change':
        fail('landing.mode', 'read-only work requires no-repo-change delivery')
    if landing['mode'] == 'no-repo-change':
        if workspace['worktree'] != 'none' or workspace['branch'] != 'none':
            fail('workspace', 'no-repo-change delivery requires no branch/worktree')
        if not re.fullmatch(r'hub:tasks/(?:\$\{task.id\}|T-[A-Za-z0-9]+)/result\.md', landing['artifact'] or ''):
            fail('landing.artifact', 'must be a hub task result URI')
    else:
        for key in ('artifact', 'plan'):
            if landing[key] is not None:
                _relative(re.sub(r'\$\{[^{}]+\}', 'placeholder', landing[key]), 'landing.' + key)
    if assignee == 'owner':
        if write or workspace != {'worktree': 'none', 'branch': 'none'}:
            fail('assignee', 'owner work cannot use worker workspace or repository writes')
        if gate != 'none' or review['required']:
            fail('gate', 'owner decisions require no worker gate or model review')


def normalize_type(raw: dict) -> dict:
    """Validate an already merged definition and fill schema defaults.

    Layer directives are retained for the registry, which must resolve parents
    and removals before persisting a resolved snapshot. No type-name branching
    encodes shipped policies; those remain opt-in registry data.
    """
    sections = {'assignee', 'fields', 'access', 'workspace', 'routing', 'context', 'skills',
                'acceptance', 'gate', 'review', 'landing', 'followups', 'lifecycle', 'forms', 'tunable'}
    _mapping(raw, '')
    _json(raw)
    _keys(raw, sections | {'schema_version', 'name', 'revision', 'description', 'extends', 'replace', 'remove_fields'})
    if type(raw.get('schema_version')) is not int or raw['schema_version'] != 1:
        _fail('schema_version', 'must be integer 1')
    _id(raw.get('name'), 'name')
    out = deepcopy(raw)
    out.setdefault('revision', 1)
    if type(out['revision']) not in (str, int) or (isinstance(out['revision'], str) and not out['revision'].strip()):
        _fail('revision', 'must be a human revision string or integer')
    out.setdefault('description', '')
    _text(out['description'], 'description', False)
    if 'extends' in out:
        parent = out['extends']
        _text(parent, 'extends')
        _id(parent.removeprefix('builtin/').removeprefix('lower/'), 'extends')
    if 'replace' in out:
        _boolean(out['replace'], 'replace')
        if out['replace'] and 'extends' in out:
            _fail('replace', 'cannot combine replacement and inheritance')
    if 'remove_fields' in out:
        _strings(out['remove_fields'], 'remove_fields')
        for i, name in enumerate(out['remove_fields']):
            _id(name, f'remove_fields.{i}', FIELD_ID)
            if name in CORE_IDS:
                _fail(f'remove_fields.{i}', 'core fields cannot be removed')
    out.setdefault('assignee', 'agent')
    _choice(out['assignee'], ('agent', 'owner'), 'assignee')
    owner = out['assignee'] == 'owner'
    _mapping(out.setdefault('fields', {}), 'fields')
    for name, field in out['fields'].items():
        _id(name, 'fields.' + name, FIELD_ID)
        if name in CORE_IDS:
            _fail('fields.' + name, 'reserved core field ID')
        out['fields'][name] = _field(field, 'fields.' + name)
    if len(out['fields']) > 1000:
        _fail('fields', 'at most 1000 fields are permitted')
    access = _section(raw, 'access', {'write': not owner, 'read_access': [] if owner else ['${repo}@${default_branch}'],
                                      'sandbox': None if owner else 'workspace-write'})
    _boolean(access['write'], 'access.write')
    write = access['write']
    if 'sandbox' not in raw.get('access', {}) and not owner:
        access['sandbox'] = 'workspace-write' if write else 'read-only'
    _strings(access['read_access'], 'access.read_access')
    for i, ref in enumerate(access['read_access']):
        _read_ref(ref.replace('${repo}', 'repo').replace('${default_branch}', 'main'), f'access.read_access.{i}')
    if owner:
        if access['sandbox'] is not None or access['read_access']:
            _fail('access', 'owner work has no worker sandbox or repository read grants')
    else:
        _choice(access['sandbox'], ('read-only', 'workspace-write'), 'access.sandbox')
        if access['sandbox'] != ('workspace-write' if write else 'read-only'):
            _fail('access.sandbox', 'sandbox must match write capability')
    out['access'] = access
    out['workspace'] = _section(raw, 'workspace', {'worktree': 'new' if write else 'none', 'branch': 'new' if write else 'none'})
    for key in ('worktree', 'branch'):
        _choice(out['workspace'][key], ('new', 'existing', 'none'), 'workspace.' + key)
    routing = deepcopy(raw.get('routing', {}))
    _keys(routing, set(ROLES), 'routing')
    if owner and routing:
        _fail('routing', 'owner work has no agent routes')
    if not owner:
        for role in ROLES:
            route = _section(routing, role, {'backend': 'auto', 'model': 'auto', 'effort': 'high', 'escalation': []},
                             'routing.' + role)
            _choice(route['backend'], ('auto', 'claude', 'codex'), f'routing.{role}.backend')
            _text(route['model'], f'routing.{role}.model')
            _choice(route['effort'], ('low', 'medium', 'high', 'xhigh', 'max', 'ultra'), f'routing.{role}.effort')
            _strings(route['escalation'], f'routing.{role}.escalation')
            routing[role] = route
    out['routing'] = routing
    context = _section(raw, 'context', {'template': None, 'include': [], 'framework': 'respect',
                                       'budget_tokens': None, 'handoff_at_tokens': 60000, 'compact_at_tokens': None})
    if context['template'] is not None:
        _text(context['template'], 'context.template')
        _relative(re.sub(r'\$\{[^{}]+\}', 'placeholder', context['template']), 'context.template')
    _strings(context['include'], 'context.include')
    for i, include in enumerate(context['include']):
        _relative(include, f'context.include.{i}', glob=True)
    _choice(context['framework'], ('respect',), 'context.framework')
    if context['budget_tokens'] is not None:
        _integer(context['budget_tokens'], 'context.budget_tokens', 1)
    _integer(context['handoff_at_tokens'], 'context.handoff_at_tokens', 1)
    if context['handoff_at_tokens'] > 60000:
        _fail('context.handoff_at_tokens', 'cannot exceed 60000')
    if context['compact_at_tokens'] is not None:
        _fail('context.compact_at_tokens', 'compaction is forbidden by pinned constraints')
    out['context'] = context
    skills = deepcopy(raw.get('skills', []))
    if not isinstance(skills, list):
        _fail('skills', 'must be a list')
    for i, skill in enumerate(skills):
        _keys(skill, {'name', 'required'}, f'skills.{i}')
        _id(skill.get('name'), f'skills.{i}.name')
        skill.setdefault('required', False)
        _boolean(skill['required'], f'skills.{i}.required')
    if owner and skills:
        _fail('skills', 'owner work cannot require agent skills')
    out['skills'] = skills
    out['acceptance'] = _section(raw, 'acceptance', {'defaults': []})
    _strings(out['acceptance']['defaults'], 'acceptance.defaults')
    out.setdefault('gate', 'scoped' if write else 'none')
    _choice(out['gate'], ('none', 'scoped', 'full'), 'gate')
    out['review'] = _section(raw, 'review', {'required': write, 'role': 'reviewer'})
    _boolean(out['review']['required'], 'review.required')
    _choice(out['review']['role'], ('reviewer',), 'review.role')
    out['landing'] = _section(raw, 'landing', {'mode': 'commit+land' if write else 'no-repo-change',
                                             'artifact': None if write else 'hub:tasks/${task.id}/result.md', 'plan': None})
    _choice(out['landing']['mode'], ('commit+land', 'spec-only', 'no-repo-change'), 'landing.mode')
    for key in ('artifact', 'plan'):
        if out['landing'][key] is not None:
            _text(out['landing'][key], 'landing.' + key)
    followups = _section(raw, 'followups', {'allowed': [], 'propose_requires': {}})
    _strings(followups['allowed'], 'followups.allowed')
    for i, destination in enumerate(followups['allowed']):
        _id(destination, f'followups.allowed.{i}')
    followups['allowed'] = _unique(followups['allowed'])
    _mapping(followups['propose_requires'], 'followups.propose_requires')
    for destination, guards in followups['propose_requires'].items():
        path = 'followups.propose_requires.' + destination
        if destination not in followups['allowed']:
            _fail(path, 'destination must be in followups.allowed')
        _strings(guards, path)
        for i, guard in enumerate(guards):
            _id(guard, _path(path, i))
        followups['propose_requires'][destination] = _unique(guards)
    if owner and followups['allowed']:
        _fail('followups.allowed', 'owner work has no followups')
    out['followups'] = followups
    out.setdefault('lifecycle', 'owner-v1' if owner else 'standard-v1')
    # Lifecycle graph documents are independently normalized by lifecycle.py.
    if isinstance(out['lifecycle'], str):
        _id(out['lifecycle'], 'lifecycle')
    else:
        _mapping(out['lifecycle'], 'lifecycle')
    out['forms'] = _forms(raw.get('forms', {}), out['fields'], write)
    out.setdefault('tunable', [] if owner else list(TUNABLE_PATHS))
    _strings(out['tunable'], 'tunable')
    for i, key in enumerate(out['tunable']):
        if key not in TUNABLE_PATHS or owner:
            _fail(f'tunable.{i}', 'only routing candidate weights/model preference/effort may be tuned')
    _placeholders(out, out['fields'])
    _delivery(write, out['workspace'], out['landing'], out['gate'], out['review'], out['assignee'])
    return out
