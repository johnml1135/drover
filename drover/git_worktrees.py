"""One stage run's real listing of each repo. Git is the authority, and a repo it cannot list stays wholly unknown."""
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from . import placement, placement_paths

LISTING_TIMEOUT_S = 400.0       # 1.5 times the worst measured listing, motif's 265.9 s
EVIDENCE_TIMEOUT_S = 60         # every other evidence command keeps today's fixed timeout until slice 6a


class RegistrationsUnknown(OSError):
    """Git could not say what a repo has registered; the message is what it said."""


def _git(repo, *args, timeout_s, side):
    try:
        done = placement.git(str(repo), *args, timeout=timeout_s, side=side)
    except Exception as error:      # a timeout included; this module may not name `subprocess` (R1)
        raise RegistrationsUnknown(str(error)) from error
    if done.returncode:
        raise RegistrationsUnknown(done.stderr.strip() or 'git command failed')
    return done.stdout


@dataclass(frozen=True)
class Listing:
    repo: str
    rows: tuple[Mapping[str, str], ...]     # git's rows, the main checkout first

    def row(self, path) -> Mapping[str, str] | None:
        """The first row at `path`. Two admin directories can name one path, so rows are never keyed by it."""
        parts = placement_paths.cleanup_parts(path)
        return next((row for row in self.rows if placement_paths.cleanup_parts(row['worktree']) == parts), None)


def listing(repo: str, *, timeout_s: float, side: str | None = None) -> Listing:
    rows = []
    for block in _git(repo, 'worktree', 'list', '--porcelain', '-z', timeout_s=timeout_s, side=side).split('\0\0'):
        row = {}
        for field in block.split('\0'):
            key, _, value = field.partition(' ')
            if key:
                row[key] = value
        if 'worktree' in row:
            row['worktree'] = placement_paths.cleanup_path(row['worktree'])
            rows.append(row)
    return Listing(str(repo), tuple(rows))


class Listings:
    """One stage run's listings. Each repo is listed once, on first use, and its answer or failure is kept."""

    def __init__(self, repos: Sequence[str], *, timeout_s: Callable[[], float] = lambda: LISTING_TIMEOUT_S,
                 side: str | None = None):
        self._repos = {}
        for repo in repos:
            self._repos.setdefault(placement_paths.cleanup_parts(repo), repo)
        self._timeout_s, self._side = timeout_s, side
        self._listed, self._common, self._repaired = {}, {}, {}

    def _once(self, answers, repo, read):
        key = placement_paths.cleanup_parts(repo)
        spelling = self._repos.setdefault(key, repo)
        if key not in answers:
            try:
                answers[key] = read(spelling)
            except RegistrationsUnknown as error:
                answers[key] = error
        if isinstance(answers[key], RegistrationsUnknown):
            raise answers[key]
        return answers[key]

    def of(self, repo: str) -> Listing:
        """This repo's listing, or its failure raised."""
        return self._once(self._listed, repo, lambda spelled: listing(spelled, timeout_s=self._timeout_s(), side=self._side))

    def every(self) -> tuple[Listing, ...]:
        """Every repo's listing. All are listed first, then the first failure is raised."""
        found, failures = [], []
        for repo in tuple(self._repos.values()):
            try:
                found.append(self.of(repo))
            except RegistrationsUnknown as error:
                failures.append(error)
        if failures:
            raise failures[0]
        return tuple(found)

    def common_dir(self, repo: str) -> str:
        return self._once(self._common, repo, lambda spelled: placement_paths.native(_git(
            spelled, 'rev-parse', '--path-format=absolute', '--git-common-dir',
            timeout_s=EVIDENCE_TIMEOUT_S, side=self._side).strip()))

    def removed(self, repo: str, path) -> None:
        """This stage's own `worktree remove` took this row, the first at its path as in git. A repo not listed yet
        has nothing to drop: its listing will come after the removal."""
        key = placement_paths.cleanup_parts(repo)
        kept = self._listed.get(key)
        gone = kept.row(path) if isinstance(kept, Listing) else None
        if gone is not None:
            self._listed[key] = replace(kept, rows=tuple(row for row in kept.rows if row is not gone))

    def repaired(self, repo: str, path) -> None:
        """This stage's own `reregister` added this pointer. No row is added: git did not list it."""
        self._repaired.setdefault(placement_paths.cleanup_parts(repo), set()).add(placement_paths.cleanup_parts(path))

    def repaired_pointers(self, repo: str) -> frozenset[tuple[str, ...]]:
        return frozenset(self._repaired.get(placement_paths.cleanup_parts(repo), ()))
