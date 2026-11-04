"""The real-git worktree fixture. It lives outside any test module so a spawn is blamed on the test that asked for it
(tests/__init__.py walks to the first `tests.test_*` frame), not on the module that happens to define the base class."""
import json
from pathlib import Path

from drover import config, repos
from tests.disk_fixture import DiskCase
from tests.gitfix import Repo


class WorktreeCase(DiskCase):
    def setUp(self):
        super().setUp()
        from unittest.mock import patch
        from drover import placement
        root = patch.object(repos, 'wsl_worktrees_root', return_value=str(self.root / 'ext4'))   # never ~/work
        root.start()
        self.addCleanup(root.stop)
        self.r = Repo(ignore='generated/\ndata/\n.venv/\n')
        self.addCleanup(self.r.cleanup)
        self.tree_count = 0
        self.trees = self.r.root / 'alpha.worktrees'
        self.r.commit('.drover/config.toml', config.dumps({
            'schema_version': 1, 'cleanup': {'disposable': ['generated']}}))
        self.r.git('push', '-q', 'origin', 'HEAD:main')
        self.register(self.r, 'alpha', self.trees)

    def register(self, repo, name, trees):
        self.repos_dir.mkdir(parents=True, exist_ok=True)
        (self.repos_dir / (name + '.toml')).write_text(config.dumps({
            'name': name, 'repo': str(repo.repo), 'worktrees': str(trees),
            'remote': 'origin', 'default_branch': 'main'}), encoding='utf-8')

    def worktree(self, name, *, lease=True, lock='native', detached=False):
        from drover import ownership
        if lock == 'native':
            lock = self.side
        path = self.trees / name
        self.tree_count += 1
        branch = 'fixture-' + str(self.tree_count)
        args = ['--detach'] if detached else ['-b', branch]
        self.r.git('worktree', 'add', '-q', *args, path, 'origin/main')
        if lock:
            self.r.git('worktree', 'lock', '--reason', lock, path)
        if lease:
            ownership.tag(str(path), 'alpha', 'session:' + name,
                          self.now + 86400, now=self.now)
        return path

    def merge(self, path, *, squash=False):
        branch = self.r.git('symbolic-ref', '--short', 'HEAD', cwd=path)
        self.r.git('merge', '--squash' if squash else '--no-edit', branch)
        if squash:
            self.r.git('commit', '-q', '-m', 'squashed work')
        self.r.git('push', '-q', 'origin', 'HEAD:main')

    def cleanup(self, *, apply=False, mode='on-demand'):
        return self.run_cleanup(apply=apply, mode=mode, real=True)

    def branches(self):
        return self.r.git('for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads')

    def registrations(self):
        raw = self.r.git('worktree', 'list', '--porcelain', '-z')
        return '\0'.join('worktree ' + str(Path(field[9:])) if field.startswith('worktree ') else field
                         for field in raw.split('\0'))

    def journal(self):
        path = self.state / 'disk-cleanup-manifest.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()]

    def reported(self, result, path):
        # Offending descendants may be reported separately or in structured metadata.
        self.assertIn(json.dumps(str(path), ensure_ascii=False), json.dumps(result['items'], ensure_ascii=False))
