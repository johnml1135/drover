from drover import placement
import json
import sqlite3
import time
import sys
import subprocess
import unittest
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

from drover import ci, landing, landing_batch, landing_control, origin, repos
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
        real = landing.git
        base = self.r.git('rev-parse', 'main')
        self.r.git('config', 'core.autocrlf', 'true')
        calls = []

        def git(repo, *args, **kw):
            if 'add' in args:
                calls.append(args)
                if '--relative-paths' in args:
                    return subprocess.CompletedProcess(args, 129, '', "error: unknown option `relative-paths'")
            return real(repo, *args, **kw)

        with patch.object(landing_batch, 'git', git), patch.object(placement, 'is_linux_native', return_value=False) as _placement_patch_1, patch('drover.placement_exec.is_linux_native', _placement_patch_1), patch('drover.placement_host.is_linux_native', _placement_patch_1), patch('drover.placement_paths.is_linux_native', _placement_patch_1), patch.object(placement, 'routes_through_wsl', return_value=False) as _placement_patch_2, patch('drover.placement_git.routes_through_wsl', _placement_patch_2), patch('drover.placement_paths.routes_through_wsl', _placement_patch_2):
            with self.assertRaisesRegex(ci.CIError, 'both Windows and WSL: update git'):
                landing.build_tip(self.r.repo, self.work, base, [])
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.work.exists())
        calls.clear()
        with patch.object(landing_batch, 'git', git), patch.object(placement, 'is_linux_native', return_value=True) as _placement_patch_3, patch('drover.placement_exec.is_linux_native', _placement_patch_3), patch('drover.placement_host.is_linux_native', _placement_patch_3), patch('drover.placement_paths.is_linux_native', _placement_patch_3), patch.object(placement, 'routes_through_wsl', return_value=False) as _placement_patch_4, patch('drover.placement_git.routes_through_wsl', _placement_patch_4), patch('drover.placement_paths.routes_through_wsl', _placement_patch_4):
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
            return real(repo, *args, **kw)

        with patch.object(landing_batch, 'git', assert_repaired):
            tip, applied, conflicts = landing.build_tip(self.r.repo, self.work, base, [])
        self.assertEqual((tip, applied, conflicts), (base, [], []))
        self.assertEqual(self.r.git('rev-parse', 'HEAD', cwd=self.work), base)


@integration
class RepositoryLandingTests(LandingCase):
    def test_advances_main_in_each_checkout_location(self):
        # First land through main's original checkout.
        tid = self.task('first', {'base.txt': 'landed content'})
        first = self.land(push=False)
        self.assertEqual(first['landed'], [tid])
        self.assertEqual(self.r.git('rev-parse', 'HEAD'), first['tip'])
        self.assertEqual(self.r.git('status', '--porcelain'), '')
        self.assertEqual((self.r.repo / 'base.txt').read_text(), 'landed content')

        # Then main lives in a different checkout with spaces in its path.
        self.r.git('checkout', '-q', '--detach')
        checkout = self.r.root / 'main checkout with spaces'
        self.r.git('worktree', 'add', '-q', str(checkout), 'main')
        tid = self.task('second', {'base.txt': 'second content'}, base='main')
        second = self.land(push=False)
        self.assertEqual(second['landed'], [tid])
        self.assertEqual(self.r.git('rev-parse', 'HEAD', cwd=checkout), second['tip'])
        self.assertEqual(self.r.git('status', '--porcelain', cwd=checkout), '')
        self.assertEqual((checkout / 'base.txt').read_text(), 'second content')
        self.assertEqual((self.r.repo / 'base.txt').read_text(), 'landed content')

        # Finally main has no checkout: use CAS and leave detached HEAD alone.
        self.r.git('worktree', 'remove', '--force', str(checkout))
        tid = self.task('third', {'base.txt': 'third content'}, base='main')
        old = self.r.git('rev-parse', 'main')
        with patch.object(landing_control, 'git', wraps=landing.git) as run:
            third = self.land(push=False)
        self.assertEqual(third['landed'], [tid])
        self.assertTrue(any(c.args[1:] == ('update-ref', 'refs/heads/main', third['tip'], old)
                            for c in run.call_args_list))
        self.assertEqual(self.r.git('rev-parse', 'main'), third['tip'])
        self.assertEqual(self.r.git('rev-parse', 'HEAD'), first['tip'])
        self.assertEqual(self.r.git('status', '--porcelain'), '')
        self.assertEqual((self.r.repo / 'base.txt').read_text(), 'landed content')

    def test_moved_and_dirty_main_preserve_owner_state(self):
        self.r.commit('local.txt')
        self.task('feature', {'feature.txt': 'finished'})
        tasks = landing.form_batch(self.conn, self.r.repo, 'main')
        old = self.r.git('rev-parse', 'main')
        tip, tasks, _ = landing.build_tip(self.r.repo, self.work, old, tasks)
        receipt = self.gate(self.work, tip, tasks)
        real = landing.git
        moved = self.r.git('rev-parse', 'main^')

        def run(repo, *args, **kw):
            if args[:2] == ('worktree', 'list'):
                self.r.git('reset', '--hard', moved)
            return real(repo, *args, **kw)

        with patch.object(landing_control, 'git', side_effect=run):
            with self.assertRaisesRegex(ci.CIError, 'local main moved; rebase and regate'):
                landing.local_fast_forward(self.conn, self.r.repo, self.work, 'main', tip, receipt)
        self.assertEqual(self.r.git('rev-parse', 'main'), moved)
        self.assertEqual(self.r.git('status', '--porcelain'), '')
        self.assertIsNone(self.conn.execute('SELECT * FROM landing_lock').fetchone())

        # Edit the owner checkout after the isolated gate succeeds, so the
        # final advance must recheck it and preserve both files and index.
        tid = self.task('dirty-feature', {'base.txt': 'task change\n'}, base='main')
        path = self.r.repo / 'base.txt'
        real_gate = self.gate

        def owner_edits_during_gate(*args):
            receipt = real_gate(*args)
            self.assertEqual(receipt['status'], 'passed')
            path.write_text('owner work\n')
            return receipt

        self.gate = owner_edits_during_gate
        before = self.r.git('rev-parse', 'main')
        remote_before = self.r.remote_head()
        index = (self.r.repo / '.git' / 'index').read_bytes()
        res = self.land(push=False)
        self.assertEqual(res['outcome'], 'refused')
        self.assertEqual(res['gates'], 1)
        self.assertIn(tid, res['batch'])
        self.assertIn('main checkout dirty', res['error'])
        self.assertEqual(self.r.git('rev-parse', 'main'), before)
        self.assertEqual(self.r.remote_head(), remote_before)
        self.assertEqual(path.read_text(), 'owner work\n')
        self.assertEqual((self.r.repo / '.git' / 'index').read_bytes(), index)
        self.assertEqual(self.status(tid), 'reviewed')

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
        ids = [self.task(n, {f'{n}.txt': n}) for n in 'abc']
        result = self.land()
        self.assertEqual((result['outcome'], result['gates']), ('landed', 1))
        self.assertEqual(sorted(result['landed']), sorted(ids))
        self.assertEqual(self.r.remote_head(), result['tip'])
        for tid in ids:
            self.assertEqual(self.status(tid), 'landed')
        bad = self.task('bad', {'bad.txt': 'BAD'})
        result = self.land()
        self.assertEqual((result['outcome'], result['dropped']), ('red', {bad: 'single'}))
        self.assertEqual(self.status(bad), 'ready')
        self.assertEqual(self.r.remote_head(), self.r.git('rev-parse', 'origin/main'))

    def test_base_red_blames_nobody_holds_landing_and_alerts_owner(self):
        self.r.commit('poison.txt', 'BAD')
        self.r.git('push', '-q', 'origin', 'HEAD:main')
        self.r.git('fetch', '-q', 'origin')
        before = self.r.remote_head()
        a, b = (self.task(n, {f'{n}.txt': n}) for n in 'ab')
        res = self.land(size=2)
        self.assertEqual((res['outcome'], res['dropped'], res['landed']), ('base-red', {}, []))
        self.assertEqual(res['base_sha'], before)
        self.assertEqual(res['gates'], 2)  # Failing batch and the base itself.
        self.assertEqual([self.status(t) for t in (a, b)], ['reviewed'] * 2)
        questions = ops.open_questions(self.conn)
        self.assertEqual(len(questions), 1)
        self.assertIn('red', questions[0]['title'])
        self.assertEqual(self.r.remote_head(), before)
        # A held queue starts no runner; explicit retry rechecks the red base.
        with patch.object(self.ci, 'run', wraps=self.ci.run) as run:
            held = self.land(size=2)
            self.assertEqual((held['outcome'], held['gates']), ('held', 0))
            run.assert_not_called()
            ops.answer_question(self.conn, questions[0]['id'], choice=landing.RETRY)
            retry = self.land(size=2)
            self.assertEqual((retry['outcome'], retry['dropped'], retry['landed']), ('base-red', {}, []))
            self.assertEqual(retry['gates'], 2)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args.args[2], before)
        self.assertEqual([self.status(t) for t in (a, b)], ['reviewed'] * 2)
        self.assertEqual(len(ops.open_questions(self.conn)), 1)
        self.assertEqual(self.r.remote_head(), before)
