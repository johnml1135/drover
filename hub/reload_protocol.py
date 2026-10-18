"""Version-one reload wire records, admission fencing and durable event storage.

This file is a frozen bootstrap peer: it must also import by filename using
only the standard library. Validation checks record shape, not live evidence,
readiness, authorization or reducer transitions. It never restamps identities.

Enrollment uses source_root/common_dir/default_branch, interpreter {path,
identity}, paths {ledger,runs,repo_config,config_home,token,overlay}, the verified
schema pair and bootstrap_digest. Release manifests use sha, release_path,
tree_digest/content_digest, files (relative path -> SHA256), dependency_digests,
schema_digest/bootstrap_digest and the verified schema pair. Additional JSON
fields are preserved for consumers (for example durable idempotency results).

The installer supplies an owner-ACL state root. Journal(root) owns only
root/journal; append publishes an immutable event before the caller replaces
root/state.json with its reducer result and last_journal_seq. replay validates
the *entire* log, including records before the cursor, before returning anything.
No method here writes enrollment, requests, commands, child status or active SHA.
"""
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from enum import StrEnum
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import tempfile
import threading
from uuid import UUID, uuid4

PROTOCOL = 1
RUN_FORMAT = 1


class ProtocolError(ValueError):
    """Invalid or incompatible record; callers must block, without effects."""


class Phase(StrEnum):
    IDLE = 'idle'
    PENDING = 'pending'
    DRAINING = 'draining'
    STOPPING = 'stopping'
    VALIDATING = 'validating'
    COMMITTED = 'committed'
    BLOCKED = 'blocked'
    ROLLING_BACK = 'rolling_back'
    ROLLED_BACK = 'rolled_back'
    FAILED = 'failed'


class RequestAction(StrEnum):
    REQUEST = 'request'
    CANCEL = 'cancel'
    RETRY = 'retry'
    STOP = 'stop'
    SET_AUTO = 'set_auto'


class CommandAction(StrEnum):
    DRAIN = 'drain'
    STOP = 'stop'
    COMMIT = 'commit'
    CANCEL = 'cancel'
    PROBE = 'probe'


class RequestSource(StrEnum):
    LANDING = 'landing'
    OWNER = 'owner'
    RECONCILE = 'reconcile'


class EventKind(StrEnum):
    REQUESTED = 'hub.reload.requested'
    PENDING = 'hub.reload.pending'
    DRAINED = 'hub.reload.drained'
    STARTING = 'hub.reload.starting'
    VALIDATING = 'hub.reload.validating'
    SUCCEEDED = 'hub.reload.succeeded'
    START_FAILED = 'hub.reload.start_failed'
    ROLLING_BACK = 'hub.reload.rolling_back'
    ROLLED_BACK = 'hub.reload.rolled_back'
    FAILED = 'hub.reload.failed'
    BLOCKED = 'hub.reload.blocked'
    CANCELLED = 'hub.reload.cancelled'
    AUTO_CHANGED = 'hub.reload.auto_changed'
    SUPERVISOR_RECOVERED = 'hub.reload.supervisor_recovered'
    CONTROL_REJECTED = 'hub.reload.control_rejected'


def _fail():
    # Do not echo records, paths, environment or arbitrary field values.
    raise ProtocolError('invalid or incompatible reload record')


def _integer(value):
    if type(value) is not int or value < 0:
        _fail()


def _positive(value):
    _integer(value)
    if value == 0:
        _fail()


def _boolean(value):
    if type(value) is not bool:
        _fail()


def _text(value):
    if not isinstance(value, str) or not value or '\x00' in value:
        _fail()


def _uuid(value):
    _text(value)
    try:
        parsed = UUID(value)
    except ValueError:
        _fail()
    if str(parsed) != value.lower():
        _fail()


def _hex(length):
    def check(value):
        if not isinstance(value, str) or re.fullmatch('[0-9a-f]{%d}' % length, value) is None:
            _fail()
    return check


_sha = _hex(40)
_digest = _hex(64)


def _time(value):
    _text(value)
    if not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|\+00:00)', value):
        _fail()
    try:
        datetime.fromisoformat(value)
    except ValueError:
        _fail()


def _enum(enum):
    def check(value):
        if not isinstance(value, str) or value not in enum:
            _fail()
    return check


def _nullable(check):
    def validate(value):
        if value is not None:
            check(value)
    return validate


def _object(value, fields):
    if not isinstance(value, dict):
        _fail()
    for field, check in fields.items():
        if field not in value:
            _fail()
        check(value[field])


def _list(check):
    def validate(value):
        if not isinstance(value, list):
            _fail()
        for item in value:
            check(item)
    return validate


def _version(value):
    _integer(value)
    if value != PROTOCOL:
        _fail()


def _absolute(value):
    _text(value)
    if not (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()):
        _fail()


def _relative(value):
    _text(value)
    if ('\\' in value or PureWindowsPath(value).drive or
            PurePosixPath(value).is_absolute() or
            any(part in ('', '.', '..') for part in value.split('/'))):
        _fail()


def _digests(value):
    if not isinstance(value, dict) or not value:
        _fail()
    for name, digest in value.items():
        _relative(name)
        _digest(digest)


def _schema_pair(value):
    _object(value, {'ledger_schema': _integer, 'required_schema': _integer})
    if value['ledger_schema'] != value['required_schema']:
        _fail()


def _enrollment(value):
    _object(value, dict(protocol=_version, source_root=_absolute, common_dir=_absolute,
                        default_branch=_text, interpreter=lambda v: _object(v, {
                            'path': _absolute, 'identity': _text}),
                        paths=lambda v: _object(v, {k: _absolute for k in (
                            'ledger', 'runs', 'repo_config', 'config_home', 'token', 'overlay')}),
                        bootstrap_digest=_digest))
    _schema_pair(value)


def _release(value):
    _object(value, dict(protocol=_version, run_format=_version, sha=_sha,
                        release_path=_absolute, tree_digest=_digest, content_digest=_digest,
                        dependency_digests=_digests, schema_digest=_digest,
                        bootstrap_digest=_digest, files=_digests))
    _schema_pair(value)
    if not {'hub/uv.lock', 'hub/pyproject.toml'} <= value['dependency_digests'].keys():
        _fail()


def _request(value):
    _object(value, dict(protocol=_version, id=_uuid, key=_text, action=_enum(RequestAction),
                        source=_enum(RequestSource), operation_id=_nullable(_uuid),
                        generation=_integer, instance=_uuid, from_sha=_nullable(_sha),
                        target_sha=_nullable(_sha), batch_id=_nullable(_integer), auto=_boolean,
                        enabled=_nullable(_boolean), created_at=_time))
    if value['action'] in ('cancel', 'retry') and value['operation_id'] is None:
        _fail()
    if value['action'] == 'set_auto':
        _boolean(value['enabled'])
    elif value['enabled'] is not None:
        _fail()
    if value['action'] == 'request':
        _sha(value['from_sha'])
        _sha(value['target_sha'])


def _command(value):
    _object(value, dict(protocol=_version, operation_id=_nullable(_uuid), generation=_integer,
                        instance=_uuid, request_id=_nullable(_uuid), action=_enum(CommandAction),
                        target_sha=_nullable(_sha), challenge=_nullable(_uuid)))
    if value['action'] in ('drain', 'stop', 'cancel'):
        _uuid(value['operation_id'])
    if value['action'] == 'probe':
        _uuid(value['challenge'])
    elif value['challenge'] is not None:
        _fail()


def _proof(value):
    _object(value, dict(active_sections=_integer, open_gate_ids=_list(_positive),
                        landing_idle=_boolean, services_idle=_boolean, recovery_complete=_boolean,
                        scan_known=_boolean, unsafe_detached_ids=_list(_positive),
                        unresolved_launches=_list(_positive), wsl_live_run_ids=_list(_positive),
                        participant_generations=lambda v: _object(v, {
                            k: _nullable(_integer) for k in ('dispatcher', 'landing', 'services', 'api')})))


def _ready_fields(schema):
    return dict(protocol=_version, operation_id=_nullable(_uuid), generation=_integer,
                instance=_uuid, runtime_sha=_sha, ledger_schema=schema, heartbeat_seq=_integer,
                recovery_complete=_boolean, dispatcher_lock=_boolean)


def _child(value):
    fields = _ready_fields(_nullable(_integer))
    fields.update(pid=_positive, creation_identity=_text, phase=_enum(Phase),
                  challenge=_nullable(_uuid), barrier=_proof, blockers=_list(_text))
    _object(value, fields)


def _healthy(value):
    _object(value, dict(sha=_sha, manifest_digest=_digest, instance=_uuid, healthy_at=_time))


def _handoff(value):
    _object(value, dict(operation_id=_uuid, generation=_integer, instance=_uuid,
                        creation_identity=_text, ledger_schema=_integer,
                        challenge=_uuid, heartbeat_seq=_integer))


def _drained(value):
    _proof(value)
    _object(value, {'generation': _integer})


def _state(value):
    _object(value, dict(protocol=_version, operation_id=_nullable(_uuid), generation=_integer,
                        phase=_enum(Phase), request=_nullable(_request), active=_nullable(_healthy),
                        previous=_nullable(_healthy), child=_nullable(lambda v: _object(v, {
                            'sha': _sha, 'instance': _uuid, 'pid': _positive, 'creation_identity': _text})),
                        handoff_schema=_nullable(_handoff), auto_enabled=_boolean,
                        owner_dispatch_enabled=_boolean, blockers=_list(_text), drained=_nullable(_drained),
                        attempts=lambda v: _object(v, {'window_started': _nullable(_time),
                                                      'count': _integer, 'fallback_used': _boolean}),
                        quarantined_sha=_nullable(_sha), updated_at=_time, last_journal_seq=_integer))
    for field in ('handoff_schema', 'drained'):
        if value[field] is not None and value[field]['generation'] > value['generation']:
            _fail()
    if value['handoff_schema'] is not None:
        if value['handoff_schema']['operation_id'] != value['operation_id']:
            _fail()
    # request is the producer's observed tuple, often older than this state.


def _event_data(value):
    _object(value, dict(operation_id=_nullable(_uuid), request_id=_nullable(_uuid),
                        generation=_integer, from_sha=_nullable(_sha), target_sha=_nullable(_sha),
                        active_sha=_nullable(_sha), phase=_enum(Phase), reason=_nullable(_text), actor=_text))
    if 'new_operation_id' in value:
        _uuid(value['new_operation_id'])


def _journal(value):
    _object(value, dict(protocol=_version, seq=_positive, id=_uuid, at=_time,
                        kind=_enum(EventKind), data=_event_data))


def _json_value(value):
    """Reject non-wire values even inside consumer extension fields."""
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            _json_value(item)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _json_value(item)
        return
    _fail()


def validate_record(kind: str, value: dict) -> dict:
    """Return an independent validated record; raise ProtocolError on refusal.

    All listed fields are mandatory, including explicit nullable fields. This
    does not claim a child is ready or a handoff is fresh. Those decisions need
    independently anchored contracts, live identity and a monotonic clock.
    """
    validators = dict(enrollment=_enrollment, release=_release, request=_request,
                      command=_command, child=_child, state=_state, journal=_journal,
                      ready=lambda v: _object(v, _ready_fields(_integer)),
                      healthy=_healthy, handoff_schema=_handoff)
    if kind not in validators or not isinstance(value, dict):
        _fail()
    _json_value(value)
    validators[kind](value)
    return deepcopy(value)


def _sync_directory(directory):
    # Windows has no stdlib directory FlushFileBuffers. File contents are synced
    # before publication there; on POSIX also persist the namespace operation.
    if os.name != 'nt':
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


@contextmanager
def _prepared_json(path, value):
    if not isinstance(value, dict):
        _fail()
    _json_value(value)
    payload = (json.dumps(value, ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
    # Same directory gives atomic publication and inherits the installed ACL.
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.partial', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        yield temporary
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, value: dict) -> None:
    """Flush/fsync a private same-directory temp, then atomically replace path.

    The owning writer must validate its record and create its directory first.
    Do not use replacement for create-once requests or immutable journal files.
    """
    path = Path(path)
    with _prepared_json(path, value) as temporary:
        os.replace(temporary, path)
        _sync_directory(path.parent)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


class Journal:
    """Immutable contiguous events under the installer-owned state root.

    Publication is an atomic create-only hard link to a fully fsynced file.
    Concurrent appenders retry sequence collisions, never replace a record.
    Hard-link/flush failure fails closed; there is no unsafe overwrite fallback.
    An interrupted state update is recovered with replay(state.last_journal_seq).
    Consumers persist each event ID with ledger mirroring in one transaction.
    """

    def __init__(self, root: Path):
        self.directory = Path(root) / 'journal'
        try:
            self.directory.mkdir()
        except FileExistsError:
            if self.directory.is_symlink() or not self.directory.is_dir():
                _fail()
        else:
            _sync_directory(self.directory.parent)

    def replay(self, after: int) -> list[dict]:
        _integer(after)
        paths = list(self.directory.glob('*.json'))
        indexed = {}
        for path in paths:
            if not re.fullmatch(r'[1-9][0-9]*\.json', path.name):
                _fail()
            indexed[int(path.stem)] = path
        records = []
        identities = set()
        generation = 0
        for expected, seq in enumerate(sorted(indexed), 1):
            if seq != expected:
                _fail()
            path = indexed[seq]
            if path.is_symlink():
                _fail()
            try:
                record = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_unique_object)
            except (ValueError, UnicodeError, OSError):
                raise ProtocolError('unreadable reload journal') from None
            record = validate_record('journal', record)
            if (record['seq'] != seq or record['id'] in identities or
                    record['data']['generation'] < generation):
                _fail()
            generation = record['data']['generation']
            identities.add(record['id'])
            records.append(record)
        if after > len(records):
            _fail()
        return records[after:]

    def append(self, kind: str, data: dict) -> dict:
        _enum(EventKind)(kind)
        data = deepcopy(data)
        _event_data(data)
        identity = str(uuid4())
        while True:
            records = self.replay(0)
            if records and data['generation'] < records[-1]['data']['generation']:
                _fail()
            record = validate_record('journal', dict(
                protocol=PROTOCOL, seq=len(records) + 1, id=identity,
                at=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                kind=kind, data=data))
            destination = self.directory / (str(record['seq']) + '.json')
            with _prepared_json(destination, record) as temporary:
                try:
                    os.link(temporary, destination)
                except FileExistsError:
                    continue
                _sync_directory(self.directory)
            return record


class _Section:
    def __init__(self, barrier, kind):
        self._barrier = barrier
        self._kind = kind
        self._released = False
        self._entered = False

    def __enter__(self):
        with self._barrier._mutex:
            if self._released or self._entered:
                raise RuntimeError('activity section cannot be reused')
            self._entered = True
        return self

    def __exit__(self, exc_type, exc, traceback):
        with self._barrier._mutex:
            if not self._released:
                self._released = True
                self._barrier._counts[self._kind] -= 1
                if self._barrier._counts[self._kind] == 0:
                    del self._barrier._counts[self._kind]
        return False


class ActivityBarrier:
    """Admission and immediate section accounting share one mutex.

    A returned token is already counted. The caller must exit it, even if it
    delays __enter__. close denies work, seal also denies counted controls;
    only reopen for the exact current generation can unseal/reopen admission.
    Equal generations are idempotent; stale close/seal/reopen have no effect.
    """

    def __init__(self):
        self._mutex = threading.Lock()
        self._generation = 0
        self._closed = False
        self._sealed = False
        self._counts = Counter()

    def try_enter(self, kind: str, *, control: bool = False):
        _text(kind)
        _boolean(control)
        with self._mutex:
            if self._sealed or (self._closed and not control):
                return None
            section = _Section(self, kind)
            self._counts[kind] += 1
            return section

    def close(self, generation: int) -> None:
        _integer(generation)
        with self._mutex:
            if generation >= self._generation:
                self._generation = generation
                self._closed = True

    def seal(self, generation: int) -> None:
        _integer(generation)
        with self._mutex:
            if generation == self._generation:
                self._closed = self._sealed = True

    def reopen(self, generation: int) -> None:
        _integer(generation)
        with self._mutex:
            if generation == self._generation:
                self._closed = self._sealed = False

    def snapshot(self) -> dict:
        with self._mutex:
            return dict(generation=self._generation, closed=self._closed,
                        sealed=self._sealed, active_sections=sum(self._counts.values()),
                        counts=dict(self._counts))
