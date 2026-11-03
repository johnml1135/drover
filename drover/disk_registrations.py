"""Worktree registrations kept out of the other side's prune, and lost ones re-made without touching a file.

Windows git cannot open a Linux path, so a `git worktree prune` from Windows drops the registration of every
unlocked WSL worktree, leaving a directory whose `.git` file points at an admin directory that is gone. Prevent: the
repo never expires prunable entries and every worktree carries a lock naming its side. Repair: such a directory is
matched to a branch by name and by content, and only then registered again; anything else is reported for the owner.
Nothing here removes or rewrites a working file."""
import os
import posixpath
import re
import subprocess
import tempfile
import uuid
from pathlib import Path

from . import durable, git_worktrees, placement
from .disk_paths import item, safe_path, worktree_roots
from .disk_worktrees import git, gitdir_at

THRESHOLD = 10   # percent of the branch's tracked files that may differ from the directory
WIN = re.compile(r'^[A-Za-z]:[\\/]')
MNT = re.compile(r'^/mnt/[a-zA-Z](/|$)')


def owning_side(admin, pointer):
    """The side that owns the worktree an admin directory's `gitdir` pointer names: a drive letter (or the /mnt mount
    of one) is Windows, any other path is WSL. A relative pointer is resolved lexically from the admin directory
    spelled as WSL sees it, so a pointer written by WSL git into ext4 is not mistaken for a drive path."""
    text = pointer.strip()
    if WIN.match(text):
        return 'windows'
    if not text.startswith('/'):
        base = placement.to_wsl_path(str(admin)).replace('\\', '/')
        text = posixpath.normpath(posixpath.join(base, text))
    return 'windows' if MNT.match(text) else 'wsl'


def common_dir(machine):
    return Path(placement.native(git(machine['repo'], 'rev-parse', '--path-format=absolute', '--git-common-dir').strip()))


def pointer_at(admin):
    """(raw pointer text, worktree directory it names) for one admin directory, or None."""
    try:
        raw = (admin / 'gitdir').read_text(encoding='utf-8').strip()
    except OSError:
        return None
    where = raw if os.path.isabs(raw) or WIN.match(raw) or raw.startswith('/') else os.path.normpath(os.path.join(admin, raw))
    return raw, Path(placement.native(where)).parent


def protect(request, machine):
    """Prevent: the repo's prune expiry is `never` and every worktree is locked with its side (or a note already there).
    A report changes nothing. The lock file is written directly, as `git worktree lock --reason` does: Windows git
    cannot address a Linux worktree by its path. A worktree this side can see is gone is left prunable."""
    repo, out, apply = machine['repo'], [], request['apply']
    common = common_dir(machine)
    current = placement.git(repo, 'config', '--get', 'gc.worktreePruneExpire', timeout=60).stdout.strip()
    if current != 'never':
        row = item(repo, request, 'prune_expiry_unset', repo=repo, name=machine['name'], value=current or None)
        row.update(action='report')
        if apply:
            try:
                git(repo, 'config', 'gc.worktreePruneExpire', 'never')
                row.update(action='configured')
            except ValueError as error:
                row.update(action='keep', error=str(error))
        out.append(row)
    for admin in sorted((common / 'worktrees').glob('*')):
        found = pointer_at(admin)
        if found is None or (admin / 'locked').exists():
            continue
        raw, path = found
        side = owning_side(admin, raw)
        if side == placement.cleanup_side() and not path.exists():
            continue
        row = item(path, request, 'unlocked_worktree', repo=repo, name=machine['name'], lock=side)
        row.update(action='report')
        if apply:
            try:
                with open(admin / 'locked', 'x', encoding='utf-8') as stream:
                    stream.write(side)
                row.update(action='locked')
            except FileExistsError:
                continue
            except OSError as error:
                row.update(action='keep', error=str(error))
        out.append(row)
    return out


def differing(repo, path, ref):
    """(files that differ, tracked files): the branch's tree against the directory, using a disposable index."""
    with tempfile.TemporaryDirectory(prefix='disk-index-') as directory:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(directory) / 'index'))
        git(repo, 'read-tree', ref, env=env)
        total = [n for n in git(repo, 'ls-files', '-z', env=env).split('\0') if n]
        changed = [n for n in git(repo, '--work-tree=' + str(path), 'diff', '--name-only', '-z', env=env).split('\0') if n]
    return len(changed), len(total)


def candidate(repo, name, rel=None):
    """The local branch named like the directory: its path under the worktree root (`hx/api-names`), or its last
    component, which `drover-T1` also reads as `drover/T1`."""
    for branch in dict.fromkeys(filter(None, (rel, name, name.replace('-', '/', 1)))):
        if placement.git(repo, 'rev-parse', '--verify', '--quiet', 'refs/heads/' + branch, timeout=60).returncode == 0:
            return branch
    return None


SKIP = {'.git', 'node_modules', 'obj', 'bin', 'target', '.venv', '__pycache__'}


def newest_files(path, count=5):
    """The most recently written files of a directory, build output and `.git` pruned, links not followed."""
    found = []
    for folder, names, files in os.walk(path):
        names[:] = [n for n in names if n not in SKIP and not os.path.islink(os.path.join(folder, n))]
        for name in files:
            full = os.path.join(folder, name)
            if not os.path.islink(full):
                found.append((os.stat(full).st_mtime, full))
    return [os.path.relpath(full, path).replace(os.sep, '/') for _, full in sorted(found, reverse=True)[:count]]


def suggest(repo, path):
    """Report-only: the one local branch alone holding a newest file of the directory exactly as committed (the skill's
    SUGGEST ranked trees; this reads one blob per file and costs one `git log` each, at most five). Never applied."""
    for name in newest_files(path):
        blob = placement.git(repo, 'hash-object', '--path=' + name, '--', str(path / name), timeout=60).stdout.strip()
        commit = placement.git(repo, 'log', '--branches', '--format=%H', '-1', '--raw', '--find-object=' + blob,
                              timeout=60).stdout.split()[:1]
        commit = commit[0] if commit else ''
        if not commit:
            continue
        held = placement.git(repo, 'branch', '--contains', commit, '--format=%(refname:short)', timeout=60).stdout.split()
        if len(held) == 1:
            return held[0]
    return None


def judge(machine, path, listed, rel=None):
    """('repair', branch, why) for a proven match, ('unproven', branch, why) otherwise; a suggestion rides in why."""
    repo = machine['repo']
    branch = candidate(repo, path.name, rel)
    if branch is None:
        maybe = suggest(repo, path)
        note = f'; suggestion: {maybe} holds its newest file (not applied)' if maybe else ''
        return 'unproven', None, f'no branch named like {rel or path.name}{note}', maybe
    for entry in listed:
        if entry.get('branch') == 'refs/heads/' + branch:
            return 'unproven', branch, f'branch {branch} is checked out at {entry["worktree"]}', None
    bad, total = differing(repo, path, 'refs/heads/' + branch)
    if total == 0 or bad * 100 > THRESHOLD * total:
        return 'unproven', branch, f'branch {branch} does not match its content ({bad} of {total} files differ; limit {THRESHOLD}%)', None
    return 'repair', branch, f'{bad} of {total} files differ from {branch}', None


DEPTH = 4   # `hx/api-names` is two below the root; deeper trees are not worktree layouts


def checkouts(root):
    """Directories below a worktree root that hold a `.git` file, found by walking down and stopping at the first
    one: each is a (path, path relative to the root). Links, `.git` and build output are not entered, so a tree of
    hundreds of worktrees costs one listing per folder above them."""
    stack = [(root, 0)]
    while stack:
        folder, depth = stack.pop()
        try:
            children = sorted(os.scandir(folder), key=lambda e: e.name, reverse=True)
        except OSError:
            continue
        for entry in children:
            if entry.name in SKIP or entry.name.startswith('.drover-repair') or entry.is_symlink():
                continue
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                marker = os.path.join(entry.path, '.git')
                if os.path.isfile(marker) and not os.path.islink(marker):
                    yield Path(entry.path), Path(entry.path).relative_to(root).as_posix()
                elif not os.path.lexists(marker) and depth + 1 < DEPTH:
                    stack.append((Path(entry.path), depth + 1))
            except OSError:
                continue


def lost(machine, listed):
    """Directories under this side's worktree roots whose `.git` file names an admin directory of this repo that is
    gone, or that no longer points back at the directory (an interrupted registration).
    Returns {path: (admin directory, path relative to its root)}."""
    common, found = common_dir(machine), {}
    registered = {placement.cleanup_parts(placement.native(e['worktree'])) for e in listed}
    for root in worktree_roots(machine, this_side=True):
        if not root.is_dir() or not safe_path(root):
            continue
        for path, rel in checkouts(root):
            try:
                first = (path / '.git').read_text(encoding='utf-8', errors='replace').splitlines()[0]
            except (OSError, IndexError):
                continue
            if not first.startswith('gitdir:'):
                continue
            raw = first[7:].strip()
            target = Path(os.path.normpath(raw if os.path.isabs(raw) else os.path.join(path, raw)))
            if target.parent.name != 'worktrees' or placement.cleanup_parts(target.parent.parent) != placement.cleanup_parts(common):
                continue
            if placement.cleanup_parts(path) in registered:
                continue
            if target.exists():
                back = pointer_at(target)
                if back is not None and placement.cleanup_parts(back[1]) == placement.cleanup_parts(path):
                    continue       # fully registered
            found[str(path)] = (target, rel)
    return found


def report(request, machine, listed, found, store):
    """Decide each lost directory; on apply, register the proven ones. Dry runs only describe."""
    rows = []
    for text, (target, rel) in found.items():
        path = Path(text)
        row = item(path, request, 'lost_registration', repo=machine['repo'], name=machine['name'])
        try:
            if target.exists():
                verdict, branch, why, maybe = 'repair', None, 'interrupted registration; pointers are rewritten', None
            else:
                verdict, branch, why, maybe = judge(machine, path, listed, rel)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            verdict, branch, why, maybe = 'unproven', None, f'could not be matched: {error}', None
        row.update(branch=branch, detail=why, suggestion=maybe)
        if verdict != 'repair':
            row.update(action='keep', reason_code='lost_registration_unproven')
        elif not request['apply']:
            row.update(action='report')
        else:
            try:
                reregister(request, machine, path, target, branch, store)
                row.update(action='reregistered')
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                row.update(action='keep', reason_code='lost_registration_failed', error=str(error))
        rows.append(row)
    return rows


def reregister(request, machine, path, target, branch, store):
    """Add the branch's registration under a temporary parent (so the admin directory is named after the directory),
    move its `.git` file over the orphan's, repair both pointers, and rebuild a missing index. The old `.git` file is
    kept under the state directory first and never overwritten."""
    repo = machine['repo']
    record = dict(v=1, kind='registration_repair', id=uuid.uuid4().hex, ts=request['now'], phase='planned',
                  path=str(path), repo=repo, branch=branch, side=placement.cleanup_side())
    backups = Path(request['state_dir']) / 'disk' / 'registration-backup'
    backups.mkdir(parents=True, exist_ok=True)
    holder = path.parent / ('.drover-repair-' + uuid.uuid4().hex[:8])
    place = holder / path.name
    moved = False
    store.append(record)
    try:
        saved = backups / f'{path.name}-{uuid.uuid4().hex[:8]}.git'
        with open(saved, 'x', encoding='utf-8') as stream:
            stream.write((path / '.git').read_text(encoding='utf-8'))
        if branch is not None:
            holder.mkdir()
            placement.add_worktree(repo, str(place), '--no-checkout', '--', str(place), branch, error=ValueError)
            durable.move(place / '.git', path / '.git', wait=durable.NO_WAIT)
            moved = True
        git(repo, 'worktree', 'repair', str(path))
        admin = Path(placement.native(gitdir_at(path)))
        if not (admin / 'index').exists():
            git(path, 'reset', '-q')
        if git_worktrees.listing(repo, timeout_s=git_worktrees.LISTING_TIMEOUT_S).row(path) is None:
            raise ValueError('registration did not take')
        request['_listings'].repaired(repo, path)
        store.append(dict(record, phase='completed'))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        try:
            if not moved and place.exists():
                placement.git(repo, 'worktree', 'remove', '-f', '-f', str(place), timeout=60)
        finally:
            store.append(dict(record, phase='failed'))
        raise
    finally:
        for leftover in (place, holder):    # empty by now; rmdir never removes content
            try:
                leftover.rmdir()
            except OSError:
                pass
