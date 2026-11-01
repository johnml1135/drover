"""A resident director at the handoff level, with its context reading in the test's hands. Fakes only."""
from unittest import mock

from drover import handoff, pane_context, pane_handoff, pane_send
from drover.ledger import ops_panes
from tests.pane_launch_support import LONE_SHELL, PROMPT
from tests.pane_send_support import CLAUDE_IDLE, PANE
from tests.panes_support import ROLE, Resident

FULL = ''.join(f'## {section}\nSomething true.\n\n' for section in handoff.SECTIONS)
HANDOFF, FORCED = 275000, 300000
EXIT = ['agent', 'prompt', PANE, pane_send.EXIT_TEXT]


class Rotating(Resident):
    def setUp(self):
        super().setUp()
        self.launched()
        self.cfg['director'].update(handoff_tokens=HANDOFF)
        self.cfg['watch'] = dict(context_tokens=FORCED, interval_s=600, stalled_hours=24, red_rate=0.5, min_gates=6)
        self.idle()
        reading = mock.patch.object(pane_context, 'read', return_value=HANDOFF)
        self.read = reading.start()
        self.addCleanup(reading.stop)
        self.fake.calls.clear()
        self.path = handoff.path_for(self.cfg, ROLE, self.run_row['generation'])
        self.restated = handoff.restated_path(self.path)

    def idle(self):
        self.fake.screens[PANE] = CLAUDE_IDLE
        self.fake.agents[PANE].update(agent_status='idle', focused=False)

    def at(self, tokens=HANDOFF):
        self.read.return_value = tokens
        pane_handoff.maintain(self.panes, self.run_row)
        return self.row_now()

    def written(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(FULL, encoding='utf-8')

    def told_to_exit(self):
        """A file on hand, a safe pane, and the `/exit` gone out."""
        self.at()
        self.written()
        self.assertEqual(self.at()['state'], 'rotating')

    def exits(self):
        return [call for call in self.fake.calls if call == EXIT]

    def cli_exits(self):
        self.fake.processes = LONE_SHELL
        self.fake.screens[PANE] = PROMPT
        self.fake.agents.pop(PANE)

    def follow(self):
        pane_handoff.follow_exit(self.panes, self.run_row)

    def sessions(self):
        return self.conn.execute('SELECT COUNT(*) FROM director_sessions WHERE role_id=?', (ROLE,)).fetchone()[0]

    def notices(self, prefix):
        found = [event['data']['notice']['id'] for event in self.port.events(kind='pane.notice')]
        return [notice for notice in found if notice.startswith(prefix)]

    def rotated(self):
        self.told_to_exit()
        self.cli_exits()
        self.follow()

    def next_agent_boots(self):
        def boot():
            self.fake.processes = self.foreground
            self.fake.screens[PANE] = CLAUDE_IDLE
            self.fake.agents[PANE] = dict(pane_id=PANE, terminal_id='t1', agent='claude', agent_session={'value': 's2'},
                                          agent_status='idle', focused=False)
        self.fake.after[('agent', 'start', 2)] = boot

    def relaunched(self):
        """The rotation done and the next run up on the pane."""
        self.rotated()
        self.next_agent_boots()
        self.next_run, = self.tick()['launched']
        self.next_row = ops_panes.by_run(self.conn, self.next_run)
