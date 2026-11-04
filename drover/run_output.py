"""What a run's output is judged against, as a record that survives the ledger.

The base is the worktree's HEAD as the run begins, not the tip of main: a reused worktree can sit on an older main,
and the check is that the worker's commits descend from where this run started. Scope is measured from the fork
point with the default branch (Pipeline.scope_changed), so earlier runs' commits stay covered.

The stored shape is a dict of base, scope_files and worktree. Runs started by older code may have stored only
{'base': ...}, and recovery adopts them, so from_event reads both. This module imports nothing from Drover."""
from dataclasses import dataclass


@dataclass(frozen=True)
class RunOutput:
    base: str
    scope_files: tuple[str, ...] = ()
    worktree: str | None = None

    def to_event(self) -> dict:
        return {'base': self.base, 'scope_files': list(self.scope_files), 'worktree': self.worktree}

    @classmethod
    def from_event(cls, value: dict | None) -> 'RunOutput | None':
        if not value or not value.get('base'):
            return None
        return cls(value['base'], tuple(value.get('scope_files') or ()), value.get('worktree'))

    def owns(self, worktree: str | None) -> bool:
        """True when the task still has the worktree this run started in."""
        return worktree == self.worktree
