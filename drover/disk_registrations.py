"""Worktree registrations kept out of the other side's prune, and lost ones re-made without touching a file.

Windows git cannot open a Linux path, so a `git worktree prune` from Windows drops the registration of every
unlocked WSL worktree, leaving a directory whose `.git` file points at an admin directory that is gone. Prevent: the
repo never expires prunable entries and every worktree carries a lock naming its side. Repair: such a directory is
matched to a branch by name and by content, and only then registered again; anything else is reported for the owner.
Nothing here removes or rewrites a working file."""
import os
import posixpath
import re
import tempfile
import uuid
from pathlib import Path

from . import placement
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


def candidate(repo, name):
    """The local branch named like the directory: `drover-T1` is `drover-T1` or `drover/T1`."""
    for branch in dict.fromkeys((name, name.replace('-', '/', 1))):
        if placement.git(repo, 'rev-parse', '--verify', '--quiet', 'refs/heads/' + branch, timeout=60).returncode == 0:
            return branch
    return None


def judge(machine, path, listed):
    """('repair', branch, '') for a proven match, ('unproven', None, reason) otherwise."""
    repo = machine['repo']
    branch = candidate(repo, path.name)
    if branch is None:
        return 'unproven', None, f'no branch named like {path.name}'
    for entry in listed:
        if entry.get('branch') == 'refs/heads/' + branch:
            return 'unproven', branch, f'branch {branch} is checked out at {entry["worktree"]}'
    bad, total = differing(repo, path, 'refs/heads/' + branch)
    if total == 0 or bad * 100 > THRESHOLD * total:
        return 'unproven', branch, f'branch {branch} does not match its content ({bad} of {total} files differ; limit {THRESHOLD}%)'
    return 'repair', branch, f'{bad} of {total} files differ from {branch}'


def lost(machine, listed):
    """Directories under this side's worktree roots whose `.git` file names an admin directory of this repo that is
    gone, or that no longer points back at the directory (an interrupted registration). Returns {path: target}."""
    common, found = common_dir(machine), {}
    registered = {placement.cleanup_parts(placement.native(e['worktree'])) for e in listed}
    for root in worktree_roots(machine, this_side=True):
        if not root.is_dir() or not safe_path(root):
            continue
        for path in sorted(root.iterdir()):
            dotgit = path / '.git'
            try:
                if path.is_symlink() or not path.is_dir() or not dotgit.is_file():
                    continue
                first = dotgit.read_text(encoding='utf-8', errors='replace').splitlines()[0]
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
            found[str(path)] = target
    return found


def report(request, machine, listed, found, store):
    """Decide each lost directory; on apply, register the proven ones. Dry runs only describe."""
    rows = []
    for text, target in found.items():
        path = Path(text)
        row = item(path, request, 'lost_registration', repo=machine['repo'], name=machine['name'])
        try:
            if target.exists():
                verdict, branch, why = 'repair', None, 'interrupted registration; pointers are rewritten'
            else:
                verdict, branch, why = judge(machine, path, listed)
        except (OSError, ValueError) as error:
            verdict, branch, why = 'unproven', None, f'could not be matched: {error}'
        row.update(branch=branch, detail=why)
        if verdict != 'repair':
            row.update(action='keep', reason_code='lost_registration_unproven')
        elif not request['apply']:
            row.update(action='report')
        else:
            try:
                reregister(request, machine, path, target, branch, store)
                row.update(action='reregistered')
            except (OSError, ValueError, RuntimeError) as error:
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
            os.replace(place / '.git', path / '.git')
            moved = True
        git(repo, 'worktree', 'repair', str(path))
        admin = Path(placement.native(gitdir_at(path)))
        if not (admin / 'index').exists():
            git(path, 'reset', '-q')
        if not any(placement.cleanup_parts(e['worktree']) == placement.cleanup_parts(path) for e in _entries(repo)):
            raise ValueError('registration did not take')
        store.append(dict(record, phase='completed'))
    except (OSError, ValueError, RuntimeError):
        if not moved and place.exists():
            placement.git(repo, 'worktree', 'remove', '-f', '-f', str(place), timeout=60)
        store.append(dict(record, phase='failed'))
        raise
    finally:
        for leftover in (place, holder):    # empty by now; rmdir never removes content
            try:
                leftover.rmdir()
            except OSError:
                pass


def _entries(repo):
    from .disk_worktrees import entries
    return entries(repo)
