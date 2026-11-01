"""The hub's side of a pane director: the wiring, one tick with Herdr closed, and what the runtime asks of a resident."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from drover import config, dispatcher
from drover.directors import CLEAN_OUTCOMES, DirectorRuntime, RunnerDirectorAdapter
from drover.herdr_client import Herdr
from drover.ledger import ops_panes
from drover.ledger.db import connect
from drover.notifications import Notifications
from drover.panes import PaneDirectorAdapter, PaneService
from drover.port import DirectPort
from tests.director_fixture import ControlledAdapter, ResidentAdapter
from tests.dispatch_fixture import Observations
from tests.fake_herdr import FakeHerdr
from tests.pane_send_support import PANE
from tests.panes_support import ROLE
from tests.test_dispatcher import Base as DispatcherBase


class Hub(DispatcherBase):
    def wired(self, fake):
        herdr = Herdr('herdr', run=fake, retry_s=0)
        panes = PaneService(self.port, herdr, cfg=config.get())
        runtime = DirectorRuntime(self.port, PaneDirectorAdapter(panes))
        observed = Observations(self.conn, self.d.now, lambda backend: {'remaining_pct': 90.0}, lambda backend: True)
        return panes, dispatcher.Dispatcher(self.port, self.pipe, dispatcher.DispatchRuntime(
            self.fl, lambda **kw: self.verdict, directors=runtime, panes=panes), observed, decide=self.decide)

    def test_hub_tick_with_herdr_closed_dispatches_workers_and_publishes_one_notice(self):
        fake = FakeHerdr()
        fake.closed = True
        panes, tick = self.wired(fake)
        self.notifications_for_a_live_director(panes)
        task = self.task()
        for _ in range(2):
            tick.tick()
        self.assertEqual(self.status(task), 'running')
        self.assertEqual(len(self.fl.specs), 1)
        self.assertEqual(len(self.port.events(kind='pane.notice')), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM questions WHERE target_kind='director' AND status='open'").fetchone()[0], 0)
        director = self.conn.execute('SELECT outcome FROM runs WHERE id=?', (self.director_run,)).fetchone()
        self.assertIsNone(director['outcome'])

    def notifications_for_a_live_director(self, panes):
        """A director run on a pane, recorded as the runtime leaves it, without a launch."""
        service = Notifications(self.conn)
        service.register(ROLE, allowed_models=['opus-5.5'], token_budget=100)
        self.port.set_setting('director_execution', json.dumps({}))
        runtime = DirectorRuntime(self.port, panes and PaneDirectorAdapter(panes))
        session = service.issue_session(ROLE, 'director-run:n', ttl=86400)
        task_id = runtime._anchor()
        self.conn.commit()
        self.director_run = self.conn.execute("INSERT INTO runs(task_id,role,backend,model,started) VALUES(?,'director','claude','opus-5.5',?)",
                                              (task_id, '2026-11-01T00:00:00Z')).lastrowid
        spec = dict(run_id=self.director_run, role_id=ROLE, generation=session['generation'], session_id=session['session_id'],
                    control_epoch=service.admission()['epoch'], nonce='n', placement='windows', seat='seat', backend='claude')
        self.conn.execute("INSERT INTO director_runs(run_id,role_id,generation,checkpoint_revision,control_epoch,nonce,session_id,spec,"
                          "launch_action_id,state) VALUES(?,?,?,0,?,?,?,?,NULL,'running')",
                          (self.director_run, ROLE, session['generation'], spec['control_epoch'], 'n', session['session_id'], json.dumps(spec)))
        self.conn.commit()
        ops_panes.record_agent(self.conn, pane_id=PANE, terminal_id='t1', kind='claude', seat='seat', placement='windows',
                               started_by='drover', state='live', role_id=ROLE, run_id=self.director_run, session_id='s1', name='n')


class Wiring(unittest.TestCase):
    def service(self, tmp='x'):
        from hub.dispatch import DispatcherService
        settings = SimpleNamespace(dispatcher_lock=Path(tmp) / 'dispatcher.lock', runs_dir=tmp, distro='Ubuntu')
        return DispatcherService(SimpleNamespace(get_settings=lambda: {}), settings, landing_service=object())

    def built(self, panes):
        cfg = dict(config.get(), panes=dict(config.get()['panes'], enabled=panes, herdr_bin='herdr'))
        port = mock.MagicMock()
        with mock.patch.object(config, 'get', return_value=cfg), mock.patch('drover.seat_paths.validate', return_value={}), \
                mock.patch('hub.dispatch.Dispatcher') as made, mock.patch('hub.dispatch.Pipeline'), \
                mock.patch('drover.directors.Notifications'), mock.patch('drover.panes.Notifications'), \
                mock.patch('drover.herdr_client.shutil.which', return_value='herdr'):
            port.get_setting.return_value = '{}'
            self.service()._default_dispatcher(port)
        return made.call_args.args[2]

    def test_panes_disabled_keeps_the_runner_adapter(self):
        runtime = self.built(False)
        self.assertIsInstance(runtime.directors.adapter, RunnerDirectorAdapter)
        self.assertIsNone(runtime.panes)

    def test_panes_enabled_builds_the_pane_adapter(self):
        runtime = self.built(True)
        self.assertIsInstance(runtime.directors.adapter, PaneDirectorAdapter)
        self.assertIsInstance(runtime.panes, PaneService)

    def test_panes_off_looks_for_a_director_left_in_a_pane(self):
        with mock.patch('drover.pane_fault.left_running') as looked:
            self.built(False)
        looked.assert_called_once()
        with mock.patch('drover.pane_fault.left_running') as looked:
            self.built(True)
        looked.assert_not_called()

    def test_a_failing_look_never_stops_the_dispatcher(self):
        with mock.patch('drover.pane_fault.left_running', side_effect=OSError('C:/secret')), \
                self.assertLogs('hub.dispatcher', 'WARNING') as logs:
            self.assertIsNone(self.built(False).panes)
        self.assertNotIn('secret', ''.join(logs.output))

    def test_the_runner_path_builds_no_client(self):
        with mock.patch('drover.herdr_client.from_config') as client:
            self.built(False)
        client.assert_not_called()

    def test_dispatcher_keeps_the_panes_service(self):
        made = dispatcher.DispatchRuntime(panes='p')
        self.assertEqual(made.panes, 'p')
        self.assertIsNone(dispatcher.DispatchRuntime().panes)


class Runtime(unittest.TestCase):
    """The runtime's side, on an adapter that has no pane."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.conn = connect(Path(self.tmp.name) / 'ledger.db')
        self.addCleanup(self.conn.close)
        self.time = 1000.
        self.service = Notifications(self.conn, now=lambda: self.time)
        self.port = DirectPort(self.conn, now=lambda: self.time)
        self.port.set_setting('dispatch_enabled', 'true')
        self.port.set_setting('director_execution_enabled', 'true')
        self.port.set_setting('director_execution', json.dumps({ROLE: {
            'backend': 'claude', 'model': 'test-model', 'seat': 'fixture', 'max_tokens': 20, 'placement': 'windows',
            'cwd': self.tmp.name}}))
        self.service.register(ROLE, allowed_models=['test-model'], token_budget=100, wake_limit_per_hour=100, replacement_limit=1)
        self.adapter = ResidentAdapter(self.service)
        self.runtime = DirectorRuntime(self.port, self.adapter, now=lambda: self.time)

    def tick(self):
        return self.runtime.tick(self.service.admission(), {'slots': 2, 'admit': lambda spec: None})

    def wake(self):
        self.service.broadcast('owner', [ROLE], 'work', urgency='urgent')

    def test_every_clean_outcome_keeps_the_replacement(self):
        self.assertEqual(CLEAN_OUTCOMES, ('done', 'stopped', 'admission-held', 'rotated', 'pane-gone', 'compaction-fresh', 'revived'))
        for outcome in CLEAN_OUTCOMES:
            with self.subTest(outcome):
                self.time += 601
                self.wake()
                rid, = self.tick()['launched']
                spec = self.adapter.started[-1]
                self.adapter.states[spec['nonce']] = 'gone'
                self.adapter.handles[rid].result = {'status': outcome, 'usage': {}}
                self.time += 601
                self.wake()
                self.tick()
                self.assertEqual(self.service.role_state(ROLE)['replacements_used'], 0)
                for run in self.runtime.active_runs():
                    self.adapter.states[run['spec']['nonce']] = 'gone'
                    self.adapter.handles[run['run_id']].result = {'status': 'done', 'usage': {}}
                self.tick()

    def test_a_clean_outcome_waits_for_the_wake_window_unless_it_rotated(self):
        for outcome, launches in (('error', 2), ('rotated', 2), ('pane-gone', 1), ('compaction-fresh', 1)):
            with self.subTest(outcome):
                self.setUp()
                self.service.conn.execute("UPDATE director_roles SET policy=json_set(policy,'$.wake_window_s',600)")
                self.conn.commit()
                self.wake()
                rid, = self.tick()['launched']
                self.adapter.states[self.adapter.started[0]['nonce']] = 'gone'
                self.adapter.handles[rid].result = {'status': outcome, 'usage': {}}
                self.wake()
                self.time += 1
                self.tick()
                self.assertEqual(len(self.adapter.started), launches)

    def test_a_failing_maintain_is_logged_and_the_tick_goes_on(self):
        self.wake()
        rid, = self.tick()['launched']
        self.assertEqual(self.adapter.swept, 1, 'nothing was active when the tick looked')
        self.adapter.fail_maintain = True
        with self.assertLogs('drover.directors', 'WARNING') as logs:
            self.tick()
        self.assertEqual(self.adapter.maintained, [rid])
        self.assertEqual(self.adapter.swept, 1)
        self.assertNotIn('lost pane', ''.join(logs.output))

    def test_a_failing_sweep_is_logged_and_the_tick_goes_on(self):
        self.adapter.sweep = mock.Mock(side_effect=OSError('lost pane'))
        with self.assertLogs('drover.directors', 'WARNING'):
            self.tick()

    def finite(self):
        self.adapter = self.runtime.adapter = ControlledAdapter(self.service)
        self.wake()
        with mock.patch.object(config, 'seat_by_name', return_value=dict(sandbox='workspace-write')):
            self.tick()
        return self.adapter.started[0]

    def test_a_finite_run_keeps_its_read_only_spec_and_hour(self):
        spec = self.finite()
        self.assertEqual((spec['sandbox'], spec['write']), ('read-only', False))
        expires = self.conn.execute('SELECT expires FROM director_sessions').fetchone()[0]
        self.assertEqual(expires, self.time + 3600)

    def test_pausing_dispatch_still_stops_a_finite_run(self):
        spec = self.finite()
        self.port.set_setting('dispatch_enabled', 'false')
        self.tick()
        self.assertEqual(self.adapter.stops, [(spec['run_id'], spec['nonce'])])

    def test_an_adapter_that_is_not_resident_is_not_maintained(self):
        self.runtime.adapter = ControlledAdapter(self.service)
        self.wake()
        self.tick()
        with self.assertNoLogs('drover.directors', 'WARNING'):
            self.tick()


if __name__ == '__main__':
    unittest.main()
