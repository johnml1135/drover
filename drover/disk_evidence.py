"""One stage run's evidence, held once.

Slice 5 landed this holding the inventory, the listings, and the process proof with its report rows; slice 6a adds
the reads, each git call taking the stage clock's patience, and the fresh proofs. A listing selects and a first-pass
value selects: neither authorises a deletion. Only `FreshRegistrations` and `FreshProcesses` are proof at removal, and
only `fresh_*` here make them, each a new read. A failed read is unknown: it raises, and the stage keeps the row.
"""
import hashlib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import git_client, git_worktrees, ownership, placement, repos
from .disk_inventory import Inventory, safe_path
from .disk_stage import StageClock, StageRequest

_TOKEN = object()       # held here alone: no type checker runs, so each fresh proof's constructor checks it


class _Fresh:
    def __post_init__(self):
        if self._token is not _TOKEN:
            raise TypeError(f'{type(self).__name__} is made only by StageEvidence.fresh_*, read now')


@dataclass(frozen=True)
class FreshRegistrations(_Fresh):
    """The fresh proof of the registrations. Removal's final check takes no other kind."""
    refused: tuple[git_worktrees.Refusal, ...]      # empty means nothing objected
    overlaps: bool          # the target nests in, or holds, another place in the proof's reach
    _token: object = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class FreshProcesses(_Fresh):
    """A process proof taken now, with the same constructor rule."""
    proof: object           # placement's ProcessProof
    _token: object = field(default=None, repr=False, compare=False)


def overlaps(target, own, reach) -> bool:
    """Whether `target` nests in, or holds, a place in `reach` (cleanup_parts tuples) other than `own`'s. Registrations
    protect descendants even when their pointer or folder is broken. This never lists."""
    target_parts, own_parts = placement.cleanup_parts(str(target)), placement.cleanup_parts(str(own))
    return any(other != own_parts and (other[:len(target_parts)] == target_parts or target_parts[:len(other)] == other)
               for other in reach)


# Any failure is the ValueError every disk tuple holds: git's stderr, or why it did not end.
def _out(repo, *args, env=None, timeout_s=git_client.NORMAL):
    return git_client.out(str(repo), *map(str, args), timeout_s=timeout_s, env=env,
                          error=lambda f: ValueError(str(f) if f.why == 'timeout' else f.err.strip() or 'git command failed'))


def _task_facts(snapshot, path, tag):
    """(live, terminal) for `path` and its tag against a fresh task snapshot, or None when there is none."""
    if snapshot is None:
        return bool(tag and tag['owner'].startswith('drover:')), False
    live = any(placement.cleanup_parts(p) == placement.cleanup_parts(path)
               for p in snapshot.get('active_worktrees', []))
    terminal = False
    found = False
    for task in snapshot.get('tasks', []):
        owns = bool(tag and tag['owner'] == 'drover:' + str(task.get('id')))
        at_path = task.get('worktree') and placement.cleanup_parts(task['worktree']) == placement.cleanup_parts(path)
        if owns or at_path:
            live |= task.get('terminal') is not True or task.get('live') is True
        if owns:
            found = True
            terminal |= task.get('terminal') is True and task.get('live') is False
    if tag and tag['owner'].startswith('drover:') and not found:
        live = True
    return live, terminal


class StageEvidence:
    """What one stage run knows. `processes` binds at definition: a test passes its own, as
    `partial(StageEvidence, processes=fake)` in place of `disk_side.StageEvidence`. `prove` left as None is
    `git_worktrees.prove` looked up at each proof, so a test patching it there reaches every stage's proof."""

    def __init__(self, request: StageRequest, clock: StageClock, *, listings=git_worktrees.Listings,
                 prove=None, processes=placement.cleanup_processes):
        self._request = request
        self._clock = clock
        self._inventory = Inventory(request.now, request.side or placement.cleanup_side(), request.drive['path'],
                                    request.state_dir, request.inventory_id)
        # Each lazy listing takes its patience when it starts, so the clamp to the worker timeout applies. The Listings
        # change as this stage removes and repairs, so they never leave this object.
        self._listings = listings([machine['repo'] for machine in request.repos],
                                  timeout_s=lambda: clock.patience(git_worktrees.LISTING_TIMEOUT_S))
        self._prove = prove
        self._query = processes
        self._proof = self._regenerable = None
        # Every proof's diagnostics, the first pass's and each fresh one's, by (pid, name), and its query failures.
        self._diagnostics, self._failures = {}, []

    @property
    def inventory(self) -> Inventory:
        return self._inventory

    def listed(self, repo: str) -> git_worktrees.Listing:
        """The first pass's listing of `repo`; its failure raised."""
        return self._listings.of(repo)

    def every_registered(self) -> tuple[tuple[str, ...], ...]:
        """cleanup_parts of every listed row in every repo; any repo's failure raised."""
        return self._listings.reach()

    def row(self, repo: str, path):
        """The listed row a removal is compared with; it never authorises one."""
        return self._listings.of(repo).row(path)

    def took(self, repo: str, path) -> None:
        """This stage's own `worktree remove` took this row."""
        self._listings.removed(repo, path)

    def repaired(self, repo: str, path) -> None:
        """This stage's own `reregister` added this pointer."""
        self._listings.repaired(repo, path)

    def processes(self, *, regenerable=False):
        """The first pass's process proof: one snapshot per stage run. `regenerable` asks for the one proof for
        regenerable output, an external build root's, which takes the budget treatment: the owner's process_ignore
        names clear a process whose executable cannot be read (placement_proof `_cleared`)."""
        if regenerable:
            if self._regenerable is None:
                self._regenerable = self._snapshot(budget=True)
            return self._regenerable
        if self._proof is None:
            self._proof = self._snapshot()
        return self._proof

    def fresh_processes(self, *, regenerable=False) -> FreshProcesses:
        """A process proof taken now, always a new snapshot, its diagnostics joining `process_rows`; `regenerable` as
        for `processes`."""
        return FreshProcesses(self._snapshot(budget=regenerable), _TOKEN)

    def fresh_registrations(self, repo: str, target, own, *, pointer: bool) -> FreshRegistrations:
        """The fresh proof of ruling 13, read now: `own`, the worktree, proven against this stage's listing in every
        repo, and whether `target` overlaps anything in the proof's reach. A failed listing in any repo raises."""
        proof = (self._prove or git_worktrees.prove)(self._listings, repo, str(own), pointer=pointer,
                                                     timeout_s=self._clock.patience(git_worktrees.PROOF_TIMEOUT_S))
        return FreshRegistrations(proof.refused, overlaps(target, own, proof.reach), _TOKEN)

    def _snapshot(self, *, budget=False):
        request = self._request
        return self._noted(self._query(
            paths=request.policy, state_dir=request.state_dir, inventory_id=request.inventory_id,
            stage=request.stage, drive=request.drive, budget=bool(request.budget) or budget))

    def _noted(self, proof):
        _, unreadable, failure = placement.proof_diagnostics(proof)
        self._diagnostics.update(((row['pid'], row['name']), row) for row in unreadable)
        if failure:
            self._failures.append(failure)
        return proof

    def process_rows(self) -> list[dict]:
        """The report rows of the proofs taken: processes out of scope, unreadable ones, and failed queries. None
        before the first pass's proof."""
        if self._proof is None:
            return []
        rows = []
        foreign = placement.proof_diagnostics(self._proof)[0]
        if foreign:
            rows.append(dict(action='report', reason_code='process_out_of_scope', count=foreign))
        unreadable = list(self._diagnostics.values())
        if unreadable:
            rows.append(dict(action='report', reason_code='process_visibility_unavailable', count=len(unreadable),
                             processes=unreadable))
        rows.extend(dict(action='report', **failure) for failure in self._failures)
        return rows

    def _read(self, repo, *args, env=None):
        """One read's git stdout, its timeout the clock's patience when it starts (section 6.1)."""
        return _out(repo, *args, env=env, timeout_s=self._clock.patience(git_client.NORMAL))

    def _ask(self, repo, *args):
        return git_client.ask(repo, *args, timeout_s=self._clock.patience(git_client.NORMAL), no=(1,))

    def fresh_snapshot(self) -> dict | None:
        """The task snapshot when it is recent enough to trust, else None."""
        snapshot = self._request.tasks or {}
        at = snapshot.get('at')
        return snapshot if type(at) in (float, int) and 0 <= self._request.now - at <= 900 else None

    def task_facts(self, path, tag) -> tuple[bool, bool]:
        """(live, terminal): whether a task holds `path` or its tag, and whether the tag's task is closed."""
        return _task_facts(self.fresh_snapshot(), path, tag)

    def measure(self, path, *, ignore_activity=(), strict=True) -> dict:
        """Strict safety facts. Only a report-only run, ignoring no activity, reuses the stage's inventory."""
        reuse = not self._request.apply and not ignore_activity
        inventory = self._inventory if reuse else Inventory(self._request.now, ignore_activity=ignore_activity)
        row = inventory.measure(path, strict=strict)
        return {key: row[key] for key in ('files', 'bytes', 'age_s')}

    def status(self, path, *, expand=True) -> tuple[list[str], list[str]]:
        """NUL-safe full status; expand ignored directories without following links."""
        raw = self._read(path, 'status', '--porcelain=v1', '-z',
                         '--untracked-files=all' if expand else '--untracked-files=normal', '--ignored=matching')
        fields = iter(raw.split('\0'))
        dirty, ignored = [], []
        for field in fields:
            if not field:
                continue
            code, name = field[:2], field[3:]
            target = Path(path) / name
            if code == '!!':
                # Keep empty directories and every descendant, including protected/link entries.
                def leaves(p):
                    if placement.cleanup_link(p):
                        return [str(p)]
                    if p.is_dir():
                        children = list(p.iterdir())
                        return [s for child in children for s in leaves(child)] or [str(p)]
                    return [str(p)]
                ignored.extend(leaves(target) if expand else [str(target)])
            else:
                dirty.append(str(target))
                if 'R' in code or 'C' in code:
                    next(fields, None)
        return dirty, ignored

    def head(self, path) -> str:
        return self._read(path, 'rev-parse', '--verify', 'HEAD').strip()

    def gitdir(self, path) -> str:
        return self._read(path, 'rev-parse', '--absolute-git-dir').strip()

    def tracked(self, path, relative=None) -> list[str]:
        args = ['ls-files', '-z'] + (['--', str(relative)] if relative is not None else [])
        return [name for name in self._read(path, *args).split('\0') if name]

    def merged(self, machine, head) -> bool:
        repo = machine['repo']
        base = self._read(repo, 'rev-parse', '--verify',
                          machine['remote'] + '/' + machine['default_branch'] + '^{commit}').strip()
        yes, unknown = git_client.Answer.YES, git_client.Answer.UNKNOWN
        ancestor = self._ask(repo, 'merge-base', '--is-ancestor', head, base)
        if ancestor.answer is yes:
            return True
        if ancestor.answer is unknown:
            raise ValueError('ancestry unavailable')
        cherry = self._read(repo, 'cherry', base, head).splitlines()
        if cherry and all(line.startswith('- ') for line in cherry):
            return True
        tree = self._ask(repo, 'merge-tree', '--write-tree', base, head)
        if tree.answer is unknown:
            raise ValueError('merge-tree unavailable')
        return tree.answer is yes and tree.out.splitlines()[0] == self._read(repo, 'rev-parse', base + '^{tree}').strip()

    def tag(self, path) -> dict | None:
        return ownership.read(str(path))

    def declarations(self, machine, path) -> list[str]:
        """The disposable paths declared for `machine`, as `outputs_at` reads them."""
        return outputs_at(machine, path)


def identity(path):
    """Fingerprint activity and identity, without walking links or trusting scan age."""
    values = []
    def visit(p):
        if placement.cleanup_link(p):
            raise ValueError('linked snapshot')
        info = p.stat()
        values.append((str(p.relative_to(path)), info.st_dev, info.st_ino, info.st_size,
                       info.st_mtime_ns, info.st_ctime_ns))
        if p.is_dir():
            for child in sorted(p.iterdir()):
                visit(child)
    visit(path)
    return hashlib.sha256(repr(values).encode()).hexdigest()


def registration_identity(entry):
    """Compare administrative evidence using canonical identities for its paths."""
    return {key: placement.cleanup_parts(value) if key in ('worktree', 'gitdir') and value else value
            for key, value in entry.items()}


def outputs_at(machine, path):
    """Only the committed default branch may authorize disposal, on every branch."""
    branch = machine.get('default_branch', 'main')
    if not isinstance(branch, str) or not branch.strip():
        raise ValueError('default_branch must be a non-empty git branch name')
    revision = 'refs/heads/' + branch.removeprefix('refs/heads/')
    committed = repos.load(machine, revision=revision, checkout=str(path))
    declared = repos.cleanup_config(committed)['disposable'] if committed['_adopted'] else []
    # The owner's local list is a second source, read from the machine registration alone.
    local = repos.local_disposable(machine)
    return declared + [p for p in local if p not in declared]


def snapshot(machine, path, *, evidence):
    """Prove an unlisted directory equals this repo's HEAD, using a disposable index and a fresh process proof. Each
    git read takes the stage clock's patience."""
    if not safe_path(path) or any(p.name == '.git' for p in path.rglob('*')):
        raise ValueError('independent or broken git metadata')
    if evidence.fresh_processes().proof.busy(str(path)):        # never `processes()`: R7 cannot see in here
        raise ValueError('snapshot busy')
    head = evidence._read(machine['repo'], 'rev-parse', '--verify', 'HEAD^{commit}').strip()
    with tempfile.TemporaryDirectory(prefix='disk-index-') as directory:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(directory) / 'index'))
        evidence._read(machine['repo'], 'read-tree', head, env=env)
        evidence._read(machine['repo'], '--work-tree=' + str(path), 'diff', '--exit-code', head, '--', env=env)
        others = evidence._read(machine['repo'], '--work-tree=' + str(path), 'ls-files', '--others', '-z', env=env)
        if others:
            raise ValueError('snapshot has untracked data')
    return head
