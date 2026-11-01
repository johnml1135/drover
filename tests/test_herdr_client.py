"""The one client that reaches Herdr: what it may send, and what a closed or refusing Herdr looks like."""
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from drover import config, herdr_client, placement_exec
from drover.herdr_client import Herdr, HerdrRefused, HerdrUnavailable
from tests.fake_herdr import FakeHerdr

ALLOWED = {
    'agent': ('get', 'list', 'read', 'prompt', 'send-keys', 'start', 'rename'),
    'pane': ('get', 'list', 'read', 'split', 'run', 'send-text', 'send-keys', 'close', 'process-info', 'report-agent',
             'report-agent-session', 'release-agent'),
    'tab': ('create', 'list'),
    'workspace': ('list',),
}


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


def client(fake=None, **kw):
    fake = fake or FakeHerdr()
    return Herdr('herdr', run=fake, **kw), fake


def replying(code, out='', err=''):
    return lambda argv, **kw: subprocess.CompletedProcess(argv, code, out, err)


class Allowlist(unittest.TestCase):
    def test_only_allowlisted_verbs_reach_the_binary(self):
        reached = []

        def run(argv, **kw):
            reached.append(tuple(argv[1:]))
            return subprocess.CompletedProcess(argv, 0, '', '')

        herdr = Herdr('herdr', run=run)
        sent = [(group, verb) for group, verbs in ALLOWED.items() for verb in verbs]
        for group, verb in sent:
            herdr.call(group, verb)
        self.assertEqual(reached, sent)
        self.assertEqual(herdr_client.ALLOWED, ALLOWED)
        reached.clear()
        for argv in (('agent', 'wait'), ('agent', 'stop'), ('pane', 'wait-output'), ('pane', 'move'), ('pane', 'current'),
                     ('tab', 'close'), ('tab', 'focus'), ('workspace', 'create'), ('workspace', 'delete'),
                     ('worktree', 'list'), ('terminal', 'list'), ('session', 'list'), ('notification', 'list'),
                     ('integration', 'list'), ('machine', 'list'), ('Agent', 'list'), ('agent', 'LIST')):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                herdr.call(*argv)
        self.assertEqual(reached, [])

    def test_bare_and_server_commands_are_refused(self):
        for enabled in (True, False):
            herdr, fake = client(enabled=enabled)
            for argv in ((), ('server',), ('server', 'stop'), ('server', 'start'), ('agent',), ('pane',), ('--help',),
                         ('--machine', 'box', 'agent', 'list'), ('--session', 's', 'pane', 'list'),
                         ('agent', '--help'), ('agent', 'get', 7), ('', '')):
                for send in (herdr.call, herdr.text):
                    with self.subTest(argv=argv), self.assertRaises(ValueError):
                        send(*argv)
            self.assertEqual(fake.calls, [])

    def test_refusal_of_a_verb_does_not_quote_its_arguments(self):
        herdr, _ = client()
        with self.assertRaises(ValueError) as caught:
            herdr.call('pane', 'wait-output', 'secret text')
        self.assertNotIn('secret text', str(caught.exception))


class Isolation(unittest.TestCase):
    def test_tests_cannot_reach_a_real_server(self):
        socket = Path(os.environ['HERDR_SOCKET_PATH'])
        self.assertFalse(socket.parent.exists())
        self.assertNotIn('HERDR_ENV', os.environ)
        herdr, fake = client()
        herdr.call('workspace', 'list')
        self.assertEqual(fake.kwargs[-1]['env']['HERDR_SOCKET_PATH'], str(socket))

    def test_configured_socket_is_passed_to_the_binary(self):
        herdr, fake = client(socket=r'\\.\pipe\herdr')
        herdr.call('workspace', 'list')
        self.assertEqual(fake.kwargs[-1]['env']['HERDR_SOCKET_PATH'], r'\\.\pipe\herdr')
        self.assertNotEqual(os.environ['HERDR_SOCKET_PATH'], r'\\.\pipe\herdr')

    def test_disabled_or_missing_binary_makes_no_call(self):
        fake = FakeHerdr()
        for herdr in (Herdr('herdr', enabled=False, run=fake), Herdr('', run=fake)):
            self.assertFalse(herdr.available())
            for send in (herdr.call, herdr.text):
                with self.assertRaises(HerdrUnavailable):
                    send('workspace', 'list')
        self.assertEqual(fake.calls, [])

    def test_no_window_on_windows(self):
        herdr, fake = client()
        with mock.patch.object(placement_exec, 'NO_WINDOW', {'creationflags': 0x08000000}):
            herdr.call('workspace', 'list')
        self.assertEqual(fake.kwargs[-1]['creationflags'], 0x08000000)
        self.assertIs(fake.kwargs[-1]['stdin'], subprocess.DEVNULL)

    def test_output_decodes_as_utf8(self):
        herdr, fake = client(FakeHerdr(screens={'w1:p1': 'caf\u00e9 \u2500 \u203a'.encode('utf-8') + b' \xff'}))
        self.assertEqual(herdr.text('pane', 'read', 'w1:p1'), 'caf\u00e9 \u2500 \u203a \ufffd')
        self.assertEqual((fake.kwargs[-1]['encoding'], fake.kwargs[-1]['errors']), ('utf-8', 'replace'))


class Replies(unittest.TestCase):
    def test_call_returns_the_result(self):
        herdr, _ = client(FakeHerdr(agents={'w1:p1': {'pane_id': 'w1:p1', 'agent_status': 'idle'}}))
        self.assertEqual(herdr.call('agent', 'get', 'w1:p1'), {'agent': {'pane_id': 'w1:p1', 'agent_status': 'idle'}})

    def test_empty_output_is_an_empty_result(self):
        herdr = Herdr('herdr', run=replying(0))
        self.assertEqual(herdr.call('pane', 'send-keys', 'w1:p1', 'enter'), {})

    def test_text_never_reads_a_screen_as_a_reply(self):
        screen = json.dumps({'error': {'code': 'agent_blocked'}})
        herdr, _ = client(FakeHerdr(screens={'w1:p1': screen}))
        self.assertEqual(herdr.text('pane', 'read', 'w1:p1'), screen)

    def test_timeout_raises_unavailable(self):
        herdr, fake = client(timeout_s=7.0)
        fake.hang = True
        with self.assertRaises(HerdrUnavailable) as caught:
            herdr.call('agent', 'prompt', 'w1:p1', 'private words')
        self.assertEqual(fake.kwargs[-1]['timeout'], 7.0)
        self.assertNotIn('private words', str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_a_binary_that_will_not_start_is_unavailable(self):
        def missing(argv, **kw):
            raise FileNotFoundError(2, 'not found', argv[0])
        with self.assertRaises(HerdrUnavailable):
            Herdr('herdr', run=missing).call('workspace', 'list')

    def test_refusal_keeps_its_code(self):
        herdr, fake = client()
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
                Herdr('herdr', run=run).text('pane', 'read', 'w1:p1') if 'exit 0' not in name \
                    else Herdr('herdr', run=run).call('pane', 'get', 'w1:p1')
            self.assertEqual(caught.exception.code, 'pane_not_found')

    def test_a_refusal_means_herdr_is_up(self):
        herdr, fake = client()
        fake.refuse[('pane', 'get')] = 'pane_not_found'
        with self.assertRaises(HerdrRefused):
            herdr.call('pane', 'get', 'w1:p1')
        self.assertTrue(herdr.available())
        self.assertEqual(len(fake.calls), 2)

    def test_a_stopped_server_is_unavailable_never_a_refusal(self):
        for code in ('server_not_running', 'server_unavailable'):
            herdr, fake = client()
            fake.refuse[('pane', 'get')] = code
            with self.subTest(code), self.assertRaises(HerdrUnavailable):
                herdr.call('pane', 'get', 'w1:p1')

    def test_a_reply_nothing_can_read_is_unavailable(self):
        for run in (replying(1, 'plain words'), replying(1, '', 'failed to connect to server'), replying(1),
                    replying(101, '', '{"unrelated": true}'), replying(0, 'not json'), replying(0, '[1, 2]'),
                    replying(0, '{"result": [1, 2]}')):
            with self.assertRaises(HerdrUnavailable):
                Herdr('herdr', run=run).call('pane', 'get', 'w1:p1')
        self.assertRaises(HerdrUnavailable, Herdr('herdr', run=replying(1, 'plain')).text, 'pane', 'read', 'w1:p1')


class Remembered(unittest.TestCase):
    def test_unavailable_is_remembered_for_the_retry_window(self):
        clock = Clock()
        herdr, fake = client(retry_s=60.0, clock=clock)
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
        herdr, fake = client(clock=Clock())
        fake.hang = True
        with self.assertRaises(HerdrUnavailable):
            herdr.call('workspace', 'list')
        with self.assertRaises(HerdrUnavailable):
            herdr.text('pane', 'read', 'w1:p1')
        self.assertEqual(len(fake.calls), 1)

    def test_a_refused_verb_is_refused_even_inside_the_window(self):
        herdr, fake = client(clock=Clock())
        fake.closed = True
        self.assertFalse(herdr.available())
        with self.assertRaises(ValueError):
            herdr.call('server', 'stop')

    def test_available_never_raises(self):
        herdr, fake = client(clock=Clock())
        self.assertTrue(herdr.available())
        self.assertEqual(fake.calls, [['workspace', 'list']])
        fake.closed = True
        herdr2, _ = client(fake, clock=Clock())
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
        fake = herdr.run = FakeHerdr()
        self.assertFalse(herdr.available())
        self.assertEqual(fake.calls, [])

    def test_panes_are_off_by_default(self):
        self.assertFalse(herdr_client.from_config().enabled)


if __name__ == '__main__':
    unittest.main()
