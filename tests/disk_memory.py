"""Semantic repository evidence for cleanup tests; never emulate Git argv.

Small real directories retain the path/link/journal safety checks. Commit, status,
registration and metadata observations are in memory; production decide() and
fresh validation still select every deletion. Real Git contracts live separately.
"""
import copy
import json
import shutil
from pathlib import Path
from unittest.mock import patch

from drover import config, disk_cleanup, disk_worktrees, ownership, placement, repos
from tests.disk_fixture import DiskCase


class Repository:
    def __init__(self, root):
        self.root, self.repo = Path(root), Path(root) / 'repo'
        self.repo.mkdir(parents=True)
        (self.repo / '.git').mkdir()
        self.trees = {self.repo: dict(head='base', branch='refs/heads/main', locked=None, tracked={}, metadata={})}
        self.merged_heads = {'base'}
        self.sequence = 0
        self.refs = {}
        self.policy = []
        self.commit('base.txt', 'base\n')
        self.merged_heads.add(self.head(self.repo))

    def tree(self, path):
        return self.trees[Path(path)]

    def head(self, path=None):
        return self.tree(path or self.repo)['head']

    def commit(self, name, text=None, cwd=None):
        path = Path(cwd or self.repo)
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text if text is not None else name + '\n', encoding='utf-8')
        row = self.tree(path)
        row['tracked'][name] = target.read_bytes()
        self.sequence += 1
        row['head'] = f'commit-{self.sequence}'
        if row['branch']:
            self.refs[row['branch']] = row['head']
        if path == self.repo and name == '.drover/config.toml':
            committed = repos.parse_committed_config(target.read_text(encoding='utf-8'), row['head'])
            self.policy = repos.cleanup_config(committed)['disposable']
        return row['head']

    def add(self, path, *, branch=None, lock=None):
        path = Path(path)
        path.mkdir(parents=True)
        main = self.tree(self.repo)
        row = dict(head=main['head'], branch='refs/heads/' + branch if branch else None,
                   locked=lock, tracked=copy.deepcopy(main['tracked']), metadata={})
        self.trees[path] = row
        if row['branch']:
            self.refs[row['branch']] = row['head']
        for name, data in row['tracked'].items():
            target = path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        admin = self.repo / '.git' / path.name
        admin.mkdir(exist_ok=True)
        (path / '.git').write_text('gitdir: ' + str(admin), encoding='utf-8')
        return path

    def lock(self, path, reason):
        self.tree(path)['locked'] = reason

    def track(self, path, name):
        self.tree(path)['tracked'][name] = (Path(path) / name).read_bytes()

    def merge(self, path):
        self.merged_heads.add(self.head(path))

    def entries(self):
        return [dict(worktree=str(p), HEAD=r['head'], **({'branch': r['branch']} if r['branch'] else {}),
                     **({'locked': r['locked']} if r['locked'] else {}))
                for p, r in self.trees.items() if p == self.repo or (self.repo / '.git' / p.name).exists()]

    def discard(self, path):
        path = Path(path)
        shutil.rmtree(path)
        del self.trees[path]

    def status(self, path, *, expand=True):
        path = Path(path)
        row = self.tree(path)
        dirty = [str(path / name) for name, data in row['tracked'].items()
                 if not (path / name).exists() or (path / name).read_bytes() != data]
        ignored = []
        def scan(folder):
            for p in folder.iterdir():
                relative = p.relative_to(path).as_posix()
                if relative == '.git':
                    continue
                if relative in row['tracked']:
                    continue
                is_ignored = relative.split('/')[0] in ('generated', 'data', '.venv')
                if is_ignored and not expand:
                    ignored.append(str(p))
                elif p.is_dir() and not p.is_symlink():
                    scan(p)
                else:
                    (ignored if is_ignored else dirty).append(str(p))
        scan(path)
        return dirty, ignored


class ProcessFreeDiskCase(DiskCase):
    def setUp(self):
        super().setUp()
        from tests.tiers import MARKER
        if getattr(getattr(self, self._testMethodName), MARKER, False):
            return
        from drover import disk_paths
        for context in (patch.object(disk_paths, 'repository_config', return_value=True),
                        patch('subprocess.Popen', side_effect=AssertionError('cleanup policy started a process')),
                        patch('time.sleep', side_effect=AssertionError('cleanup policy slept'))):
            context.start()
            self.addCleanup(context.stop)


class MemoryWorktreeCase(ProcessFreeDiskCase):
    def setUp(self):
        super().setUp()
        self.repositories = []
        self.r = self.new_repo('alpha')
        self.trees = self.r.root / 'alpha.worktrees'
        self.tree_count = 0
        self.r.commit('.drover/config.toml', config.dumps({
            'schema_version': 1, 'cleanup': {'disposable': ['generated']}}))
        self.r.merged_heads.add(self.r.head())
        self.register(self.r, 'alpha', self.trees)
        def repository(path):
            return next(r for r in self.repositories if Path(path) == r.repo or Path(path) in r.trees)
        def metadata(path, name, value=None):
            try:
                values = repository(path).tree(path)['metadata']
            except StopIteration:
                return None
            if value is not None:
                values[name] = value
            return values.get(name)
        def snapshot(machine, path, request=None):
            path = Path(path)
            if any(p.name == '.git' for p in path.rglob('*')):
                raise ValueError('independent or broken git metadata')
            if placement.cleanup_processes(request=request).busy(str(path)):
                raise ValueError('snapshot busy')
            repo = repository(machine['repo'])
            expected = repo.tree(repo.repo)['tracked']
            actual = {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob('*') if p.is_file()}
            if actual != expected:
                raise ValueError('not a clean snapshot')
            return repo.head()
        def machines(cfg, errors=None):
            return [dict(repos.read_machine(p), remote='origin', default_branch='main')
                    for p in sorted(self.repos_dir.glob('*.toml'))]
        bindings = [(disk_cleanup, '_repos', machines),
                    (disk_worktrees, 'entries', lambda p: repository(p).entries()),
                    (disk_worktrees, 'status', lambda p, **kw: repository(p).status(p, **kw)),
                    (disk_worktrees, 'merged', lambda m, h: h in repository(m['repo']).merged_heads),
                    (disk_worktrees, 'outputs_at', lambda m, p: list(repository(m['repo']).policy)),
                    (disk_worktrees, 'head_at', lambda p: repository(p).head(p)),
                    (disk_worktrees, 'gitdir_at', lambda p: str(repository(p).repo / '.git' / Path(p).name)),
                    (disk_worktrees, 'tracked_at', lambda p, relative=None: [n for n in repository(p).tree(p)['tracked']
                        if relative is None or n == str(relative).replace('\\', '/') or n.startswith(str(relative).rstrip('/') + '/')]),
                    (disk_worktrees, 'unlock_at', lambda repo, p: repository(repo).lock(p, None)),
                    (disk_worktrees, 'lock_at', lambda repo, p, reason: repository(repo).lock(p, reason)),
                    (disk_worktrees, 'discard_at', lambda repo, p: repository(repo).discard(p)),
                    (disk_worktrees, 'rescue_at', lambda repo, ref, head: None),
                    (disk_worktrees, 'snapshot', snapshot),
                    (placement, 'worktree_metadata', metadata),
                    (placement, 'wsl_worktrees_root', lambda: str(self.root / 'ext4'))]
        for module, name, value in bindings:
            context = patch.object(module, name, side_effect=value)
            context.start()
            self.addCleanup(context.stop)

    def new_repo(self, name):
        repo = Repository(self.root / name)
        self.repositories.append(repo)
        return repo

    def register(self, repo, name, trees):
        (self.repos_dir / (name + '.toml')).write_text(config.dumps(dict(
            name=name, repo=str(repo.repo), worktrees=str(trees), remote='origin', default_branch='main')), encoding='utf-8')

    def worktree(self, name, *, lease=True, lock='native', detached=False):
        self.tree_count += 1
        path = self.r.add(self.trees / name, branch=None if detached else f'fixture-{self.tree_count}',
                          lock=self.side if lock == 'native' else lock)
        if lease:
            ownership.tag(str(path), 'alpha', 'session:' + name, self.now + 86400, now=self.now)
        return path

    def merge(self, path, *, squash=False):
        self.r.merge(path)

    def cleanup(self, *, apply=False, mode='on-demand'):
        return self.run_cleanup(apply=apply, mode=mode, real=True)

    def branches(self):
        return dict(self.r.refs)

    def registrations(self):
        return '\n'.join('worktree ' + e['worktree'] + '\n' + ('locked ' + e['locked'] if e.get('locked') else '')
                         for e in self.r.entries())

    def journal(self):
        path = self.state / 'disk-cleanup-manifest.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()]

    def reported(self, result, path):
        self.assertIn(json.dumps(str(path), ensure_ascii=False), json.dumps(result['items'], ensure_ascii=False))
