"""What may reach a pane at all: the text gate, and which modules hold the write verbs."""
import ast

from drover import pane_send
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
            self.conn.execute('DELETE FROM pane_agents')
        self.assertEqual(self.fake.calls, [])


def constants(path):
    return [node.value for node in ast.walk(ast.parse(path.read_text(encoding='utf-8')))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)]


class OnlyThisModule(Case):
    def test_only_pane_send_reaches_the_write_verbs(self):
        reached = set()
        for path in sources():
            words = constants(path)
            if 'send-text' in words or 'send-keys' in words or 'agent' in words and 'prompt' in words:
                reached.add(path.relative_to(ROOT).as_posix())
        self.assertEqual(reached, {'drover/herdr_client.py', 'drover/pane_send.py'})

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
