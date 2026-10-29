"""Shared task schema contracts and validation primitives."""
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




TASK_INPUTS = frozenset('''id title repo repo_path epic_id goal description acceptance scope_files priority
size risk deps related branch worktree type kind type_version fields assignee status revision parent_id
handoff routing access workspace landing gate review framework write read_access'''.split())
# These are preserved when validating a trusted existing ledger record, but are
# never accepted as worker/API patches or a means of setting approval/evidence.
TASK_STORED = TASK_INPUTS | frozenset('''execution_phase rank owner_kind model attempts policy_version
blocker created updated approved_at approved_by archived result result_json framework_binding'''.split())
