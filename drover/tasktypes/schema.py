"""Pure JSON contracts for task types (schema version 1).

Registry inheritance precedes normalization. No registry, filesystem, YAML or hub
is consulted here. Reference existence, model/seat compatibility, graph safety,
approval, CAS and exact-SHA evidence belong to the consuming services. This
module checks their format and the graph-independent capability invariants.

Paths in ValidationError.errors use dotted keys and zero-based list indexes.
Task patches merge custom fields by key; null is a value, never deletion. Only
creation materializes defaults and appends acceptance defaults. Storage adapters
use decode_fields_json/encode_fields_json explicitly; public task payloads use
``fields`` dictionaries and never accept a serialized ``fields_json`` shortcut.
"""
from copy import deepcopy
import hashlib
import json
import math
import re
from typing import TypedDict
from urllib.parse import urlsplit

from drover.task_context import split_read


class TypeSnapshot(TypedDict):
    hash: str
    name: str
    revision: str | int
    schema_version: int
    resolved: dict
    provenance: dict


class Principal(TypedDict, total=False):
    role: str
    identity: str
    client: str
    source: str
    task_id: str
    run_id: int
    launch_nonce: str
    scopes: list[str]


class FrameworkBinding(TypedDict, total=False):
    kind: str
    evidence: list[dict]
    version: str | None
    repo: str
    ref_sha: str
    override: str | None
    root: str
    design: str
    plan: str
    tasks: str
    source: dict


class TaskRecord(TypedDict, total=False):
    id: str
    type: str
    kind: str
    type_version: str
    fields: dict
    assignee: str
    title: str
    repo: str | None
    goal: str
    description: str
    acceptance: list[str]
    scope_files: list[str]
    write: bool
    read_access: list[str]
    branch: str | None
    worktree: str | None
    priority: str
    size: str
    risk: str
    deps: list
    related: list[str]
    status: str
    execution_phase: str
    revision: int
    parent_id: str | None
    result: dict | None
    framework: FrameworkBinding | None


class ActionRequest(TypedDict):
    principal: Principal
    target: dict
    action: str
    payload: dict
    expected_revision: int | str
    request_key: str


class ActionReceipt(TypedDict):
    target: dict
    revision: int
    event_id: int
    state: str
    phase: str
    status: str
    evidence: dict
    request_key: str


class FormDescriptor(TypedDict):
    type: str
    type_version: str
    action: str
    fields: list[str]
    schema: dict
    values: dict
    web: dict
    cli: dict
    permitted_actions: list[dict]


class ValidationError(ValueError):
    """A validation failure with transport-neutral path/message diagnostics."""

    def __init__(self, path: str, message: str):
        self.errors = [{'path': path, 'message': message}]
        super().__init__(f'{path}: {message}')


def _fail(path, message):
    raise ValidationError(path, message)


def _path(parent, key):
    return f'{parent}.{key}' if parent else str(key)


def _json(value, path='', depth=0):
    if depth > 32:
        _fail(path, 'JSON nesting exceeds 32')
    if value is None or type(value) in (bool, int):
        return
    if isinstance(value, str):
        try:
            value.encode('utf-8')
        except UnicodeEncodeError:
            _fail(path, 'must be valid UTF-8 text')
        return
    if type(value) is float:
        if not math.isfinite(value):
            _fail(path, 'must be a finite number')
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                _fail(path, 'JSON object keys must be strings')
            _json(key, _path(path, key), depth + 1)
            _json(item, _path(path, key), depth + 1)
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _json(item, _path(path, i), depth + 1)
        return
    _fail(path, 'must be a JSON-compatible value')


def _mapping(value, path):
    if not isinstance(value, dict):
        _fail(path, 'must be an object')
    return value


def _keys(value, allowed, path=''):
    _mapping(value, path)
    for key in value:
        if key not in allowed:
            _fail(_path(path, key), 'unknown key')


def _text(value, path, nonempty=True):
    if not isinstance(value, str) or (nonempty and not value.strip()):
        _fail(path, 'must be a nonempty string' if nonempty else 'must be a string')
    return value


def _choice(value, choices, path):
    if not isinstance(value, str) or value not in choices:
        _fail(path, 'must be one of ' + ', '.join(choices))


def _boolean(value, path):
    if type(value) is not bool:
        _fail(path, 'must be a boolean')


def _integer(value, path, minimum=0):
    if type(value) is not int or value < minimum:
        _fail(path, f'must be an integer >= {minimum}')


def _strings(value, path):
    if not isinstance(value, list):
        _fail(path, 'must be a list')
    for i, item in enumerate(value):
        _text(item, _path(path, i))
    return value


def _unique(values):
    return list(dict.fromkeys(values))


def _dumps(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def canonical_hash(value: dict) -> str:
    """SHA-256 of sorted, compact UTF-8 JSON; no scalar coercion or NaN."""
    _mapping(value, '')
    _json(value)
    return hashlib.sha256(_dumps(value).encode('utf-8')).hexdigest()


def decode_fields_json(value: str) -> dict:
    """Decode the ledger's fields_json TEXT, rejecting duplicates/non-JSON data."""
    _text(value, 'fields_json')

    def pairs(entries):
        result = {}
        for key, item in entries:
            if key in result:
                _fail('fields_json', f'duplicate key {key!r}')
            result[key] = item
        return result

    try:
        result = json.loads(value, object_pairs_hook=pairs)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, ValidationError):
            raise
        _fail('fields_json', 'must contain a JSON object')
    _mapping(result, 'fields_json')
    _json(result, 'fields_json')
    return result


def encode_fields_json(value: dict) -> str:
    """Encode validated custom values as canonical JSON TEXT, not a task field."""
    _mapping(value, 'fields')
    _json(value, 'fields')
    return _dumps(value)


TYPE_ID = re.compile(r'[a-z][a-z0-9-]{0,63}\Z')
FIELD_ID = re.compile(r'[a-z][a-z0-9_-]{0,63}\Z')
TASK_ID = re.compile(r'T-[A-Za-z0-9]+\Z')
FIELD_KINDS = ('string', 'markdown', 'integer', 'number', 'boolean', 'enum', 'list',
               'repo-ref', 'task-ref', 'uri')
# Includes transport aliases, persistence, execution and capability names. New
# custom types remain open identifiers; these core field IDs remain reserved.
CORE_IDS = frozenset('''id title goal description repo repo_path epic_id acceptance scope_files
priority size risk deps related links task_links branch worktree kind type type_version fields fields_json write read_access
assignee execution_phase revision parent_id result result_json framework framework_binding status
rank owner_kind model attempts policy_version handoff blocker created updated approved_at approved_by
archived run_id runs events approval access workspace routing context skills gate review landing
followups lifecycle forms tunable schema_version'''.split())
# Template values are a separate contract from names reserved for transport,
# storage and policy. Adding a reserved ID must never expose a new placeholder.
TASK_TEMPLATE_VALUES = frozenset('''id title goal description repo acceptance scope_files priority
size risk deps related branch worktree type kind type_version assignee parent_id write read_access'''.split())
TEMPLATE_VALUES = frozenset({
    'repo', 'default_branch', 'models.director', 'models.review',
    'framework.design', 'framework.plan', 'framework.tasks', 'framework.root', 'framework.artifact',
}) | TASK_TEMPLATE_VALUES | frozenset('task.' + key for key in TASK_TEMPLATE_VALUES)
ROLES = ('worker', 'planner', 'reviewer')
TUNABLE_PATHS = tuple(f'routing.{role}.{key}' for role in ROLES
                      for key in ('candidate_weights', 'model_preference', 'effort'))


def _id(value, path, pattern=TYPE_ID):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        _fail(path, 'invalid identifier')


def _task_ref(value, path):
    _id(value, path, TASK_ID)


def _read_ref(value, path):
    try:
        split_read(value)
    except ValueError as exc:
        _fail(path, str(exc))


def _relative(value, path, glob=False):
    """Syntactic repo-relative path check; consumers must check symlink escape."""
    _text(value, path)
    if (value.startswith(('/', '\\')) or '\\' in value or ':' in value
            or any(part in ('.', '..', '') for part in value.rstrip('/').split('/'))
            or re.search(r'[\x00-\x1f\x7f]', value)):
        _fail(path, 'must be a normalized repo-relative path')
    if not glob and any(c in value for c in '*?['):
        _fail(path, 'wildcards are not permitted here')


def _value(value, field, path):
    if value is None:
        if not field.get('nullable', False):
            _fail(path, 'must not be null')
        return
    kind = field['type']
    if kind in ('string', 'markdown', 'repo-ref', 'task-ref', 'uri'):
        _text(value, path, field.get('required', False))
        if kind == 'repo-ref':
            _read_ref(value, path)
        elif kind == 'task-ref':
            _task_ref(value, path)
        elif kind == 'uri':
            try:
                parsed = urlsplit(value)
            except ValueError:
                _fail(path, 'invalid URI')
            if (not re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', value)
                    or re.search(r'[\s\x00-\x1f\x7f]', value)
                    or (parsed.scheme in ('http', 'https') and not parsed.netloc)):
                _fail(path, 'invalid URI')
    elif kind == 'boolean':
        _boolean(value, path)
    elif kind in ('integer', 'number'):
        if type(value) not in ((int,) if kind == 'integer' else (int, float)):
            _fail(path, f'must be a {kind}')
        if type(value) is float and not math.isfinite(value):
            _fail(path, 'must be a finite number')
        for key, compare in (('minimum', lambda a, b: a < b), ('maximum', lambda a, b: a > b)):
            if key in field and compare(value, field[key]):
                _fail(path, f'violates {key} {field[key]}')
    elif kind == 'enum':
        if isinstance(value, str):
            _text(value, path, field.get('required', False))
        if not any(type(value) is type(item) and value == item for item in field['enum']):
            _fail(path, 'must be a declared enum value')
    elif kind == 'list':
        if not isinstance(value, list):
            _fail(path, 'must be a list')
        for i, item in enumerate(value):
            _value(item, field['items'], _path(path, i))
    if isinstance(value, (str, list)):
        for key, compare in (('min_length', lambda a, b: a < b), ('max_length', lambda a, b: a > b)):
            if key in field and compare(len(value), field[key]):
                _fail(path, f'violates {key} {field[key]}')


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


TASK_INPUTS = frozenset('''id title repo repo_path epic_id goal description acceptance scope_files priority
size risk deps related branch worktree type kind type_version fields assignee status revision parent_id
handoff routing access workspace landing gate review framework write read_access'''.split())
# These are preserved when validating a trusted existing ledger record, but are
# never accepted as worker/API patches or a means of setting approval/evidence.
TASK_STORED = TASK_INPUTS | frozenset('''execution_phase rank owner_kind model attempts policy_version
blocker created updated approved_at approved_by archived result result_json framework_binding'''.split())


def _aliases(value):
    value = deepcopy(value)
    for canonical, alias in (('type', 'kind'), ('goal', 'description')):
        if canonical in value and alias in value and value[canonical] != value[alias]:
            _fail(canonical, 'conflicts with ' + alias)
        if alias in value:
            value[canonical] = value[alias]
            del value[alias]
    return value


def _connections(task):
    _strings(task['related'], 'related')
    for i, ref in enumerate(task['related']):
        _task_ref(ref, f'related.{i}')
        if ref == task.get('id'):
            _fail(f'related.{i}', 'cannot relate a task to itself')
    if not isinstance(task['deps'], list):
        _fail('deps', 'must be a list')
    for i, dep in enumerate(task['deps']):
        path = f'deps.{i}'
        if isinstance(dep, str):
            _task_ref(dep, path)
            ref = dep
        else:
            _keys(dep, {'depends_on', 'kind'}, path)
            ref = dep.get('depends_on')
            _task_ref(ref, path + '.depends_on')
            _choice(dep.get('kind', 'blocks'), ('blocks', 'waits-for'), path + '.kind')
        if ref == task.get('id'):
            _fail(path, 'cannot depend on itself')


def _merge(old, new):
    result = deepcopy(old)
    for key, value in new.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _framework(value, path='framework'):
    """Check the nullable binding shape; adapters own framework/path semantics."""
    if value is None:
        return
    _keys(value, FrameworkBinding.__annotations__, path)
    for key in ('kind', 'version', 'repo', 'ref_sha', 'override', 'root', 'design', 'plan', 'tasks'):
        if key in value:
            if key in ('version', 'override') and value[key] is None:
                continue
            _text(value[key], _path(path, key), False)
    if 'evidence' in value:
        if not isinstance(value['evidence'], list):
            _fail(path + '.evidence', 'must be a list')
        for i, item in enumerate(value['evidence']):
            _mapping(item, f'{path}.evidence.{i}')
    if 'source' in value:
        _mapping(value['source'], path + '.source')


def validate_task(payload: dict, resolved_type: dict, existing: dict | None = None) -> dict:
    """Return a complete, detached record; patches preserve omitted values.

    The caller must supply the pinned resolved type. This function grants no
    approval, checks no live repo/graph, and mutates neither input. Capability
    broadening must additionally invalidate approval in the ledger service.
    """
    _mapping(payload, '')
    _json(payload)
    _keys(payload, TASK_INPUTS)
    resolved = normalize_type(resolved_type)
    patch = _aliases(payload)
    for key in ('access', 'workspace', 'landing', 'review', 'routing'):
        if key in patch:
            _mapping(patch[key], key)
    create = existing is None
    if create:
        task = {'type': resolved['name'], 'goal': '', 'acceptance': [], 'scope_files': [],
                'priority': 'P2', 'size': 'M', 'risk': 'low', 'deps': [], 'related': [],
                'branch': None, 'worktree': None, 'status': 'proposed', 'revision': 0,
                'assignee': resolved['assignee'], 'fields': {},
                'write': resolved['access']['write'], 'read_access': deepcopy(resolved['access']['read_access'])}
    else:
        _keys(existing, TASK_STORED | {'write', 'read_access'})
        _json(existing)
        task = _aliases(existing)
        if 'fields' not in task:
            _fail('fields', 'existing records must decode fields_json before validation')
        # A pinned type cannot be silently replaced by a patch, even if idle.
        # Explicit upgrades are a separate owner action that creates a revision.
        for key in ('type', 'type_version', 'assignee', 'id'):
            if key in patch and patch[key] != task.get(key):
                _fail(key, 'immutable in task patches; use an explicit owner action')
    _mapping(patch.get('fields', {}), 'fields')
    _mapping(task['fields'], 'fields')
    merged_fields = {**task['fields'], **patch.get('fields', {})}
    task = _merge(task, patch)
    task['fields'] = merged_fields
    if task.get('type') != resolved['name']:
        _fail('type', 'does not match the pinned resolved type')
    if 'type_version' in task:
        if not isinstance(task['type_version'], str) or not re.fullmatch('[0-9a-f]{64}', task['type_version']):
            _fail('type_version', 'must be a SHA-256 hash')
    if task.get('assignee') != resolved['assignee']:
        _fail('assignee', 'does not match the pinned type')
    _text(task.get('title'), 'title')
    _text(task.get('goal'), 'goal', False)
    repo = task.get('repo')
    if repo is not None:
        _read_ref(repo, 'repo')
        if '@' in repo:
            _fail('repo', 'home repo must not contain a branch')
    if 'id' in task:
        _task_ref(task['id'], 'id')
    for key in ('branch', 'worktree', 'repo_path', 'epic_id', 'parent_id'):
        if task.get(key) is not None:
            _text(task[key], key)
    if task.get('parent_id') is not None:
        _task_ref(task['parent_id'], 'parent_id')
    if 'framework' in task:
        _framework(task['framework'])
    if 'handoff' in task:
        _text(task['handoff'], 'handoff', False)
        if len(task['handoff']) > 1500:
            _fail('handoff', 'must be <= 1500 characters')
    for key, choices in (('priority', ('P0', 'P1', 'P2', 'P3', 'P4')), ('size', ('S', 'M', 'L')),
                         ('risk', ('low', 'high'))):
        _choice(task.get(key), choices, key)
    _integer(task.get('revision'), 'revision')
    _text(task.get('status'), 'status')
    if 'status' in patch and (not create or patch['status'] not in ('proposed', 'draft')):
        _fail('status', 'state changes require lifecycle actions; creation starts proposed/draft')
    _strings(task.get('acceptance'), 'acceptance')
    if create:
        task['acceptance'] = _unique(task['acceptance'] + resolved['acceptance']['defaults'])
    _strings(task.get('scope_files'), 'scope_files')
    for i, scope in enumerate(task['scope_files']):
        _relative(scope, f'scope_files.{i}', glob=True)
    for name in task['fields']:
        if name not in resolved['fields']:
            _fail('fields.' + name, 'unknown custom field')
    for name, field in resolved['fields'].items():
        if create and name not in task['fields'] and 'default' in field:
            task['fields'][name] = deepcopy(field['default'])
        if name not in task['fields']:
            if field['required']:
                _fail('fields.' + name, 'required field is missing')
        else:
            _value(task['fields'][name], field, 'fields.' + name)
    _connections(task)
    access = _mapping(task.get('access', {}), 'access')
    if 'write' in access:
        _boolean(access['write'], 'access.write')
    for key in ('write', 'read_access'):
        if key in patch.get('access', {}) and key in patch and patch[key] != patch['access'][key]:
            _fail(key, 'conflicts with access.' + key)
    _boolean(task['write'], 'write')
    _strings(task['read_access'], 'read_access')
    for i, ref in enumerate(task['read_access']):
        if '${' not in ref:  # Registry placeholders are checked on the type, below.
            _read_ref(ref, f'read_access.{i}')
    # Task access overrides keep the established flat T-7 surface. Declarative
    # policy overrides use the same type-section validators and cannot introduce
    # executable hooks. Approval and repo minima remain the consumer's duty.
    effective = deepcopy(resolved)
    for key in ('access', 'workspace', 'landing', 'review', 'routing'):
        if key in task:
            _mapping(task[key], key)
            effective[key] = _merge(effective[key], task[key])
    if 'gate' in task:
        effective['gate'] = task['gate']
    if 'write' in payload:
        effective['access']['write'] = payload['write']
    if 'read_access' in payload:
        effective['access']['read_access'] = deepcopy(payload['read_access'])
    # Flat T-7 fields already present on a record are also explicit overrides.
    if 'access' not in patch or 'write' not in patch['access']:
        effective['access']['write'] = task['write']
    if 'access' not in patch or 'read_access' not in patch['access']:
        effective['access']['read_access'] = task['read_access']
    if 'sandbox' not in task.get('access', {}):
        effective['access']['sandbox'] = (None if resolved['assignee'] == 'owner' else
                                         'workspace-write' if effective['access']['write'] else 'read-only')
    if effective['access']['write'] is True and repo is None:
        _fail('repo', 'repository writes require a home repo')
    # Expand only repo/default_branch values known without a registered-repo
    # lookup. The registry binds default_branch before calling this validator.
    refs = effective['access']['read_access']
    if isinstance(refs, list):
        effective['access']['read_access'] = [ref.replace('${repo}', repo or '') if isinstance(ref, str) else ref
                                            for ref in refs if repo is not None or '${repo}' not in str(ref)]
    # Forms were checked against the pinned contract; capability overrides do
    # not redefine which inputs the saved form collects.
    effective.pop('forms', None)
    checked = normalize_type(effective)
    task['write'] = checked['access']['write']
    task['read_access'] = checked['access']['read_access']
    for key in ('access', 'workspace', 'landing', 'review', 'routing'):
        if key in task:
            task[key] = deepcopy(checked[key])
    if task['write']:
        if repo is None:
            _fail('repo', 'repository writes require a home repo')
        if not task['scope_files']:
            _fail('scope_files', 'repository writes require a nonempty approved scope')
    for key in ('branch', 'worktree'):
        if checked['workspace'][key] == 'none' and task.get(key) is not None:
            _fail(key, 'must be null when workspace.' + key + ' is none')
        if checked['workspace'][key] == 'existing' and not task.get(key):
            _fail(key, 'existing workspace requires an explicit ' + key)
    task['kind'] = task['type']
    task['description'] = task['goal']
    return task
