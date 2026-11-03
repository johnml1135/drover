"""What a stage is asked and how long it has, on both sides of the worker pipe.

`StageRequest` is the typed request; its wire keys are today's, so the worker reads what it reads now. `StageClock` is
the one clock of a stage run: an 80% line no removal starts past, a fixed cap for each mutating step in the margin
after it, and patience for read steps.
"""
import copy
import time
from dataclasses import asdict, dataclass, fields
from typing import Callable, Mapping

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
