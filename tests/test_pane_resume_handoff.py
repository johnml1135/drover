"""A compaction resume keeps what the session already has: its brief, its handoff request and its written file."""
from unittest import mock

from drover import handoff, pane_context, pane_handoff, pane_launch
from drover.launch import AdmissionHeld
from drover.ledger import ops_panes
from tests.pane_compaction_support import Overnight
from tests.pane_rotation_support import FULL
from tests.pane_send_support import PANE, WORKING
from tests.panes_support import ROLE, Resident

HANDOFF, FORCED, LOW = 280000, 310000, 1000
SUCCESSOR_FILE = '/mnt/c/state/directors/a/handoff-1.md'


class Night(Overnight):
    def night(self, settles=True, state=None, tokens=None, file=True):
        """Up with this handoff on its row, stopped, killed and resumed."""
        self.up()
        self.cfg['director'].update(handoff_tokens=275000)
        self.cfg['watch'] = dict(context_tokens=300000, interval_s=600, stalled_hours=24, red_rate=0.5, min_gates=6)
        self.path = handoff.path_for(self.cfg, ROLE, self.run_now()['generation'])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if file:
            self.path.write_text(FULL, encoding='utf-8')
        ops_panes.update_agent(self.conn, self.row['id'], handoff_state=state, context_tokens=tokens)
        self.resumed(settles)

    def resumed(self, settles):
        self.fake.settles = True
        self.at(0)
        self.at(30)
        self.fake.settles = settles
        self.comes_back()
        self.at(900)
        self.assertEqual(self.agent()['outcome'], 'resumed')

    def run_now(self):
        return self.runtime.active_runs()[0]

    def maintain(self, tokens):
        with mock.patch.object(pane_context, 'read', return_value=tokens):
            pane_handoff.maintain(self.panes, self.run_now())
        return self.new_row()

    def typed(self, kind):
        return self.conn.execute('SELECT * FROM pane_writes WHERE agent_id=? AND kind=?', (self.new_row()['id'], kind)).fetchall()

    def own_file(self):
        self.assertEqual(self.path.read_text(encoding='utf-8'), FULL)


class ResumedSuccessor(Night):
    """A run started by a rotation keeps `handoff` in its spec for life, so the spec cannot say whether it was briefed."""

    def successor(self):
        self.up()
        self.conn.execute("UPDATE director_runs SET spec=json_set(spec,'$.handoff',?) WHERE run_id=?", (SUCCESSOR_FILE, self.rid))
        self.conn.commit()

    def test_one_that_is_working_stays_live_and_gets_no_brief(self):
        self.successor()
        self.resumed(False)
        base = pane_launch.datetime.fromisoformat(self.new_row()['updated']).timestamp()
        for seconds in (30, 59, 61, 90, 3600):
            self.time = base + seconds
            self.fake.screens[PANE] = WORKING
            self.tick()
        self.assertEqual(self.new_row()['state'], 'live')
        self.assertEqual(self.typed('brief'), [])
        self.assertEqual(self.port.events('pane-launch-failed'), [])

    def test_the_run_brief_is_the_brief_and_not_a_later_write(self):
        self.up()
        brief = ops_panes.note_write(self.conn, self.row['id'], 'brief', 'the brief')
        ops_panes.note_write(self.conn, self.row['id'], 'wake', 'a wake')
        self.assertEqual(ops_panes.run_brief(self.conn, self.rid)['id'], brief['id'])

    def test_one_that_went_idle_is_not_briefed_again(self):
        self.successor()
        self.resumed(True)
        self.time = self.since + 930
        self.tick()
        self.assertEqual(self.typed('brief'), [])
        self.assertFalse([call for call in self.fake.calls if call[:2] == ['pane', 'send-text'] and 'handoff' in call[3]])

    def test_its_restatement_is_still_chased_from_the_first_brief(self):
        self.successor()
        handoff.set_pending(self.port, ROLE, self.state / 'directors' / 'handoff-1.md')
        ops_panes.finish_write(self.conn, ops_panes.note_write(self.conn, self.row['id'], 'brief', 'the brief')['id'], 'observed')
        self.resumed(True)
        self.released()
        self.time += pane_handoff.RESTATE_S
        pane_handoff._restatement(self.panes, self.run_now(), self.new_row())
        self.assertIn(f'pane-restate:{self.rid}', [event['data']['notice']['id'] for event in self.pane_notices()])


class FirstBrief(Resident):
    def launching(self):
        self.build()
        self.turns()
        self.wake()
        rid, = self.tick()['launched']
        return rid

    def test_a_brief_refused_busy_never_fails_a_live_row(self):
        rid = self.launching()
        self.fake.agents[PANE]['agent_status'] = 'working'
        for seconds in (pane_launch.STARTUP_S + 1, 600, 7200):
            self.time += seconds
            self.tick()
        row = ops_panes.by_run(self.conn, rid)
        self.assertEqual(row['state'], 'live')
        self.assertEqual(self.port.events('pane-launch-failed'), [])

    def test_a_brief_refused_for_any_other_reason_past_the_limit_still_fails(self):
        rid = self.launching()
        self.fake.processes = dict(self.fake.processes, foreground_processes=[])
        self.time += pane_launch.STARTUP_S + 1
        self.tick()
        self.assertEqual(ops_panes.by_run(self.conn, rid)['state'], 'failed')


class WrittenHandoff(Night):
    def settled(self, **night):
        self.night(**night)
        self.released()
        self.settle()

    def test_the_new_row_carries_the_state_and_the_reading(self):
        self.settled(state='written', tokens=290000)
        row = self.new_row()
        self.assertNotEqual(row['id'], self.row['id'])
        self.assertEqual((row['handoff_state'], row['context_tokens']), ('written', 290000))

    def test_at_the_handoff_level_the_directors_file_rotates(self):
        self.settled(state='written')
        row = self.maintain(HANDOFF)
        self.assertEqual((row['state'], row['handoff_state']), ('rotating', 'written'))
        self.own_file()
        self.assertEqual(self.typed('handoff'), [])

    def test_at_the_forced_level_no_fallback_replaces_it(self):
        self.settled(state='written')
        self.assertEqual(self.maintain(FORCED)['state'], 'rotating')
        self.own_file()

    def test_a_low_reading_after_a_resume_leaves_it(self):
        """A resumed CLI may show an empty footer until its first turn, and that is no owner compaction."""
        self.settled(state='written')
        self.assertEqual(self.maintain(LOW)['handoff_state'], 'written')
        self.own_file()

    def test_a_file_with_no_state_on_a_resumed_row_is_not_cleared(self):
        self.settled(state=None)
        self.assertEqual(self.maintain(HANDOFF)['state'], 'rotating')
        self.own_file()
        self.assertEqual(self.typed('handoff'), [])

    def test_a_file_with_no_state_on_a_resumed_row_is_not_replaced(self):
        self.settled(state=None)
        self.assertEqual(self.maintain(FORCED)['state'], 'rotating')
        self.own_file()

    def test_a_request_is_not_repeated(self):
        self.settled(state='requested', file=False)
        self.assertEqual(self.maintain(HANDOFF)['handoff_state'], 'requested')
        self.assertEqual(self.typed('handoff'), [])

    def new_run(self, row, **over):
        self.killed()
        self.fake.after[('pane', 'run', 2)] = self.boot
        spec = dict(self.spec_, run_id=self.rid + 1, generation=self.spec_['generation'] + 1, **over)
        return pane_launch.resume(self.port, self.herdr, row, spec, cfg=self.cfg, credential='new')

    def test_a_lapsed_resume_is_a_new_run_and_still_takes_no_brief(self):
        self.up()
        fresh = self.new_run(self.row)
        spec = dict(self.spec_, run_id=self.rid + 1)
        self.assertIs(pane_launch.advance(self.port, self.herdr, fresh, spec, lambda: None, self.time, cfg=self.cfg), True)
        self.assertEqual(self.typed('brief'), [])

    def test_a_resume_into_a_new_generation_brings_the_file_with_it(self):
        self.up()
        generation = self.spec_['generation']
        old = handoff.path_for(self.cfg, ROLE, generation)
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_text(FULL, encoding='utf-8')
        fresh = self.new_run(ops_panes.update_agent(self.conn, self.row['id'], handoff_state='written', context_tokens=290000))
        self.assertEqual((fresh['state'], fresh['handoff_state']), ('live', 'written'))
        self.assertEqual(handoff.path_for(self.cfg, ROLE, generation + 1).read_text(encoding='utf-8'), FULL)

    def test_a_resume_that_is_refused_moves_no_file(self):
        self.up()
        old = handoff.path_for(self.cfg, ROLE, self.spec_['generation'])
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_text(FULL, encoding='utf-8')
        row = ops_panes.update_agent(self.conn, self.row['id'], handoff_state='written')
        spec = dict(self.spec_, run_id=self.rid + 1, generation=self.spec_['generation'] + 1)
        with self.assertRaises(AdmissionHeld):      # the agent is still up
            pane_launch.resume(self.port, self.herdr, row, spec, cfg=self.cfg, credential='new')
        self.assertTrue(old.exists())

    def test_a_resume_into_a_new_generation_with_no_file_asks_again(self):
        self.up()
        self.assertIsNone(self.new_run(ops_panes.update_agent(self.conn, self.row['id'], handoff_state='requested'))['handoff_state'])

    def test_a_rotation_starts_clean(self):
        self.up()
        row = ops_panes.update_agent(self.conn, self.row['id'], state='retired', handoff_state='rotated', context_tokens=290000)
        self.killed()
        self.fake.after[('pane', 'run', 2)] = self.boot
        fresh = pane_launch.rotate(self.port, self.herdr, row, dict(self.spec_, run_id=self.rid + 1), cfg=self.cfg, credential='new')
        self.assertEqual((fresh['handoff_state'], fresh['context_tokens']), (None, None))
