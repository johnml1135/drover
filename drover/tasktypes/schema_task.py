"""Task record validation."""
from copy import deepcopy
import re

from .schema_core import (
    FrameworkBinding, TASK_INPUTS, TASK_STORED, _boolean, _choice, _fail, _integer,
    _json, _keys, _mapping, _path, _read_ref, _relative, _strings, _task_ref, _text,
    _unique, _value,
)
from .schema_type import normalize_type


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
