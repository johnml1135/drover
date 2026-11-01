"""The overnight WSL compaction: every command goes through a fake runner, so WSL and the VHDX are never touched."""
import json
import os
import sys
from unittest import mock
import unittest

from drover import compaction, compaction_hold, disk, disk_cleanup, disk_state, placement
from drover import runner
from tests.compaction_fixture import Job, NOW, VHDX, Vhdx, done


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
                                ('compact', 'offline'), ('restart', 'offline'), ('resume', 'resuming'), ('release', 'resuming')])

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

    def test_a_report_that_cannot_be_written_is_a_returned_error(self):
        with mock.patch.object(compaction, 'report', side_effect=OSError('disk full')):
            result = self.run_job()
        self.assertEqual(result['status'], 'compacted')
        self.assertEqual(result['errors'], ['the report was not written: OSError: disk full'])
        self.assertIsNone(self.hold())

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


class CleanupLock(Job):
    """The real lock against a real holder: the job's kill matches a cleanup worker, so the two must never overlap."""
    WORKER = '  77 python3 -c import json,sys;from drover.placement import cleanup_worker /home/j/drover 1f2e\n'

    def locked(self):
        return (self.state / 'disk-cleanup.lock').exists()

    def test_a_running_cleanup_keeps_the_job_off_wsl_and_the_night_is_skipped(self):
        self.fake.answers['ps -eo'] = done(self.WORKER)
        with disk_state.CleanupLock(self.state):
            answer = compaction.run(self.cfg, runtime=self.runtime())
        self.assertEqual(answer['status'], 'skipped')
        self.assertEqual(self.fake.calls, [])                       # no ps, no kill, no shutdown, no restart
        self.assertIsNone(self.hold())
        self.assertEqual(self.clock() - NOW, compaction.LOCK_WAIT_S)
        self.assertEqual(self.report()['status'], 'skipped')
        notice = next(iter(self.outbox().values()))['notice']
        self.assertIn('skipped', notice['title'])
        self.assertIn('cleanup', notice['body'])

    def test_a_cleanup_that_ends_in_the_wait_loses_the_night_nothing(self):
        lock = disk_state.CleanupLock(self.state).__enter__()
        started = []
        real = self.clock.sleep

        def sleep(seconds):
            real(seconds)
            if self.clock() - NOW >= 600 and lock.acquired:
                lock.__exit__(None, None, None)

        class Watching(compaction.Runtime):
            def wait_for_hub(self, record):
                started.append(record['since'])
                return dict(stopped=[], killed=[], requeued=[], drained=True)
        job = Watching(self.cfg, run=self.fake, clock=self.clock, sleep=sleep)
        job.vhdx = lambda: VHDX
        self.assertEqual(compaction.run(self.cfg, runtime=job)['status'], 'compacted')
        self.assertEqual(started, [NOW + 600])                      # the hub's grace was not spent waiting for the lock

    def test_the_lock_is_held_to_the_release_and_no_cleanup_gets_in(self):
        seen = []
        test = self

        class Watching(compaction.Runtime):
            def wait_for_hub(self, record):
                with test.assertRaises(BlockingIOError):
                    disk_cleanup.run(test.cfg, runtime=object())
                return dict(stopped=[], killed=[], requeued=[], drained=True)

            def release(self, record):
                seen.append(test.locked())
                super().release(record)
        self.assertEqual(self.run_job(Watching)['status'], 'compacted')
        self.assertEqual(seen, [True])
        self.assertFalse(self.locked())

    def test_a_failed_night_still_lets_the_lock_go(self):
        self.fake.answers['--shutdown'] = done('', 1)
        self.assertEqual(self.run_job()['status'], 'failed')
        self.assertFalse(self.locked())

    def test_a_hold_that_cannot_be_taken_is_a_reported_failure(self):
        class Refused(compaction.Runtime):
            def claim(self, at):
                raise OSError('state directory is unwell')
        answer = self.run_job(Refused)
        self.assertEqual(answer['status'], 'failed')
        self.assertEqual(self.fake.calls, [])
        self.assertFalse(self.locked())
        self.assertEqual(self.report()['status'], 'failed')

    def test_a_second_job_skips_without_touching_the_first_ones_lock(self):
        compaction_hold.claim(self.state, at=NOW, pid=os.getpid(), birth=placement.cleanup_identity()['birth'])
        self.assertEqual(self.run_job()['status'], 'skipped')
        self.assertFalse(self.locked())

    def test_the_hub_raises_no_stale_lock_notice_for_the_job(self):
        compaction_hold.claim(self.state, at=NOW, pid=os.getpid(), birth='b')
        tick = dict(reader=lambda path: 10.0, enqueue=mock.Mock(), wsl_used=lambda: None)
        with disk_state.CleanupLock(self.state):
            disk.tick(self.cfg, now=NOW, **tick)
            disk.tick(self.cfg, now=NOW + disk.LOCK_BACKOFF_S + 60, **tick)
        self.assertEqual([ident for ident in self.outbox() if ident.startswith('lock:')], [])
        tick['enqueue'].assert_not_called()


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

    def test_a_mount_left_by_a_crash_is_dismounted_once_wsl_is_down(self):
        compaction_hold.note_mount(self.state, VHDX)
        self.run_job()
        calls = self.fake.calls.index
        self.assertLess(calls(self.fake.seen('--shutdown')[0]), calls(self.fake.seen('Dismount-VHD')[0]))
        self.assertLess(calls(self.fake.seen('Dismount-VHD')[0]), calls(self.fake.seen('Mount-VHD')[0]))
        self.assertIsNone(compaction_hold.mounted(self.state))

    def test_a_left_mount_that_will_not_dismount_ends_the_night_with_wsl_back_and_the_hold_gone(self):
        compaction_hold.note_mount(self.state, VHDX)
        self.fake = Vhdx(refuse_dismount=True, attached=True)
        self.assertEqual(self.run_job()['status'], 'failed')
        self.assertEqual(self.fake.seen('Optimize-VHD'), [])
        self.assertTrue(self.fake.seen('--exec'))
        self.assertIsNone(self.hold())
        self.assertEqual(compaction_hold.mounted(self.state), VHDX)

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


if __name__ == '__main__':
    unittest.main()
