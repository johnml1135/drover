"""What may reach a pane at all: the text gate, and which modules hold the write verbs."""
import ast
from unittest import mock

from drover import pane_screen, pane_send
from drover.pane_send import PaneRefused
from tests.pane_send_support import Case, ENTER, PASTE, ROOT, TEXT, composing, sources


class Text(Case):
    def test_text_that_could_be_a_flag_or_break_the_paste_is_refused_before_any_call(self):
        self.stage()
        for text in ('-x', '--wait', ' --wait', '', '   ', None, b'bytes', 'a\nb', 'a\rb', 'a\0b', 'a\x1b[201~enter',
                     'a\tb', 'a\u2028b', 'a\u200bb', 'a\ufffdb'):
            with self.subTest(text=text):
                self.refused(lambda: self.send(text), 'text-refused')
                self.refused(lambda: self.stop(text), 'text-refused')
        self.assertEqual(self.fake.calls, [])
        self.assertIsNone(self.last())

    def test_a_kind_outside_the_list_is_refused_before_any_call(self):
        self.stage()
        self.refused(lambda: self.send(TEXT, 'chat'), 'kind-refused')
        self.refused(lambda: pane_send.interrupt(self.port, self.herdr, self.row, 'chat'), 'kind-refused')
        self.assertEqual(self.fake.calls, [])

    def test_long_text_is_refused_and_a_path_pointer_passes(self):
        self.stage()
        self.refused(lambda: self.send('x' * (pane_send.MAX_TEXT + 1)), 'text-too-long')
        self.assertEqual(self.fake.calls, [])
        pointer = 'Read H:/state/panes/7/brief.md and follow it.'
        self.paste_settles_to(composing(pointer))
        self.assertEqual(self.send(pointer, 'brief')['state'], 'sent')

    def test_a_pane_id_that_could_be_a_flag_makes_no_call(self):
        for pane in ('--current', 'w1\0p1'):
            self.stage(row={'pane_id': pane})
            self.refused(self.send, 'not-drovers')
            self.reset()
        self.assertEqual(self.fake.calls, [])


def constants(path):
    return [node.value for node in ast.walk(ast.parse(path.read_text(encoding='utf-8')))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)]


def pairs(tree):
    """Every (group, verb) of adjacent string constants in a call or a tuple or list, so the shape is found, not a word."""
    for node in ast.walk(tree):
        items = node.args if isinstance(node, ast.Call) else node.elts if isinstance(node, (ast.Tuple, ast.List)) else ()
        words = [item.value if isinstance(item, ast.Constant) else None for item in items]
        yield from zip(words, words[1:])


class OnlyThisModule(Case):
    def test_only_pane_send_reaches_the_write_verbs(self):
        reached = set()
        for path in sources():
            found = set(pairs(ast.parse(path.read_text(encoding='utf-8'))))
            if found & {('agent', 'prompt'), ('agent', 'send-keys'), ('pane', 'send-text'), ('pane', 'send-keys'),
                        ('pane', 'run'), ('agent', 'start')} or {'send-text', 'send-keys'} & set(constants(path)):
                reached.add(path.relative_to(ROOT).as_posix())
        self.assertEqual(reached - {'drover/herdr_client.py'}, {'drover/pane_send.py'})

    def test_the_scan_sees_each_shape(self):
        """A tripwire against accident, not a proof: a verb passed through a name gets past it."""
        for source in ("h.call('pane', 'run', p, t)", "argv = ('pane', 'send-keys', p, 'enter')", "['agent', 'prompt', p]",
                       "h.call('agent', 'start', n)"):
            with self.subTest(source):
                self.assertTrue(set(pairs(ast.parse(source))) & {('pane', 'run'), ('pane', 'send-keys'), ('agent', 'prompt'),
                                                                 ('agent', 'start')})

    def test_the_client_itself_writes_nothing(self):
        tree = ast.parse((ROOT / 'drover/herdr_client.py').read_text(encoding='utf-8'))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and
                 any(isinstance(arg, ast.Constant) and arg.value in ('send-text', 'send-keys', 'prompt') for arg in node.args)]
        self.assertEqual(calls, [])


class AfterTheWrite(Case):
    def test_a_refused_write_leaves_it_unsent(self):
        fake = self.windows()
        fake.refuse[('agent', 'prompt')] = 'agent_blocked'
        with self.assertRaises(PaneRefused) as caught:
            self.send()
        self.assertEqual(caught.exception.reason, 'herdr-refused:agent_blocked')
        self.assertEqual((self.last()['state'], len(self.sends())), ('unsent', 1))
        self.assertEqual([c for c in self.fake.calls if c[:2] == ['pane', 'send-text']], [])

    def test_a_failed_read_after_the_paste_gets_no_enter(self):
        fake = self.stage()
        self.sleep.side_effect = lambda seconds: setattr(fake, 'closed', True)
        write = self.send()
        self.assertEqual((write['state'], write['reason']), ('unverified', 'herdr-unavailable'))
        self.assertEqual(self.sends(), [PASTE])

    def test_the_enter_is_on_record_before_it_is_pressed(self):
        fake = self.stage()
        self.paste_settles_to(composing(TEXT))
        seen = []
        real = fake._answer

        def answer(args):
            if args[:2] == ['pane', 'send-keys']:
                seen.append(self.last()['reason'])
            return real(args)
        fake._answer = answer
        self.send()
        self.assertEqual(seen, ['enter-pressed'])

    def test_a_failed_enter_is_never_pressed_again(self):
        fake = self.stage()
        self.paste_settles_to(composing(TEXT))
        fake.hang = {('pane', 'send-keys')}
        write = self.send()
        self.assertEqual((write['state'], write['reason']), ('unverified', 'enter-pressed'))
        fake.hang = False
        with self.assertRaises(PaneRefused):
            pane_send.submit_own(self.port, self.herdr, self.row)
        self.assertEqual(self.sends().count(ENTER), 1)

    def test_a_screen_status_of_working_or_blocked_gets_no_enter(self):
        for status in ('working', 'blocked'):
            with self.subTest(status):
                self.stage()
                self.paste_settles_to(composing(TEXT))
                with mock.patch.object(pane_screen, 'status', side_effect=['idle', 'idle', 'idle', status]):
                    self.assertEqual(self.send()['reason'], 'busy')
                self.assertNotIn(ENTER, self.sends())
                self.reset()

    def test_a_working_row_alone_gets_no_enter(self):
        self.stage()
        self.paste_settles_to(composing(TEXT).replace('history', '• Working (2s)'))
        with mock.patch.object(pane_screen, 'status', side_effect=['idle', 'idle', 'idle', None]):
            self.assertEqual(self.send()['reason'], 'busy')
        self.assertNotIn(ENTER, self.sends())

    def test_a_stalled_interrupt_never_makes_the_composer_ours(self):
        fake = self.stage()
        fake.hang = {('pane', 'send-keys')}
        pane_send.interrupt(self.port, self.herdr, self.row)
        fake.hang = False
        fake.screens[list(fake.screens)[0]] = composing('ctrl+c')
        fake.calls.clear()
        self.refused(lambda: pane_send.submit_own(self.port, self.herdr, self.row), 'nothing-unsent')
