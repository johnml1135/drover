"""What the panes tests share: a role, a runtime on a real adapter, and a director that has really been launched."""
import json
import time
from unittest import mock

from drover import pane_handoff, pane_launch, pane_wake
from drover.directors import DirectorRuntime
from drover.ledger import ops_panes
from drover.notifications import Notifications
from drover.panes import PaneDirectorAdapter, PaneService
from tests.pane_launch_support import Launching
from tests.pane_send_support import PANE

ROLE = 'director:a'


class Resident(Launching):
    def setUp(self):
        super().setUp()
        self.time = time.time()     # rows are stamped with the real clock, so this one starts there and only moves on
        self.notifications = Notifications(self.conn, now=lambda: self.time)
        self.port.set_setting('dispatch_enabled', 'true')
        self.port.set_setting('director_execution_enabled', 'true')
        self.configure(ROLE)
        self.notifications.register(ROLE, allowed_models=['opus-5.5'], token_budget=100, wake_limit_per_hour=6,
                                    replacement_limit=1)
        self.capacity = {'slots': 2, 'admit': lambda spec: None}

    def configure(self, role):
        configs = json.loads(self.port.get_setting('director_execution', '{}'))
        configs[role] = {'backend': 'claude', 'model': 'opus-5.5', 'seat': 'seat-c', 'max_tokens': 20, 'placement': 'windows',
                         'cwd': str(self.cwd)}
        self.port.set_setting('director_execution', json.dumps(configs))

    def build(self, **scene):
        self.scene(**scene)
        self.panes = PaneService(self.port, self.herdr, cfg=self.cfg, now=lambda: self.time)
        self.adapter = PaneDirectorAdapter(self.panes)
        self.runtime = DirectorRuntime(self.port, self.adapter, now=lambda: self.time)

    def tick(self):
        return self.runtime.tick(self.notifications.admission(), self.capacity)

    def wake(self, role=ROLE):
        self.notifications.broadcast('owner', [role], 'work', urgency='urgent')

    def launched(self, **scene):
        """A director up in its pane, with its first turn seen and its row in `self.row`."""
        self.build(**scene)
        self.turns()
        self.wake()
        self.rid, = self.tick()['launched']
        self.run_row = self.runtime.active_runs()[0]
        self.spec_ = self.run_row['spec']
        self.tick()                 # the brief goes on the tick after `live`
        self.row = ops_panes.by_run(self.conn, self.rid)
        self.assertEqual(self.row['state'], 'live')
        return self.row

    def guard(self):
        self.runtime._guard(self.spec_)

    def died_starting(self):
        """The hub died inside `start`: a `starting` row, and a run the runtime could not account for."""
        self.build(boots=False)
        self.fake.panes[PANE] = dict(pane_id=PANE, terminal_id='t1', cwd=str(self.cwd), focused=False,
                                     scroll={'offset_from_bottom': 0})

        def die(port, herdr, spec, credential, guard, *, cfg):
            ops_panes.record_agent(self.conn, pane_id=PANE, terminal_id='t1', kind='claude', seat='seat-c', placement='windows',
                                   started_by='drover', role_id=spec['role_id'], run_id=spec['run_id'], cwd=spec['cwd'], name='n')
            raise RuntimeError('the hub went away')
        with mock.patch.object(pane_launch, 'start', die):
            self.wake()
            self.tick()
        self.row = ops_panes.open_agents(self.conn)[-1]
        self.rid = self.row['run_id']
        self.runtime = DirectorRuntime(self.port, self.adapter, now=lambda: self.time)
        return self.row

    def agent_in_pane(self):
        """Something runs in the pane, though no session reports."""
        self.fake.processes = self.foreground

    def row_now(self):
        return dict(self.conn.execute('SELECT * FROM pane_agents WHERE id=?', (self.row['id'],)).fetchone())

    def outcome(self, run_id=None):
        return self.conn.execute('SELECT outcome FROM runs WHERE id=?', (run_id or self.rid,)).fetchone()[0]

    def hooks(self):
        """`deliver` and `maintain` as mocks: their calls are the only pane writes this task can reach."""
        wake, handoff = mock.patch.object(pane_wake, 'deliver'), mock.patch.object(pane_handoff, 'maintain')
        started = wake.start(), handoff.start()
        self.addCleanup(wake.stop)
        self.addCleanup(handoff.stop)
        return started

    def role(self, role=ROLE):
        return self.notifications.role_state(role)

    def open_questions(self):
        return self.conn.execute("SELECT COUNT(*) FROM questions WHERE target_kind='director' AND status='open'").fetchone()[0]
