"""Settled worktree policy is exercised through the cleanup coordinator."""
import json
from pathlib import Path
from unittest import mock

from drover import config, placement
from drover.ledger import states
from tests.disk_fixture import DiskCase
from tests.gitfix import Repo, sh


class WorktreeCase(DiskCase):
    def setUp(self):
        super().setUp()
        self.r = Repo(ignore='generated/\ndata/\n.venv/\n')
        self.addCleanup(self.r.cleanup)
        self.tree_count = 0
        self.trees = self.r.root / 'alpha.worktrees'
        self.r.commit('.drover/config.toml', config.dumps({
            'schema_version': 1, 'cleanup': {'build_outputs': ['generated']}}))
        self.r.git('push', '-q', 'origin', 'HEAD:main')
        self.r.git('fetch', '-q', 'origin')
        self.register(self.r, 'alpha', self.trees)

    def register(self, repo, name, trees):
        self.repos_dir.mkdir(parents=True, exist_ok=True)
        (self.repos_dir / (name + '.toml')).write_text(config.dumps({
            'name': name, 'repo': str(repo.repo), 'worktrees': str(trees),
            'remote': 'origin', 'default_branch': 'main'}), encoding='utf-8')

    def worktree(self, name, *, lease=True, lock='wsl', detached=False):
        from drover import ownership
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
        self.r.git('fetch', '-q', 'origin')

    def cleanup(self, *, apply=False, mode='on-demand'):
        return self.run_cleanup(apply=apply, mode=mode, real=True)

    def branches(self):
        return self.r.git('for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads')

    def registrations(self):
        return self.r.git('worktree', 'list', '--porcelain', '-z')

    def journal(self):
        path = self.state / 'disk-cleanup-manifest.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()]

    def reported(self, result, path):
        # Offending descendants may be reported separately or in structured metadata.
        self.assertIn(str(path), json.dumps(result['items'], ensure_ascii=False))


class Settled(WorktreeCase):
    def test_merge_cherry_and_multicommit_squash(self):
        from drover import disk_cleanup
        for mode in ('ancestry', 'cherry', 'squash'):
            with self.subTest(proof=mode):
                wt = self.worktree(mode)
                self.r.commit(mode + '.txt', mode + '\n', cwd=wt)
                if mode == 'cherry':
                    self.r.commit('intervening.txt', 'other\n')
                    self.r.git('cherry-pick', self.r.git('rev-parse', 'HEAD', cwd=wt))
                    self.r.git('push', '-q', 'origin', 'HEAD:main')
                    self.r.git('fetch', '-q', 'origin')
                    self.assertNotEqual(self.r.git('rev-parse', 'HEAD'),
                                        self.r.git('rev-parse', 'HEAD', cwd=wt))
                else:
                    if mode == 'squash':
                        self.r.commit('second.txt', 'second\n', cwd=wt)
                    self.merge(wt, squash=mode == 'squash')
                before = self.branches()
                self.cleanup()
                self.assertTrue(wt.is_dir(), 'report must not remove candidates')
                self.cleanup(apply=True)
                self.assertFalse(wt.exists(), mode)
                self.assertEqual(self.branches(), before)
                self.assertTrue(self.r.repo.is_dir(), 'main checkout survives')

    def test_dirty_busy_other_side_and_lock_notes(self):
        from drover import disk_cleanup
        paths = {key: self.worktree(key, lock=lock) for key, lock in (
            ('dirty', 'wsl'), ('busy', 'wsl'), ('foreign', 'windows'),
            ('noted', 'owner: reviewing'))}
        for key, path in paths.items():
            self.r.commit(key + '.txt', cwd=path)
            self.merge(path)
        (paths['dirty'] / 'scratch.txt').write_text('untracked data')
        self.busy_paths.add(str(paths['busy']))
        before = self.registrations()
        self.cleanup()
        result = self.cleanup(apply=True)
        self.assertEqual(self.registrations(), before)
        for path in paths.values():
            self.assertTrue(path.exists())
            self.reported(result, path)
        self.assertIn('locked owner: reviewing', before)
        self.assertIn('locked windows', before)

    def test_task_landed_or_cancelled_and_unknown(self):
        from drover import disk_cleanup, ownership
        paths = {}
        records = []
        for index, status in enumerate((*states.TERMINAL, states.RUNNING, None)):
            path = self.worktree('task-' + str(index))
            self.r.commit('unique.txt', str(index), cwd=path)
            task = 'task-' + str(index)
            ownership.tag(str(path), 'alpha', 'drover:' + task,
                          self.now + 86400, now=self.now)
            paths[status] = path
            if status is not None:
                records.append({'id': task, 'state': status, 'terminal': status in states.TERMINAL,
                                'live': status == states.RUNNING,
                                'worktree': str(path), 'owner': 'drover:' + task})
        self.tasks = {'at': self.now, 'tasks': records,
                      'active_worktrees': [str(paths[states.RUNNING])]}
        self.cleanup()
        self.cleanup(apply=True)
        for status in states.TERMINAL:
            self.assertFalse(paths[status].exists())
        self.assertTrue(paths[states.RUNNING].exists())
        self.assertTrue(paths[None].exists(), 'unknown task is not terminal proof')
        stale = self.worktree('stale-task')
        self.r.commit('stale.txt', cwd=stale)
        ownership.tag(str(stale), 'alpha', 'drover:stale', self.now + 86400, now=self.now)
        self.tasks = {'at': self.now - 901, 'tasks': [{'id': 'stale',
                      'state': states.LANDED, 'terminal': True, 'live': False, 'worktree': str(stale), 'owner': 'drover:stale'}],
                      'active_worktrees': []}
        self.cleanup()
        self.cleanup(apply=True)
        self.assertTrue(stale.exists(), 'stale snapshot cannot establish settlement')
