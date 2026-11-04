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

from . import durable, git_client, git_worktrees, placement
from .disk_inventory import safe_path, worktree_roots
from .disk_stage import item
from .disk_evidence import _out
from .disk_removal import close_journal

OTHER_DRIVE = "suggestion on its own drive's pass"   # in place of a suggestion for a folder off the stage's drive
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
    return Path(placement.native(_out(machine['repo'], 'rev-parse', '--path-format=absolute', '--git-common-dir').strip()))


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
    expiry = git_client.ask(repo, 'config', '--get', 'gc.worktreePruneExpire', timeout_s=git_client.NORMAL, no=(1,))
    if expiry.answer is git_client.Answer.UNKNOWN:      # not "unset": the repo is an error and its orphans are not judged
        raise ValueError(f'git config failed: {expiry.why}')
    current = expiry.out.strip()
    if current != 'never':
        row = item(repo, 'prune_expiry_unset', repo=repo, name=machine['name'], value=current or None)
        row.update(action='report')
        if apply:
            try:
                _out(repo, 'config', 'gc.worktreePruneExpire', 'never')
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
        row = item(path, 'unlocked_worktree', repo=repo, name=machine['name'], lock=side)
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
        _out(repo, 'read-tree', ref, env=env)
        total = [n for n in _out(repo, 'ls-files', '-z', env=env).split('\0') if n]
        changed = [n for n in _out(repo, '--work-tree=' + str(path), 'diff', '--name-only', '-z', env=env).split('\0') if n]
    return len(changed), len(total)


def candidate(repo, name, rel=None):
    """The local branch named like the directory: its path under the worktree root (`hx/api-names`), or its last
    component, which `drover-T1` also reads as `drover/T1`."""
    for branch in dict.fromkeys(filter(None, (rel, name, name.replace('-', '/', 1)))):
        found = git_client.ask(repo, 'rev-parse', '--verify', '--quiet', 'refs/heads/' + branch, timeout_s=git_client.NORMAL, no=(1,))
        if found.answer is git_client.Answer.UNKNOWN:       # unknown is not "no such branch": the folder stays unproven
            raise ValueError(f'git rev-parse failed: {found.why}')
        if found.answer is git_client.Answer.YES:
            return branch
    return None


SKIP = {'.git', 'node_modules', 'obj', 'bin', 'target', '.venv', '__pycache__'}


def newest_files(path, count=5):
    """The most recently written files of a directory, build output and `.git` pruned, links not followed. A link is
    known from the directory listing, so each file costs one stat: on 9p every extra ask is about 4 ms."""
    found, folders = [], [os.fspath(path)]
    while folders:
        try:
            entries = list(os.scandir(folders.pop()))
        except OSError:
            continue        # as os.walk did: an unreadable directory is passed over
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                if entry.name not in SKIP:
                    folders.append(entry.path)
            else:
                found.append((os.stat(entry.path).st_mtime, entry.path))
    return [os.path.relpath(full, path).replace(os.sep, '/') for _, full in sorted(found, reverse=True)[:count]]


class History:
    """One repo's branch history, read once per report and only when a suggestion needs it: what
    `git log --branches -1 --find-object=<blob>` answers (the newest commit whose diff holds the blob on either side)
    and what `git branch --contains <commit>` lists, from one raw pass and one ancestry read in place of one of each
    per file (25 s and 5 s each in the live run). A failed read is kept and raised again: a slow repo costs one
    timeout, not one per file. `timeout_s()` is read when a read starts; `late()` is the stage clock's, past whose line
    no suggestion is looked for."""
    def __init__(self, repo, *, timeout_s, late=lambda: False):
        self._repo, self._timeout_s, self._reads, self.late = repo, timeout_s, {}, late

    def _once(self, key, read):
        if key not in self._reads:
            try:
                self._reads[key] = (read(), None)
            except (OSError, ValueError) as error:
                self._reads[key] = (None, error)
        value, error = self._reads[key]
        if error is not None:
            raise error
        return value

    def _git(self, *args):
        return _out(self._repo, *args, timeout_s=self._timeout_s())

    def introduced(self, blob) -> str:
        """The commit `git log --branches -1 --find-object=<blob>` names, or ''."""
        return self._once('log', self._log).get(blob, '')

    def _log(self):
        first, commit = {}, ''
        for line in self._git('log', '--branches', '--format=%H', '--raw', '--no-abbrev').splitlines():
            if line.startswith(':'):        # `:old-mode new-mode old new status\tpath`: either side holds the blob
                for oid in line.split('\t', 1)[0].split()[2:4]:
                    first.setdefault(oid, commit)
            elif line.strip():
                commit = line.strip()
        return first

    def holders(self, commit) -> list[str]:
        """The words `git branch --contains <commit> --format=%(refname:short)` prints, split as `suggest` splits
        them: each branch name, and for a detached HEAD holding the commit at least two words, as git's
        `(HEAD detached at <short>)` or `(no branch, ...)` gives, so a name is then never alone."""
        children, tips = self._once('graph', self._graph)
        seen, stack = {commit}, [commit]
        while stack:
            for child in children.get(stack.pop(), ()):
                if child not in seen:
                    seen.add(child)
                    stack.append(child)
        return [word for oid, words in tips if oid in seen for word in words]

    def _graph(self):
        head = git_client.ask(self._repo, 'symbolic-ref', '-q', 'HEAD', timeout_s=self._timeout_s(), no=(1,))
        if head.answer is git_client.Answer.UNKNOWN:
            raise ValueError(f'git symbolic-ref failed: {head.why}')
        detached = head.answer is git_client.Answer.NO       # `git branch` lists a detached HEAD as well
        tips = [(self._git('rev-parse', '--verify', 'HEAD').strip(), ('(HEAD', 'detached)'))] if detached else []
        children = {}
        for line in self._git('rev-list', '--branches', *(['HEAD'] if detached else []), '--parents').splitlines():
            commit, *parents = line.split()
            for parent in parents:
                children.setdefault(parent, []).append(commit)
        for line in self._git('for-each-ref', 'refs/heads', '--format=%(objectname) %(refname:short)').splitlines():
            oid, _, name = line.partition(' ')
            if name:
                tips.append((oid, (name,)))
        return children, tips


def suggest(repo, path, *, history=None):
    """Report-only: the one local branch alone holding a newest file of the directory exactly as committed (the skill's
    SUGGEST ranked trees; this reads one blob per file, at most five, against the repo's `History`). Never applied."""
    history = history or History(repo, timeout_s=lambda: git_client.LONG)
    for name in newest_files(path):
        blob = _out(repo, 'hash-object', '--path=' + name, '--', str(path / name)).strip()
        commit = history.introduced(blob)
        if not commit:
            continue
        held = history.holders(commit)
        if len(held) == 1:
            return held[0]
    return None


def judge(machine, path, listed, rel=None, *, history=None, own_drive=True):
    """('repair', branch, why) for a proven match, ('unproven', branch, why) otherwise; a suggestion rides in why."""
    repo = machine['repo']
    branch = candidate(repo, path.name, rel)
    if branch is None:
        if not own_drive:       # report-only, and read on the folder's own drive's pass
            return 'unproven', None, f'no branch named like {rel or path.name}; {OTHER_DRIVE}', None
        if history is not None and history.late():     # report-only, so past the line it is left out, not waited for
            return 'unproven', None, f'no branch named like {rel or path.name}; no suggestion looked for past the stage line', None
        maybe = suggest(repo, path, history=history)
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


def report(request, machine, listed, found, store, *, evidence, clock):
    """Decide each lost directory; on apply, register the proven ones. Dry runs only describe. Each directory's row is
    yielded as it settles and the next is not started until asked for, so the stage sends each row and checks its line
    in between. `evidence` and `clock` are the stage's, which a repair tells and keeps to. The repo's history is read
    once, for every folder's suggestion, with the clock's long patience: it stands in for up to ten reads per folder. A
    folder off the stage's drive is listed and matched for repair as ever but gets no suggestion: its own drive's pass
    makes it, once."""
    history = History(machine['repo'], timeout_s=lambda: clock.patience(git_client.LONG) if clock else git_client.LONG,
                      late=lambda: bool(clock and clock.late()))
    for text, (target, rel) in found.items():
        path = Path(text)
        row = item(path, 'lost_registration', repo=machine['repo'], name=machine['name'])
        try:
            if target.exists():
                verdict, branch, why, maybe = 'repair', None, 'interrupted registration; pointers are rewritten', None
            else:
                verdict, branch, why, maybe = judge(machine, path, listed, rel, history=history,
                                                      own_drive=placement.cleanup_contains(request['drive']['path'], text))
        except (OSError, ValueError) as error:
            verdict, branch, why, maybe = 'unproven', None, f'could not be matched: {error}', None
        row.update(branch=branch, detail=why, suggestion=maybe)
        if verdict != 'repair':
            row.update(action='keep', reason_code='lost_registration_unproven')
        elif not request['apply']:
            row.update(action='report')
        else:
            try:
                journal = reregister(request, machine, path, target, branch, store, evidence=evidence, clock=clock)
                row.update(action='reregistered', **({'error': journal} if journal else {}))
            except (OSError, ValueError, RuntimeError) as error:
                row.update(action='keep', reason_code='lost_registration_failed', error=str(error))
        yield row


def reregister(request, machine, path, target, branch, store, *, evidence, clock):
    """Add the branch's registration under a temporary parent (so the admin directory is named after the directory),
    move its `.git` file over the orphan's, repair both pointers, and rebuild a missing index. The old `.git` file is
    kept under the state directory first and never overwritten. Nothing starts unless `clock` can still finish it;
    inside the journalled window each mutating step takes the clock's mutation cap and the verifying listing its
    patience (section 6.1). `completed` and `failed` take disk_removal's journal retries; a `completed` still unwritable
    is returned for the row, the repair having taken, and a `failed` one joins the error raised."""
    if not clock.can_mutate():
        raise ValueError('repair deadline: past the stage line, the repair did not start')
    repo, cap = machine['repo'], clock.mutation_cap()
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
            placement.add_worktree(repo, str(place), '--no-checkout', '--', str(place), branch, error=ValueError,
                                   timeout_s=cap)
            durable.move(place / '.git', path / '.git', wait=durable.NO_WAIT)
            moved = True
        _out(repo, 'worktree', 'repair', str(path), timeout_s=cap)
        admin = Path(placement.native(evidence.gitdir(path)))
        if not (admin / 'index').exists():
            _out(path, 'reset', '-q', timeout_s=cap)
        if git_worktrees.listing(repo, timeout_s=clock.patience(git_worktrees.LISTING_TIMEOUT_S)).row(path) is None:
            raise ValueError('registration did not take')
        evidence.repaired(repo, path)
        return close_journal(store, record, 'completed', clock)
    except (OSError, ValueError, RuntimeError) as error:
        try:
            if not moved and place.exists():
                git_client.mutate(repo, 'worktree', 'remove', '-f', '-f', str(place), timeout_s=cap)
        finally:
            journal = close_journal(store, record, 'failed', clock)
        if journal:
            raise ValueError(f'{error}; {journal}') from error
        raise
    finally:
        for leftover in (place, holder):    # empty by now; rmdir never removes content
            try:
                leftover.rmdir()
            except OSError:
                pass
