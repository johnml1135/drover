"""The one client that reaches Herdr: what it may send, and what a closed or refusing Herdr looks like."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from drover import config, herdr_client, process
from drover.herdr_client import Herdr, HerdrRefused, HerdrUnavailable
from tests.fake_herdr import FakeHerdr
from tests.fakeci import FakeCommands, answer_spawns, process_options, refuse_real_spawns

ALLOWED = {
    'agent': ('get', 'list', 'read', 'prompt', 'send-keys', 'start', 'rename'),
    'pane': ('get', 'list', 'read', 'run', 'send-text', 'send-keys', 'close', 'process-info', 'report-agent',
             'report-agent-session', 'release-agent'),
    'tab': ('create', 'list'),
    'workspace': ('list',),
}


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


class Case(unittest.TestCase):
    def client(self, fake=None, **kw):
        """A client and the FakeHerdr every command of this test reaches."""
        fake = (fake or FakeHerdr()).serve(self)
        return Herdr('herdr', **kw), fake

    def herdr(self, run, **kw):
        """A client whose commands `run` answers from here on, given process's options."""
        answer_spawns(self, run)
        return Herdr('herdr', **kw)


def replying(code, out='', err=''):
    return lambda argv, **kw: subprocess.CompletedProcess(argv, code, out, err)


class Allowlist(Case):
    def test_only_allowlisted_verbs_reach_the_binary(self):
        reached = []

        def run(argv, **kw):
            reached.append(tuple(argv[1:]))
            return subprocess.CompletedProcess(argv, 0, '', '')

        herdr = self.herdr(run)
        sent = [(group, verb) for group, verbs in ALLOWED.items() for verb in verbs]
        for group, verb in sent:
            herdr.call(group, verb)
        self.assertEqual(reached, sent)
        self.assertEqual(herdr_client.ALLOWED, ALLOWED)
        reached.clear()
        for argv in (('pane', 'split'), ('pane', 'split', 'w1:p1'), ('agent', 'wait'), ('agent', 'stop'), ('pane', 'wait-output'), ('pane', 'move'), ('pane', 'current'),
                     ('tab', 'close'), ('tab', 'focus'), ('workspace', 'create'), ('workspace', 'delete'),
                     ('worktree', 'list'), ('terminal', 'list'), ('session', 'list'), ('notification', 'list'),
                     ('integration', 'list'), ('machine', 'list'), ('Agent', 'list'), ('agent', 'LIST')):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                herdr.call(*argv)
        self.assertEqual(reached, [])

    def test_bare_and_server_commands_are_refused(self):
        for enabled in (True, False):
            herdr, fake = self.client(enabled=enabled)
            for argv in ((), ('server',), ('server', 'stop'), ('server', 'start'), ('agent',), ('pane',), ('--help',),
                         ('--machine', 'box', 'agent', 'list'), ('--session', 's', 'pane', 'list'),
                         ('agent', '--help'), ('agent', 'get', 7), ('', ''), ('pane', 'list', '--machine', 'box'),
                         ('agent', 'list', '--session', 's'), ('pane', 'get', 'w1:p1', '--remote'),
                         ('agent', 'prompt', 'w1:p1', 'a\0b')):
                for send in (herdr.call, herdr.text):
                    with self.subTest(argv=argv), self.assertRaises(ValueError):
                        send(*argv)
            self.assertEqual(fake.calls, [])

    def test_native_agent_arguments_may_use_a_remote_option_name(self):
        herdr, fake = self.client()
        herdr.call('agent', 'start', 'dir', '--pane', 'w1:p1', '--', '--session', 'x')
        self.assertEqual(fake.calls[-1][-2:], ['--session', 'x'])

    def test_refusal_of_a_verb_does_not_quote_its_arguments(self):
        herdr, _ = self.client()
        for argv in (('pane', 'wait-output', 'secret text'), ('pane', 'secret text'), ('secret text', 'list')):
            with self.assertRaises(ValueError) as caught:
                herdr.call(*argv)
            self.assertNotIn('secret', str(caught.exception))


class Isolation(Case):
    def test_tests_cannot_reach_a_real_server(self):
        socket = Path(os.environ['HERDR_SOCKET_PATH'])
        self.assertFalse(socket.parent.exists())
        self.assertEqual([name for name in os.environ if name.startswith('HERDR_')], ['HERDR_SOCKET_PATH'])
        herdr, fake = self.client()
        herdr.call('workspace', 'list')
        self.assertEqual(fake.kwargs[-1]['env']['HERDR_SOCKET_PATH'], str(socket))

    def test_configured_socket_is_passed_to_the_binary(self):
        herdr, fake = self.client(socket=r'\\.\pipe\herdr')
        herdr.call('workspace', 'list')
        self.assertEqual(fake.kwargs[-1]['env']['HERDR_SOCKET_PATH'], r'\\.\pipe\herdr')
        self.assertNotEqual(os.environ['HERDR_SOCKET_PATH'], r'\\.\pipe\herdr')

    def test_disabled_or_missing_binary_makes_no_call(self):
        fake = FakeHerdr().serve(self)
        for herdr in (Herdr('herdr', enabled=False), Herdr('')):
            self.assertFalse(herdr.available())
            for send in (herdr.call, herdr.text):
                with self.assertRaises(HerdrUnavailable):
                    send('workspace', 'list')
        self.assertEqual(fake.calls, [])

    def test_no_window_on_windows(self):
        """Herdr runs through process, which gives every command the console flag and no stdin."""
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(process, 'NO_WINDOW', {'creationflags': 0x08000000}):
            fake = FakeCommands(root)
            fake.expect(['herdr', 'workspace', 'list'], output=json.dumps({'result': {'workspaces': []}}),
                        **process_options(10.0, env=dict(os.environ)))
            with refuse_real_spawns(), fake.patch(process):
                self.assertEqual(Herdr('herdr').call('workspace', 'list'), {'workspaces': []})
        self.assertEqual(fake.calls[0][1]['creationflags'], 0x08000000)
        self.assertIs(fake.calls[0][1]['stdin'], subprocess.DEVNULL)

    def test_output_decodes_as_utf8(self):
        """UTF-8 with `replace`, and newlines made LF, as text mode read it."""
        herdr, _ = self.client(FakeHerdr(screens={'w1:p1': 'caf\u00e9 \u2500\r\n\u203a'.encode('utf-8') + b' \xff'}))
        self.assertEqual(herdr.text('pane', 'read', 'w1:p1'), 'caf\u00e9 \u2500\n\u203a \ufffd')


class Replies(Case):
    def test_call_returns_the_result(self):
        herdr, _ = self.client(FakeHerdr(agents={'w1:p1': {'pane_id': 'w1:p1', 'agent_status': 'idle'}}))
        self.assertEqual(herdr.call('agent', 'get', 'w1:p1'), {'agent': {'pane_id': 'w1:p1', 'agent_status': 'idle'}})

    def test_empty_output_is_an_empty_result(self):
        herdr = self.herdr(replying(0))
        self.assertEqual(herdr.call('pane', 'send-keys', 'w1:p1', 'enter'), {})

    def test_text_never_reads_a_screen_as_a_reply(self):
        screen = json.dumps({'error': {'code': 'agent_blocked'}})
        herdr, _ = self.client(FakeHerdr(screens={'w1:p1': screen}))
        self.assertEqual(herdr.text('pane', 'read', 'w1:p1'), screen)

    def test_timeout_raises_unavailable(self):
        herdr, fake = self.client(timeout_s=7.0)
        fake.hang = True
        with self.assertRaises(HerdrUnavailable) as caught:
            herdr.call('agent', 'prompt', 'w1:p1', 'private words')
        self.assertEqual(fake.kwargs[-1]['timeout'], 7.0)
        self.assertNotIn('private words', str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)          # nothing quoting the command rides along

    def test_a_binary_that_will_not_start_is_unavailable(self):
        def missing(argv, **kw):
            raise FileNotFoundError(2, 'not found', argv[0])
        with self.assertRaises(HerdrUnavailable):
            self.herdr(missing).call('workspace', 'list')

    def test_refusal_keeps_its_code(self):
        herdr, fake = self.client()
        fake.refuse[('agent', 'prompt')] = 'agent_blocked'
        with self.assertRaises(HerdrRefused) as caught:
            herdr.call('agent', 'prompt', 'w1:p1', 'private words')
        self.assertEqual(caught.exception.code, 'agent_blocked')
        self.assertNotIn('private words', str(caught.exception))

    def test_refusal_is_read_however_herdr_delivers_it(self):
        shapes = {'on stdout': replying(1, json.dumps({'error': {'code': 'pane_not_found', 'message': 'no pane'}})),
                  'on stderr': replying(1, '', json.dumps({'error': {'code': 'pane_not_found'}})),
                  'with exit 0': replying(0, json.dumps({'error': {'code': 'pane_not_found'}})),
                  'as a string': replying(1, json.dumps({'error': 'pane_not_found'}))}
        for name, run in shapes.items():
            with self.subTest(name), self.assertRaises(HerdrRefused) as caught:
                self.herdr(run).text('pane', 'read', 'w1:p1') if 'exit 0' not in name \
                    else self.herdr(run).call('pane', 'get', 'w1:p1')
            self.assertEqual(caught.exception.code, 'pane_not_found')

    def test_a_refusal_means_herdr_is_up(self):
        herdr, fake = self.client()
        fake.refuse[('pane', 'get')] = 'pane_not_found'
        with self.assertRaises(HerdrRefused):
            herdr.call('pane', 'get', 'w1:p1')
        self.assertTrue(herdr.available())
        self.assertEqual(len(fake.calls), 2)

    def test_a_stopped_server_is_unavailable_never_a_refusal(self):
        for code in ('server_not_running', 'server_unavailable', 'protocol_mismatch'):
            herdr, fake = self.client()
            fake.refuse[('pane', 'get')] = code
            with self.subTest(code), self.assertRaises(HerdrUnavailable):
                herdr.call('pane', 'get', 'w1:p1')

    def test_a_reply_nothing_can_read_is_unavailable(self):
        for run in (replying(1, 'plain words'), replying(1, '', 'failed to connect to server'), replying(1),
                    replying(101, '', '{"unrelated": true}'), replying(0, 'not json'), replying(0, '[1, 2]'),
                    replying(0, '{"result": [1, 2]}')):
            with self.assertRaises(HerdrUnavailable):
                self.herdr(run).call('pane', 'get', 'w1:p1')
        self.assertRaises(HerdrUnavailable, self.herdr(replying(1, 'plain')).text, 'pane', 'read', 'w1:p1')


class SlowStart(Case):
    ARGV = ('agent', 'start', 'dir', '--kind', 'claude', '--pane', 'w1:p1')

    def test_agent_start_outlasts_herdrs_own_30_seconds(self):
        herdr, fake = self.client(timeout_s=10.0)
        herdr.call(*self.ARGV)
        self.assertGreater(fake.kwargs[-1]['timeout'], 30.0)
        herdr.call('workspace', 'list')
        self.assertEqual(fake.kwargs[-1]['timeout'], 10.0)

    def test_a_longer_configured_timeout_also_covers_the_start(self):
        herdr, fake = self.client(timeout_s=90.0)
        herdr.call(*self.ARGV)
        self.assertEqual(fake.kwargs[-1]['timeout'], 90.0)

    def test_a_slow_start_succeeds_and_leaves_herdr_available(self):
        clock = Clock()
        fake = FakeHerdr()

        def twelve_seconds(argv, **kw):
            clock.now += 12.0
            return fake(argv, **kw)

        herdr = self.herdr(twelve_seconds, clock=clock)
        self.assertEqual(herdr.call(*self.ARGV), {})
        self.assertTrue(herdr.available())

    def test_a_start_past_its_budget_is_not_read_as_nothing_happened(self):
        fake = FakeHerdr(agents={'w1:p1': {'pane_id': 'w1:p1', 'agent_status': 'working'}})
        herdr, _ = self.client(fake, clock=Clock())
        fake.hang = {('agent', 'start')}
        with self.assertRaises(HerdrRefused) as caught:
            herdr.call(*self.ARGV)
        self.assertNotIsInstance(caught.exception, HerdrUnavailable)
        self.assertEqual(caught.exception.code, 'start_unconfirmed')
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(herdr.call('agent', 'get', 'w1:p1')['agent']['agent_status'], 'working')
        self.assertEqual([call[:2] for call in fake.calls].count(['agent', 'start']), 1)


class PlainTextFailures(Case):
    def test_a_usage_error_is_a_refusal_that_opens_no_window(self):
        for said in ('usage: herdr agent prompt <target> <text>', 'unknown option: --x', 'unexpected argument: y',
                     'missing value for --pane', '\n  usage: herdr pane run'):
            calls = []

            def run(argv, said=said, **kw):
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 2, '', said)

            herdr = self.herdr(run, clock=Clock())
            for _ in range(2):
                with self.subTest(said), self.assertRaises(HerdrRefused) as caught:
                    herdr.call('agent', 'prompt', 'w1:p1', 'words')
                self.assertEqual(caught.exception.code, 'usage')
            self.assertEqual(len(calls), 2)

    def test_a_usage_error_says_nothing_about_the_server(self):
        self.assertFalse(self.herdr(replying(2, '', 'usage: herdr workspace list')).available())

    def test_a_failed_connection_is_still_unavailable_and_remembered(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 1, '', 'failed to connect to server: refused')

        herdr = self.herdr(run, clock=Clock())
        for _ in range(2):
            with self.assertRaises(HerdrUnavailable):
                herdr.call('workspace', 'list')
        self.assertEqual(len(calls), 1)

    def test_errors_never_quote_what_herdr_echoed(self):
        echo = '- Read the secret note first'
        for run in (replying(2, '', 'unknown option: ' + echo), replying(1, '', 'failed to connect: ' + echo),
                    replying(1, '', echo), replying(1, echo),
                    replying(1, json.dumps({'error': {'code': 'agent_blocked', 'message': echo}})),
                    replying(1, json.dumps({'error': {'code': 'server_unavailable', 'message': echo}})),
                    replying(1, json.dumps({'error': {'code': echo}})), replying(1, json.dumps({'error': echo}))):
            with self.assertRaises((HerdrRefused, HerdrUnavailable)) as caught:
                self.herdr(run).call('agent', 'prompt', 'w1:p1', echo)
            self.assertNotIn('secret', str(caught.exception))
            self.assertNotIn('secret', getattr(caught.exception, 'code', ''))

    def test_the_length_of_the_error_output_is_kept(self):
        with self.assertRaises(HerdrRefused) as caught:
            self.herdr(replying(2, '', 'unknown option: abc')).call('workspace', 'list')
        self.assertIn('19 characters', str(caught.exception))

    def test_a_clean_exit_that_cannot_be_read_opens_no_window(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, 'sent', '')

        herdr = self.herdr(run, clock=Clock())
        for _ in range(2):
            with self.assertRaises(HerdrUnavailable):
                herdr.call('pane', 'send-keys', 'w1:p1', 'enter')
        self.assertEqual(len(calls), 2)


class Remembered(Case):
    def test_unavailable_is_remembered_for_the_retry_window(self):
        clock = Clock()
        herdr, fake = self.client(retry_s=60.0, clock=clock)
        fake.closed = True
        with self.assertRaises(HerdrUnavailable):
            herdr.call('workspace', 'list')
        self.assertEqual(len(fake.calls), 1)
        fake.closed = False
        clock.now += 59.9
        with self.assertRaises(HerdrUnavailable):
            herdr.call('workspace', 'list')
        self.assertFalse(herdr.available())
        self.assertEqual(len(fake.calls), 1)
        clock.now += 0.1
        self.assertEqual(herdr.call('workspace', 'list')['workspaces'][0]['workspace_id'], 'w1')
        self.assertEqual(len(fake.calls), 2)

    def test_a_timeout_is_remembered_too(self):
        herdr, fake = self.client(clock=Clock())
        fake.hang = True
        with self.assertRaises(HerdrUnavailable):
            herdr.call('workspace', 'list')
        with self.assertRaises(HerdrUnavailable):
            herdr.text('pane', 'read', 'w1:p1')
        self.assertEqual(len(fake.calls), 1)

    def test_a_refused_verb_is_refused_even_inside_the_window(self):
        herdr, fake = self.client(clock=Clock())
        fake.closed = True
        self.assertFalse(herdr.available())
        with self.assertRaises(ValueError):
            herdr.call('server', 'stop')

    def test_available_never_raises(self):
        herdr, fake = self.client(clock=Clock())
        self.assertTrue(herdr.available())
        self.assertEqual(fake.calls, [['workspace', 'list']])
        fake.closed = True
        herdr2, _ = self.client(fake, clock=Clock())
        self.assertFalse(herdr2.available())


class FromConfig(unittest.TestCase):
    def cfg(self, **panes):
        return {'panes': {**config.DEFAULTS['panes'], **panes}}

    def test_config_sets_the_client(self):
        cfg = self.cfg(enabled=True, herdr_bin=r'C:\herdr\herdr.exe', socket='s', call_timeout_s=3.0, retry_unavailable_s=9.0)
        with mock.patch.object(herdr_client.shutil, 'which', side_effect=lambda name: name) as which:
            herdr = herdr_client.from_config(cfg)
        which.assert_called_once_with(r'C:\herdr\herdr.exe')
        self.assertEqual((herdr.binary, herdr.socket, herdr.enabled, herdr.timeout_s, herdr.retry_s),
                         (r'C:\herdr\herdr.exe', 's', True, 3.0, 9.0))

    def test_empty_herdr_bin_looks_on_the_path(self):
        with mock.patch.object(herdr_client.shutil, 'which', return_value=r'C:\found\herdr.exe') as which:
            herdr = herdr_client.from_config(self.cfg(enabled=True))
        which.assert_called_once_with('herdr')
        self.assertEqual(herdr.binary, r'C:\found\herdr.exe')

    def test_unresolved_binary_makes_no_call(self):
        with mock.patch.object(herdr_client.shutil, 'which', return_value=None):
            herdr = herdr_client.from_config(self.cfg(enabled=True))
        fake = FakeHerdr().serve(self)
        self.assertFalse(herdr.available())
        self.assertEqual(fake.calls, [])

    def test_panes_are_off_by_default(self):
        self.assertFalse(herdr_client.from_config().enabled)


if __name__ == '__main__':
    unittest.main()
