"""Origin freshness keeps checked-out main consistent with its ref."""
import unittest
from unittest.mock import patch

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

    def test_sync_advances_checked_out_main_and_keeps_checkout_clean(self):
        sha = self.advance_remote({'base.txt': 'upstream'})
        self.assertEqual(origin.sync(self.r.repo), sha)
        self.assertEqual(self.r.git('rev-parse', 'main'), sha)
        self.assertEqual(self.r.git('status', '--porcelain'), '')
        self.assertEqual((self.r.repo / 'base.txt').read_text(), 'upstream')

    def test_sync_advances_main_in_another_worktree(self):
        self.r.git('checkout', '-q', '--detach')
        checkout = self.r.root / 'main checkout with spaces'
        self.r.git('worktree', 'add', '-q', str(checkout), 'main')
        sha = self.advance_remote({'base.txt': 'upstream'})
        self.assertEqual(origin.sync(self.r.repo), sha)
        self.assertEqual(self.r.git('rev-parse', 'HEAD', cwd=checkout), sha)
        self.assertEqual(self.r.git('status', '--porcelain', cwd=checkout), '')
        self.assertEqual((checkout / 'base.txt').read_text(), 'upstream')
        self.assertEqual((self.r.repo / 'base.txt').read_text(), 'base\n')

    def test_dirty_checkout_holds_sync_without_changing_owner_work(self):
        for kind in ('unstaged', 'staged', 'untracked'):
            with self.subTest(kind=kind):
                self.r.git('reset', '--hard', 'HEAD')
                name = 'owner.txt' if kind == 'untracked' else 'base.txt'
                (self.r.repo / name).write_text('owner work\n')
                if kind == 'staged':
                    self.r.git('add', name)
                old = self.r.git('rev-parse', 'main')
                status = self.r.git('status', '--porcelain')
                index = (self.r.repo / '.git' / 'index').read_bytes()
                if kind == 'unstaged':
                    self.advance_remote()
                with self.assertRaisesRegex(origin.SyncError, 'main checkout dirty'):
                    origin.sync(self.r.repo)
                self.assertEqual(self.r.git('rev-parse', 'main'), old)
                self.assertEqual(self.r.git('status', '--porcelain'), status)
                self.assertEqual((self.r.repo / '.git' / 'index').read_bytes(), index)
                self.assertEqual((self.r.repo / name).read_text(), 'owner work\n')

    def test_moved_main_fails_cas(self):
        for checked_out in (True, False):
            with self.subTest(checked_out=checked_out):
                r = Repo()
                self.addCleanup(r.cleanup)
                old = r.commit('local.txt')
                r.git('push', '-q', 'origin', f'{old}:main')
                upstream = r.branch('upstream', {'upstream.txt': 'upstream'}, base='main')
                r.git('push', '-q', 'origin', f'{upstream}:main')
                if not checked_out:
                    r.git('checkout', '-q', '--detach')
                moved = []

                def run(repo, *args, **kw):
                    if args[:2] == ('worktree', 'list'):
                        # A rewind remains an ancestor of the target: ff-only alone would accept it.
                        if checked_out:
                            r.git('reset', '--hard', old + '^')
                        else:
                            r.git('update-ref', 'refs/heads/main', old + '^', old)
                        moved.append(r.git('rev-parse', 'main'))
                    return origin.git(repo, *args, **kw)

                with self.assertRaisesRegex(origin.SyncError, 'main moved during origin sync'):
                    origin.sync(r.repo, run=run)
                self.assertEqual(r.git('rev-parse', 'main'), moved[0])

    def test_unchecked_out_main_uses_update_ref_and_preserves_checkout(self):
        self.r.git('checkout', '-q', '--detach')
        old = self.r.git('rev-parse', 'main')
        index = (self.r.repo / '.git' / 'index').read_bytes()
        sha = self.advance_remote({'base.txt': 'upstream'})
        with patch.object(origin, 'git', wraps=origin.git) as run:
            self.assertEqual(origin.sync(self.r.repo, run=run), sha)
        self.assertTrue(any(c.args[1:] == ('update-ref', 'refs/heads/main', sha, old)
                            for c in run.call_args_list))
        self.assertEqual(self.r.git('rev-parse', 'HEAD'), old)
        self.assertEqual(self.r.git('status', '--porcelain'), '')
        self.assertEqual((self.r.repo / 'base.txt').read_text(), 'base\n')
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
        self.assertEqual(self.r.git('status', '--porcelain'), '')
        self.assertEqual((self.r.repo / 'upstream.txt').read_text(), 'upstream')
