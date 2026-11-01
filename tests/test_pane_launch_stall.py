"""A launch that stalled, the one start for a role, and `resume`: nothing is ever resent."""
from pathlib import Path
from unittest import mock

from drover import pane_launch, pane_send
from drover.launch import AdmissionHeld
from drover.ledger import ops_panes
from tests.pane_launch_support import LONE_SHELL, PROMPT, SECRET, Launching
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

    def test_a_stall_on_a_dialog_is_named_as_the_dialog(self):
        fake = self.scene(boots=False)
        fake.hang = {('agent', 'start')}

        def blocked():
            fake.processes = self.foreground
            fake.screens[PANE] = 'Do you trust the files in this folder?\n'
        fake.after[('agent', 'start', 1)] = blocked
        self.assertEqual(self.start()['state'], 'failed')
        self.assertEqual(self.reason(), 'dialog')

    def test_a_stalled_launch_is_read_and_the_read_is_on_record(self):
        self.stalled()
        row = self.start()
        failed = self.port.events(kind='pane-launch-failed')[-1]['data']
        self.assertEqual((failed['row'], failed['reason'], failed['shell_alone']), (row['id'], 'launch-stalled', False))

    def test_a_stalled_write_is_closed_but_keeps_its_claim(self):
        self.stalled()
        row = self.start()
        self.assertEqual(ops_panes.open_writes(self.conn, row['id']), [])
        write = ops_panes.last_write(self.conn, row['id'], 'start')
        self.assertEqual((write['state'], write['reason']), ('observed', 'launch-failed'))
        self.assertIsNone(ops_panes.note_once(self.conn, row['id'], pane_send.LAUNCHES, 'start', 'x'))

    def test_a_stalled_environment_line_ends_the_launch_there(self):
        fake = self.scene(boots=False)
        fake.hang = {('pane', 'run')}
        self.assertEqual(self.start()['state'], 'failed')
        self.assertEqual((self.reason(), self.calls('agent', 'start')), ('launch-stalled', []))

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


class ShellPrompt(Launching):
    """A new tab draws its prompt when it is ready, so the launch waits for it, once, and leaves no blank tab behind."""

    def blank(self):
        fake = self.scene()
        fake.screens[PANE] = ''
        return fake

    def test_a_slow_prompt_is_waited_for_and_the_launch_goes_once(self):
        fake = self.blank()
        self.turns()
        fake.after[('pane', 'read', 3)] = lambda: fake.screens.update({PANE: PROMPT})
        self.assertEqual(self.start()['state'], 'live')
        self.assertEqual(len(self.calls('pane', 'run')), 1)

    def test_a_prompt_that_never_comes_closes_the_blank_tab(self):
        self.blank()
        row = self.start()
        self.assertEqual(self.calls('pane', 'run'), [])
        self.assertEqual(self.calls('pane', 'close'), [['pane', 'close', PANE]])
        self.assertEqual(self.reason(), 'shell-not-ready')
        self.assertEqual((row['state'], self.rows()), ('gone', []))
        self.assertIn('blank tab was closed', self.notices()[0]['body'])
        self.assertLessEqual(self.sleep.call_count, pane_launch.SHELL_PROMPT_S)

    def test_a_closed_tab_leaves_the_role_free(self):
        fake = self.blank()
        self.start()
        fake.screens['w1:p2'] = PROMPT
        self.assertEqual(self.start()['pane_id'], 'w1:p2')

    def test_a_tab_that_is_not_blank_or_not_clearly_ours_stays(self):
        cases = dict(typed=dict(screen=PROMPT + 'git status'), banner=dict(screen='PowerShell 7.5\n'),
                     focused=dict(pane={'focused': True}), program=dict(processes=running('dotnet.exe')))
        for count, (name, case) in enumerate(cases.items(), 1):
            with self.subTest(name):
                fake = self.scene()
                fake.screens[PANE] = case.get('screen', '')
                fake.processes = case.get('processes', LONE_SHELL)
                real = fake._tab_create

                def tab(args):
                    answer = real(args)
                    fake.panes[PANE].update(case.get('pane', {}))
                    return answer
                fake._tab_create = tab
                row = self.start(self.spec(role_id=name, run_id=count))
                self.assertEqual(row['state'], 'failed')
                self.assertEqual(self.calls('pane', 'close'), [])
                self.reset()

    def test_a_refusal_waiting_cannot_clear_ends_at_once_and_keeps_the_tab(self):
        fake = self.blank()
        real = fake._tab_create

        def focused(args):
            answer = real(args)
            fake.panes[PANE]['focused'] = True
            return answer
        fake._tab_create = focused
        self.assertEqual(self.start()['state'], 'failed')
        self.assertEqual((self.reason(), self.sleep.call_count, self.calls('pane', 'close')), ('focused', 0, []))

    def test_only_a_shell_that_never_came_closes_the_tab(self):
        self.blank()
        with mock.patch.object(pane_send, 'MAX_TEXT', 5):
            row = self.start()
        self.assertEqual((row['state'], self.reason(), self.calls('pane', 'close')), ('failed', 'text-too-long', []))

    def test_a_close_that_fails_leaves_the_row_failed(self):
        self.blank().refuse[('pane', 'close')] = 'pane_busy'
        row = self.start()
        self.assertEqual(row['state'], 'failed')
        self.assertIn('Closing it frees the role.', self.notices()[0]['body'])

    def test_herdr_closing_midway_keeps_the_tab(self):
        fake = self.blank()
        fake.after[('pane', 'read', 2)] = lambda: setattr(fake, 'closed', True)
        row = self.start()
        self.assertEqual((row['state'], self.calls('pane', 'close')), ('failed', []))

    def test_a_tab_with_a_launch_on_record_is_never_closed(self):
        fake = self.scene(boots=False)
        row = self.start()
        self.assertEqual((row['state'], fake.panes[PANE]['pane_id'], self.calls('pane', 'close')), ('failed', PANE, []))


class BlankTabGuards(Launching):
    """The close's own reads: earlier guards hide them on the launch path, and a close cannot be taken back."""

    def blank_row(self, **over):
        fake = self.scene()
        fake._tab_create(['tab', 'create', '--workspace', 'w1'])
        fake.screens[PANE] = ''
        self.row = ops_panes.record_agent(self.conn, **{'pane_id': PANE, 'terminal_id': 't1', 'kind': 'claude', 'seat': 'seat-c',
                                                        'placement': 'windows', 'started_by': 'drover', **over})
        return fake

    def kept(self):
        self.assertIs(pane_launch.pane_blank.close_blank(self.port, self.herdr, self.row), False)
        self.assertEqual(self.calls('pane', 'close'), [])

    def test_a_blank_first_row_closes(self):
        self.blank_row()
        self.assertIs(pane_launch.pane_blank.close_blank(self.port, self.herdr, self.row), True)
        self.assertEqual(self.calls('pane', 'close'), [['pane', 'close', PANE]])

    def test_a_write_that_never_went_out_does_not_keep_it(self):
        self.blank_row()
        ops_panes.finish_write(self.conn, ops_panes.note_write(self.conn, self.row['id'], 'shell', 'x')['id'], 'unsent')
        self.assertIs(pane_launch.pane_blank.close_blank(self.port, self.herdr, self.row), True)

    def test_a_write_that_may_have_gone_out_keeps_it(self):
        for state in ('sent', 'unverified', 'observed'):
            with self.subTest(state):
                self.blank_row()
                ops_panes.finish_write(self.conn, ops_panes.note_write(self.conn, self.row['id'], 'shell', 'x')['id'], state)
                self.kept()
                self.reset()

    def test_a_reused_pane_is_kept(self):
        self.blank_row(state='gone')
        self.row = ops_panes.record_agent(self.conn, pane_id=PANE, terminal_id='t1', kind='claude', seat='seat-c', placement='windows',
                                          started_by='drover')
        self.kept()

    def test_focus_at_the_last_read_keeps_it(self):
        for focused in (True, None):
            with self.subTest(focused):
                self.blank_row()
                self.fake.panes[PANE]['focused'] = focused
                self.kept()
                self.reset()

    def test_a_changed_terminal_keeps_it(self):
        self.blank_row()
        self.fake.panes[PANE]['terminal_id'] = 't2'
        self.kept()


class EchoedLaunchLine(Launching):
    """A shell leaves what it ran on screen. Drover's own line passes; anything else that looks like it does not."""

    def test_a_windows_launch_passes_its_own_environment_line(self):
        self.scene()
        self.turns()
        self.assertEqual(self.start()['state'], 'live')
        self.assertEqual(len(self.calls('agent', 'start')), 1)

    def test_a_line_drover_did_not_run_stops_the_agent_start(self):
        fake = self.scene()
        fake.after[('pane', 'run', 1)] = lambda: fake.screens.update({PANE: PROMPT + 'git push\n' + PROMPT})
        self.assertEqual(self.start()['state'], 'failed')
        self.assertEqual(self.calls('agent', 'start'), [])

    def test_only_a_shell_write_on_this_pane_counts(self):
        self.scene()
        row = ops_panes.record_agent(self.conn, pane_id=PANE, terminal_id='t1', kind='claude', seat='s', placement='windows', started_by='drover')
        other = ops_panes.record_agent(self.conn, pane_id='w1:p9', terminal_id='t9', kind='claude', seat='s', placement='windows', started_by='drover')
        recycled = ops_panes.record_agent(self.conn, pane_id=PANE, terminal_id='t2', kind='claude', seat='s', placement='windows', started_by='drover')
        ops_panes.finish_write(self.conn, ops_panes.note_write(self.conn, row['id'], 'brief', 'a')['id'], 'sent')
        ops_panes.finish_write(self.conn, ops_panes.note_write(self.conn, other['id'], 'shell', 'b')['id'], 'sent')
        ops_panes.finish_write(self.conn, ops_panes.note_write(self.conn, recycled['id'], 'shell', 'd')['id'], 'sent')
        self.assertEqual(ops_panes.ran_on_pane(self.conn, PANE, 't1'), set())
        ops_panes.finish_write(self.conn, ops_panes.note_write(self.conn, row['id'], 'shell', 'c')['id'], 'sent')
        self.assertEqual(len(ops_panes.ran_on_pane(self.conn, PANE, 't1')), 1)
        self.assertEqual(len(ops_panes.ran_on_pane(self.conn, PANE, 't2')), 1)
        self.assertEqual(ops_panes.ran_on_pane(self.conn, PANE, None), set())


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

    def test_herdr_closed_is_said_before_any_failed_pane_is_read(self):
        self.scene().closed = True
        self.failed_row()
        self.held('herdr-unavailable')

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

    def test_a_credential_takes_the_pane_into_the_new_run(self):
        old = self.live()
        row = pane_launch.resume(self.port, self.herdr, old, self.spec('codex', 'wsl', run_id=99), cfg=self.cfg, credential=SECRET)
        self.assertEqual((row['run_id'], row['pane_id']), (99, old['pane_id']))
        path, text, side = self.private[-1]
        self.assertEqual((Path(path).name, text, side), ('run-99.credential', SECRET, 'wsl'))
        self.assertFalse(any(SECRET in str(call) for call in self.fake.calls))

    def test_claude_on_windows_resumes_through_agent_start(self):
        old = self.live('claude', 'windows')
        self.assertEqual(self.resume(old)['state'], 'live')
        self.assertEqual(self.calls('agent', 'start')[0][8:10], ['--resume', 'sess-1'])
        self.assertEqual(len(self.calls('pane', 'run')), 1)

    def test_seat_and_placement_come_from_the_row_not_the_spec(self):
        old = self.live()
        row = self.resume(old, self.spec('claude', 'windows', run_id=99))
        self.assertEqual((row['seat'], row['placement'], row['kind']), ('seat-x', 'wsl', 'codex'))
        self.assertIn('exec codex resume sess-1 ', self.sh())

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

    def test_a_stale_copy_of_a_row_cannot_resume(self):
        old = self.live()
        newer = ops_panes.record_agent(self.conn, pane_id=PANE, kind='codex', seat='seat-x', placement='wsl', started_by='drover')
        with self.assertRaises(AdmissionHeld):
            self.resume(old)
        self.assertEqual(self.calls('pane', 'run'), [])
        self.assertEqual(ops_panes.by_pane(self.conn, PANE)['id'], newer['id'])

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

    def test_an_agent_still_in_the_pane_holds_and_changes_nothing(self):
        old = self.live()
        self.fake.processes = running('wsl.exe')
        self.fake.screens[PANE] = IDLE
        with self.assertRaises(AdmissionHeld) as caught:
            self.resume(old)
        self.assertEqual(str(caught.exception), 'pane-agent-running')
        self.assertEqual(self.calls('pane', 'run'), [])
        self.assertEqual([r['id'] for r in self.rows()], [old['id']])

    def ran_before(self, old, line="& 'C:\\state\\panes\\7\\launch.ps1'"):
        """The old launch's line is still on screen above the prompt, as a shell leaves it."""
        write = ops_panes.note_write(self.conn, old['id'], 'shell', line)
        ops_panes.finish_write(self.conn, write['id'], 'sent')
        self.fake.screens[PANE] = f'{PROMPT}{line}\nagent output\n{PROMPT}'

    def test_a_resume_passes_the_old_launch_line_on_screen(self):
        old = self.live()
        self.ran_before(old)
        self.assertEqual(self.resume(old)['state'], 'live')
        self.assertEqual(len(self.calls('pane', 'run')), 1)

    def test_any_other_line_on_screen_refuses_with_no_wait(self):
        old = self.live()
        self.ran_before(old)
        self.fake.screens[PANE] = PROMPT + 'claude --model opus\n' + self.fake.screens[PANE]
        row = self.resume(old)
        self.assertEqual((row['state'], self.calls('pane', 'run'), self.sleep.call_count), ('failed', [], 0))
        self.assertEqual(self.reason(), 'shell-line-not-empty')

    def test_a_resume_that_refuses_falls_back_to_a_fresh_pane(self):
        fake = self.fake_after_refused_resume()
        row = self.start()
        self.assertEqual(self.calls('tab', 'create')[0][:2], ['tab', 'create'])
        self.assertEqual(row['pane_id'], 'w1:p2')
        self.assertEqual({call[2] for call in self.calls('pane', 'run')}, {'w1:p2'})
        self.assertEqual(fake.screens[PANE], PROMPT + 'typed\n' + PROMPT)

    def fake_after_refused_resume(self):
        old = self.live('claude', 'windows')
        self.fake.screens[PANE] = PROMPT + 'typed\n' + PROMPT
        self.fake.screens['w1:p2'] = PROMPT
        self.assertEqual(self.resume(old)['state'], 'failed')
        self.fake.after.clear()
        return self.fake

    def test_a_refused_resume_never_closes_the_pane(self):
        self.fake_after_refused_resume()
        self.assertEqual(self.calls('pane', 'close'), [])

    def test_a_stalled_resume_is_never_repeated(self):
        old = self.live()
        self.fake.hang = {('pane', 'run')}
        row = self.resume(old)
        self.assertEqual((row['state'], len(self.calls('pane', 'run'))), ('failed', 1))
