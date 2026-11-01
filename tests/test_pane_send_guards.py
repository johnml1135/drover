"""What holds across calls: an open write stops the next one, one Enter is claimed, and a shell is never an agent."""
import threading
from unittest import mock

from drover import compaction_hold, pane_send
from drover.ledger import connect, ops_panes
from drover.pane_send import PaneRefused
from tests.pane_send_support import CLAUDE_IDLE, Case, DIALOG, DRAFT, ENTER, IDLE, PANE, TEXT, WORKING, composing, running

SHELL_PROMPT = 'PS C:\\repos> claude\nbye\n❯ \n'


class Resend(Case):
    def opened(self, state, reason=None):
        write = ops_panes.note_write(self.conn, self.row['id'], 'wake', TEXT)
        if state != 'pending':
            ops_panes.finish_write(self.conn, write['id'], state, reason)

    def test_an_open_write_refuses_before_any_herdr_call(self):
        cases = (('pending', None, 'last-write-pending'), ('unverified', 'pasted', 'last-write-unverified'),
                 ('unverified', 'herdr-unavailable', 'last-write-unverified'),
                 ('unverified', 'enter-pressed', 'last-write-enter-pressed'))
        for state, reason, refusal in cases:
            with self.subTest(state=state, reason=reason):
                self.stage()
                self.opened(state, reason)
                self.refused(self.send, refusal)
                self.assertEqual(self.fake.calls, [])
                self.reset()

    def test_a_closed_write_lets_the_next_one_through(self):
        for state in ('sent', 'unsent', 'observed', 'acknowledged'):
            with self.subTest(state):
                self.windows()
                self.opened(state)
                self.assertEqual(self.send()['state'], 'sent')
                self.reset()

    def test_a_stalled_prompt_is_never_written_twice(self):
        fake = self.windows()
        fake.hang = {('agent', 'prompt')}
        self.assertEqual(self.send()['state'], 'unverified')
        fake.hang = False
        with self.assertRaises(PaneRefused) as caught:
            self.send()
        self.assertEqual(caught.exception.reason, 'last-write-unverified')
        self.assertEqual(len(self.sends()), 1)

    def test_after_an_enter_that_failed_no_second_paste_goes_out(self):
        fake = self.stage()
        self.paste_settles_to(composing(TEXT))
        fake.hang = {('pane', 'send-keys')}
        self.send()
        fake.hang = False
        fake.screens[PANE] = IDLE
        with self.assertRaises(PaneRefused) as caught:
            self.send()
        self.assertEqual(caught.exception.reason, 'last-write-enter-pressed')
        self.assertEqual(len(self.sends()), 2)


class Gate(Case):
    def stalled(self):
        fake = self.stage()
        fake.hang = {('pane', 'send-text')}
        self.send()
        fake.hang = False
        fake.calls.clear()
        return fake

    def test_an_interrupt_after_a_stalled_write_does_not_hide_it(self):
        fake = self.windows()
        fake.hang = {('agent', 'prompt')}
        self.send()
        fake.hang = False
        self.assertEqual(pane_send.interrupt(self.port, self.herdr, self.row)['state'], 'sent')
        fake.calls.clear()
        self.refused(self.send, 'last-write-unverified')

    def test_a_refused_stop_does_not_hide_an_open_write(self):
        fake = self.stalled()
        with mock.patch.object(compaction_hold, 'active', return_value={'id': 'h1'}):
            fake.after[('pane', 'read', fake.seen[('pane', 'read')] + 1)] = lambda: fake.screens.update({PANE: DRAFT})
            self.refused(self.stop, 'draft')
        self.assertEqual(self.last()['state'], 'unsent')
        fake.screens[PANE] = IDLE
        self.refused(self.send, 'last-write-unverified')

    def test_two_open_writes_still_refuse(self):
        self.stage()
        ops_panes.note_write(self.conn, self.row['id'], 'wake', TEXT)
        ops_panes.note_write(self.conn, self.row['id'], 'stop', 'stop now')
        self.refused(self.send, 'last-write-pending')
        self.assertEqual(self.fake.calls, [])

    def test_a_state_this_module_does_not_know_stays_open(self):
        self.stage()
        write = ops_panes.note_write(self.conn, self.row['id'], 'wake', TEXT)
        ops_panes.finish_write(self.conn, write['id'], 'wake_unverified')
        self.refused(self.send, 'last-write-wake_unverified')
        self.assertEqual(self.fake.calls, [])

    def test_submit_finds_the_open_write_under_a_newer_row(self):
        fake = self.stalled()
        write = ops_panes.note_write(self.conn, self.row['id'], 'interrupt', 'ctrl+c')
        ops_panes.finish_write(self.conn, write['id'], 'unsent', 'draft')
        fake.screens[PANE] = composing(TEXT)
        self.assertEqual(pane_send.submit_own(self.port, self.herdr, self.row)['kind'], 'wake')
        self.assertEqual(self.sends(), [ENTER])

    def test_open_writes_are_listed_oldest_first_and_the_closed_left_out(self):
        self.stage()
        states = ('pending', 'sent', 'unverified', 'unsent', 'observed', 'acknowledged', 'wake_unverified')
        for state in states:
            write = ops_panes.note_write(self.conn, self.row['id'], 'wake', TEXT)
            if state != 'pending':
                ops_panes.finish_write(self.conn, write['id'], state)
        self.assertEqual([w['state'] for w in ops_panes.open_writes(self.conn, self.row['id'])],
                         ['pending', 'unverified', 'wake_unverified'])


class Refused(Case):
    def test_only_a_code_that_proves_nothing_went_out_is_unsent(self):
        for code in ('usage', 'agent_blocked', 'pane_not_found'):
            with self.subTest(code):
                self.windows().refuse[('agent', 'prompt')] = code
                self.assertRaises(PaneRefused, self.send)
                self.assertEqual(self.last()['state'], 'unsent')
                self.reset()

    def test_any_other_code_stays_open_and_stops_the_next_write(self):
        for code in ('timeout', 'other', 'internal_error', 'agent_prompt_stalled', 'agent_not_ready'):
            with self.subTest(code):
                fake = self.windows()
                fake.refuse[('agent', 'prompt')] = code
                write = self.send()
                self.assertEqual((write['state'], write['reason']), ('unverified', f'herdr-refused:{code}'))
                del fake.refuse[('agent', 'prompt')]
                fake.calls.clear()
                self.refused(self.send, 'last-write-unverified')
                self.reset()

    def test_a_paste_and_a_key_follow_the_same_rule(self):
        fake = self.stage()
        fake.refuse[('pane', 'send-text')] = 'internal_error'
        self.assertEqual(self.send()['state'], 'unverified')
        self.sleep.assert_not_called()
        self.reset()
        fake = self.stage(WORKING)
        fake.refuse[('pane', 'send-keys')] = 'internal_error'
        self.assertEqual(pane_send.interrupt(self.port, self.herdr, self.row)['state'], 'unverified')


TURN = 'history\n\n› {}\n\n• Working (3s • esc to interrupt)\n\n› {}\n\n? for shortcuts\n'


class Landed(Case):
    def land(self):
        return pane_send.confirm_landed(self.port, self.herdr, self.row)

    def stalled(self, status='working', **stage):
        fake = self.stage(**stage)
        fake.hang = {('pane', 'send-text')}
        self.send()
        fake.hang = False
        fake.calls.clear()
        fake.agents[PANE]['agent_status'] = status
        fake.screens[PANE] = TURN.format(TEXT, '')
        return fake

    def test_the_prompt_then_a_turn_under_it_clears_the_write(self):
        self.stalled()
        write = self.land()
        self.assertEqual((write['state'], write['reason']), ('observed', 'turn-seen'))
        self.assertEqual(self.sends(), [])

    def test_a_wrapped_prompt_still_matches(self):
        self.stalled()
        self.fake.screens[PANE] = TURN.format(TEXT[:30].rstrip() + '\n  ' + TEXT[30:].lstrip(), '')
        self.assertEqual(self.land()['state'], 'observed')

    def test_the_next_send_goes_once_it_is_cleared(self):
        fake = self.stalled()
        self.land()
        fake.agents[PANE]['agent_status'] = 'idle'
        fake.screens[PANE] = IDLE
        self.paste_settles_to(composing(TEXT))
        self.assertEqual(self.send()['state'], 'sent')

    def test_no_read_that_lacks_the_proof_clears_it(self):
        history = 'docs: the footer says esc to interrupt\n\n› \n\n? for shortcuts\n'
        cases = (('idle pane', IDLE, 'idle', 'turn-not-seen'),
                 ('history text', history, 'idle', 'turn-not-seen'),
                 ('history text under herdr working', history, 'working', 'turn-not-seen'),
                 ('herdr working alone', IDLE, 'working', 'turn-not-seen'),
                 ('herdr blocked alone', IDLE, 'blocked', 'turn-not-seen'),
                 ('herdr idle under a turn', TURN.format(TEXT, ''), 'idle', 'turn-not-seen'),
                 ('herdr done under a turn', TURN.format(TEXT, ''), 'done', 'turn-not-seen'),
                 ('a turn with no prompt', WORKING, 'working', 'turn-not-seen'),
                 ('the owners prompt', TURN.format('do something else', ''), 'working', 'turn-not-seen'),
                 ('a collapsed prompt', TURN.format('[Pasted Content 900 chars]', ''), 'working', 'turn-not-seen'),
                 ('own text unsent', composing(TEXT), 'working', 'draft'),
                 ('a draft under the turn', TURN.format(TEXT, 'mine'), 'working', 'draft'),
                 ('turn above the prompt', 'x\n\n• Working (3s • esc to interrupt)\n\n› ' + TEXT + '\n\n› \n', 'working', None),
                 ('turn above an answered prompt', 'x\n\n• Working (3s • esc to interrupt)\n\n› ' + TEXT +
                  '\n\n• done\n\n› \n\n? for shortcuts\n', 'working', 'turn-not-seen'),
                 ('own prompt, then the owners', 'x\n\n› ' + TEXT + '\n\n• done\n\n› my words\n\n'
                  '• Working (3s • esc to interrupt)\n\n› \n\n? for shortcuts\n', 'working', 'turn-not-seen'),
                 ('dialog', DIALOG, 'working', 'dialog'),
                 ('held viewport', TURN.format(TEXT, '') + '^c copy · enter copy & follow · esc clear\n', 'working', None),
                 ('unreadable', 'bad \ufffd', 'working', 'screen-unreadable'))
        for name, screen, status, reason in cases:
            with self.subTest(name):
                self.stalled(status)
                self.fake.screens[PANE] = screen
                self.refused(self.land, reason)
                self.assertEqual(self.last()['state'], 'unverified')
                self.reset()

    def test_a_scrolled_or_focused_or_changed_pane_clears_nothing(self):
        for name, stage, reason in (('scrolled', dict(pane={'scroll': {'offset_from_bottom': 2}}), 'viewport_held'),
                                    ('focused', dict(agent={'focused': True}), 'focused'),
                                    ('occupant', dict(agent={'terminal_id': 't2'}), 'occupant-changed')):
            with self.subTest(name):
                fake = self.stage()
                write = ops_panes.note_write(self.conn, self.row['id'], 'wake', TEXT)
                ops_panes.finish_write(self.conn, write['id'], 'unverified', 'herdr-unavailable')
                fake.screens[PANE] = TURN.format(TEXT, '')
                fake.agents[PANE].update(agent_status='working', **stage.get('agent', {}))
                fake.panes[PANE].update(stage.get('pane', {}))
                self.refused(self.land, reason)
                self.reset()

    def test_only_a_typed_prompt_can_land(self):
        for kind, text in (('stop', TEXT), ('interrupt', 'ctrl+c'), ('shell', TEXT), ('startup', TEXT), ('start', TEXT)):
            with self.subTest(kind):
                fake = self.stage()
                write = ops_panes.note_write(self.conn, self.row['id'], kind, text)
                ops_panes.finish_write(self.conn, write['id'], 'unverified', 'herdr-unavailable')
                fake.screens[PANE] = TURN.format(text, '')
                fake.agents[PANE]['agent_status'] = 'working'
                self.refused(self.land, 'nothing-open')
                self.assertEqual(self.last()['state'], 'unverified')
                self.reset()

    def test_a_claude_read_stays_open_until_a_capture_shows_its_prompt_rows(self):
        self.stage(CLAUDE_IDLE, kind='claude')
        write = ops_panes.note_write(self.conn, self.row['id'], 'wake', TEXT)
        ops_panes.finish_write(self.conn, write['id'], 'unverified', 'herdr-unavailable')
        self.fake.screens[PANE] = f'❯ {TEXT}\n\n✢ Thinking… (3s · esc to interrupt)\n───\n❯ \n───\n'
        self.fake.agents[PANE]['agent_status'] = 'working'
        self.refused(self.land)
        self.assertEqual(self.last()['state'], 'unverified')

    def test_a_row_drover_does_not_hold_proves_nothing(self):
        self.stalled()
        stale = dict(self.row)
        self.conn.execute("UPDATE pane_agents SET started_by='owner'")
        self.refused(self.land, 'not-drovers')
        self.conn.execute("UPDATE pane_agents SET started_by='drover', state='retired'")
        self.row = stale
        self.refused(self.land, 'not-live')

    def test_herdr_closed_and_nothing_open_are_refusals(self):
        self.stalled().closed = True
        self.refused(self.land, 'herdr-unavailable')
        self.reset()
        self.windows()
        self.refused(self.land, 'nothing-open')
        self.send()
        self.fake.calls.clear()
        self.refused(self.land, 'nothing-open')


class Allowlist(Case):
    def submit(self):
        return pane_send.submit_own(self.port, self.herdr, self.row)

    def stranded(self, kind, text, **stage):
        self.stage(composing(text), **stage)
        write = ops_panes.note_write(self.conn, self.row['id'], kind, text)
        ops_panes.finish_write(self.conn, write['id'], 'unverified', 'not-own')

    def test_only_a_kind_drover_types_gets_the_enter(self):
        for kind in ('brief', 'wake', 'handoff', 'resume'):
            with self.subTest(kind):
                self.stranded(kind, TEXT)
                self.assertEqual(self.submit()['state'], 'sent')
                self.reset()
        for kind in ('interrupt', 'startup', 'shell', 'chat'):
            with self.subTest(kind):
                self.stranded(kind, TEXT)
                self.refused(self.submit, 'nothing-unsent')
                self.reset()

    def test_ctrl_c_never_gets_an_enter_under_any_kind(self):
        for kind in ('wake', 'interrupt', 'stop'):
            with self.subTest(kind), mock.patch.object(compaction_hold, 'active', return_value={'id': 'h1'}):
                self.stranded(kind, 'ctrl+c')
                self.refused(self.submit, 'nothing-unsent')
                self.reset()

    def test_interrupt_cannot_borrow_another_kind(self):
        self.stage(WORKING)
        for kind in ('brief', 'wake', 'handoff', 'resume', 'stop', 'startup'):
            with self.subTest(kind):
                self.refused(lambda: pane_send.interrupt(self.port, self.herdr, self.row, kind), 'kind-refused')
        self.assertEqual(self.fake.calls, [])

    def test_a_stop_needs_a_live_hold(self):
        self.stranded('stop', 'stop now', row={'state': 'compaction'})
        self.refused(self.submit, 'no-live-hold')
        self.reset()
        self.stranded('stop', 'stop now')
        self.refused(self.submit, 'no-live-hold')
        self.reset()
        self.stranded('stop', 'stop now', row={'state': 'compaction'}, agent={'focused': True, 'agent_status': 'working'})
        with mock.patch.object(compaction_hold, 'active', return_value={'id': 'h1'}):
            self.assertEqual(self.submit()['state'], 'sent')
        self.assertEqual(self.sends(), [ENTER])

    def test_a_compaction_row_gets_no_enter_for_any_other_kind(self):
        self.stranded('wake', TEXT, row={'state': 'compaction'})
        with mock.patch.object(compaction_hold, 'active', return_value={'id': 'h1'}):
            self.refused(self.submit, 'not-live')


class Claim(Case):
    def test_two_callers_press_one_enter(self):
        fake = self.stage()

        def second_caller(seconds):
            fake.screens[PANE] = composing(TEXT)
            self.assertEqual(pane_send.submit_own(self.port, self.herdr, self.row)['state'], 'sent')
        self.sleep.side_effect = second_caller
        self.assertEqual(self.send()['state'], 'sent')
        self.assertEqual(self.sends().count(ENTER), 1)
        self.assertEqual((self.last()['state'], self.last()['reason']), ('sent', None))

    def test_the_second_of_two_submits_is_refused(self):
        self.stage(composing(TEXT))
        write = ops_panes.note_write(self.conn, self.row['id'], 'wake', TEXT)
        ops_panes.finish_write(self.conn, write['id'], 'unverified', 'not-own')
        real = ops_panes.claim_enter

        def after_a_rival(*args):
            self.assertIsNotNone(real(*args))
            return real(*args)
        with mock.patch.object(ops_panes, 'claim_enter', after_a_rival):
            self.refused(lambda: pane_send.submit_own(self.port, self.herdr, self.row), 'enter-claimed')

    def test_a_claim_compares_the_row_in_one_transaction(self):
        self.stage()
        write = ops_panes.note_write(self.conn, self.row['id'], 'wake', TEXT)
        self.assertIsNone(ops_panes.claim_enter(self.conn, write['id'], 'unverified', 'pasted'))
        ops_panes.finish_write(self.conn, write['id'], 'unverified', 'pasted')
        self.assertEqual(ops_panes.claim_enter(self.conn, write['id'], 'unverified', 'pasted')['reason'], 'enter-pressed')
        self.assertIsNone(ops_panes.claim_enter(self.conn, write['id'], 'unverified', 'pasted'))

    def test_two_connections_cannot_both_win(self):
        self.stage()
        write = ops_panes.note_write(self.conn, self.row['id'], 'wake', TEXT)
        ops_panes.finish_write(self.conn, write['id'], 'unverified', 'pasted')
        wins, barrier = [], threading.Barrier(2)

        def caller():
            conn = connect(self.path)
            try:
                barrier.wait(5)
                wins.append(ops_panes.claim_enter(conn, write['id'], 'unverified', 'pasted'))
            finally:
                conn.close()
        threads = [threading.Thread(target=caller) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(sorted(win is not None for win in wins), [False, True])


class Foreground(Case):
    def test_a_shell_under_the_agents_mark_gets_nothing(self):
        for name, processes in (('shell only', running('pwsh.exe', shell=2)), ('nothing listed', running()),
                                ('no name', {'shell_pid': 1, 'foreground_processes': [{'pid': 2}]}),
                                ('another program', running('vim')), ('no list', {'shell_pid': 1}),
                                ('no shell pid', {'foreground_processes': [{'pid': 2, 'name': 'wsl.exe'}]}),
                                ('not a list', {'shell_pid': 1, 'foreground_processes': 'wsl.exe'}),
                                ('bad entry', {'shell_pid': 1, 'foreground_processes': ['wsl.exe']})):
            with self.subTest(name):
                fake = self.stage(SHELL_PROMPT, kind='claude')
                fake.processes = processes
                self.refused(self.send, 'agent-not-foreground')
                self.refused(lambda: pane_send.check(self.herdr, self.row), 'agent-not-foreground')
                self.refused(lambda: pane_send.interrupt(self.port, self.herdr, self.row), 'agent-not-foreground')
                self.reset()

    def test_a_shell_that_is_wsl_itself_is_no_proof(self):
        fake = self.stage()
        fake.processes = {'shell_pid': 1, 'foreground_processes': [{'pid': 1, 'name': 'wsl.exe'}]}
        self.refused(self.send, 'agent-not-foreground')

    def test_a_mark_with_nothing_of_an_agent_refuses_in_wsl(self):
        for name, screen in (('bare', 'history\n❯ \n'), ('fence far above', '───\nhistory\nmore\n❯ \n'),
                             ('text row under it', 'history\n❯ \nsome output\n')):
            with self.subTest(name):
                self.stage(screen, kind='claude')
                self.refused(self.send, 'agent-not-marked')
                self.refused(lambda: pane_send.interrupt(self.port, self.herdr, self.row), 'agent-not-marked')
                self.reset()

    def test_a_footer_or_a_fence_marks_an_agent(self):
        for name, screen in (('both', CLAUDE_IDLE), ('fence only', 'history\n───\n❯ \n───\n'),
                             ('footer only', 'history\n❯ \n⏵⏵ auto mode on\n')):
            with self.subTest(name):
                self.stage(screen, kind='claude')
                self.paste_settles_to(screen.replace('❯ ', '❯ ' + TEXT))
                self.assertEqual(self.send()['state'], 'sent')
                self.reset()

    def test_a_windows_pane_has_its_process_proof_instead(self):
        self.windows('history\n❯ \n')
        self.assertEqual(self.send()['state'], 'sent')

    def test_a_mark_that_loses_its_footer_after_the_paste_gets_no_enter(self):
        self.stage(CLAUDE_IDLE, kind='claude')
        self.paste_settles_to(f'history\n❯ {TEXT}\n')
        self.assertEqual(self.send()['reason'], 'agent-not-marked')
        self.assertNotIn(ENTER, self.sends())

    def test_process_info_missing_refuses(self):
        fake = self.stage()
        fake.processes = None
        self.refused(self.send, 'agent-not-foreground')

    def test_a_process_of_the_wrong_placement_is_no_proof(self):
        fake = self.stage()
        fake.processes = running('claude.exe')
        self.refused(self.send, 'agent-not-foreground')
        self.reset()
        fake = self.windows()
        fake.processes = running('wsl.exe')
        self.refused(self.send, 'agent-not-foreground')

    def test_the_agent_in_the_foreground_passes(self):
        for name in ('claude.exe', 'node.exe', 'Claude'):
            with self.subTest(name):
                fake = self.windows()
                fake.processes = running(name)
                self.assertEqual(self.send()['state'], 'sent')
                self.reset()
        fake = self.stage()
        fake.processes = running('wsl.exe', shell=9)
        self.paste_settles_to(composing(TEXT))
        self.assertEqual(self.send()['state'], 'sent')

    def test_a_shell_is_refused_after_the_paste_too(self):
        fake = self.stage()
        self.sleep.side_effect = lambda seconds: (fake.screens.update({PANE: composing(TEXT)}),
                                                  setattr(fake, 'processes', running()))
        self.assertEqual(self.send()['reason'], 'agent-not-foreground')
        self.assertNotIn(ENTER, self.sends())
