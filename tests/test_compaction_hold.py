"""The compaction's VHDX step, the hold file, its Inbox notice and the scheduled task."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from drover import compaction, compaction_hold, compaction_vhd, config, disk, disk_runtime, disk_state, ledger, placement
from drover import placement_git, placement_host, resources
from drover.ledger import disk_notices
from drover.port import DirectPort
from tests.disk_fixture import DiskCase
from tests.compaction_fixture import Clock, FakeRun, NOW, ROOT, VHDX, Vhdx, done


class Vhd(unittest.TestCase):
    def compact(self, fake):
        return compaction_vhd.compact(VHDX, run=fake, sleep=lambda seconds: None, size=lambda path: 7.0)

    def vhd_calls(self, fake):
        return [call for call in fake.calls if '-VHD ' in call]

    def test_vhd_is_mounted_read_only(self):
        fake = FakeRun()
        self.assertEqual(self.compact(fake), dict(status='compacted', before_gb=7.0, after_gb=7.0, errors=[], dismounted=True))
        script = self.vhd_calls(fake)
        self.assertEqual(len(script), 3)
        self.assertIn('Mount-VHD -Path', script[0])
        self.assertIn('Optimize-VHD -Path', script[1])
        self.assertIn('.Attached', script[2])
        self.assertIn('Dismount-VHD -Path', script[2])
        self.assertIn('-ReadOnly', script[0])
        self.assertIn('-NoDriveLetter', script[0])
        self.assertIn('-Mode Full', script[1])
        self.assertIn(f"-Path '{VHDX}'", script[0])

    def test_every_vhd_step_fails_loudly(self):
        fake = FakeRun()
        self.compact(fake)
        for call in self.vhd_calls(fake):
            self.assertIn('-ErrorAction Stop', call)

    def test_the_path_is_quoted(self):
        fake = FakeRun()
        compaction_vhd.compact("H:\\o'brien\\ext4.vhdx", run=fake, sleep=lambda s: None, size=lambda p: None)
        self.assertTrue(fake.seen("'H:\\o''brien\\ext4.vhdx'"))

    def test_a_held_file_is_waited_for_then_fails_safely(self):
        fake = FakeRun(**{'IO.File': done('', 1)})
        waits = []
        answer = compaction_vhd.compact(VHDX, run=fake, sleep=waits.append, size=lambda p: None)
        self.assertEqual(answer['status'], 'failed')
        self.assertEqual(sum(waits), compaction_vhd.RELEASE_WAIT_S)
        self.assertFalse(fake.seen('Mount-VHD'))

    def test_an_unknown_running_list_fails_safely(self):
        fake = FakeRun(**{'--running': OSError('no wsl.exe')})
        self.assertEqual(self.compact(fake)['status'], 'failed')
        self.assertFalse(fake.seen('Mount-VHD'))

    def test_no_running_distributions_is_how_wsl_says_none(self):
        fake = FakeRun(**{'--running': done('There are no running distributions.', 1)})
        self.assertEqual(self.compact(fake)['status'], 'compacted')

    def test_mount_failure_fails_without_optimizing_and_counts_as_dismounted(self):
        fake = Vhdx(refuse_mount=True)
        answer = self.compact(fake)
        self.assertEqual(answer['status'], 'failed')
        self.assertTrue(answer['dismounted'])
        self.assertEqual(len(answer['errors']), 1)
        self.assertFalse(fake.seen('Optimize-VHD'))

    def test_a_timed_out_optimize_is_a_failure_and_dismounts(self):
        fake = FakeRun(**{'Optimize-VHD': subprocess.TimeoutExpired('pwsh', 1)})
        self.assertEqual(self.compact(fake)['status'], 'failed')
        self.assertTrue(fake.seen('Dismount-VHD'))

    def test_the_optimize_timeout_is_inside_the_hold(self):
        self.assertLess(compaction_vhd.OPTIMIZE_TIMEOUT_S, compaction_hold.MAX_AGE_S)


class Hold(DiskCase):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        patcher = mock.patch.object(compaction_hold, 'clock', self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def live(self, phase='draining'):
        record = compaction_hold.claim(self.state, at=self.clock(), pid=os.getpid(), birth='b')
        record['phase'] = phase
        disk_state.save_file(self.state, compaction_hold.HOLD_FILE, record)
        return record

    def test_no_file_holds_nothing(self):
        self.assertIsNone(compaction_hold.active(self.state))
        self.assertFalse(disk.held('wsl', self.cfg))

    def test_live_file_holds_wsl_work_only(self):
        self.live()
        self.assertTrue(disk.held('wsl', self.cfg))
        self.assertFalse(disk.held('windows', self.cfg))

    def test_a_hold_older_than_two_hours_is_ignored(self):
        self.live()
        self.clock.now += 2 * 3600 - 1
        self.assertTrue(compaction_hold.active(self.state))
        self.clock.now += 1
        self.assertIsNone(compaction_hold.active(self.state))
        self.assertFalse(disk.held('wsl', self.cfg))

    def test_a_corrupt_hold_is_ignored(self):
        (self.state / 'disk').mkdir(exist_ok=True)
        (self.state / 'disk' / compaction_hold.HOLD_FILE).write_text('{', encoding='utf-8')
        self.assertIsNone(compaction_hold.active(self.state))

    def test_resources_hold_the_wsl_side_only(self):
        self.live()
        seat = self.cfg['seats'][0]
        cfg = dict(self.cfg, seats=[dict(seat, placement='windows'), dict(seat, name='w', placement='wsl')])
        fake = dict(platform='windows', cores=8, load5=0.0, ram_available_gb=64.0,
                    disk_free_gb={'test-drive': 900.0}, wsl_up=True)
        answer = resources.check(fake=fake, config=cfg)
        self.assertEqual(answer['verdict'], 'go')
        self.assertEqual(answer['side_admission']['wsl']['verdict'], 'hold')
        self.assertEqual(answer['side_admission']['windows']['verdict'], 'go')

    def test_a_cleanup_due_in_the_window_waits(self):
        self.live()
        enqueue = mock.Mock()
        with mock.patch.object(placement, 'cleanup_identity', return_value=dict(pid=1, birth='b')):
            disk.tick(self.cfg, now=self.clock(), reader=lambda path: 10.0, enqueue=enqueue, wsl_used=lambda: 600.0)
        enqueue.assert_not_called()
        self.assertEqual(disk_state.read(self.state)['cleanup_deferred']['verdict'], 'compaction')

    def test_the_wsl_stage_of_cleanup_does_not_call_wsl(self):
        self.live()
        with mock.patch.object(disk_runtime.subprocess, 'run') as run, mock.patch.object(disk_runtime.placement, 'IS_WINDOWS', True):
            answer = disk_runtime.cleanup_call('wsl', dict(state_dir=str(self.state), repos=[], paths={}))
        run.assert_not_called()
        self.assertEqual(answer, dict(items=[], errors=[dict(reason_code='compaction_window')], reclaimed_bytes=0))

    def test_a_df_reading_returns_its_cached_value(self):
        self.addCleanup(placement._WSL_USED.clear)
        with mock.patch.object(placement_host, 'IS_WINDOWS', True):
            run = mock.Mock(return_value=done('h\n%d\n' % 1024 ** 3))
            self.assertEqual(placement.wsl_used_gb('U', True, run=run), 1)
            self.live()
            run.reset_mock()
            self.assertEqual(placement.wsl_used_gb('U', True, run=run, ttl=0), 1)
            self.assertIsNone(placement.wsl_used_gb('Other', True, run=run, ttl=0))
            run.assert_not_called()

    def test_offline_refuses_any_wsl_command_as_an_os_error(self):
        self.live('offline')
        with self.assertRaises(OSError):
            placement.wsl_control(['-l'])
        with self.assertRaises(OSError):
            placement.WSL('U').command(['true'])

    def test_git_in_wsl_offline_is_a_failed_command(self):
        self.cfg['paths']['wsl_distro'] = 'Ubuntu'
        self.live('offline')
        with mock.patch.object(placement_git, 'routes_through_wsl', return_value=True):
            self.assertEqual(placement_git.git('/home/j/x', 'status').returncode, 127)

    def test_draining_still_lets_a_wsl_command_through(self):
        self.live('draining')
        self.assertEqual(placement.wsl_control(['-l']), ['wsl.exe', '-l'])


class Notice(unittest.TestCase):
    def test_the_notice_reaches_the_inbox_through_the_existing_path(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            state = Path(folder)
            answer = dict(status='failed', stopped=[], killed=[], requeued=[], before_gb=None, after_gb=None, errors=['x'])
            compaction.report(state, answer, NOW)
            conn = ledger.connect(state / 'l.db')
            self.addCleanup(conn.close)
            with mock.patch.object(config, 'state_dir', return_value=state):
                delivered = disk_notices.replay(conn, {}, NOW)
            self.assertEqual(len([ident for ident in delivered if ident.startswith('compaction:')]), 1)
            self.assertEqual(DirectPort(conn).events('disk.notice')[0]['data']['notice']['kind'], 'compaction')


class Install(unittest.TestCase):
    def test_the_task_runs_nightly_elevated_as_the_user(self):
        text = (ROOT / 'scripts' / 'Install-CompactionTask.ps1').read_text(encoding='utf-8')
        for needle in ("'DroverCompaction'", '-At 03:30', '-RunLevel Highest', '-Daily', 'drover.compaction',
                       '-ExecutionTimeLimit'):
            self.assertIn(needle, text)
        self.assertNotIn('-StartWhenAvailable', text)
        self.assertIn(r"$Root = 'H:\repos\drover.deploy'", text)
        self.assertIn('DROVER_HOME=$StateDir', text)
        self.assertNotIn('SYSTEM', text)
        self.assertIn(r"$Python = (Join-Path $Root 'hub\.venv\Scripts\python.exe')", text)
        self.assertNotIn('Mandatory', text)
        self.assertNotRegex(text, r'"python(\.exe)?"')


if __name__ == '__main__':
    unittest.main()
