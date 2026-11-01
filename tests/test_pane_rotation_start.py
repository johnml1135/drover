"""The session after a rotation: the same pane, a fresh agent, and the handoff to take up. Fakes only."""
from unittest import mock

from drover import handoff, pane_files, pane_handoff, pane_launch
from drover.ledger import ops_panes
from tests.pane_rotation_support import Rotating
from tests.pane_send_support import PANE
from tests.panes_support import ROLE, Resident


class NewSession(Rotating):
    def brief(self):
        return [call[-1] for call in self.fake.calls if call[:2] == ['agent', 'prompt'] and 'restated.md' in call[-1]]

    def test_new_session_gets_the_file_and_restates_constraints_first(self):
        self.relaunched()
        self.tick()
        text, = self.brief()
        self.assertIn(str(self.path), text)
        self.assertLess(text.index('restated.md'), text.index('Read your inbox'))

    def test_it_takes_the_same_pane_on_a_new_row_with_a_fresh_session(self):
        self.relaunched()
        self.assertEqual(self.next_row['pane_id'], PANE)
        self.assertNotEqual(self.next_row['id'], self.row['id'])
        self.assertEqual(self.next_row['session_id'], 's2')
        self.assertEqual(self.row_now()['state'], 'gone')
        self.assertEqual(self.fake.seen.get(('tab', 'create')), 1)
        started = [call for call in self.fake.calls if call[:2] == ['agent', 'start']]
        self.assertEqual(len(started), 1)
        self.assertNotIn('--resume', started[0])
        self.assertEqual([row['id'] for row in ops_panes.open_agents(self.conn)], [self.next_row['id']])

    def test_the_new_run_has_its_own_generation_and_file(self):
        self.relaunched()
        run = self.runtime.active_runs()[0]
        self.assertGreater(run['generation'], self.run_row['generation'])
        self.assertEqual(run['spec']['handoff'], str(self.path))

    def test_a_wsl_session_gets_the_wsl_form(self):
        self.assertEqual(handoff.for_launch(self.port, ROLE, 'wsl', 'fresh'), {})
        handoff.set_pending(self.port, ROLE, 'C:\\state\\h.md')
        self.assertEqual(handoff.for_launch(self.port, ROLE, 'wsl', 'fresh'), dict(handoff='/mnt/c/state/h.md'))
        self.assertEqual(handoff.for_launch(self.port, ROLE, 'wsl', 'resume'), {})

    def test_a_refused_start_falls_back_to_a_fresh_pane_with_a_flag(self):
        self.rotated()
        self.fake.panes[PANE]['terminal_id'] = 't9'
        self.spec_ = dict(self.spec_, handoff=str(self.path), mode='fresh', run_id=self.rid)
        with mock.patch.object(pane_launch, 'start') as start:
            self.adapter.launch(self.spec_, 'credential', lambda: None)
        start.assert_called_once()
        self.assertEqual(len(self.notices('pane-rotation-moved:')), 1)

    def test_herdr_closed_is_no_reason_to_move(self):
        self.rotated()
        self.fake.closed = True
        spec = dict(self.spec_, handoff=str(self.path), mode='fresh')
        with mock.patch.object(pane_launch, 'start') as start, self.assertRaises(pane_launch.AdmissionHeld):
            self.adapter.launch(spec, 'credential', lambda: None)
        start.assert_not_called()

    def test_a_launch_with_no_handoff_never_takes_the_pane(self):
        self.rotated()
        with mock.patch.object(pane_launch, 'start') as start, mock.patch.object(pane_launch, 'rotate') as rotate:
            self.adapter.launch(dict(self.spec_, mode='fresh'), 'credential', lambda: None)
        start.assert_called_once()
        rotate.assert_not_called()

    def test_only_a_rotated_row_is_taken(self):
        self.cli_exits()
        retired = ops_panes.update_agent(self.conn, self.row['id'], state='retired')
        self.assertIsNone(self.panes.rotated(ROLE))
        with self.assertRaises(pane_launch.AdmissionHeld) as held:
            pane_launch.rotate(self.port, self.herdr, retired, self.spec_, cfg=self.cfg, credential='c')
        self.assertEqual(str(held.exception), 'pane-not-rotated')

    def test_a_run_that_took_no_handoff_does_not_settle_one(self):
        self.written()
        self.restated.write_text('Not this run.', encoding='utf-8')
        handoff.set_pending(self.port, ROLE, self.path)
        pane_handoff.maintain(self.panes, self.run_row)
        self.assertEqual(handoff.pending(self.port, ROLE), str(self.path))

    def test_missing_restatement_is_one_notice(self):
        self.relaunched()
        self.tick()
        run = self.runtime.active_runs()[0]
        pane_handoff.maintain(self.panes, run)
        self.assertEqual(self.notices('pane-restate:'), [])
        self.time += pane_handoff.RESTATE_S + 1
        for _ in range(2):
            pane_handoff.maintain(self.panes, run)
        self.assertEqual(len(self.notices('pane-restate:')), 1)
        self.assertEqual(handoff.pending(self.port, ROLE), str(self.path))

    def test_a_restatement_settles_the_handoff(self):
        self.relaunched()
        self.tick()
        self.restated.write_text('The rules, in my words.', encoding='utf-8')
        pane_handoff.maintain(self.panes, self.runtime.active_runs()[0])
        self.assertIsNone(handoff.pending(self.port, ROLE))


class WritableRoots(Rotating):
    def test_a_workspace_write_seat_may_write_the_handoff_folder(self):
        spec = dict(backend='codex', sandbox='workspace-write', run_config=dict(sandbox_writable_roots=['/mnt/c/state/directors/R1']))
        args = pane_files.agent_args(spec, model='m', effort='high')
        self.assertIn('sandbox_workspace_write.writable_roots=["/mnt/c/state/directors/R1"]', args)

    def test_a_read_only_seat_gets_no_writable_root(self):
        spec = dict(backend='codex', sandbox='read-only', run_config=dict(sandbox_writable_roots=['/x']))
        self.assertFalse([arg for arg in pane_files.agent_args(spec, model='m', effort='high') if 'writable_roots' in arg])

    def test_every_launch_names_the_folder_and_makes_it(self):
        self.assertTrue(handoff.folder(self.cfg, ROLE).is_dir())
        started = next(call for call in self.fake.calls + self.launch_calls if call[:2] == ['agent', 'start'])
        self.assertEqual(started[started.index('--add-dir') + 1], str(handoff.folder(self.cfg, ROLE)))

    def setUp(self):
        super().setUp()
        self.launch_calls = []
        self.rotated()
        self.next_agent_boots()
        self.tick()
        self.launch_calls = list(self.fake.calls)


class FirstLaunch(Resident):
    def test_the_handoff_folder_exists_from_the_first_launch(self):
        self.launched()
        self.assertTrue(handoff.folder(self.cfg, ROLE).is_dir())
