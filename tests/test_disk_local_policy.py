"""A repo may declare disposable paths in local, owner-only Drover config, checked like committed ones."""
import unittest

from drover import config, repos
from drover.disk_worktrees import outputs_at
from tests.gitfix import Repo
from tests.tiers import integration


@integration
class LocalPolicy(unittest.TestCase):
    def setUp(self):
        self.fixture = Repo()
        self.addCleanup(self.fixture.cleanup)

    def machine(self, **extra):
        return dict(name='alpha', repo=str(self.fixture.repo), **extra)

    def commit_policy(self, *paths):
        self.fixture.commit('.drover/config.toml', config.dumps(
            {'schema_version': 1, 'cleanup': {'disposable': list(paths)}}))

    def test_local_disposable_used_when_repo_has_no_committed_config(self):
        self.assertEqual(outputs_at(self.machine(), self.fixture.repo), [])
        self.assertEqual(outputs_at(self.machine(local_disposable=['obj', 'bin/**']), self.fixture.repo),
                         ['obj', 'bin/**'])

    def test_local_policy_is_validated_like_committed(self):
        for bad in (['/abs'], ['..'], ['a/../b'], ['.git'], ['a\\b'], ['c:x'], [''], 'obj', [3]):
            with self.subTest(bad=bad):
                with self.assertRaises(repos.RepoError):
                    outputs_at(self.machine(local_disposable=bad), self.fixture.repo)

    def test_local_policy_survives_repo_save(self):
        raw = self.machine(local_disposable=['obj'])
        for adopted in (False, True):
            if adopted:
                self.commit_policy('generated')
            loaded = repos.load(raw)
            self.assertEqual(loaded['_adopted'], adopted)
            saved = repos.machine_overrides('alpha', str(self.fixture.repo), loaded['worktrees'], base=loaded,
                                            adopted=adopted)
            self.assertEqual(saved['local_disposable'], ['obj'])
            self.assertEqual(repos.load(saved)['_machine']['local_disposable'], ['obj'])

    def test_committed_and_local_lists_merge_without_duplicates(self):
        self.commit_policy('generated', 'obj')
        self.assertEqual(outputs_at(self.machine(local_disposable=['obj', 'bin']), self.fixture.repo),
                         ['generated', 'obj', 'bin'])

    def test_a_committed_file_cannot_declare_local_disposable(self):
        self.fixture.commit('.drover/config.toml', config.dumps(
            {'schema_version': 1, 'local_disposable': ['data']}))
        with self.assertRaises(repos.RepoError):
            outputs_at(self.machine(), self.fixture.repo)
