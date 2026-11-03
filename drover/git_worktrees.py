"""One stage run's real listing of each repo, and the fresh proof that nothing it said has moved.

Git is the authority, and a repo it cannot list stays wholly unknown. Only `prove` reads the admin files: it compares
them with the listing and can only refuse (ruling 13).
"""
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as PoolTimeout
from dataclasses import dataclass, replace
from functools import partial
import ntpath
import os
import posixpath
import re
import time
from typing import Literal, TypedDict

from . import placement, placement_paths

LISTING_TIMEOUT_S = 400.0       # 1.5 times the worst measured listing, motif's 265.9 s
EVIDENCE_TIMEOUT_S = 60         # every other evidence command keeps today's fixed timeout until slice 6a
READERS = 8
OBJECT_ID = re.compile(r'[0-9a-f]{40}(?:[0-9a-f]{24})?')
GARBAGE = ''                    # a `HEAD` that is neither a ref nor an object id: equal to no row's field

AdminFiles = TypedDict('AdminFiles', {'gitdir': str | None, 'HEAD': str | None, 'locked': str | None,
                                      'commondir': str | None, 'unreadable': tuple[str, ...]})


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


@dataclass(frozen=True)
class Refusal:
    repo: str
    worktree: str | None        # None when the pointer itself is unknown
    why: Literal['changed', 'unknown']
    detail: str                 # which file or field, for the row


@dataclass(frozen=True)
class Proof:
    refused: tuple[Refusal, ...]            # what refuses this removal, the target's own first; empty means nothing did
    reach: tuple[tuple[str, ...], ...]      # cleanup_parts of every listed row and every readable pointer


@dataclass(frozen=True)
class Admin:
    """One admin directory as `parse` reads it. None is missing or unreadable, and `unreadable` says which."""
    path: str
    pointer: str | None         # spelled as git on this side spells the row
    place: str | None           # the true place of a pointer git spells somewhere else (O3)
    head: str | None            # a ref, an object id, or GARBAGE
    locked: str | None
    commondir: str | None
    unreadable: tuple[str, ...]


def _resolved(admin, text, side):
    """A path as git spells it from `admin`: a relative one is lexical, and stops at the root."""
    if text.startswith('/') or (side == 'windows' and placement_paths.WIN_RE.match(text)):
        return text
    base = str(admin).replace('\\', '/')
    flavour = ntpath if placement_paths.WIN_RE.match(base) else posixpath
    return flavour.normpath(f'{base}/{text}').replace('\\', '/')


def _head(text):
    if text is None:
        return None
    text = text.strip()
    if text.startswith('ref:'):
        name = text.removeprefix('ref:').strip()
        return name if name.startswith('refs/') else GARBAGE
    return text if OBJECT_ID.fullmatch(text) else GARBAGE


def parse(admin: str, files: AdminFiles, *, side: str | None = None) -> Admin:
    """Pure. `side` is the side whose git reads the pointer: WSL git takes `H:/x` as relative, so its row lies under
    the admin directory, and the pointer's true place goes in `Proof.reach`."""
    side = side or placement_paths.cleanup_side()
    raw = (files['gitdir'] or '').rstrip().removesuffix('/.git')
    commondir = (files['commondir'] or '').strip()
    return Admin(path=admin, pointer=_resolved(admin, raw, side) if raw else None,
                 place=raw if side != 'windows' and placement_paths.WIN_RE.match(raw) else None,
                 head=_head(files['HEAD']), locked=None if files['locked'] is None else files['locked'].strip(),
                 commondir=_resolved(admin, commondir, side) if commondir else None, unreadable=files['unreadable'])


def _read_files(admin, names) -> AdminFiles:
    found, unreadable = {}, []
    for name in names:
        try:
            with open(os.path.join(admin, name), 'rb') as file:
                found[name] = file.read().decode('utf-8')
        except FileNotFoundError:
            pass
        except (OSError, UnicodeDecodeError):
            unreadable.append(name)
    if 'locked' in names and 'locked' not in found and 'locked' not in unreadable and not os.path.isdir(admin):
        unreadable.append('locked')     # not found because the directory went, which is not unlocked
    return dict(gitdir=found.get('gitdir'), HEAD=found.get('HEAD'), locked=found.get('locked'),
                commondir=found.get('commondir'), unreadable=tuple(unreadable))


def _admin_paths(common_dir):
    try:
        with os.scandir(os.path.join(common_dir, 'worktrees')) as entries:
            return [entry.path for entry in entries if entry.is_dir()]      # a stray file is ignored, as git ignores it
    except FileNotFoundError:
        return []


def _read_main(common_dir) -> AdminFiles:
    """The main `HEAD`. A `reftable` directory, or not being able to tell, is under `unreadable`."""
    read = _read_files(common_dir, ('HEAD',))
    try:
        os.lstat(os.path.join(common_dir, 'reftable'))
    except FileNotFoundError:
        return read
    except OSError:
        pass
    return dict(read, unreadable=(*read['unreadable'], 'reftable'))


def _in_time(deadline, jobs):
    """Each job's answer, on `READERS` threads; `TimeoutError` past `deadline`, a stuck read left behind."""
    pool = ThreadPoolExecutor(READERS)
    try:
        futures = {key: pool.submit(job) for key, job in jobs.items()}
        answers = {}
        for key, future in futures.items():
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError('admin read past its timeout')
            answers[key] = future.result(left)
        return answers
    except PoolTimeout:
        raise TimeoutError('admin read past its timeout') from None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _read_admin(common_dir: str, *, timeout_s: float) -> dict[str, AdminFiles]:
    """`gitdir`, `locked` and `commondir` of every admin directory by its path, and the main files under `common_dir`."""
    deadline = time.monotonic() + timeout_s
    paths = _in_time(deadline, {common_dir: partial(_admin_paths, common_dir)})[common_dir]
    jobs = {path: partial(_read_files, path, ('gitdir', 'locked', 'commondir')) for path in paths}
    return _in_time(deadline, {**jobs, common_dir: partial(_read_main, common_dir)})


def _named(admin):
    return placement_paths.cleanup_parts(admin.pointer)


def _names(admin, target):
    """Whether this pointer names `target`, as git spells it or in truth."""
    return any(place and placement_paths.cleanup_parts(place) == target for place in (admin.pointer, admin.place))


def _admins(common_dir, target, deadline, side):
    """The main files and every admin directory; only those at `target` read their `HEAD`."""
    read = _read_admin(common_dir, timeout_s=deadline - time.monotonic())
    main = read.pop(common_dir)
    admins = [parse(path, files, side=side) for path, files in read.items()]
    ours = [admin.path for admin in admins if admin.pointer and _named(admin) == target]
    heads = _in_time(deadline, {path: partial(_read_files, path, ('HEAD',)) for path in ours}) if ours else {}
    return main, [replace(admin, head=parse(admin.path, heads[admin.path], side=side).head)
                  if admin.path in heads else admin for admin in admins]


def _main_agrees(rows, head):
    if not rows or not head:
        return False
    return 'bare' in rows[0] or head == rows[0].get('branch', rows[0].get('HEAD'))


def _every_removal(found, common_dir, main, admins, *, target, repaired):
    """What in this repo refuses any removal. A row's own `HEAD`, lock text and tip are not read: ruling 14."""
    refused = []

    def refuse(worktree, detail):
        refused.append(Refusal(found.repo, worktree, 'unknown', detail))
    if 'reftable' in main['unreadable']:
        refuse(None, 'reftable')
    if not _main_agrees(found.rows, _head(main['HEAD'])):
        refuse(found.rows[0]['worktree'] if found.rows else None, 'main HEAD')
    named = [admin for admin in admins if admin.pointer]
    common = placement_paths.cleanup_parts(common_dir)
    for admin in named:
        if 'locked' in admin.unreadable:
            refuse(admin.pointer, 'locked')
        if not admin.commondir or placement_paths.cleanup_parts(admin.commondir) != common:
            refuse(admin.pointer, 'commondir')
    rows = Counter(placement_paths.cleanup_parts(row['worktree']) for row in found.rows[1:])
    pointers = Counter(_named(admin) for admin in named)
    for parts in rows - pointers:
        if parts != target:     # the target's own row is `_this_removal`'s
            refuse(None, f'row with no pointer: {"/".join(parts)}')
    for parts in pointers - rows - Counter(repaired):
        refuse(None, f'pointer the listing lacks: {"/".join(parts)}')
    return refused


def _differences(row, admin):
    differences = []
    if admin.head is None:
        differences.append(('unknown', 'HEAD'))
    elif admin.head != row.get('branch', row.get('HEAD')):
        differences.append(('changed', 'HEAD'))
    if 'locked' not in admin.unreadable and admin.locked != row.get('locked'):      # unreadable refuses every removal
        differences.append(('changed', 'locked'))
    return differences


def _this_removal(found, admins, target, worktree):
    """Each of the target's rows against an admin directory of its own: two can share a path, so none is keyed by it."""
    free = [admin for admin in admins if admin.pointer and _named(admin) == target]
    refused = []
    for row in found.rows[1:]:
        if placement_paths.cleanup_parts(row['worktree']) != target:
            continue
        differences = [_differences(row, admin) for admin in free]
        if [] in differences:
            free.pop(differences.index([]))
        elif differences:
            refused += [Refusal(found.repo, str(worktree), why, detail) for why, detail in differences[0]]
        elif all(admin.pointer for admin in admins):    # else an unreadable pointer may be this one: ruling 9's row
            refused.append(Refusal(found.repo, str(worktree), 'changed', 'pointer'))
    return refused


def _dot_git(worktree):
    """Whether <worktree>/.git is there; None when that cannot be told."""
    try:
        os.lstat(os.path.join(worktree, '.git'))
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return None
    return True


def prove(listings: Listings, repo: str, worktree: str, *, pointer: bool, timeout_s: float, side=None) -> Proof:
    """Read every repo's admin files now and compare with `listings`. `pointer` says whether <worktree>/.git should exist.

    A failed listing or tip raises. `Proof.refused` holds only what refuses this removal; `why` picks its row."""
    listings.every()            # a failed listing anywhere raises before anything is read
    own = listings.of(repo)
    side, deadline = side or placement_paths.cleanup_side(), time.monotonic() + timeout_s
    target, unlisted = placement_paths.cleanup_parts(worktree), own.row(worktree) is None
    present = _dot_git(worktree)
    first, refused, reach = [], [], set()
    if present is not pointer:
        first.append(Refusal(own.repo, str(worktree), 'changed' if present is not None else 'unknown', '.git'))
    for found in listings.every():
        reach.update(placement_paths.cleanup_parts(row['worktree']) for row in found.rows)
        common_dir = listings.common_dir(found.repo)
        try:
            main, admins = _admins(common_dir, target if found is own else None, deadline, side)
        except OSError as error:
            refused.append(Refusal(found.repo, None, 'unknown', str(error)))
            continue
        reach.update(_named(admin) for admin in admins if admin.pointer)
        reach.update(placement_paths.cleanup_parts(admin.place) for admin in admins if admin.place)
        if found is own:
            first += _this_removal(found, admins, target, worktree)
            refused += [Refusal(found.repo, None, 'unknown', f'no pointer: {admin.path}')
                        for admin in admins if not admin.pointer]
        if unlisted and any(_names(admin, target) for admin in admins):
            first.append(Refusal(found.repo, str(worktree), 'changed', 'pointer'))
        refused += _every_removal(found, common_dir, main, admins, target=target if found is own else None,
                                  repaired=listings.repaired_pointers(found.repo))
    refused = first + refused
    if not refused:             # last, and only if it can still matter: the one spawn a proof makes
        refused = [Refusal(own.repo, str(worktree), 'changed', 'resolved HEAD') for row in own.rows
                   if placement_paths.cleanup_parts(row['worktree']) == target and 'branch' in row
                   and _git(own.repo, 'rev-parse', '--verify', row['branch'], timeout_s=EVIDENCE_TIMEOUT_S,
                            side=side).strip() != row['HEAD']]
    return Proof(tuple(refused), tuple(sorted(reach)))
