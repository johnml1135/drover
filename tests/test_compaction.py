"""The overnight WSL compaction: every command goes through a fake runner, so WSL and the VHDX are never touched."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from drover import compaction, compaction_hold, compaction_vhd, config, disk, disk_runtime, disk_state, ledger, placement
from drover import placement_exec, placement_git, placement_host, resources, usage
from drover.launch import RunnerLauncher
from drover.ledger import disk_notices
from drover.port import DirectPort
from drover.runner import ScanError
from tests.disk_fixture import DiskCase
from tests.test_dispatcher import Base, res

NOW = 2_000_000_000.0
VHDX = r'H:\WSL\Ubuntu\ext4.vhdx'
ROOT = Path(__file__).resolve().parents[1]
DONE_RECORD = '{"status": "done", "sha": "' + 'a' * 40 + '", "blocker": null, "note": "ok"}'


def done(stdout='', returncode=0):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr='')


class FakeRun:
    """Records every argv. `answers` maps a substring of the joined argv to a result, or to an exception to raise."""

    def __init__(self, **answers):
        self.calls, self.answers = [], answers

    def __call__(self, argv, **_kw):
        text = ' '.join(argv)
        self.calls.append(text)
        for key, value in self.answers.items():
            if key in text:
                if isinstance(value, Exception):
                    raise value
                return value
        return done()

    def seen(self, text):
        return [call for call in self.calls if text in call]


class Vhdx(FakeRun):
    """A disk with attached state. Like the real command, Dismount-VHD refuses a disk that is not attached."""

    def __init__(self, refuse_mount=False, refuse_dismount=False, attached=False):
        super().__init__()
        self.refuse_mount, self.refuse_dismount, self.attached = refuse_mount, refuse_dismount, attached

    def __call__(self, argv, **kw):
        text = ' '.join(argv)
        if ' Mount-VHD ' in text:
            self.calls.append(text)
            self.attached = not self.refuse_mount
            return done('', 1 if self.refuse_mount else 0)
        if 'Dismount-VHD' in text:
            self.calls.append(text)
            if not self.attached:
                return done('', 0 if '.Attached' in text else 1)
            if self.refuse_dismount:
                return done('', 1)
            self.attached = False
            return done()
        return super().__call__(argv, **kw)


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Job(DiskCase):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.fake = FakeRun()
        self.ack = dict(stopped=[1], killed=[2], requeued=['T-1'], drained=True)
        patcher = mock.patch.object(compaction_hold, 'clock', self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def runtime(self, cls=compaction.Runtime):
        job = cls(self.cfg, run=self.fake, clock=self.clock, sleep=self.clock.sleep)
        job.vhdx = lambda: VHDX
        job.wait_for_hub = lambda record: dict(self.ack)
        return job

    def run_job(self, cls=compaction.Runtime):
        return compaction.run(self.cfg, runtime=self.runtime(cls), now=NOW)

    def hold(self):
        return compaction_hold.read(self.state)

    def report(self):
        return json.loads((self.state / 'disk' / compaction_hold.REPORT_FILE).read_text(encoding='utf-8'))

    def outbox(self):
        return disk_state.read(self.state).get('outbox', {})


class Order(Job):
    def test_order_is_hold_stop_kill_trim_shutdown_compact_restart_release(self):
        order = []

        class Recording(compaction.Runtime):
            def claim(self, at):
                order.append('hold')
                return super().claim(at)

            def wait_for_hub(self, record):
                order.append('stop')
                return dict(stopped=[], killed=[], requeued=[], drained=True)

            def kill(self):
                order.append('kill')
                return []

            def trim(self):
                order.append('trim')

            def shutdown(self):
                order.append('shutdown')

            def compact(self, vhdx):
                order.append('compact')
                return dict(status='compacted', before_gb=10.0, after_gb=4.0, errors=[])

            def restart(self):
                order.append('restart')

            def release(self, record):
                order.append('release')
                super().release(record)
        job = Recording(self.cfg, run=self.fake, clock=self.clock, sleep=self.clock.sleep)
        job.vhdx = lambda: VHDX
        panes = []
        with mock.patch.object(compaction, 'pane_stop', panes.append):
            answer = compaction.run(self.cfg, runtime=job, now=NOW)
        self.assertEqual(order, ['hold', 'stop', 'kill', 'trim', 'shutdown', 'compact', 'restart', 'release'])
        self.assertEqual(panes, [job])
        self.assertEqual(answer, dict(status='compacted', stopped=[], killed=[], requeued=[], before_gb=10.0, after_gb=4.0,
                                      errors=[]))
        self.assertIsNone(self.hold())

    def test_each_step_is_stored_and_shutdown_takes_wsl_offline(self):
        seen = []

        class Watching(compaction.Runtime):
            def save(self, record):
                super().save(record)
                seen.append((record['step'], record['phase']))
        self.run_job(Watching)
        self.assertEqual(seen, [('stop', 'draining'), ('kill', 'draining'), ('trim', 'draining'), ('shutdown', 'offline'),
                                ('compact', 'offline'), ('restart', 'offline'), ('release', 'offline')])

    def test_the_hub_report_reaches_the_result(self):
        answer = self.run_job()
        self.assertEqual((answer['stopped'], answer['requeued']), ([1], ['T-1']))
        self.assertEqual(answer['killed'], [dict(run=2)])

    def test_the_job_issues_shutdown_and_restart_itself(self):
        self.run_job()
        self.assertEqual(self.fake.seen('--shutdown'), ['wsl.exe --shutdown'])
        self.assertEqual(self.fake.seen('--exec true'), ['wsl.exe -d Disk Test Distro --exec true'])


class Kill(Job):
    def test_kill_covers_unaccounted_processes_by_pid(self):
        table = ('  101 /usr/bin/python3 -m drover.runner --run-id 9\n  102 node /home/j/.local/bin/claude -p x\n'
                 '  103 /usr/bin/codex exec\n  104 /usr/sbin/cron -f\n  105 ps -eo pid=,args=\n')
        self.fake.answers['ps -eo'] = done(table)
        answer = self.run_job()
        self.assertEqual(answer['killed'], [dict(run=2), dict(pid=101), dict(pid=102), dict(pid=103)])
        kills = self.fake.seen('kill -9')
        self.assertEqual(len(kills), 1)
        self.assertTrue(kills[0].endswith('kill -9 101 102 103'))
        self.assertIn('-u root', kills[0])

    def test_a_failed_kill_is_recorded_and_the_job_goes_on(self):
        self.fake.answers['ps -eo'] = done('', 1)
        answer = self.run_job()
        self.assertEqual(answer['status'], 'compacted')
        self.assertTrue(any('process list' in error for error in answer['errors']))


class Failure(Job):
    def test_optimize_failure_dismounts_restarts_releases_reports(self):
        self.fake.answers['Optimize-VHD'] = done('', 1)
        answer = self.run_job()
        self.assertEqual(answer['status'], 'failed')
        self.assertTrue(self.fake.seen('Dismount-VHD'))
        self.assertTrue(self.fake.seen('--exec true'))
        self.assertIsNone(self.hold())
        self.assertEqual(self.report()['status'], 'failed')
        notice = next(iter(self.outbox().values()))['notice']
        self.assertEqual(notice['kind'], 'compaction')
        self.assertIn('failed', notice['body'])

    def test_running_distro_before_mount_fails_safely(self):
        self.fake.answers['--running'] = done('Ubuntu\n')
        answer = self.run_job()
        self.assertEqual(answer['status'], 'failed')
        self.assertFalse(self.fake.seen('Mount-VHD'))
        self.assertFalse(self.fake.seen('Optimize-VHD'))
        self.assertTrue(self.fake.seen('--exec true'))
        self.assertIsNone(self.hold())
        self.assertTrue(any('running' in error for error in answer['errors']))

    def test_a_failed_shutdown_compacts_nothing(self):
        self.fake.answers['--shutdown'] = done('', 1)
        answer = self.run_job()
        self.assertEqual(answer['status'], 'failed')
        self.assertFalse(self.fake.seen('Mount-VHD'))
        self.assertTrue(self.fake.seen('--exec true'))
        self.assertIsNone(self.hold())

    def test_a_failed_restart_is_a_failure_even_after_compaction(self):
        self.fake.answers['--exec true'] = done('', 1)
        self.assertEqual(self.run_job()['status'], 'failed')
        self.assertIsNone(self.hold())

    def test_a_missing_distro_disk_fails_and_still_releases(self):
        job = self.runtime()
        job.vhdx = mock.Mock(side_effect=KeyError('Ubuntu'))
        answer = compaction.run(self.cfg, runtime=job, now=NOW)
        self.assertEqual(answer['status'], 'failed')
        self.assertFalse(self.fake.seen('--shutdown'))
        self.assertIsNone(self.hold())

    def test_a_record_that_cannot_be_written_stops_no_restart_release_or_report(self):
        class Unrecorded(compaction.Runtime):
            def save(self, record):
                raise RuntimeError('state directory is unwell')
        answer = self.run_job(Unrecorded)
        self.assertEqual(answer['status'], 'failed')
        self.assertTrue(self.fake.seen('--exec true'))
        self.assertFalse(self.fake.seen('--shutdown'))              # the hub was never told WSL goes offline
        self.assertIsNone(self.hold())
        self.assertEqual(self.report()['status'], 'failed')

    def test_only_the_closing_records_failing_changes_nothing_but_the_errors(self):
        class Closing(compaction.Runtime):
            def save(self, record):
                if record['step'] in ('restart', 'release'):
                    raise ValueError('nope')
                super().save(record)
        answer = self.run_job(Closing)
        self.assertEqual(answer['status'], 'compacted')
        self.assertTrue(self.fake.seen('--exec true'))
        self.assertIsNone(self.hold())
        self.assertEqual(len(self.outbox()), 1)

    def test_a_release_that_raises_anything_is_still_reported(self):
        class Stuck(compaction.Runtime):
            def release(self, record):
                raise ValueError('nope')
        self.assertEqual(self.run_job(Stuck)['status'], 'failed')
        self.assertEqual(self.report()['status'], 'failed')

    def test_a_compacted_night_with_errors_raises_a_notice(self):
        self.fake.answers['ps -eo'] = done('', 1)
        self.assertEqual(self.run_job()['status'], 'compacted')
        notice = next(iter(self.outbox().values()))['notice']
        self.assertIn('compacted with errors', notice['title'])
        self.assertIn('process list', notice['body'])

    def test_no_hub_answer_is_reported_and_the_job_goes_on(self):
        self.ack = dict(stopped=[], killed=[], requeued=[], drained=False)
        answer = self.run_job()
        self.assertEqual(answer['status'], 'compacted')
        self.assertTrue(any('hub' in error for error in answer['errors']))

    def test_success_raises_no_notice(self):
        self.assertEqual(self.run_job()['status'], 'compacted')
        self.assertEqual(self.report()['status'], 'compacted')
        self.assertEqual(self.outbox(), {})


class Waiting(Job):
    def test_the_job_waits_for_the_hub_within_a_deadline(self):
        job = compaction.Runtime(self.cfg, run=self.fake, clock=self.clock, sleep=self.clock.sleep)
        record = compaction_hold.claim(self.state, at=NOW, pid=os.getpid(), birth='b')
        answer = job.wait_for_hub(record)
        self.assertFalse(answer['drained'])
        self.assertEqual(self.clock(), NOW + compaction.DRAIN_WAIT_S)

    def test_a_drained_answer_from_another_hold_is_ignored(self):
        job = compaction.Runtime(self.cfg, run=self.fake, clock=self.clock, sleep=self.clock.sleep)
        record = compaction_hold.claim(self.state, at=NOW, pid=os.getpid(), birth='b')
        compaction_hold.save_drain(self.state, dict(id='old', stopped=[], killed=[], requeued=[], drained=True))
        self.assertFalse(job.wait_for_hub(record)['drained'])
        compaction_hold.save_drain(self.state, dict(id=record['id'], stopped=[5], killed=[], requeued=[], drained=True))
        self.assertEqual(job.wait_for_hub(record)['stopped'], [5])

    def test_the_wait_allows_the_grace_and_the_kill(self):
        self.assertGreater(compaction.DRAIN_WAIT_S, compaction_hold.WIND_DOWN_S + 60)


class Skip(Job):
    def test_second_run_while_active_is_skipped(self):
        first = compaction_hold.claim(self.state, at=NOW, pid=os.getpid(), birth=placement.cleanup_identity()['birth'])
        answer = self.run_job()
        self.assertEqual(answer['status'], 'skipped')
        self.assertEqual(self.fake.calls, [])                       # no restart: this run holds nothing
        self.assertEqual(self.hold()['id'], first['id'])
        self.assertEqual(len(self.outbox()), 1)

    def test_a_skip_does_not_swallow_that_nights_failure(self):
        compaction.report(self.state, dict(status='skipped', errors=['x']), NOW)
        compaction.report(self.state, dict(status='failed', errors=['y']), NOW)
        self.assertEqual(len(self.outbox()), 2)

    def test_a_stale_hold_is_replaced(self):
        compaction_hold.claim(self.state, at=NOW - compaction_hold.MAX_AGE_S - 1, pid=os.getpid(), birth='b')
        self.assertEqual(self.run_job()['status'], 'compacted')

    def test_a_dead_holder_is_replaced(self):
        compaction_hold.claim(self.state, at=NOW, pid=999999, birth='b')
        with mock.patch.object(placement, 'cleanup_identity', return_value=dict(pid=999999, birth='absent')):
            self.assertEqual(self.run_job()['status'], 'compacted')

    def test_unreadable_holder_is_not_dead(self):
        record = compaction_hold.claim(self.state, at=NOW, pid=77, birth='b')
        with mock.patch.object(placement, 'cleanup_identity', return_value=dict(pid=77, birth=None)):
            self.assertIsNone(compaction.recover(self.cfg))
        self.assertEqual(self.hold()['id'], record['id'])

    def test_release_needs_the_holders_token(self):
        compaction_hold.claim(self.state, at=NOW, pid=1, birth='b')
        compaction_hold.release(self.state, 'someone-else')
        self.assertIsNotNone(self.hold())


class Recovery(Job):
    def test_hub_start_releases_a_dead_job_and_says_so(self):
        record = compaction_hold.claim(self.state, at=NOW, pid=999999, birth='b')
        record.update(step='compact', phase='offline')
        disk_state.save_file(self.state, compaction_hold.HOLD_FILE, record)
        with mock.patch.object(placement, 'cleanup_identity', return_value=dict(pid=999999, birth='absent')):
            answer = compaction.recover(self.cfg)
        self.assertEqual(answer['status'], 'failed')
        self.assertIsNone(self.hold())
        notice = next(iter(self.outbox().values()))['notice']
        self.assertIn('compact', notice['body'])

    def test_a_mount_left_by_a_crash_is_dismounted_first(self):
        compaction_hold.note_mount(self.state, VHDX)
        self.run_job()
        self.assertLess(self.fake.calls.index(self.fake.seen('Dismount-VHD')[0]),
                        self.fake.calls.index(self.fake.seen('--shutdown')[0]))
        self.assertIsNone(compaction_hold.mounted(self.state))

    def test_the_mount_is_noted_before_mounting_and_cleared_after(self):
        noted = []
        recording = self.fake

        def mount(argv, **kw):
            if any(word == 'Mount-VHD' for word in ' '.join(argv).split()):
                noted.append(compaction_hold.mounted(self.state))
            return recording(argv, **kw)
        self.fake = mount
        compaction.run(self.cfg, runtime=self.runtime(), now=NOW)
        self.assertEqual(noted, [VHDX])
        self.assertIsNone(compaction_hold.mounted(self.state))

    def test_an_attached_disk_that_will_not_dismount_keeps_the_mount_note(self):
        self.fake = Vhdx(refuse_dismount=True)
        self.assertEqual(self.run_job()['status'], 'failed')
        self.assertEqual(compaction_hold.mounted(self.state), VHDX)

    def test_a_failed_mount_leaves_no_note_to_fail_later_nights(self):
        self.fake = Vhdx(refuse_mount=True)
        self.assertEqual(self.run_job()['status'], 'failed')
        self.assertIsNone(compaction_hold.mounted(self.state))
        self.fake = Vhdx()
        self.assertEqual(self.run_job()['status'], 'compacted')

    def test_a_reboot_while_mounted_clears_the_note_on_the_next_night(self):
        compaction_hold.note_mount(self.state, VHDX)
        self.fake = Vhdx()                                          # the reboot detached it
        self.assertEqual(self.run_job()['status'], 'compacted')
        self.assertIsNone(compaction_hold.mounted(self.state))


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


class Hubbed(Base):
    """A dispatcher and ledger on a private state directory."""

    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)
        patcher = mock.patch.dict(os.environ, {'DROVER_HOME': str(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)
        config.reset()
        self.addCleanup(config.reset)


class Polling(Hubbed):
    """Every timer in the hub, with a spy on every binding of wsl_control."""

    def setUp(self):
        super().setUp()
        compaction_hold.claim(self.state, at=compaction_hold.clock(), pid=os.getpid(), birth='b')
        self.calls = []

        def spy(args):
            self.calls.append(args)
            raise AssertionError(f'wsl.exe during the window: {args}')
        from hub import watchdog
        for module in (placement_exec, placement_host, placement, watchdog):
            patcher = mock.patch.object(module, 'wsl_control', spy)
            patcher.start()
            self.addCleanup(patcher.stop)
        win = mock.patch.object(placement_host, 'IS_WINDOWS', True)
        win.start()
        self.addCleanup(win.stop)
        self.addCleanup(placement._WSL_USED.clear)
        self.addCleanup(placement._WSL_FREE.clear)

    def test_polling_makes_no_wsl_call_during_window(self):
        from hub.services import Services
        from hub.watchdog import Watchdog
        run = mock.Mock(side_effect=AssertionError('run called'))
        self.assertIsNone(placement.wsl_free_gb('U', True, run=run, ttl=0))
        self.assertIsNone(placement.wsl_used_gb('U', True, run=run, ttl=0))
        self.assertFalse(placement.wsl_running('U', run=run))
        self.assertEqual(resources.read_wsl('U', True, True), (False, None))
        self.assertIsNone(resources.read_wsl_ram_gb('U', run=run))
        self.assertIsNone(placement.wsl_zoneinfo_path('Etc/UTC'))
        cfg = config.get()
        self.assertIsNone(disk._wsl_used({**cfg, 'trackers': [dict(kind='disk', wsl_combined=True), *cfg['trackers']]}))
        launcher = RunnerLauncher(runs_dir=self.state, run=run, windows=True, distro='U')
        self.assertFalse(launcher.wsl_up())
        with self.assertRaises(ScanError):
            launcher.scan(True)
        store = mock.MagicMock()
        store._json_setting.return_value = {'ts': 0}
        store.get_settings.return_value = {}
        keepalive = mock.Mock()
        keepalive.poll.return_value = None
        settings = SimpleNamespace(wsl_needed_ttl_s=9e12, distro='U', wsl_timeout_s=1, wsl_boot_timeout_s=1, keepalive_s=1)
        dog = Watchdog(store, settings, wsl=mock.Mock(side_effect=AssertionError('watchdog called wsl')),
                       keepalive=mock.Mock(side_effect=AssertionError('keepalive started')), now=lambda: 0)
        dog.keepalive = keepalive
        self.assertEqual(dog.check(), 'compaction-hold')
        keepalive.kill.assert_called_once()
        services = Services(store, SimpleNamespace(runs_dir=str(self.state / 'r'), wsl_timeout_s=1, distro='U'),
                            wsl=mock.Mock(side_effect=AssertionError('services called wsl')))
        services.check_wsl()
        self.assertFalse(services.wsl_up())
        self.assertEqual(services.discover_tools(), {})
        poller = usage.Poller(self.conn, run=run, windows=True, launcher=launcher)
        seat = dict(name='w', backend='codex', placement='wsl', binary='codex', config_dir='')
        with mock.patch.object(usage, 'headroom', return_value='kept'):
            self.assertEqual(poller.refresh(seat, 0), 'kept')
        self.assertEqual(self.calls, [])

    def test_a_hub_restart_in_the_window_checks_no_seat_home(self):
        from hub import dispatch
        service = mock.Mock(_seat_home_distro=None)
        service._distro = lambda: 'U'
        with mock.patch('drover.seat_paths.validate', side_effect=AssertionError('validated')):
            dispatch.DispatcherService._validate_seat_homes(service, mock.Mock())

    def test_landing_waits_for_the_window(self):
        from hub import dispatch
        service = mock.Mock(store=SimpleNamespace(conn=self.conn))
        with mock.patch.object(dispatch, 'Pipeline', side_effect=AssertionError('landing started')):
            self.assertEqual(dispatch.LandingService.tick(service), {})


class WindDown(Hubbed):
    """The hub sees the file and winds WSL work down through the durable stop and the run requeue."""

    def setUp(self):
        super().setUp()
        self.clock = Clock()
        patcher = mock.patch.object(compaction_hold, 'clock', self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.allow['codex'] = False
        self.stops = []
        self.fl.cancel = lambda run_id, nonce=None: self.stops.append(run_id) or True
        self.d = self.make(now=self.clock)
        self.tid = self.task(size='S')
        self.d.tick()
        self.rid = self.port.list_runs(self.tid)[0]['id']

    def side(self, name):
        self.d.live[self.rid][2]['placement'] = name

    def hold(self, phase='draining'):
        record = compaction_hold.claim(self.state, at=self.clock(), pid=os.getpid(), birth='b')
        record['phase'] = phase
        disk_state.save_file(self.state, compaction_hold.HOLD_FILE, record)
        return record

    def ack(self):
        return compaction_hold.read_drain(self.state)

    def outcome(self):
        return self.port.list_runs(self.tid)[0]['outcome']

    def test_headless_gets_300s_then_killed_and_requeued(self):
        self.side('wsl')
        self.verdict = dict(self.verdict, verdict='hold')           # WSL work stays held, so nothing relaunches it
        record = self.hold()
        self.clock.now += 299
        self.d.tick()
        self.assertEqual(self.stops, [])
        self.assertFalse(self.ack()['drained'])
        self.assertEqual(self.ack()['id'], record['id'])
        self.clock.now += 1
        self.d.tick()
        self.assertEqual(self.stops, [self.rid])
        self.assertEqual(len(self.events('run.stop-requested')), 1)
        self.finish(0, res('error', 'killed'))
        self.d.tick()
        self.assertEqual(self.outcome(), 'control-held')
        self.assertEqual(self.status(self.tid), 'ready')
        self.assertEqual(self.events('worker-decision'), [])        # no decider, no continuation budget spent
        self.d.tick()
        ack = self.ack()
        self.assertTrue(ack['drained'])
        self.assertEqual((ack['stopped'], ack['killed'], ack['requeued']), ([], [self.rid], [self.tid]))

    def test_a_run_ending_in_the_grace_is_only_stopped(self):
        self.side('wsl')
        self.hold()
        self.d.tick()
        self.finish(0, res('rotate'))
        self.d.tick()
        self.d.tick()
        ack = self.ack()
        self.assertTrue(ack['drained'])
        self.assertEqual((ack['stopped'], ack['killed'], ack['requeued']), ([self.rid], [], []))

    def test_the_grace_counts_from_the_file_not_from_the_hub(self):
        self.side('wsl')
        self.hold()
        self.clock.now += 301
        self.d.tick()                                               # the first tick that ever saw this hold
        self.assertEqual(self.stops, [self.rid])

    def test_windows_work_is_left_alone(self):
        self.side('windows')
        self.hold()
        self.clock.now += 301
        self.d.tick()
        self.assertEqual(self.stops, [])
        self.assertIsNone(self.outcome())
        self.assertTrue(self.ack()['drained'])

    def test_a_stop_is_issued_once(self):
        self.side('wsl')
        self.hold()
        self.clock.now += 301
        self.d.tick()
        self.d.tick()
        self.assertEqual(self.stops, [self.rid])

    def test_a_run_with_a_finished_record_keeps_its_result(self):
        self.side('wsl')
        self.hold()
        self.clock.now += 301
        self.d.tick()
        self.finish(0, res('done', DONE_RECORD))
        self.d.tick()
        self.assertNotEqual(self.outcome(), 'control-held')

    def test_an_unstopped_wsl_run_is_not_finished_offline(self):
        self.side('wsl')
        self.hold('offline')
        self.finish(0, res('done', DONE_RECORD))
        self.d.tick()
        self.assertIsNone(self.outcome())

    def test_a_stopped_review_is_derived_again(self):
        self.side('wsl')
        self.d.live[self.rid][2]['role'] = 'review'
        self.hold()
        self.clock.now += 301
        self.d.tick()
        self.finish(0, res('error', 'killed'))
        self.d.tick()
        runs = self.port.list_runs(self.tid)
        self.assertEqual(runs[0]['outcome'], 'control-held')
        self.assertTrue(self.d._awaiting_review([dict(role='worker', outcome='gated', ended='t'), dict(runs[0], role='review')]))

    def test_a_stopped_run_that_finished_anyway_waits_for_wsl(self):
        self.side('wsl')
        self.hold()
        self.clock.now += 301
        self.d.tick()
        record = compaction_hold.read(self.state)
        record['phase'] = 'offline'
        disk_state.save_file(self.state, compaction_hold.HOLD_FILE, record)
        self.finish(0, res('done', DONE_RECORD))
        self.d.tick()
        self.assertIsNone(self.outcome())
        self.assertIn(self.rid, self.d.live)

    def test_a_ready_task_in_a_wsl_worktree_is_not_claimed(self):
        self.side('windows')
        other = self.task(size='S')
        self.port.update_task(other, worktree='/home/j/work/x')
        self.hold()
        self.d.tick()
        self.assertEqual(self.status(other), 'ready')
        self.assertEqual(self.port.list_runs(other), [])

    def test_after_the_window_the_task_launches_again_with_nothing_left_held(self):
        self.side('wsl')
        go = self.verdict
        self.verdict = dict(go, verdict='hold')
        record = self.hold()
        self.clock.now += 301
        self.d.tick()
        self.finish(0, res('error', 'killed'))
        self.d.tick()
        self.assertEqual(self.status(self.tid), 'ready')
        compaction_hold.release(self.state, record['id'])
        self.verdict = go
        self.d.tick()
        runs = self.port.list_runs(self.tid)
        self.assertEqual([r['outcome'] for r in runs], ['control-held', None])
        self.assertEqual(self.status(self.tid), 'running')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM questions').fetchone()[0], 0)

    def test_a_new_worktree_waits_while_wsl_is_offline(self):
        self.side('windows')
        other = self.task(size='S')
        self.hold('offline')
        self.d.tick()
        self.assertEqual(self.status(other), 'ready')
        self.assertEqual(self.port.list_runs(other), [])

    def test_a_windows_task_with_its_worktree_still_launches_offline(self):
        self.side('windows')
        other = self.task(size='S')
        self.port.update_task(other, worktree=str(self.state))
        self.hold('offline')
        self.d.tick()
        self.assertEqual(self.status(other), 'running')
        self.assertEqual(len(self.port.list_runs(other)), 1)

    def test_a_dead_job_is_released_on_the_first_tick(self):
        compaction_hold.claim(self.state, at=self.clock(), pid=999999, birth='b')
        d = self.make(now=self.clock)
        d.recover_orphans = list
        with mock.patch.object(placement, 'cleanup_identity', return_value=dict(pid=999999, birth='absent')):
            d.tick()
        self.assertIsNone(compaction_hold.read(self.state))


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


if __name__ == '__main__':
    unittest.main()
