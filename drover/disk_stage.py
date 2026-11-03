"""What a stage is asked and how long it has, on both sides of the worker pipe.

`StageRequest` is the typed request; its wire keys are today's, so the worker reads what it reads now. `StageClock` is
the one clock of a stage run: an 80% line no removal starts past, a fixed cap for each mutating step in the margin
after it, and patience for read steps. `StageRun` is what a stage says, one settled row per line on the worker's
stdout, and `read_rows` reads those lines back on the coordinator's side, keeping what arrived before a kill.
"""
import copy
import heapq
import json
import time
from dataclasses import asdict, dataclass, fields
from typing import BinaryIO, Callable, Mapping

from . import placement_paths

# Field name to wire key, where they differ; every other field is its own key.
_WIRE = {'policy': 'paths', 'inventory_id': '_inventory_id', 'worker_id': '_worker_id'}
# Absent from today's dict when not set, so absent from the wire when None.
_OPTIONAL = ('mode', 'budget', 'approved_path', 'inventory_id', 'worker_id')


@dataclass(frozen=True)
class StageRequest:
    stage: int
    side: str
    mode: str | None
    apply: bool
    now: float
    drive: Mapping
    repos: tuple[Mapping, ...]
    policy: Mapping              # today's request['paths'], the disk_cleanup settings
    state_dir: str
    tasks: Mapping
    budget: Mapping | None = None
    approved_path: str | None = None
    inventory_id: str | None = None
    worker_id: str | None = None

    def __post_init__(self):
        object.__setattr__(self, 'repos', tuple(self.repos))

    def to_wire(self) -> dict:
        """Today's request dict, a deep copy: an optional field that is None is left out, as today's dicts leave it."""
        return {_WIRE.get(name, name): list(item) if name == 'repos' else item
                for name, item in asdict(self).items() if not (item is None and name in _OPTIONAL)}

    @classmethod
    def from_wire(cls, value: dict) -> 'StageRequest':
        value = copy.deepcopy(value)
        return cls(**{field.name: value.get(_WIRE.get(field.name, field.name)) for field in fields(cls)})

    def for_side(self, side: str, convert: Callable[[str], str]) -> 'StageRequest':
        """The request a worker on `side` reads when it runs across sides: its paths converted by `convert`, and only
        that side's temp roots, report roots and caches kept (disk_runtime.py:39-59)."""
        value = self.to_wire()
        value['side'] = side
        value['state_dir'] = convert(value['state_dir'])
        snapshot = value.get('tasks') or {}
        if snapshot:
            snapshot['active_worktrees'] = [convert(path) for path in snapshot.get('active_worktrees', [])]
            for task in snapshot.get('tasks', []):
                if task.get('worktree'):
                    task['worktree'] = convert(task['worktree'])
        for repo in value['repos']:
            for key in ('repo', 'worktrees'):
                if repo.get(key):
                    repo[key] = convert(repo[key])
        policy = value['paths']
        for key in ('temp_roots', 'report_roots'):
            policy[key] = [convert(path) for path in policy.get(key, [])
                           if placement_paths.cleanup_path_side(path) == side]
        policy['caches'] = [cache for cache in policy.get('caches', [])
                            if placement_paths.cleanup_path_side(cache.get('path', cache.get('cwd'))) == side]
        for cache in policy['caches']:
            key = 'path' if 'path' in cache else 'cwd'
            cache[key] = convert(cache[key])
        return StageRequest.from_wire(value)

    def decide_view(self) -> dict:
        """The plain dict `decide` and `evaluate` read, so `dict(view, approved_path=None)` stays as it is."""
        return dict(self.to_wire(), approved_path=self.approved_path)


class StageClock:
    """One clock per stage run, started when it is made. Times are read from `clock`, in seconds."""
    SHARE = 0.8
    FLOOR_S = 10.0
    MUTATION_STEPS = 5

    def __init__(self, timeout_s: float, *, clock=time.monotonic, exempt: bool = False):
        self._clock = clock
        self._exempt = exempt
        started = clock()
        self._end = started + timeout_s
        self._line = started + timeout_s * self.SHARE
        # The margin is subtracted, not multiplied by 0.2, so 100 s gives exactly 4 s a step.
        self._cap = min(60.0, (timeout_s - timeout_s * self.SHARE) / self.MUTATION_STEPS)

    def late(self) -> bool:
        """Past the 80% line; never when exempt."""
        return not self._exempt and self._clock() >= self._line

    def left(self) -> float:
        """Seconds to the worker timeout, never below 0."""
        return max(0.0, self._end - self._clock())

    def remaining(self) -> float:
        """Seconds to the 80% line, never below 0; an exempt clock has no line, so it is `left()`."""
        return self.left() if self._exempt else max(0.0, self._line - self._clock())

    def patience(self, cap_s: float) -> float:
        """A read step's timeout: up to `cap_s`, at least `FLOOR_S` near the line, never past the worker timeout,
        never under 1 s."""
        return max(1.0, min(cap_s, max(self.FLOOR_S, self.remaining()), self.left()))

    def mutation_cap(self) -> float:
        """Each mutating step's timeout: min(60 s, the 20% margin / MUTATION_STEPS), fixed for the stage run."""
        return self._cap

    def can_mutate(self) -> bool:
        """Whether a removal may begin now: not past the 80% line, and `left()` covers MUTATION_STEPS capped steps, so
        it ends before the worker timeout; always when exempt. A stage that omits its own `late()` check still keeps."""
        return self._exempt or (not self.late() and self.left() >= self.MUTATION_STEPS * self._cap)

    def within(self, budget_s: float) -> 'StageClock':
        """A clock whose line is `budget_s` from now, or the stage's own line if that comes first; the worker timeout
        and the mutation cap stay the stage's. A per-root budget binds even under an exempt clock."""
        inner = copy.copy(self)
        inner._exempt = False
        inner._line = min(self._clock() + budget_s, self._end if self._exempt else self._line)
        return inner


class StageAbandoned(BaseException):
    """The reader has gone, so the worker has lost its authority: the stage ends. A BaseException, so that no row's
    `except Exception`, nor its `except OSError` (BrokenPipeError is one), reads it as a row failure."""


def _line(**value) -> bytes:
    # A row is whole on one line; ensure_ascii, so a non-UTF-8 name (os.fsdecode's surrogates) crosses and comes back.
    return json.dumps(dict(v=1, **value), ensure_ascii=True, separators=(',', ':')).encode('ascii') + b'\n'


def _largest(rows, limit):
    return heapq.nlargest(limit, rows, key=lambda r: (r.get('bytes', 0), r['path']))


class StageRun:
    """What one stage says, as lines on the worker's stdout. `add` is the only way a row enters, and a row added is
    settled; `checkpoint` sends what was settled since the last one, and runs between removal units only.

    `root_of(row)` is compact's root step, passed in by `disk_side`: the root a row groups under, or None for a row
    that always crosses whole. `limit` is `disk_inventory.LIMIT`. `add` caps as `compact` does, so `read_rows` gives
    the rows `compact` gives, and the worker holds no more than `compact` keeps."""

    def __init__(self, request: StageRequest, clock: StageClock, *, out: BinaryIO,
                 root_of: Callable[[dict], str | None], limit: int):
        self.request, self.clock = request, clock
        self.evidence = None                 # set by disk_side; disk_stage does not import disk_evidence
        self.scope_errors: list[dict] = []
        self._out, self._root_of, self._limit = out, root_of, limit
        self._pending: list[bytes] = []
        self._whole: dict[tuple, int] = {}   # (reason_code, root, action) -> whole rows added
        self._more: dict[tuple, dict] = {}   # the running tally of a group's rows past `limit`
        self._changed: dict[tuple, None] = {}
        self._reclaimed = self._sent_reclaimed = 0
        self._stopped = False

    @property
    def stopped(self) -> bool:
        """`stop` has run."""
        return self._stopped

    @property
    def reclaimed_bytes(self) -> int:
        """The running total of `reclaimed`."""
        return self._reclaimed

    def add(self, row: dict) -> None:
        """The only way in: the row is settled now, so later changes to the caller's dict do not cross."""
        row = copy.deepcopy(row)
        for key in ('dirty', 'ignored', 'excluded'):
            if key in row:
                row[key + '_count'] = len(row[key])
                row[key] = row[key][:self._limit]
        root = self._root_of(row)
        if root is None:
            self._pending.append(_line(row=row))
            return
        group = (row['reason_code'], root, row['action'])
        if self._whole.get(group, 0) < self._limit:
            self._whole[group] = self._whole.get(group, 0) + 1
            self._pending.append(_line(row=row, root=root))
            return
        more = self._more.setdefault(group, dict(reason_code=group[0], root=root, action=group[2], count=0, files=0,
                                                 bytes=0, age_s=row.get('age_s', 0), sample=[]))
        more['count'] += 1
        more['files'] += row.get('files', 0)
        more['bytes'] += row.get('bytes', 0)
        more['age_s'] = min(more['age_s'], row.get('age_s', 0))
        more['sample'] = _largest(more['sample'] + [row], self._limit)
        self._changed[group] = None

    def error(self, row: dict) -> None:
        self._pending.append(_line(error=row))

    def reclaimed(self, count: int) -> None:
        """Bytes that actually went, added as each path goes."""
        self._reclaimed += count

    def checkpoint(self) -> None:
        """Send everything settled since the last checkpoint. A failed write raises StageAbandoned."""
        lines = self._pending + [_line(more=self._more[group]) for group in self._changed]
        if self._reclaimed != self._sent_reclaimed:
            lines.append(_line(reclaimed=self._reclaimed))
        if not lines:
            return
        try:
            self._out.write(b''.join(lines))
            self._out.flush()
        except (OSError, ValueError) as error:          # a closed pipe raises ValueError
            raise StageAbandoned('the stage lost its reader: ' + str(error)) from error
        self._pending, self._changed, self._sent_reclaimed = [], {}, self._reclaimed

    def stop(self, reason: str, machine, left, later) -> None:
        """The one deadline row: the worktrees in `left` not reached, and the repos in `later` after them. An item, not
        an error, so they are left alone and every removal is re-proven; the run is now partial. `machine` None is a
        stage with no repo in hand, and the row names the drive. A second call adds nothing."""
        if self._stopped:
            return
        self._stopped = True
        path = self.request.drive['path'] if machine is None else machine['repo']
        self.add(dict(path=str(path), action='report', reason_code=reason, owner=None, files=0, age_s=0, bytes=0,
                      count=len(left) + len(later), worktrees=[e['worktree'] for e in left],
                      repos=[m['repo'] for m in later]))
        self.checkpoint()

    def finish(self, **extra) -> None:
        """The scope errors, after the stage's own as today, then the `end` line, carrying `extra`."""
        for row in self.scope_errors:
            self.error(row)
        self.checkpoint()
        self._pending.append(_line(end=dict(reclaimed_bytes=self._reclaimed, partial=self._stopped, **extra)))
        self.checkpoint()


@dataclass(frozen=True)
class StageAnswer:
    items: list[dict]
    errors: list[dict]
    reclaimed_bytes: int
    peak_intermediate_bytes: int
    budget: dict | None
    complete: bool       # an `end` line arrived; `cleanup_call` clears it on a non-zero exit
    partial: bool        # cut off: not complete, or `end` says a deadline was reached


_KINDS = ('row', 'error', 'more', 'reclaimed', 'end')


def _number(value) -> bool:
    return type(value) in (int, float)


def _grouped(row) -> bool:
    """A row `read_rows` groups or samples: it indexes these keys and sums these numbers."""
    return (isinstance(row, dict) and all(isinstance(row.get(key), str) for key in ('path', 'reason_code', 'action'))
            and all(_number(row[key]) for key in ('files', 'bytes', 'age_s') if key in row))


def _shaped(kind: str, line: dict) -> bool:
    value = line[kind]
    if kind == 'row':
        return isinstance(value, dict) and ('root' not in line or isinstance(line['root'], str) and _grouped(value))
    if kind == 'error':
        return isinstance(value, dict)
    if kind == 'reclaimed':
        return type(value) is int
    if kind == 'more':
        return (isinstance(value, dict) and type(value.get('count')) is int
                and all(isinstance(value.get(key), str) for key in ('reason_code', 'root', 'action'))
                and all(_number(value.get(key)) for key in ('files', 'bytes', 'age_s'))
                and isinstance(value.get('sample'), list) and all(_grouped(row) for row in value['sample']))
    return (isinstance(value, dict) and type(value.get('partial', False)) is bool
            and all(type(value[key]) is int for key in ('reclaimed_bytes', 'peak_intermediate_bytes') if key in value)
            and isinstance(value.get('budget'), (dict, type(None))))


def _parse(raw: bytes) -> tuple[str, dict]:
    """One complete line, or a plain ValueError: `cleanup_call` reads that as a worker failure, and lets a UnicodeError
    or any other type through (disk_runtime.py:115). The wire is ASCII, as `_line` writes it."""
    try:
        text = raw.decode('ascii')
        line = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError('not a stage line: not ASCII JSON') from None
    kinds = [kind for kind in _KINDS if kind in line] if isinstance(line, dict) else []
    if len(kinds) != 1 or type(line.get('v')) is not int or line['v'] != 1 or not _shaped(kinds[0], line):
        raise ValueError('not a stage line: ' + text[:200])
    return kinds[0], line


def read_rows(output: bytes) -> StageAnswer:
    """A stage's answer from its lines. A partial last line is ignored; a malformed complete line raises ValueError.
    Rows group by their stamped root as `compact` groups them, and a group with a `more` tally crosses as one row."""
    *lines, _partial = output.split(b'\n')
    items, grouped, more, errors, reclaimed, end = [], {}, {}, [], 0, None
    for raw in lines:
        kind, line = _parse(raw)
        value = line[kind]
        if kind == 'row' and 'root' in line:
            grouped.setdefault((value['reason_code'], line['root'], value['action']), []).append(value)
        elif kind == 'row':
            items.append(value)
        elif kind == 'error':
            errors.append(value)
        elif kind == 'more':
            more[(value['reason_code'], value['root'], value['action'])] = value     # a running total: the last stands
        elif kind == 'reclaimed':
            reclaimed = value
        else:
            end = value
    for (reason, root, action), rows in grouped.items():
        tally = more.get((reason, root, action))
        if tally is None:
            items.extend(rows)
            continue
        # A tally starts only once `limit` whole rows were added, so their number is the sample's size.
        items.append(dict(path=root, root=root, action=action, reason_code=reason, count=len(rows) + tally['count'],
                          files=sum(r.get('files', 0) for r in rows) + tally['files'],
                          bytes=sum(r.get('bytes', 0) for r in rows) + tally['bytes'],
                          age_s=min([r.get('age_s', 0) for r in rows] + [tally['age_s']]), owner=None,
                          sample=_largest(rows + tally['sample'], len(rows))))
    complete = end is not None
    end = end or {}
    return StageAnswer(items=items, errors=errors,
                       reclaimed_bytes=end.get('reclaimed_bytes', 0) if complete else reclaimed,
                       peak_intermediate_bytes=end.get('peak_intermediate_bytes', 0), budget=end.get('budget'),
                       complete=complete, partial=not complete or bool(end.get('partial')))
