"""`exit_agent`: the one write that ends a session, on the reads a send needs. Fakes only."""
from drover import pane_send
from drover.ledger import ops_panes
from tests.pane_launch_support import CLAUDE_WORKING
from tests.pane_rotation_support import EXIT
from tests.pane_send_support import CLAUDE_IDLE, Case, DIALOG, PANE, composing


class ExitAgent(Case):
    def ready(self, screen=CLAUDE_IDLE, **stage):
        self.windows(screen, **stage)
        ops_panes.update_agent(self.conn, self.row['id'], handoff_state='written', state='rotating')
        self.row = ops_panes.by_pane(self.conn, PANE)

    def exit(self):
        return pane_send.exit_agent(self.port, self.herdr, self.row)

    def test_it_goes_as_one_prompt_on_windows(self):
        self.ready()
        self.assertEqual(self.exit()['kind'], 'exit')
        self.assertEqual(self.sends(), [EXIT])

    def test_a_wsl_agent_gets_one_paste_and_one_enter(self):
        self.stage(row=dict(handoff_state='written', state='rotating'))
        self.sleep.side_effect = lambda seconds: self.fake.screens.update({PANE: composing(pane_send.EXIT_TEXT)})
        self.assertEqual(self.exit()['state'], 'sent')
        self.assertEqual([call[:2] for call in self.sends()], [['pane', 'send-text'], ['pane', 'send-keys']])

    def test_it_needs_the_handoff_written(self):
        for state in (None, 'requested', 'rotated'):
            self.windows(row=dict(handoff_state=state, state='rotating'))
            self.refused(self.exit, 'no-handoff')
            self.reset()

    def test_it_goes_once_for_a_row(self):
        self.ready()
        self.exit()
        self.fake.calls.clear()
        self.refused(self.exit, 'already-started')

    def test_every_guard_of_a_send_refuses_it(self):
        for name, stage, reason in (('focused', dict(agent=dict(focused=True)), 'focused'),
                                    ('dialog', dict(screen=DIALOG), None),
                                    ('turn', dict(screen=CLAUDE_WORKING), 'busy'),
                                    ('draft', dict(screen=composing('half a thought').replace('›', '❯')), None),
                                    ('working', dict(agent=dict(agent_status='working')), 'busy')):
            with self.subTest(name):
                self.ready(**stage)
                self.refused(self.exit, reason)
                self.reset()

    def test_a_changed_occupant_refuses_it(self):
        self.ready(agent=dict(agent_session={'value': 'other'}))
        self.refused(self.exit, 'occupant-changed')

    def test_an_open_write_refuses_it(self):
        self.ready()
        ops_panes.note_write(self.conn, self.row['id'], 'wake', 'x')
        self.refused(self.exit)

    def test_an_owner_draft_on_the_last_read_stops_it(self):
        self.ready(screen=[CLAUDE_IDLE, CLAUDE_IDLE, CLAUDE_IDLE.replace('❯ ', '❯ mine')])
        self.refused(self.exit)

    def test_it_needs_the_row_rotating(self):
        self.windows(row=dict(handoff_state='written'))
        self.refused(self.exit, 'not-live')

    def test_submit_own_cannot_finish_it(self):
        self.stage(row=dict(handoff_state='written', state='rotating'))
        self.sleep.side_effect = lambda seconds: self.fake.screens.update({PANE: composing('someone else')})
        self.assertEqual(self.exit()['reason'], 'not-own')
        self.fake.calls.clear()
        ops_panes.update_agent(self.conn, self.row['id'], state='live')
        self.refused(lambda: pane_send.submit_own(self.port, self.herdr, self.row), 'nothing-unsent')

    def test_forced_closes_nothing_when_the_last_read_refuses(self):
        self.ready(screen=[CLAUDE_IDLE, CLAUDE_IDLE, CLAUDE_IDLE + ' '])
        wake = ops_panes.note_write(self.conn, self.row['id'], 'wake', 'x')
        self.refused(lambda: pane_send.exit_agent(self.port, self.herdr, self.row, forced=True), 'screen-changed')
        self.assertEqual([write['id'] for write in ops_panes.open_writes(self.conn, self.row['id'])], [wake['id']])

    def test_send_cannot_type_it(self):
        self.ready()
        self.refused(lambda: pane_send.send(self.port, self.herdr, self.row, pane_send.EXIT_TEXT, 'exit'), 'kind-refused')
