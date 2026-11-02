"""A temp tree of real repositories and admin files, and `TraceGit`, the git that reads every answer from it."""
import json
import shutil
import subprocess
from pathlib import Path

from drover import ownership, placement
from tests.disk_fixture import DAY, DiskCase

NOW = 2_000_000_000.
MERGED, EARLIER, TIP, FEATURE = '1' * 40, '4' * 40, '2' * 40, '3' * 40    # MERGED, EARLIER: ancestors of TIP, the remote default
TRACKS_BIN = '9' * 40                                                     # a commit with a file under the declared `bin`
PICKED, PARTLY_PICKED, SQUASHED, CLEAN_MERGE = '5' * 40, '6' * 40, '7' * 40, '8' * 40
CHERRY = {PICKED: f'- {PICKED}\n', PARTLY_PICKED: f'- {PICKED}\n+ {PARTLY_PICKED}\n'}   # others: `+`, not upstream
BASE_TREE = 'a' * 40
MERGE_TREES = {SQUASHED: BASE_TREE, CLEAN_MERGE: 'b' * 40}      # others conflict
TREE = {'README.md': 'sentinel\n'}
EXPIRE = 'worktreePruneExpire'


def active(path):
    return dict(tasks=dict(active_worktrees=[str(path)]))


def task_at(path):
    """A live task named by its path alone: no tag or `active_worktrees` entry says so."""
    return dict(tasks=dict(tasks=[dict(id='T9', terminal=False, live=True, worktree=str(path))]))


def terminal(task, state):
    return dict(tasks=dict(tasks=[dict(id=task, terminal=True, live=False, state=state)]))


class Failed(Exception):
    def __init__(self, returncode, stderr):
        self.returncode, self.stderr = returncode, stderr


def tracked_differences(work, tree, read):
    return [name for name, text in tree.items() if not (work / name).is_file() or read(work / name) != text]


def files_of(work):
    names = (p.relative_to(work) for p in work.rglob('*') if p.is_file() and p.name != '.git')
    return sorted(name.as_posix() for name in names if name.parts[0] != '.git')


class TraceGit:
    """Answers by meaning, not order: the command's words and the tree decide. A command it cannot answer raises."""
    def __init__(self, scene):
        self.scene = scene
        self.after, self.after_listing = {}, None      # after: git's subcommand, to the one change that follows its next answer
        self.read = {}

    def __getattr__(self, name):
        return getattr(subprocess, name)

    def text(self, path):
        """A file's text without opening what the scene wrote: on Windows the first open of a new file is the costly step, by
        far, and these few files answer most commands. The stat still shows a file gone or written by the stage."""
        info = path.stat()
        key, seen = (info.st_mtime_ns, info.st_size), self.read.get(path)
        if seen is None or seen[0] != key:
            seen = self.read[path] = key, path.read_text(encoding='utf-8')
        return seen[1]

    def seed(self, path, text):
        info = path.stat()
        self.read[path] = (info.st_mtime_ns, info.st_size), text

    def head(self, gitdir):
        return self.text(gitdir / 'HEAD').strip()

    def run(self, argv, *, env=None, **_):
        if argv[0] in self.scene.commands:
            return self.scene.commands[argv[0]](argv)
        if list(argv[:2]) != ['git', '-C']:
            raise AssertionError(f'TraceGit cannot answer {argv}')
        where, args, work = Path(argv[2]), [str(a) for a in argv[3:]], None
        while args[0] == '-c' or args[0].startswith('--work-tree='):
            work = Path(args[0].partition('=')[2]) if args[0] != '-c' else work
            args = args[2 if args[0] == '-c' else 1:]
        answer = getattr(self, 'do_' + args[0].replace('-', '_'), None)
        if answer is None:
            raise AssertionError(f'TraceGit cannot answer {argv}')
        try:
            self.scene.check_refused(where, ' '.join(args))
            output = answer(where, args[1:], work, env or {})
            self.after.pop(args[0], lambda: None)()
            return subprocess.CompletedProcess(argv, 0, output, '')
        except Failed as failure:
            return subprocess.CompletedProcess(argv, failure.returncode, '', failure.stderr)

    def locate(self, where):
        """(git directory, common directory), read from the checkout's own `.git`."""
        marker = where / '.git'
        if marker.is_dir():
            return marker, marker
        if marker.is_file():
            gitdir = Path(self.text(marker).removeprefix('gitdir:').strip())
            if (gitdir / 'commondir').is_file():
                return gitdir, gitdir.parent.parent
        raise Failed(128, 'fatal: not a git repository')

    def resolve(self, gitdir, common, name):
        if name.endswith('^{tree}'):
            return BASE_TREE
        name = name.removesuffix('^{commit}')
        if name in self.scene.commits:
            return name
        text = 'ref: ' + name if name != 'HEAD' else self.head(gitdir)
        if not text.startswith('ref: '):
            return text
        name = text[5:]
        for ref in (common / name, common / 'refs/remotes' / name):
            if ref.is_file():
                return self.text(ref).strip()
        raise Failed(128, f'fatal: bad revision {name}')

    def index_tree(self, env):
        return self.scene.commits[self.text(Path(env['GIT_INDEX_FILE']))][0]

    def do_rev_parse(self, where, args, work, env):
        gitdir, common = self.locate(where)
        known = {'--absolute-git-dir': gitdir.as_posix(), '--git-common-dir': common.as_posix()}
        return ''.join((known[arg] if arg in known else self.resolve(gitdir, common, arg)) + '\n'
                       for arg in args if arg in known or not arg.startswith('-'))

    def do_status(self, where, args, work, env):
        gitdir, common = self.locate(where)
        tree = self.scene.commits[self.resolve(gitdir, common, 'HEAD')][0]
        rows = [' M ' + name for name in tracked_differences(where, tree, self.text)]
        exclude = self.scene.excluded(common)
        for name in files_of(where):
            top, nested = name.split('/')[0], '/' in name
            if name in self.scene.staged:
                rows.append('A  ' + name)
            elif name not in tree:
                folder = nested and not any(t.startswith(top + '/') for t in tree)
                rows.append(f'!! {top}/' if top in exclude and folder else '!! ' + name if top in exclude
                            else f'?? {top}/' if folder and '--untracked-files=normal' in args else '?? ' + name)
        return ''.join(row + '\0' for row in dict.fromkeys(rows))

    def do_read_tree(self, where, args, work, env):
        gitdir, common = self.locate(where)
        self.scene.write(Path(env['GIT_INDEX_FILE']), self.resolve(gitdir, common, args[0]))
        return ''

    def do_ls_files(self, where, args, work, env):
        if 'GIT_INDEX_FILE' not in env:
            gitdir, common = self.locate(where)
            under = args[args.index('--') + 1].replace('\\', '/') if '--' in args else ''
            names = [*self.scene.commits[self.resolve(gitdir, common, 'HEAD')][0], *sorted(self.scene.staged)]
            return ''.join(name + '\0' for name in names if not under or name == under or name.startswith(under + '/'))
        tree = self.index_tree(env)
        names = [n for n in files_of(work) if n not in tree] if '--others' in args else list(tree)
        return ''.join(name + '\0' for name in names)

    def do_diff(self, where, args, work, env):
        changed = tracked_differences(work, self.index_tree(env), self.text)
        if '--exit-code' in args and changed:
            raise Failed(1, '')
        return ''.join(name + '\0' for name in changed)

    def do_update_ref(self, where, args, work, env):
        target = self.locate(where)[1] / args[0]
        target.parent.mkdir(parents=True, exist_ok=True)
        self.scene.write(target, args[1] + '\n')
        return ''

    def do_ls_tree(self, where, args, work, env):
        return f'100644 blob {"4" * 40}\t{args[-1]}\n' if self.scene.declared else ''

    def do_show(self, where, args, work, env):
        return f'schema_version = 1\n[cleanup]\ndisposable = {json.dumps(self.scene.declared)}\n'

    def do_merge_base(self, where, args, work, env):
        if args[1] not in self.scene.commits[args[2]][1] and args[1] != args[2]:
            raise Failed(1, '')
        return ''

    def do_cherry(self, where, args, work, env):
        return CHERRY.get(args[1], f'+ {args[1]}\n')

    def do_merge_tree(self, where, args, work, env):
        if args[-1] not in MERGE_TREES:
            raise Failed(1, '')
        return MERGE_TREES[args[-1]] + '\n'

    def do_hash_object(self, where, args, work, env):
        return '4' * 40 + '\n'

    def do_log(self, where, args, work, env):
        return ''

    def do_reset(self, where, args, work, env):
        self.scene.write(self.locate(where)[0] / 'index', '')
        return ''

    def do_config(self, where, args, work, env):
        config = self.locate(where)[1] / 'config'
        text = self.text(config)
        if args[0] == '--get':
            if EXPIRE not in text:
                raise Failed(1, '')
            return 'never\n'
        self.scene.write(config, f'{text}[gc]\n\t{EXPIRE} = {args[1]}\n')
        return ''

    def do_worktree(self, where, args, work, env):
        common = self.locate(where)[1]
        if args[0] == 'list':
            return self.listing(common)
        action, rest = args[0], [a for a in args[1:] if a != '--']
        if action == 'add':
            return self.add(common, rest[-2:], rest[rest.index('--reason') + 1])
        path = Path(rest[-1])
        admin = next((a for a in sorted((common / 'worktrees').glob('*'))
                      if placement.cleanup_parts(Path(self.text(a / 'gitdir').strip()).parent)
                      == placement.cleanup_parts(path)), None) if action != 'repair' else self.locate(path)[0]
        if admin is None:
            raise Failed(128, f'fatal: {path} is not a working tree')
        locked = admin / 'locked'
        if action == 'lock':
            self.scene.write(locked, rest[rest.index('--reason') + 1])
        elif action == 'unlock':
            locked.unlink()
        elif action == 'repair':
            self.scene.write(admin / 'gitdir', (path / '.git').as_posix() + '\n')
        elif action == 'remove' and locked.exists() and rest.count('--force') + rest.count('-f') < 2:
            raise Failed(128, 'fatal: cannot remove a locked working tree')
        elif action == 'remove':
            shutil.rmtree(path, ignore_errors=True)
            shutil.rmtree(admin)
        else:
            raise AssertionError(f'TraceGit cannot answer worktree {args}')
        return ''

    def add(self, common, tail, reason):
        self.scene.register(common, Path(tail[0]), 'ref: refs/heads/' + tail[1], reason)
        return ''

    def listing(self, common):
        blocks = [[f'worktree {common.parent.as_posix()}', *self.head_fields(common, common)]]
        for admin in sorted((common / 'worktrees').glob('*')):
            pointer = Path(self.text(admin / 'gitdir').strip())
            fields = [f'worktree {pointer.parent.as_posix()}', *self.head_fields(admin, common)]
            if (admin / 'locked').exists():
                fields.append(f'locked {self.text(admin / "locked")}'.rstrip())
            elif not pointer.exists():
                fields.append('prunable gitdir file points to non-existent location')
            blocks.append(fields)
        if self.after_listing:
            self.after_listing()
        return ''.join('\0'.join(fields) + '\0\0' for fields in blocks)

    def head_fields(self, gitdir, common):
        text = self.head(gitdir)
        return [f'HEAD {self.resolve(gitdir, common, "HEAD")}', 'branch ' + text[5:] if text.startswith('ref: ') else 'detached']


class SceneTree(DiskCase):
    """A temp tree of real repositories and admin files; builders here, and what TraceGit asks of it."""
    def __init__(self):
        super().__init__()
        self.setUp()
        self.cfg['paths']['wsl_worktrees'] = str(self.root / 'wsl')
        self.machines, self.refused, self.elsewhere = [], [], set()
        self.commits = {commit: (TREE, ()) for commit in (MERGED, EARLIER, FEATURE, PICKED, PARTLY_PICKED, SQUASHED, CLEAN_MERGE)}
        self.commits[TIP] = (TREE, (MERGED, EARLIER))
        self.commits[TRACKS_BIN] = ({**TREE, 'bin/tracked.txt': 'kept\n'}, ())
        self.declared, self.midstage, self.process_reason, self.clock, self.journal_seen = [], None, None, 0., 0
        self.fresh, self.each_run, self.proofs, self.staged, self.commands = None, None, 0, set(), {}
        self.links, self.unreadable, self.foreign, self.used_gb, self.free_gb = set(), set(), set(), [None], [10.]
        self.process_facts, self.budget_side = dict(unreadable=[], query_failure=None, out_of_scope=0), self.side
        self.git = TraceGit(self)
        self.repo()

    def write(self, path, text):
        path.write_text(text, encoding='utf-8')
        self.git.seed(path, text)

    def file(self, path, text='sentinel', age=8 * DAY):
        path = super().file(path, text, age)
        self.git.seed(path, text)
        return path

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.doCleanups()

    def common(self, name='alpha'):
        return self.root / 'repos' / name / '.git'

    def repo(self, name='alpha', prune_expiry=True):
        common = self.common(name)
        for folder in ('refs/heads', 'refs/remotes/origin', 'info', 'worktrees'):
            (common / folder).mkdir(parents=True)
        self.write(common / 'HEAD', 'ref: refs/heads/main\n')
        self.write(common / 'config', '[core]\n\tbare = false\n' + (f'[gc]\n\t{EXPIRE} = never\n' if prune_expiry else ''))
        for ref in ('refs/heads/main', 'refs/remotes/origin/main'):
            self.write(common / ref, TIP + '\n')
        self.write_tree(common.parent, TIP)
        root = self.root / 'worktrees' / f'{name}.worktrees'
        root.mkdir(parents=True)
        self.machines.append(dict(name=name, repo=str(common.parent), worktrees=str(root), remote='origin', default_branch='main'))

    def write_tree(self, path, commit):
        for name, text in self.commits[commit][0].items():
            self.file(path / name, text)

    def admin_files(self, common, path, head, lock, name=None):
        admin = common / 'worktrees' / (name or path.name)
        admin.mkdir()
        self.write(admin / 'gitdir', (path / '.git').as_posix() + '\n')
        self.write(admin / 'HEAD', head + '\n')
        self.write(admin / 'commondir', '../..\n')
        if lock:
            self.write(admin / 'locked', lock)
        return admin

    def register(self, common, path, head, lock, name=None):
        admin = self.admin_files(common, path, head, lock, name)
        path.mkdir(parents=True, exist_ok=True)
        self.write(path / '.git', f'gitdir: {admin.as_posix()}\n')
        return admin

    def worktree(self, name, *, repo='alpha', commit=MERGED, detached=False, lock='side', owner='session:w', at=None, extra=(),
                 lease=NOW + 400 * DAY):
        """A linked worktree, leased to `owner` (None for no tag), locked with this side's name unless `lock` says else."""
        path = at or self.root / 'worktrees' / f'{repo}.worktrees' / name
        common = self.common(repo)
        if not detached:
            self.write(common / 'refs/heads' / name, commit + '\n')
        head = commit if detached else 'ref: refs/heads/' + name
        admin = self.register(common, path, head, self.side if lock == 'side' else lock, name)
        self.write_tree(path, commit)
        for relative in extra:
            self.file(path / relative)
        if owner:
            self.tag(name, repo, owner, lease)
        return path

    def tag(self, name, repo, owner, lease=NOW + 400 * DAY, claims=None):
        self.write(self.common(repo) / 'worktrees' / name / ownership.TAG, json.dumps(dict(
            v=2, repo=claims or repo, owner=owner, created=NOW - 30 * DAY, lease_until=lease)))

    def folder(self, name, extra=()):
        """A plain folder under the worktree root, holding the repo's HEAD, registered nowhere."""
        path = self.root / 'worktrees' / 'alpha.worktrees' / name
        self.write_tree(path, TIP)
        for relative in extra:
            self.file(path / relative)
        return path

    def lost(self, name):
        """A checkout whose `.git` names an admin directory that is gone."""
        path = self.folder(name)
        self.write(path / '.git', f'gitdir: {(self.common() / "worktrees" / name).as_posix()}\n')
        return path

    def registered_only(self, path, name, head='ref: refs/heads/main'):
        """A registration of `path`, whose own `.git` is not written yet: a listing names it, and a look at the folder cannot.
        Admin files alone: anything written in the folder changes its mtime, so the fingerprint would refuse before the listing."""
        self.admin_files(self.common(), path, head, None, name)

    def exclude(self, *names):
        self.write(self.common() / 'info' / 'exclude', ''.join(n + '\n' for n in names))

    def excluded(self, common):
        exclude = common / 'info' / 'exclude'
        return self.git.text(exclude).split() if exclude.exists() else []

    def advance(self, name):
        """The worktree's branch moves to another commit that is still merged; only its head differs."""
        self.write(self.common() / 'refs/heads' / name, EARLIER + '\n')

    def move(self, name, commit=MERGED):
        """The worktree's registration changes: it now has another branch checked out, at `commit`."""
        self.write(self.common() / 'refs/heads/moved', commit + '\n')
        self.write(self.common() / 'worktrees' / name / 'HEAD', 'ref: refs/heads/moved\n')

    def refuse(self, command, message, where=None):
        self.refused.append((where, command, message))

    def check_refused(self, where, command):
        for place, prefix, message in self.refused:
            if command.startswith(prefix) and (place is None or Path(place) == where):
                raise Failed(128, message)
