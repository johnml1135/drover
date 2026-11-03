from drover import placement
import json
import sqlite3
import time
import sys
import unittest
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

from drover import ci, git_client, landing, origin, repos
from drover.ledger import ci as slots
from drover.ledger import landing as landing_storage
from drover.ledger import db as ledger_db
from drover.ledger import ops
from tests.fakeci import InProcessLauncher, resources_go
from tests.gitfix import Repo
from tests.tiers import integration
from tests.landing_fixture import MemoryLandingCase, ProcessFreeCase
from tests.test_landing import LandingCase, fake_decider, SCRIPT


@integration
class WorktreeTests(LandingCase):
    def test_relative_layout_repair_and_native_legacy_fallback(self):
        real, real_out = git_client.mutate, git_client.out
        base = self.r.git('rev-parse', 'main')
        self.r.git('config', 'core.autocrlf', 'true')
        calls = []

        def git(repo, *args, **kw):
            if 'add' in args:
                calls.append(args)
                if '--relative-paths' in args:
                    return git_client.GitDone(False, '', "error: unknown option `relative-paths'", 129, 'exit')
            return real(repo, *args, **kw)

        with patch.object(git_client, 'mutate', git), patch.object(placement, 'is_linux_native', return_value=False) as _placement_patch_1, patch('drover.placement_exec.is_linux_native', _placement_patch_1), patch('drover.placement_host.is_linux_native', _placement_patch_1), patch('drover.placement_paths.is_linux_native', _placement_patch_1), patch.object(placement, 'routes_through_wsl', return_value=False) as _placement_patch_2, patch('drover.placement_git.routes_through_wsl', _placement_patch_2), patch('drover.placement_paths.routes_through_wsl', _placement_patch_2):
            with self.assertRaisesRegex(ci.CIError, 'both Windows and WSL: update git'):
                landing.build_tip(self.r.repo, self.work, base, [])
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.work.exists())
        calls.clear()
        with patch.object(git_client, 'mutate', git), patch.object(placement, 'is_linux_native', return_value=True) as _placement_patch_3, patch('drover.placement_exec.is_linux_native', _placement_patch_3), patch('drover.placement_host.is_linux_native', _placement_patch_3), patch('drover.placement_paths.is_linux_native', _placement_patch_3), patch.object(placement, 'routes_through_wsl', return_value=False) as _placement_patch_4, patch('drover.placement_git.routes_through_wsl', _placement_patch_4), patch('drover.placement_paths.routes_through_wsl', _placement_patch_4):
            landing.build_tip(self.r.repo, self.work, base, [])
        self.assertEqual(len(calls), 2)
        for args in calls:
            self.assertEqual(args[:4], ('-c', 'core.autocrlf=false', 'worktree', 'add'))
        self.assertEqual((self.work / 'base.txt').read_bytes(), b'base\n')

        # A new Git creates relative pointers, even with autocrlf enabled.
        self.r.git('worktree', 'remove', '--force', '--force', str(self.work))
        tip, applied, conflicts = landing.build_tip(self.r.repo, self.work, base, [])
        pointer = (self.work / '.git').read_text().strip().removeprefix('gitdir: ')
        self.assertFalse(Path(pointer).is_absolute())
        self.assertFalse(PureWindowsPath(pointer).is_absolute())
        self.assertEqual((tip, applied, conflicts), (base, [], []))
        self.assertEqual((self.work / 'base.txt').read_bytes(), b'base\n')

        # Remove the checkpoint so a reused absolute pointer must be repaired
        # before Git opens the checkout, as with a pre-relative-paths checkout.
        (self.work.parent / ('.' + self.work.name + '.merge-group.json')).unlink()
        self.r.git('worktree', 'repair', '--no-relative-paths', str(self.work))
        pointer = (self.work / '.git').read_text().strip().removeprefix('gitdir: ')
        self.assertTrue(Path(pointer).is_absolute() or PureWindowsPath(pointer).is_absolute())

        def assert_repaired(repo, *args, **kw):
            if Path(repo) == self.work:
                pointer = (self.work / '.git').read_text().strip().removeprefix('gitdir: ')
                self.assertFalse(Path(pointer).is_absolute() or PureWindowsPath(pointer).is_absolute())
            return real_out(repo, *args, **kw)

        with patch.object(git_client, 'out', assert_repaired):
            tip, applied, conflicts = landing.build_tip(self.r.repo, self.work, base, [])
        self.assertEqual((tip, applied, conflicts), (base, [], []))
        self.assertEqual(self.r.git('rev-parse', 'HEAD', cwd=self.work), base)


@integration
class RepositoryLandingTests(LandingCase):
    def test_replay_pins_reviewed_head_and_reuses_checkpoint(self):
        a = self.task('a', {'a.txt': 'a'})
        (t,) = landing.form_batch(self.conn, self.r.repo, 'origin/main', 1)
        wt = self.r.root / 'mv'
        self.r.git('worktree', 'add', '-q', wt, 'a')
        self.r.commit('late.txt', cwd=wt)
        self.r.git('worktree', 'remove', '--force', wt)
        first = tip, applied, _ = landing.build_tip(self.r.repo, self.work, self.r.git('rev-parse', 'origin/main'), [t])
        self.addCleanup(self.r.git, 'worktree', 'remove', '--force', '--force', str(self.work))
        files = self.r.git('ls-tree', '-r', '--name-only', tip).split()
        self.assertIn('a.txt', files)
        self.assertNotIn('late.txt', files)

        with patch.dict('os.environ', {'GIT_COMMITTER_DATE': '2001-01-01T00:00:00Z'}):
            resumed = landing.build_tip(self.r.repo, self.work, self.r.git('rev-parse', 'origin/main'), [t])
        self.assertEqual(resumed, first)
        self.assertEqual(self.r.git('rev-parse', 'HEAD', cwd=self.work), first[0])

        # A genuine cherry-pick conflict reaches the adapter's conflict list.
        upstream = self.r.branch('upstream', {'a.txt': 'incompatible change'})
        _, applied, conflicts = landing.build_tip(self.r.repo, self.work, upstream, [t])
        self.assertEqual(applied, [])
        self.assertEqual(conflicts, [a])

    def test_hub_retry_adopts_interrupted_ci_and_runs_script_once(self):
        from hub.dispatch import LandingService
        from hub.settings import Settings
        from hub.store import Store

        configs = self.r.root / 'repos'
        configs.mkdir()
        trees = self.r.root / 'worktrees'
        (configs / 'p.toml').write_text(
            f'name="p"\nrepo={json.dumps(str(self.r.repo))}\n'
            f'worktrees={json.dumps(str(trees))}\n')
        self.r.commit('.drover/config.toml', 'schema_version=1\n[scripts]\ntest=".drover/scripts/check.ps1"\n[gate]\ndocs_only=false\n')
        store = Store(self.r.root / 'ledger.db')
        self.addCleanup(store.close)
        settings = Settings(runs_dir=self.r.root / 'receipts')
        service = LandingService(store, settings, repos_dir=configs)
        service.ci.resource_check = resources_go
        ops.set_setting(self.conn, 'dispatch_enabled', 'true')
        work = trees / 'drover-landing-p'
        count, release = self.r.root / 'count', self.r.root / 'release'
        self.addCleanup(release.touch)
        # The script stays live until the test lets the adopted runner finish.
        self.r.commit('.drover/scripts/check.ps1',
                      f"Add-Content '{count}' started\n"
                      f"while (-not (Test-Path '{release}')) {{ Start-Sleep -Milliseconds 50 }}\nexit 0\n")
        self.r.git('push', '-q', 'origin', 'HEAD:main')
        tid = self.task('a', {'a.txt': 'a'})
        def fail_heartbeat(*args):
            deadline = time.monotonic() + 20
            while not count.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(count.exists())
            raise sqlite3.OperationalError('database is locked')
        with patch.object(slots, 'lease_heartbeat', side_effect=fail_heartbeat):
            with self.assertRaises(ci.CIInterrupted):
                service.tick()
        self.assertTrue(work.is_dir())
        self.assertTrue((work.parent / ('.' + work.name + '.merge-group.json')).is_file())
        self.assertEqual(len(slots.lease_queue(self.conn)), 1)
        self.assertEqual(self.status(tid), 'reviewed')
        release.touch()
        # A fresh hub service must adopt saved execution before rebuilding the checkout.
        service = LandingService(store, settings, repos_dir=configs)
        service.ci.resource_check = resources_go
        result = service.tick()['p']
        self.assertEqual(result['outcome'], 'landed')
        self.assertEqual(count.read_text().splitlines(), ['started'])
        self.assertFalse(work.exists())
        self.assertEqual(slots.lease_queue(self.conn), [])
        self.assertEqual(len(list(settings.runs_dir.glob('ci-*/status.json'))), 1)


@integration
class RealRunnerLandingTests(LandingCase):
    """Real runner receipts wire green and red execution into the landing driver."""

    def make_ci(self, runs):
        return ci.CI(runs, poll_s=0.05, resource_check=resources_go)

    def test_green_and_red_receipts_drive_landing(self):
        # Main lives in a checkout whose path has spaces: landing advances it there, through real git.
        # Moved main, a dirty checkout and a main with no checkout are LocalFastForwardTests in test_landing_recovery.
        self.r.git('checkout', '-q', '--detach')
        checkout = self.r.root / 'main checkout with spaces'
        self.r.git('worktree', 'add', '-q', str(checkout), 'main')
        detached = self.r.git('rev-parse', 'HEAD')
        ids = [self.task(n, {f'{n}.txt': n}) for n in 'abc']
        result = self.land()
        self.assertEqual((result['outcome'], result['gates']), ('landed', 1))
        self.assertEqual(sorted(result['landed']), sorted(ids))
        self.assertEqual(self.r.remote_head(), result['tip'])
        self.assertEqual(self.r.git('rev-parse', 'HEAD', cwd=checkout), result['tip'])
        self.assertEqual(self.r.git('status', '--porcelain', cwd=checkout), '')
        self.assertTrue(all((checkout / f'{n}.txt').read_text() == n for n in 'abc'))
        self.assertEqual(self.r.git('rev-parse', 'HEAD'), detached)
        for tid in ids:
            self.assertEqual(self.status(tid), 'landed')
        bad = self.task('bad', {'bad.txt': 'BAD'})
        result = self.land()
        self.assertEqual((result['outcome'], result['dropped']), ('red', {bad: 'single'}))
        self.assertEqual(self.status(bad), 'ready')
        self.assertEqual(self.r.remote_head(), self.r.git('rev-parse', 'origin/main'))
