"""The hub during a compaction: nothing polls WSL, WSL runs wind down, and landing waits."""
import csv
import gc
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import unittest

from drover import compaction, compaction_hold, config, disk, disk_state, ledger, placement
from drover import ci, durable, placement_exec, placement_host, process, resources, runner, runner_supervise, usage
from drover.launch import RunnerLauncher
from drover.runner import ScanError
from tests.test_dispatcher import Base, res
from tests.compaction_fixture import Clock, DONE_RECORD, done
from tests.tiers import integration


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
        compaction_hold.claim(self.state, at=compaction_hold.clock(), pid=os.getpid(), birth=placement.cleanup_identity()['birth'])
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
        with mock.patch.object(process.subprocess, 'run', run):
            self.assertIsNone(placement.wsl_free_gb('U', True, ttl=0))
            self.assertIsNone(placement.wsl_used_gb('U', True, ttl=0))
            self.assertFalse(placement.wsl_running('U'))
        self.assertEqual(resources.read_wsl('U', True, True), (False, None))
        self.assertIsNone(resources.read_wsl_ram_gb('U', run=run))
        self.assertIsNone(placement.wsl_zoneinfo_path('Etc/UTC'))
        cfg = config.get()
        self.assertIsNone(disk._wsl_used({**cfg, 'trackers': [dict(kind='disk', wsl_combined=True), *cfg['trackers']]}))
        launcher = RunnerLauncher(runs_dir=self.state, run=run, windows=True, distro='U')
        self.assertFalse(launcher.wsl_up())
        with mock.patch.object(runner, 'find_runners', return_value=[]), self.assertRaises(ScanError):
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


class Draining(Hubbed):
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
        record = compaction_hold.claim(self.state, at=self.clock(), pid=os.getpid(), birth=placement.cleanup_identity()['birth'])
        record['phase'] = phase
        disk_state.save_file(self.state, compaction_hold.HOLD_FILE, record)
        return record

    def ack(self):
        return compaction_hold.read_drain(self.state)


class WindDown(Draining):
    """The hub sees the file and winds WSL work down through the durable stop and the run requeue."""

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


class DiedUnderARunningHub(Draining):
    """A hub that is already up makes no start-up pass, so the tick itself has to notice the job is gone."""

    def die(self, *hold_args):
        record = self.hold(*hold_args)
        record.update(pid=999999, birth='b', step='compact')
        disk_state.save_file(self.state, compaction_hold.HOLD_FILE, record)
        self.assertTrue(self.d.recovered)
        return mock.patch.object(placement, 'cleanup_identity', return_value=dict(pid=999999, birth='absent'))

    def test_the_next_tick_releases_the_hold_and_raises_the_notice(self):
        with self.die('offline'):
            out = self.d.tick()
        self.assertIsNone(compaction_hold.read(self.state))
        self.assertNotIn('compaction', out)
        notices = [row['notice'] for row in disk_state.read(self.state).get('outbox', {}).values()]
        self.assertEqual([notice['kind'] for notice in notices], ['compaction'])
        self.assertIn('the job died at step compact', notices[0]['body'])

    def identity_of_a_child(self):
        """A real process that reports the identity it would claim a hold with. Under a venv Popen.pid is only a launcher."""
        code = ('import sys,time;sys.path.insert(0,sys.argv[1]);from drover import placement;'
                'me=placement.cleanup_identity();print(me["pid"],me["birth"],flush=True);sys.stdin.read()')
        child = subprocess.Popen([sys.executable, '-c', code, str(Path(__file__).resolve().parents[1])],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.stdout.close)
        pid, birth = child.stdout.readline().split()
        return child, int(pid), birth

    def claim_as(self, pid, birth):
        record = self.hold()
        record.update(pid=pid, birth=birth)
        disk_state.save_file(self.state, compaction_hold.HOLD_FILE, record)
        return record

    @integration
    def test_a_real_job_in_another_process_keeps_its_hold_until_it_is_gone(self):
        child, pid, birth = self.identity_of_a_child()
        record = self.claim_as(pid, birth)
        self.d.tick()
        self.assertEqual(compaction_hold.read(self.state)['id'], record['id'])
        child.communicate('')
        del child                                               # Windows keeps a process visible while a handle is open
        gc.collect()
        deadline = time.monotonic() + 5                         # a scanner may hold a handle for a moment after exit
        while True:
            self.d.tick()
            if compaction_hold.read(self.state) is None or time.monotonic() > deadline:
                break
            time.sleep(0.1)
        self.assertIsNone(compaction_hold.read(self.state))

    @integration
    def test_a_live_process_the_hub_may_not_open_is_not_taken_for_dead(self):
        if os.name != 'nt':
            self.skipTest('the protected process is a Windows one')
        rows = subprocess.run(['tasklist', '/fi', 'IMAGENAME eq lsass.exe', '/fo', 'csv', '/nh'],
                              capture_output=True, text=True, check=True).stdout
        pid = int(next(csv.reader(rows.splitlines()))[1])
        if placement.cleanup_identity(pid)['birth']:
            self.skipTest('this session is elevated, so nothing here is denied')
        record = self.claim_as(pid, 'recorded')
        self.d.tick()
        self.assertEqual(compaction_hold.read(self.state)['id'], record['id'])

    def test_a_report_that_cannot_be_written_does_not_stop_the_tick(self):
        with self.die(), mock.patch.object(compaction, 'report', side_effect=OSError('disk full')):
            self.assertNotIn('compaction', self.d.tick())
        self.assertIsNone(compaction_hold.read(self.state))

    def test_a_live_job_keeps_its_hold(self):
        record = self.hold()
        self.d.tick()
        self.assertEqual(compaction_hold.read(self.state)['id'], record['id'])

    def test_a_holder_that_cannot_be_read_is_not_taken_for_dead(self):
        record = self.hold()
        with mock.patch.object(placement, 'cleanup_identity', side_effect=OSError('denied')):
            self.d.tick()
        self.assertEqual(compaction_hold.read(self.state)['id'], record['id'])


class LandingGate(Draining):
    """Landing CI in WSL is no dispatcher run: it is found through the saved request the landing service keeps."""
    GATE = 'ci-gate'
    NONCE = 'f' * 32

    def setUp(self):
        super().setUp()
        self.side('windows')
        self.runs = Path(self.pipe.runs_dir)
        self.request = self.save_gate('wsl', 'ci-1.json', self.GATE, self.NONCE)
        self.gate_status('running')

    def save_gate(self, platform, name, run_id, nonce, execution='issued'):
        self.runs.mkdir(parents=True, exist_ok=True)
        spec = dict(run_id=run_id, nonce=nonce, placement=platform)
        request = self.runs / name
        durable.write_json(request, dict(execution=execution, context=dict(spec=spec), ticket=None), sync=False)
        return request

    def gate_status(self, state, reason=None, run_id=GATE, nonce=NONCE):
        folder = runner.run_dir(str(self.runs), run_id)
        folder.mkdir(parents=True, exist_ok=True)
        durable.write_json(folder / runner.STATUS_FILE, dict(nonce=nonce, state=state, reason=reason, run_id=run_id), sync=False)

    def test_the_drain_waits_for_a_running_gate_and_stops_it_after_the_grace(self):
        self.hold()
        self.clock.now += 299
        self.d.tick()
        self.assertFalse(self.ack()['drained'])
        self.assertFalse((runner.run_dir(str(self.runs), self.GATE) / runner.STOP_FILE).exists())
        self.clock.now += 1
        self.d.tick()
        self.assertFalse(self.ack()['drained'])
        folder = runner.run_dir(str(self.runs), self.GATE)
        self.assertEqual(runner_supervise.read_stop(folder, self.NONCE), ci.COMPACTION_STOP)    # the runner keeps the reason
        self.assertEqual(self.ack()['killed'], [self.GATE])
        self.gate_status('killed', ci.COMPACTION_STOP)
        self.d.tick()
        self.assertTrue(self.ack()['drained'])

    def test_a_gate_that_ends_in_the_grace_is_only_stopped(self):
        self.hold()
        self.d.tick()
        self.gate_status('exited')
        self.d.tick()
        ack = self.ack()
        self.assertTrue(ack['drained'])
        self.assertEqual((ack['stopped'], ack['killed']), ([self.GATE], []))

    def test_a_windows_gate_and_an_unissued_gate_are_left_alone(self):
        self.request.unlink()
        self.save_gate('windows', 'ci-2.json', 'ci-win', 'a' * 32)
        self.save_gate('wsl', 'ci-3.json', 'ci-new', 'b' * 32, execution='prepared')
        self.hold()
        self.d.tick()
        self.assertTrue(self.ack()['drained'])

    def test_a_night_with_a_gate_and_a_dispatcher_run_answers_the_job(self):
        self.side('wsl')                # run ids are numbers and gate ids are text: the answer sorts both
        self.hold()
        self.d.tick()
        self.assertEqual(self.ack()['stopped'], [self.rid, self.GATE])
        self.clock.now += 300
        self.d.tick()
        self.assertEqual(self.ack()['killed'], [self.rid, self.GATE])

    def result(self, reason):
        """What the landing service makes of a stop that was not the compaction's, from the saved files."""
        self.gate_status('killed', reason)
        gate = ci.CI(self.runs, launcher=mock.Mock())
        spec = dict(run_id=self.GATE, nonce=self.NONCE, placement='wsl')
        durable.write_json(runner.run_dir(str(self.runs), self.GATE) / 'plan.json',
                           dict(run_id=self.GATE, nonce=self.NONCE, platform='wsl', sha='a' * 40, kind='full'), sync=False)
        return gate._classify(spec, dict(status='error', returncode=None), dict(kind='full'), self.request, 'a' * 40, 0)

    def test_a_gate_stopped_for_any_other_reason_still_counts(self):
        self.assertEqual(self.result('requested')['outcome'], 'infrastructure-failed')


if __name__ == '__main__':
    unittest.main()
