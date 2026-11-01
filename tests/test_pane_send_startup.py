"""The writes to a new pane: each runs once for a row, proves the pane is fresh itself, and never meets a typed line."""
from unittest import mock

from drover import pane_send
from drover.ledger import ops_panes
from tests.pane_send_support import Case, DRAFT, IDLE, PANE, running

TRUST = 'Trust this folder?\n› 1. Trust and continue\n2. Quit\n'
ANSWER = ['pane', 'send-keys', PANE, 'enter']
COMMAND = 'wsl.exe -d Ubuntu -e bash -li /home/x/launch.sh'
PROMPT = 'PS C:\\repos> '
LONE_SHELL = {'shell_pid': 1, 'foreground_processes': [{'pid': 1, 'name': 'pwsh.exe'}]}


class Base(Case):
    def fresh(self, screen=PROMPT, *, row=None, processes=None, **stage):
        fake = self.stage(screen, row={'state': 'starting', 'session_id': None, **(row or {})}, **stage)
        fake.agents.clear()
        fake.processes = processes or LONE_SHELL
        return fake

    def launched(self, state='sent'):
        write = ops_panes.note_write(self.conn, self.row['id'], 'shell', COMMAND)
        ops_panes.finish_write(self.conn, write['id'], state)


class Startup(Base):
    def trust(self, screen=TRUST, launched='sent', **stage):
        stage.setdefault('processes', running('wsl.exe'))
        fake = self.fresh(screen, **stage)
        if launched:
            self.launched(launched)
        return fake

    def answer(self, **over):
        return pane_send.answer_startup(self.port, self.herdr, self.row, **{'trusted': True, **over})

    def test_one_enter_goes_to_a_fresh_trust_dialog(self):
        self.trust()
        write = self.answer()
        self.assertEqual(self.sends(), [ANSWER])
        self.assertEqual((write['kind'], write['state']), ('startup', 'sent'))

    def test_the_key_is_noted_first(self):
        fake = self.trust()
        states = []
        real = fake._answer
        fake._answer = lambda args: (states.append(self.last()['state']) if args[:2] == ['pane', 'send-keys'] else None,
                                     real(args))[1]
        self.answer()
        self.assertEqual(states, ['pending'])

    def test_it_runs_once_for_a_row(self):
        fake = self.trust()
        self.answer()
        fake.calls.clear()
        self.refused(self.answer, 'already-started')

    def test_a_key_refused_before_it_went_may_be_tried_again(self):
        fake = self.trust()
        real = ops_panes.note_once

        def noted(*args):
            write = real(*args)
            fake.screens[PANE] = DRAFT
            return write
        with mock.patch.object(ops_panes, 'note_once', noted):
            self.refused(self.answer, 'not-the-trust-dialog')
        self.assertEqual(self.last()['state'], 'unsent')
        fake.screens[PANE] = TRUST
        self.assertEqual(self.answer()['state'], 'sent')

    def test_trust_is_the_callers_to_prove(self):
        self.trust()
        self.refused(lambda: self.answer(trusted=False), 'not-trusted')
        self.assertEqual(self.fake.calls, [])

    def test_a_caller_cannot_claim_fresh(self):
        self.trust()
        with self.assertRaises(TypeError):
            self.answer(fresh=True)

    def test_a_row_with_no_launch_on_record_is_not_fresh(self):
        self.trust(launched=None)
        self.refused(self.answer, 'not-fresh')
        self.assertEqual(self.fake.calls, [])
        self.reset()
        self.trust(launched='unsent')
        self.refused(self.answer, 'not-fresh')

    def test_every_other_screen_or_pane_gets_no_key(self):
        cases = dict(other=dict(screen='Trust this folder?\n› 1. Trust and continue\n2. Quit\n3. More\n'),
                     blank=dict(screen=''), idle=dict(screen=IDLE), draft=dict(screen=DRAFT),
                     focused=dict(pane={'focused': True}), unknown_focus=dict(pane={'focused': None}),
                     owner=dict(row={'started_by': 'owner'}), terminal=dict(pane={'terminal_id': 't2'}),
                     kind=dict(kind='claude'), scrolled=dict(pane={'scroll': {'offset_from_bottom': 1}}),
                     unreadable=dict(screen='bad \ufffd'), no_terminal=dict(row={'terminal_id': None}, pane={'terminal_id': None}))
        for name, over in cases.items():
            with self.subTest(name):
                self.trust(**over)
                self.refused(self.answer)
                self.assertEqual(self.last()['kind'], 'shell')
                self.reset()

    def test_a_row_that_moved_on_gets_no_key(self):
        self.trust()
        ops_panes.update_agent(self.conn, self.row['id'], state='live')
        self.refused(self.answer, 'not-live')

    def test_a_key_that_stalled_is_never_pressed_again(self):
        fake = self.trust()
        fake.hang = {('pane', 'send-keys')}
        self.assertEqual(self.answer()['state'], 'unverified')
        fake.hang = False
        fake.calls.clear()
        self.refused(self.answer, 'last-write-unverified')

    def test_a_shell_gets_no_key(self):
        self.trust(processes=LONE_SHELL)
        self.refused(self.answer, 'agent-not-foreground')

    def test_herdr_closed_is_a_refusal(self):
        self.trust().closed = True
        self.refused(self.answer, 'herdr-unavailable')


class Launch(Base):
    """`start_in_shell` and `start_agent` share one guard and one claim, so each case runs on both."""

    def run_one(self):
        return pane_send.start_in_shell(self.port, self.herdr, self.row, COMMAND)

    def agent(self):
        return pane_send.start_agent(self.port, self.herdr, self.row, 'director', ('--model', 'opus'))

    def ways(self):
        return (self.run_one, dict(placement='wsl')), (self.agent, dict(placement='windows', kind='claude'))

    def test_a_command_runs_once_in_a_fresh_shell(self):
        self.fresh()
        write = self.run_one()
        self.assertEqual(self.sends(), [['pane', 'run', PANE, COMMAND]])
        self.assertEqual((write['kind'], write['state']), ('shell', 'sent'))

    def test_an_agent_starts_once_by_explicit_pane(self):
        self.fresh(placement='windows', kind='claude')
        write = self.agent()
        self.assertEqual(self.sends(), [['agent', 'start', 'director', '--kind', 'claude', '--pane', PANE, '--',
                                         '--model', 'opus']])
        self.assertEqual((write['kind'], write['state']), ('start', 'sent'))

    def test_agent_start_is_for_a_windows_pane(self):
        self.fresh()
        self.refused(self.agent, 'not-windows')
        self.assertEqual(self.fake.calls, [])

    def test_a_bad_name_or_argument_makes_no_call(self):
        self.fresh(placement='windows', kind='claude')
        for name, args in (('-x', ()), ('a\nb', ()), ('', ()), ('director', ('a\x1b[201~',)), ('director', ('a\nb',)),
                           ('director', (7,)), ('director', 'not-a-list')):
            with self.subTest(name=name, args=args):
                self.refused(lambda: pane_send.start_agent(self.port, self.herdr, self.row, name, args), 'text-refused')
        self.assertEqual(self.fake.calls, [])

    def test_one_launch_for_a_row_either_way(self):
        for name, (act, stage) in zip(('run', 'agent'), self.ways()):
            with self.subTest(name):
                fake = self.fresh(**stage)
                act()
                fake.calls.clear()
                self.refused(act, 'already-started')
                self.reset()
        self.fresh(placement='windows', kind='claude')
        self.agent()
        self.fake.calls.clear()
        self.refused(self.run_one, 'already-started')

    def test_a_launch_that_stalled_stays_claimed(self):
        fake = self.fresh()
        fake.hang = {('pane', 'run')}
        self.assertEqual(self.run_one()['state'], 'unverified')
        ops_panes.finish_write(self.conn, self.last()['id'], 'observed')
        fake.hang = False
        fake.calls.clear()
        self.refused(self.run_one, 'already-started')

    def test_a_launch_refused_before_it_went_may_be_tried_again(self):
        fake = self.fresh()
        fake.refuse[('pane', 'run')] = 'pane_not_found'
        self.assertRaises(pane_send.PaneRefused, self.run_one)
        self.assertEqual(self.last()['state'], 'unsent')
        del fake.refuse[('pane', 'run')]
        self.assertEqual(self.run_one()['state'], 'sent')

    def test_an_unconfirmed_start_may_have_started_the_agent(self):
        for code in ('start_unconfirmed', 'agent_not_ready'):
            with self.subTest(code):
                fake = self.fresh(placement='windows', kind='claude')
                fake.refuse[('agent', 'start')] = code
                self.assertEqual(self.agent()['state'], 'unverified')
                del fake.refuse[('agent', 'start')]
                fake.calls.clear()
                self.refused(self.agent, 'last-write-unverified')
                self.reset()

    def test_the_claim_is_one_transaction(self):
        self.fresh()
        first = ops_panes.note_once(self.conn, self.row['id'], ('shell', 'start'), 'shell', COMMAND)
        self.assertEqual(first['state'], 'pending')
        self.assertIsNone(ops_panes.note_once(self.conn, self.row['id'], ('shell', 'start'), 'start', COMMAND))
        ops_panes.finish_write(self.conn, first['id'], 'unsent')
        self.assertIsNotNone(ops_panes.note_once(self.conn, self.row['id'], ('shell', 'start'), 'start', COMMAND))

    def test_only_an_empty_shell_line_is_written_to(self):
        lines = dict(typed='PS C:\\repos> git push --force origin ', typed_cmd='C:\\repos>dir', typed_bash='x@h:~$ rm -rf',
                     cursor_only='', unknown='Welcome to something', agent='❯ ', codex='› ', after='PS C:\\repos>\nmore output')
        for way, (act, stage) in zip(('run', 'agent'), self.ways()):
            for name, screen in lines.items():
                with self.subTest(way=way, line=name):
                    self.fresh(screen, **stage)
                    self.refused(act)
                    self.assertIsNone(self.last())
                    self.reset()

    def test_a_wrapped_draft_whose_tail_looks_like_a_prompt_is_refused(self):
        wrapped = dict(percent='PS C:\\repos> git commit -m "raise the limit to\n100%',
                       path_tail='PS C:\\repos> type\nC:\\logs\\a.txt >',
                       bash_tail='x@h:~$ echo long words here\nabc$',
                       history_then_tail='ok\nPS C:\\repos> git log --grep\n#')
        for way, (act, stage) in zip(('run', 'agent'), self.ways()):
            for name, screen in wrapped.items():
                with self.subTest(way=way, line=name):
                    self.fresh(screen, **stage)
                    self.refused(act, 'shell-line-not-empty')
                    self.assertIsNone(self.last())
                    self.reset()

    def test_each_empty_prompt_passes(self):
        for screen in (PROMPT, 'history\nPS C:\\my repos\\x>', 'C:\\repos>', 'user@host:~/x$ ', 'history\n\n$ \n\n\n'):
            with self.subTest(screen):
                self.fresh(screen)
                self.assertEqual(self.run_one()['state'], 'sent')
                self.reset()

    def test_the_line_is_read_again_after_the_note(self):
        fake = self.fresh()
        real = ops_panes.note_once

        def noted(*args):
            write = real(*args)
            fake.screens[PANE] = PROMPT + 'git status'
            return write
        with mock.patch.object(ops_panes, 'note_once', noted):
            self.refused(self.run_one, 'shell-line-not-empty')
        self.assertEqual(self.last()['state'], 'unsent')

    def test_anything_but_a_lone_shell_gets_no_command(self):
        cases = dict(program={'processes': running('claude.exe')}, listed_none={'processes': running()},
                     unknown={'processes': {'shell_pid': 1}}, two={'processes': {'shell_pid': 1, 'foreground_processes': [
                         {'pid': 1, 'name': 'pwsh'}, {'pid': 3, 'name': 'dotnet'}]}},
                     focused={'pane': {'focused': True}}, live={'row': {'state': 'live'}},
                     owner={'row': {'started_by': 'owner'}}, terminal={'pane': {'terminal_id': 't2'}})
        for name, over in cases.items():
            with self.subTest(name):
                self.fresh(**over)
                self.refused(self.run_one)
                self.assertIsNone(self.last())
                self.reset()

    def test_a_row_that_moved_on_gets_no_command(self):
        for act, stage in self.ways():
            self.fresh(**stage)
            ops_panes.update_agent(self.conn, self.row['id'], state='live')
            self.refused(act, 'not-live')
            self.reset()

    def test_the_text_gate_holds_before_any_call(self):
        self.fresh()
        for text in ('-x', 'a\nb', 'a\x1b[201~', ''):
            self.refused(lambda: pane_send.start_in_shell(self.port, self.herdr, self.row, text), 'text-refused')
        self.assertEqual(self.fake.calls, [])

    def test_a_stalled_command_is_unverified_and_never_repeated(self):
        fake = self.fresh()
        fake.hang = {('pane', 'run')}
        self.assertEqual(self.run_one()['state'], 'unverified')
        fake.hang = False
        fake.calls.clear()
        self.refused(self.run_one, 'last-write-unverified')

    def test_an_open_write_stops_a_launch(self):
        self.fresh(placement='windows', kind='claude')
        ops_panes.note_write(self.conn, self.row['id'], 'wake', 'x')
        self.refused(self.agent, 'last-write-pending')
        self.assertEqual(self.fake.calls, [])
