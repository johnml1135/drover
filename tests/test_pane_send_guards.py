"""What holds across calls: an open write stops the next one, one Enter is claimed, and a shell is never an agent."""
import threading
from unittest import mock

from drover import compaction_hold, pane_send
from drover.ledger import connect, ops_panes
from drover.pane_send import PaneRefused
from tests.pane_send_support import (CLAUDE_IDLE, Case, DRAFT, ENTER, IDLE, PANE, TEXT, WORKING, composing, running)

SHELL_PROMPT = 'PS C:\\repos> claude\nbye\n❯ \n'
TRUST = 'Trust this folder?\n› 1. Trust and continue\n2. Quit\n'
ANSWER = ['pane', 'send-keys', PANE, 'enter']
COMMAND = 'wsl.exe -d Ubuntu -e bash -li /home/x/launch.sh'


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


class Landed(Case):
    def land(self):
        return pane_send.confirm_landed(self.port, self.herdr, self.row)

    def stalled(self, screen=CLAUDE_IDLE, **agent):
        fake = self.windows(screen)
        fake.hang = {('agent', 'prompt')}
        self.send()
        fake.hang = False
        fake.calls.clear()
        fake.agents[PANE].update(agent)

    def test_a_read_that_shows_the_turn_clears_the_write(self):
        for name, screen, agent in (('herdr working', CLAUDE_IDLE, {'agent_status': 'working'}),
                                    ('herdr blocked', CLAUDE_IDLE, {'agent_status': 'blocked'}),
                                    ('turn on screen', WORKING, {})):
            with self.subTest(name):
                self.stalled(**agent)
                self.fake.screens[PANE] = screen
                self.assertEqual(self.land()['state'], 'observed')
                self.assertEqual(self.sends(), [])
                self.reset()

    def test_the_next_send_goes_once_it_is_cleared(self):
        self.stalled(agent_status='working')
        self.land()
        self.fake.agents[PANE]['agent_status'] = 'idle'
        self.assertEqual(self.send()['state'], 'sent')

    def test_an_idle_empty_pane_proves_nothing(self):
        self.stalled()
        self.refused(self.land, 'turn-not-seen')
        self.assertEqual(self.last()['state'], 'unverified')

    def test_own_text_still_in_the_composer_has_not_landed(self):
        self.stalled(agent_status='working')
        self.fake.screens[PANE] = CLAUDE_IDLE.replace('❯ ', '❯ ' + TEXT)
        self.refused(self.land, 'turn-not-seen')

    def test_another_occupant_or_an_unreadable_screen_proves_nothing(self):
        self.stalled(agent_status='working', terminal_id='t2')
        self.refused(self.land, 'occupant-changed')
        self.reset()
        self.stalled(agent_status='working')
        self.fake.screens[PANE] = 'bad \ufffd screen'
        self.refused(self.land, 'screen-unreadable')

    def test_a_row_drover_does_not_hold_proves_nothing(self):
        self.stalled(agent_status='working')
        stale = dict(self.row)
        self.conn.execute("UPDATE pane_agents SET started_by='owner'")
        self.refused(self.land, 'not-drovers')
        self.conn.execute("UPDATE pane_agents SET started_by='drover', state='retired'")
        self.row = stale
        self.refused(self.land, 'not-live')

    def test_herdr_closed_and_nothing_open_are_refusals(self):
        self.stalled(agent_status='working')
        self.fake.closed = True
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


class Startup(Case):
    def fresh(self, screen=TRUST, *, row=None, **stage):
        fake = self.stage(screen, row={'state': 'starting', 'session_id': None, **(row or {})}, **stage)
        fake.agents.clear()
        return fake

    def answer(self, **over):
        return pane_send.answer_startup(self.port, self.herdr, self.row, **{'trusted': True, 'fresh': True, **over})

    def test_one_enter_goes_to_a_fresh_trust_dialog(self):
        self.fresh()
        write = self.answer()
        self.assertEqual(self.sends(), [ANSWER])
        self.assertEqual((write['kind'], write['state']), ('startup', 'sent'))

    def test_the_key_is_noted_first(self):
        fake = self.fresh()
        states = []
        real = fake._answer
        fake._answer = lambda args: (states.append(self.last()['state']) if args[:2] == ['pane', 'send-keys'] else None,
                                     real(args))[1]
        self.answer()
        self.assertEqual(states, ['pending'])

    def test_trust_and_freshness_are_the_callers_to_prove(self):
        self.fresh()
        self.refused(lambda: self.answer(trusted=False), 'not-trusted')
        self.refused(lambda: self.answer(fresh=False), 'not-trusted')
        self.assertEqual(self.fake.calls, [])

    def test_every_other_screen_or_pane_gets_no_key(self):
        cases = dict(other=dict(screen='Trust this folder?\n› 1. Trust and continue\n2. Quit\n3. More\n'),
                     blank=dict(screen=''), idle=dict(screen=IDLE), draft=dict(screen=DRAFT),
                     focused=dict(pane={'focused': True}), unknown_focus=dict(pane={'focused': None}),
                     live=dict(row={'state': 'live'}), owner=dict(row={'started_by': 'owner'}),
                     terminal=dict(pane={'terminal_id': 't2'}), kind=dict(kind='claude'),
                     scrolled=dict(pane={'scroll': {'offset_from_bottom': 1}}), unreadable=dict(screen='bad \ufffd'),
                     no_terminal=dict(row={'terminal_id': None}, pane={'terminal_id': None}))
        for name, over in cases.items():
            with self.subTest(name):
                self.fresh(**over)
                self.refused(self.answer)
                self.assertIsNone(self.last())
                self.reset()

    def test_a_row_that_moved_on_gets_no_key(self):
        self.fresh()
        ops_panes.update_agent(self.conn, self.row['id'], state='live')
        self.refused(self.answer, 'not-live')

    def test_a_key_that_stalled_is_never_pressed_again(self):
        fake = self.fresh()
        fake.hang = {('pane', 'send-keys')}
        self.assertEqual(self.answer()['state'], 'unverified')
        fake.hang = False
        fake.calls.clear()
        self.refused(self.answer, 'last-write-unverified')

    def test_a_shell_gets_no_key(self):
        fake = self.fresh()
        fake.processes = running()
        self.refused(self.answer, 'agent-not-foreground')

    def test_a_dialog_gone_on_the_last_read_gets_no_key(self):
        fake = self.fresh()
        real = ops_panes.note_write

        def noted(*args):
            write = real(*args)
            fake.screens[PANE] = DRAFT
            return write
        with mock.patch.object(ops_panes, 'note_write', noted):
            self.refused(self.answer)
        self.assertEqual(self.last()['state'], 'unsent')

    def test_herdr_closed_is_a_refusal(self):
        self.fresh().closed = True
        self.refused(self.answer, 'herdr-unavailable')


class ShellStart(Case):
    def fresh(self, screen='PS C:\\repos> ', *, row=None, processes=None, **stage):
        fake = self.stage(screen, row={'state': 'starting', 'session_id': None, **(row or {})}, **stage)
        fake.agents.clear()
        fake.processes = processes or {'shell_pid': 1, 'foreground_processes': [{'pid': 1, 'name': 'pwsh.exe'}]}
        return fake

    def start(self, command=COMMAND):
        return pane_send.start_in_shell(self.port, self.herdr, self.row, command)

    def test_a_command_runs_once_in_a_fresh_shell(self):
        self.fresh()
        write = self.start()
        self.assertEqual([c for c in self.fake.calls if c[:2] == ['pane', 'run']], [['pane', 'run', PANE, COMMAND]])
        self.assertEqual((write['kind'], write['state']), ('shell', 'sent'))

    def test_anything_but_a_lone_shell_gets_no_command(self):
        cases = dict(program={'processes': running('claude.exe')}, listed_none={'processes': running()},
                     unknown={'processes': {'shell_pid': 1}}, two={'processes': {'shell_pid': 1, 'foreground_processes': [
                         {'pid': 1, 'name': 'pwsh'}, {'pid': 3, 'name': 'dotnet'}]}},
                     focused={'pane': {'focused': True}}, live={'row': {'state': 'live'}},
                     owner={'row': {'started_by': 'owner'}}, terminal={'pane': {'terminal_id': 't2'}})
        for name, over in cases.items():
            with self.subTest(name):
                self.fresh(**over)
                self.refused(self.start)
                self.assertIsNone(self.last())
                self.reset()

    def test_a_row_that_moved_on_gets_no_command(self):
        self.fresh()
        ops_panes.update_agent(self.conn, self.row['id'], state='live')
        self.refused(self.start, 'not-live')

    def test_the_text_gate_holds_before_any_call(self):
        self.fresh()
        for text in ('-x', 'a\nb', 'a\x1b[201~', ''):
            self.refused(lambda: self.start(text), 'text-refused')
        self.assertEqual(self.fake.calls, [])

    def test_a_stalled_command_is_unverified_and_never_repeated(self):
        fake = self.fresh()
        fake.hang = {('pane', 'run')}
        self.assertEqual(self.start()['state'], 'unverified')
        fake.hang = False
        fake.calls.clear()
        self.refused(self.start, 'last-write-unverified')
