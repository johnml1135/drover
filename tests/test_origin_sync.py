"""Origin freshness without changing the owner's checkout."""
import unittest

from drover import origin
from tests.gitfix import Repo


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.r = Repo()
        self.addCleanup(self.r.cleanup)

    def advance_remote(self, files=None):
        sha = self.r.branch('upstream', files or {'upstream.txt': 'upstream'})
        self.r.git('push', '-q', 'origin', f'{sha}:main')
        return sha

    def test_sync_advances_main_without_touching_files_or_index(self):
        before = (self.r.repo / 'base.txt').read_bytes()
        index = (self.r.repo / '.git' / 'index').read_bytes()
        sha = self.advance_remote({'base.txt': 'upstream'})
        self.assertEqual(origin.sync(self.r.repo), sha)
        self.assertEqual(self.r.git('rev-parse', 'main'), sha)
        self.assertEqual((self.r.repo / 'base.txt').read_bytes(), before)
        self.assertEqual((self.r.repo / '.git' / 'index').read_bytes(), index)

    def test_divergence_refuses_without_moving_main(self):
        sha = self.r.commit('local.txt')
        self.advance_remote()
        with self.assertRaisesRegex(origin.SyncError, 'main diverged from origin'):
            origin.sync(self.r.repo)
        self.assertEqual(self.r.git('rev-parse', 'main'), sha)

    def test_local_unpushed_landings_are_preserved(self):
        sha = self.r.commit('local.txt')
        self.assertEqual(origin.sync(self.r.repo), sha)

    def test_offline_does_not_use_cached_remote(self):
        self.r.git('remote', 'set-url', 'origin', str(self.r.root / 'offline.git'))
        with self.assertRaisesRegex(origin.FetchFailed, 'fetch failed'):
            origin.sync(self.r.repo)

    def test_push_uses_local_main_and_never_forces(self):
        sha = self.r.commit('local.txt')
        self.assertEqual(origin.ahead(self.r.repo), 1)
        origin.push_main(self.r.repo)
        self.assertEqual(self.r.remote_head(), sha)
        self.assertEqual(origin.ahead(self.r.repo), 0)

    def test_sync_tracks_main_even_with_a_restricted_fetch_refspec(self):
        self.r.git('config', 'remote.origin.fetch', '+refs/heads/other:refs/remotes/origin/other')
        self.r.git('update-ref', '-d', 'refs/remotes/origin/main')
        origin.sync(self.r.repo)
        self.r.commit('local.txt')
        self.assertEqual(origin.ahead(self.r.repo), 1)
        origin.push_main(self.r.repo)
        self.assertEqual(origin.ahead(self.r.repo), 0)

    def test_configured_remote_and_main_are_used(self):
        self.r.git('remote', 'rename', 'origin', 'upstream')
        self.r.git('branch', '-m', 'main', 'trunk')
        self.r.git('push', '-q', 'upstream', 'trunk:trunk')
        sha = self.r.branch('upstream-change', {'upstream.txt': 'upstream'}, base='trunk')
        self.r.git('push', '-q', 'upstream', f'{sha}:trunk')
        self.assertEqual(origin.sync(self.r.repo, 'upstream', 'trunk'), sha)
        self.assertEqual(self.r.git('rev-parse', 'trunk'), sha)
        self.assertFalse((self.r.repo / 'upstream.txt').exists())
