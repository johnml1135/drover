"""A rotation: the old CLI leaves by one guarded `/exit`, and only then does a fresh session take the pane. Fakes only."""
from unittest import mock

from drover import compaction_hold, handoff, pane_context, pane_files, pane_handoff, pane_launch, pane_send
from drover.ledger import ops_panes
from drover.pane_send import PaneRefused
from tests.pane_launch_support import CLAUDE_WORKING, LONE_SHELL, PROMPT
from tests.pane_send_support import CLAUDE_IDLE, Case, DIALOG, PANE, composing
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


class OldCliLeavesFirst(Rotating):
    def test_valid_file_rotates_at_the_next_idle_moment(self):
        self.at()
        self.written()
        self.fake.agents[PANE].update(agent_status='working')
        self.fake.screens[PANE] = CLAUDE_WORKING
        self.assertEqual(self.at()['state'], 'live')
        self.assertEqual(self.exits(), [])
        self.idle()
        self.assertEqual(self.at()['state'], 'rotating')
        self.assertEqual(len(self.exits()), 1)
        self.cli_exits()
        self.follow()
        self.assertEqual(self.outcome(), 'rotated')
        self.assertEqual(self.row_now()['handoff_state'], 'rotated')

    def test_at_300k_drover_writes_its_own_handoff_and_rotates_when_safe(self):
        self.assertEqual(self.at(FORCED)['state'], 'rotating')
        self.assertEqual(handoff.missing(self.path.read_text(encoding='utf-8')), [])
        self.assertEqual(len(self.exits()), 1)
        self.cli_exits()
        self.follow()
        self.assertEqual(self.outcome(), 'rotated')

    def test_nothing_is_fenced_and_nothing_starts_while_the_old_cli_lives(self):
        self.told_to_exit()
        for _ in range(3):
            self.follow()
            self.assertEqual(self.tick()['launched'], [])
        self.assertIsNone(self.outcome())
        self.assertEqual(self.sessions(), 1)
        self.assertEqual(self.row_now()['state'], 'rotating')
        self.assertEqual(self.fake.seen.get(('tab', 'create')), 1)

    def test_a_read_that_cannot_say_ends_nothing(self):
        self.told_to_exit()
        self.fake.closed = True
        self.follow()
        self.assertIsNone(self.outcome())

    def test_the_exit_goes_once(self):
        self.told_to_exit()
        for _ in range(3):
            self.at()
            self.follow()
        self.tick()
        self.assertEqual(len(self.exits()), 1)

    def test_a_late_exit_is_one_notice_and_no_more_input(self):
        self.told_to_exit()
        self.fake.calls.clear()
        self.follow()
        self.assertEqual(self.notices('pane-exit:'), [])
        self.time += pane_handoff.EXIT_S + 1
        self.follow()
        self.follow()
        self.assertEqual(len(self.notices('pane-exit:')), 1)
        self.assertEqual(self.exits(), [])

    def test_a_refused_exit_waits_with_one_notice(self):
        self.at()
        self.written()
        with mock.patch.object(pane_send, 'exit_agent', side_effect=PaneRefused('last-write-unverified')):
            for _ in range(2):
                self.assertEqual(self.at()['state'], 'live')
        self.assertEqual(len(self.notices('pane-rotation-waits:')), 1)
        self.assertIsNone(self.outcome())
        self.assertIsNone(handoff.pending(self.port, ROLE))
        self.assertEqual(self.at()['state'], 'rotating')

    def test_a_write_that_may_have_landed_is_never_made_again(self):
        self.at()
        self.written()
        with mock.patch.object(handoff, 'set_pending', side_effect=RuntimeError('the hub went away')):
            with self.assertRaises(RuntimeError):
                self.at()
        self.assertEqual(self.row_now()['state'], 'live')
        self.assertEqual(len(self.exits()), 1)
        self.assertEqual(self.at()['state'], 'rotating')
        self.assertEqual(len(self.exits()), 1)

    def test_no_exit_during_a_compaction(self):
        self.at()
        self.written()
        with mock.patch.object(compaction_hold, 'active', return_value={'id': 'h'}):
            self.assertEqual(self.at()['state'], 'live')
        self.assertEqual(self.exits(), [])

    def test_a_stop_during_a_rotation_still_ends_the_run_when_the_cli_leaves(self):
        self.told_to_exit()
        self.runtime.stop_active(self.notifications.admission()['epoch'])
        run = self.runtime.active_runs()[0]
        self.assertIsNotNone(run['stop_action_id'])
        self.cli_exits()
        self.panes._keep(run, self.guard)
        self.assertEqual(self.outcome(), 'rotated')
        self.assertEqual(self.conn.execute("SELECT state FROM action_outbox WHERE action_id=?", (run['stop_action_id'],)).fetchone()[0], 'complete')

    def test_a_rotating_agent_leaving_is_no_lost_director(self):
        self.told_to_exit()
        self.cli_exits()
        self.tick()
        self.assertEqual(self.notices('pane-agent-left:'), [])
        self.assertEqual(self.outcome(), 'rotated')


class Rotated(Rotating):
    def test_rotation_needs_no_replacement_budget(self):
        self.relaunched()
        self.assertEqual(self.outcome(), 'rotated')
        self.assertEqual(self.outcome(self.next_run), None)
        self.assertEqual(self.role()['replacements_used'], 0)

    def test_old_session_is_fenced_and_its_pane_retired(self):
        self.rotated()
        row = self.row_now()
        self.assertEqual((row['state'], row['handoff_state']), ('retired', 'rotated'))
        self.assertEqual(self.sessions(), 0)
        self.assertEqual(self.role()['pending_wake'], 1)
        self.assertEqual(self.port.events(kind='director.rotated')[-1]['data']['run_id'], self.rid)

    def test_open_writes_close_with_the_row(self):
        self.told_to_exit()
        ops_panes.note_write(self.conn, self.row['id'], 'wake', 'x')
        self.cli_exits()
        self.follow()
        self.assertEqual(ops_panes.open_writes(self.conn, self.row['id']), [])

    def test_a_cleared_pending_wake_does_not_lose_the_role(self):
        self.rotated()
        self.conn.execute('UPDATE director_runtime SET pending_wake=0')
        self.next_agent_boots()
        self.assertEqual(len(self.tick()['launched']), 1)

    def test_a_handoff_still_owed_starts_the_role_after_a_failed_run(self):
        self.relaunched()
        self.runtime.finish(self.next_run, dict(status='pane-gone', usage={}))
        ops_panes.update_agent(self.conn, self.next_row['id'], state='gone')
        self.conn.execute('UPDATE director_runtime SET pending_wake=0')
        self.time += 700
        with mock.patch.object(pane_launch, 'start'):
            self.assertEqual(len(self.tick()['launched']), 1)
        self.assertEqual(self.runtime.active_runs()[0]['spec']['handoff'], str(self.path))

    def test_last_rotations_restatement_does_not_count(self):
        self.told_to_exit()
        self.restated.write_text('from before', encoding='utf-8')
        self.cli_exits()
        self.follow()
        self.assertFalse(self.restated.exists())

    def test_a_restatement_that_cannot_be_cleared_ends_nothing(self):
        self.told_to_exit()
        self.cli_exits()
        with mock.patch.object(handoff, 'clear', side_effect=PermissionError('held')):
            self.follow()
        self.assertIsNone(self.outcome())


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
        with self.assertRaises(pane_launch.AdmissionHeld):
            pane_launch.rotate(self.port, self.herdr, self.row, self.spec_, cfg=self.cfg, credential='c')

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


class ExitAgent(Case):
    def ready(self, screen=CLAUDE_IDLE, **stage):
        self.windows(screen, **stage)
        ops_panes.update_agent(self.conn, self.row['id'], handoff_state='written')
        self.row = ops_panes.by_pane(self.conn, PANE)

    def exit(self):
        return pane_send.exit_agent(self.port, self.herdr, self.row)

    def test_it_goes_as_one_prompt_on_windows(self):
        self.ready()
        self.assertEqual(self.exit()['kind'], 'exit')
        self.assertEqual(self.sends(), [EXIT])

    def test_a_wsl_agent_gets_one_paste_and_one_enter(self):
        self.stage(row=dict(handoff_state='written'))
        self.sleep.side_effect = lambda seconds: self.fake.screens.update({PANE: composing(pane_send.EXIT_TEXT)})
        self.assertEqual(self.exit()['state'], 'sent')
        self.assertEqual([call[:2] for call in self.sends()], [['pane', 'send-text'], ['pane', 'send-keys']])

    def test_it_needs_the_handoff_written(self):
        for state in (None, 'requested', 'rotated'):
            self.windows(row=dict(handoff_state=state))
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

    def test_send_cannot_type_it(self):
        self.ready()
        self.refused(lambda: pane_send.send(self.port, self.herdr, self.row, pane_send.EXIT_TEXT, 'exit'), 'kind-refused')
