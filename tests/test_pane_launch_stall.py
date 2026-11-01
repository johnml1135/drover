"""A launch that stalled, the one start for a role, and `resume`: nothing is ever resent."""
from drover import pane_launch, pane_send
from drover.launch import AdmissionHeld
from drover.ledger import ops, ops_panes
from tests.pane_launch_support import LONE_SHELL, PROMPT, Launching
from tests.pane_send_support import IDLE, PANE, running


class Stalled(Launching):
    def stalled(self, code='start_unconfirmed'):
        fake = self.scene(boots=False)
        if code == 'start_unconfirmed':
            fake.hang = {('agent', 'start')}
        else:
            fake.refuse[('agent', 'start')] = code
        fake.after[('agent', 'start', 1)] = self.boot
        return fake

    def test_start_unconfirmed_ends_the_row_failed_and_starts_no_second_agent(self):
        self.stalled()
        row = self.start()
        self.assertEqual((row['state'], len(self.calls('agent', 'start'))), ('failed', 1))
        self.assertEqual(self.calls('agent', 'prompt'), [])

    def test_a_stalled_launch_is_read_and_the_read_is_on_record(self):
        self.stalled()
        row = self.start()
        failed = ops.events(self.conn, kind='pane-launch-failed')[-1]['data']
        self.assertEqual((failed['row'], failed['reason'], failed['shell_alone']), (row['id'], 'launch-stalled', False))

    def test_a_stalled_write_is_closed_but_keeps_its_claim(self):
        self.stalled()
        row = self.start()
        self.assertEqual(ops_panes.open_writes(self.conn, row['id']), [])
        write = ops_panes.last_write(self.conn, row['id'], 'start')
        self.assertEqual((write['state'], write['reason']), ('observed', 'launch-failed'))
        self.assertIsNone(ops_panes.note_once(self.conn, row['id'], pane_send.LAUNCHES, 'start', 'x'))

    def test_a_stalled_pane_run_is_never_repeated(self):
        fake = self.scene('codex', 'wsl', boots=False)
        fake.hang = {('pane', 'run')}
        row = self.start(self.spec('codex', 'wsl'))
        self.assertEqual((row['state'], len(self.calls('pane', 'run'))), ('failed', 1))

    def test_a_failed_row_gets_no_more_input(self):
        fake = self.stalled()
        row = self.start()
        fake.hang = False
        fake.calls.clear()
        self.refused(lambda: pane_send.send(self.port, self.herdr, row, 'hello', 'wake'), 'not-live')
        self.refused(lambda: pane_send.start_agent(self.port, self.herdr, row, 'director-7'), 'not-live')

    def test_a_launch_refused_before_it_went_fails_with_one_notice(self):
        fake = self.scene()
        fake.screens[PANE] = PROMPT + 'git status'
        row = self.start()
        self.assertEqual((row['state'], self.calls('pane', 'run')), ('failed', []))
        self.assertEqual(len(self.notices()), 1)


class OneAgentForARole(Launching):
    def failed_row(self, **over):
        return ops_panes.record_agent(self.conn, **{
            'pane_id': 'w1:p8', 'terminal_id': 't8', 'kind': 'claude', 'seat': 'seat-c', 'placement': 'windows',
            'started_by': 'drover', 'state': 'failed', 'role_id': 'R1', 'run_id': 3, **over})

    def old_pane(self, processes):
        self.fake.panes['w1:p8'] = dict(pane_id='w1:p8', terminal_id='t8', focused=False)
        self.fake.processes = processes

    def held(self, reason):
        with self.assertRaises(AdmissionHeld) as caught:
            self.start()
        self.assertEqual(str(caught.exception), reason)
        self.assertEqual(self.calls('tab', 'create'), [])

    def test_a_row_still_running_holds_the_role(self):
        for state in ('starting', 'live', 'rotating', 'compaction'):
            with self.subTest(state):
                self.scene()
                self.failed_row(state=state)
                self.held('pane-role-occupied')
                self.reset()

    def test_another_role_or_a_retired_row_does_not(self):
        self.scene()
        self.turns()
        self.failed_row(role_id='R2', state='live')
        self.failed_row(pane_id='w1:p7', state='retired')
        self.assertEqual(self.start()['state'], 'live')

    def test_an_agent_still_in_a_failed_pane_holds_the_role(self):
        self.scene()
        self.failed_row()
        self.old_pane(running('claude.exe'))
        self.held('pane-role-unconfirmed')

    def test_a_pane_that_cannot_be_read_holds_the_role(self):
        fake = self.scene()
        self.failed_row()
        self.old_pane(LONE_SHELL)
        fake.refuse[('pane', 'process-info')] = 'server_busy'
        self.held('pane-role-unconfirmed')

    def test_a_fresh_row_starts_after_a_read_shows_no_agent(self):
        self.scene()
        self.turns()
        old = self.failed_row()
        self.old_pane(LONE_SHELL)
        row = self.start()
        self.assertEqual(row['state'], 'live')
        self.assertNotEqual(row['id'], old['id'])
        self.assertIn(['pane', 'process-info', '--pane', 'w1:p8'], self.fake.calls)

    def test_a_failed_pane_that_is_gone_clears_the_role(self):
        fake = self.scene()
        self.turns()
        self.failed_row()
        real = fake._answer
        fake._answer = lambda args: (1, '', '{"error": {"code": "pane_not_found"}}') if 'w1:p8' in args else real(args)
        self.assertEqual(self.start()['state'], 'live')

    def test_the_stalled_launch_is_what_holds_the_next_one(self):
        fake = self.scene(boots=False)
        fake.hang = {('agent', 'start')}
        fake.after[('agent', 'start', 1)] = self.boot
        self.start()
        fake.hang = False
        fake.calls.clear()
        self.held('pane-role-unconfirmed')
        self.assertEqual(self.calls('agent', 'start'), [])


class Resume(Launching):
    def live(self, kind='codex', placement='wsl', **over):
        fake = self.scene(kind, placement, boots=False)
        fake._tab_create(['tab', 'create', '--workspace', 'w1'])
        fake.screens[PANE] = PROMPT
        fake.after[('pane', 'run', 1) if placement == 'wsl' else ('agent', 'start', 1)] = self.boot
        return ops_panes.record_agent(self.conn, **{
            'pane_id': PANE, 'terminal_id': 't1', 'kind': kind, 'seat': f"seat-{'x' if kind == 'codex' else 'c'}",
            'placement': placement, 'started_by': 'drover', 'state': 'compaction', 'session_id': 'sess-1', 'role_id': 'R1',
            'run_id': 7, 'cwd': str(self.cwd), 'name': 'director-7', **over})

    def resume(self, row, spec=None):
        return pane_launch.resume(self.port, self.herdr, row, spec or self.spec(row['kind'], row['placement']), cfg=self.cfg)

    def sh(self):
        return (self.state / 'panes' / '7' / 'launch.sh').read_text(encoding='utf-8')

    def test_codex_resumes_its_recorded_session_in_the_same_pane(self):
        old = self.live()
        row = self.resume(old)
        self.assertEqual((row['state'], row['pane_id'], row['session_id']), ('live', PANE, 's1'))
        self.assertIn('exec codex resume sess-1 ', self.sh())
        self.assertEqual(self.calls('tab', 'create'), [])

    def test_claude_resumes_with_its_flag(self):
        old = self.live('claude')
        self.resume(old)
        self.assertIn('exec claude --resume sess-1 ', self.sh())

    def test_claude_on_windows_resumes_through_agent_start(self):
        old = self.live('claude', 'windows')
        self.assertEqual(self.resume(old)['state'], 'live')
        self.assertEqual(self.calls('agent', 'start')[0][8:10], ['--resume', 'sess-1'])
        self.assertEqual(len(self.calls('pane', 'run')), 1)

    def test_seat_and_placement_come_from_the_row_not_the_spec(self):
        old = self.live()
        row = self.resume(old, self.spec('claude', 'windows'))
        self.assertEqual((row['seat'], row['placement'], row['kind']), ('seat-x', 'wsl', 'codex'))

    def test_no_brief_goes(self):
        old = self.live()
        self.resume(old)
        self.assertEqual(self.calls('pane', 'send-text'), [])
        self.assertEqual(self.calls('agent', 'prompt'), [])

    def test_the_old_row_is_history_and_a_new_row_holds_the_pane(self):
        old = self.live()
        row = self.resume(old)
        self.assertEqual(ops_panes.by_pane(self.conn, PANE)['id'], row['id'])
        self.assertNotEqual(row['id'], old['id'])
        self.assertEqual(self.conn.execute('SELECT state FROM pane_agents WHERE id=?', (old['id'],)).fetchone()[0], 'gone')

    def test_a_row_that_cannot_resume_changes_nothing(self):
        cases = dict(retired=dict(state='retired'), failed=dict(state='failed'), starting=dict(state='starting'),
                     no_session=dict(session_id=None), bad_session=dict(session_id='--dangerous'),
                     owners=dict(started_by='owner'))
        for name, over in cases.items():
            with self.subTest(name):
                row = self.live(**over)
                with self.assertRaises(AdmissionHeld):
                    self.resume(row)
                self.assertEqual([r['id'] for r in self.rows()], [row['id']])
                self.assertEqual(self.calls('pane', 'run'), [])
                self.reset()

    def test_an_open_write_stops_a_resume(self):
        row = self.live()
        ops_panes.note_write(self.conn, row['id'], 'stop', 'stop now')
        with self.assertRaises(AdmissionHeld) as caught:
            self.resume(row)
        self.assertEqual(str(caught.exception), 'pane-open-write')

    def test_a_changed_terminal_is_no_longer_drovers(self):
        row = self.live()
        self.fake.panes[PANE]['terminal_id'] = 't2'
        with self.assertRaises(AdmissionHeld) as caught:
            self.resume(row)
        self.assertEqual(str(caught.exception), 'pane-occupant-changed')
        self.assertEqual(self.calls('pane', 'run'), [])

    def test_herdr_closed_holds(self):
        row = self.live()
        self.fake.closed = True
        with self.assertRaises(AdmissionHeld) as caught:
            self.resume(row)
        self.assertEqual(str(caught.exception), 'herdr-unavailable')
        self.assertEqual(self.rows()[0]['id'], row['id'])

    def test_an_agent_still_in_the_pane_gets_no_command(self):
        old = self.live()
        self.fake.processes = running('wsl.exe')
        self.fake.screens[PANE] = IDLE
        row = self.resume(old)
        self.assertEqual((row['state'], self.calls('pane', 'run')), ('failed', []))

    def test_a_stalled_resume_is_never_repeated(self):
        old = self.live()
        self.fake.hang = {('pane', 'run')}
        row = self.resume(old)
        self.assertEqual((row['state'], len(self.calls('pane', 'run'))), ('failed', 1))
