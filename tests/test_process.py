"""One short-lived command: a required timeout, no shell, the console flag, and one outcome that raises nothing."""
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from drover.process import Command, run
from drover import process
from tests.fakeci import FakeCommands, process_options

ARGV = ('tool', '--flag', 'value')


class Clock:
    """`process.time` whose perf_counter answers the start, then the end, of one command."""
    def __init__(self, *readings):
        self.readings = list(readings)

    def perf_counter(self):
        return self.readings.pop(0)


class Run(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.fake = FakeCommands(temp.name)

    def ran(self, command=None, *, clock=(0.0, 1.0), **answer):
        command = command or Command(ARGV, 10)
        options = process_options(command.timeout_s, cwd=command.cwd, env=command.env,
                                  input=command.stdin.encode() if isinstance(command.stdin, str) else command.stdin)
        self.fake.expect(command.argv, **answer, **options)
        with self.fake.patch(process), mock.patch.object(process, 'time', Clock(*clock)):
            return run(command)

    def test_exit_is_kept_with_bytes(self):
        r = self.ran(returncode=3, output=b'x', stderr_output=b'why')
        self.assertEqual((r.kind, r.code, r.out, r.err, r.elapsed_s, r.detail), ('exited', 3, b'x', b'why', 1.0, ''))

    def test_ok_only_for_exit_zero(self):
        self.assertTrue(self.ran(returncode=0).ok)
        self.assertFalse(self.ran(returncode=1).ok)
        timed_out = self.ran(clock=(0.0, 10.0), returncode=0)
        self.assertEqual((timed_out.kind, timed_out.ok), ('timed_out', False))
        self.assertFalse(self.ran(error=FileNotFoundError(2, 'missing')).ok)

    def test_stdout_decodes_utf8_with_replace(self):
        r = self.ran(output='é'.encode('utf-8') + b'\xff', stderr_output=b'\xfe!')
        self.assertEqual((r.stdout, r.stderr), ('é\ufffd', '\ufffd!'))

    def test_out_keeps_bytes_a_locale_site_decodes_itself(self):
        r = self.ran(output='é'.encode('cp1252'))
        self.assertEqual(r.out, 'é'.encode('cp1252'))
        self.assertEqual(r.out.decode('cp1252'), 'é')

    def test_timeout_is_timed_out_and_keeps_partial_output(self):
        error = subprocess.TimeoutExpired(list(ARGV), 10, output=b'part', stderr=b'err')
        r = self.ran(clock=(0.0, 10.5), error=error)
        self.assertEqual((r.kind, r.code, r.out, r.err, r.elapsed_s), ('timed_out', None, b'part', b'err', 10.5))
        self.assertIn('10', r.detail)
        r = self.ran(error=subprocess.TimeoutExpired(list(ARGV), 10))
        self.assertEqual((r.kind, r.out, r.err), ('timed_out', b'', b''))

    def test_elapsed_at_the_timeout_is_timed_out_with_output_kept(self):
        r = self.ran(clock=(5.0, 15.0), returncode=0, output=b'late', stderr_output=b'e')
        self.assertEqual((r.kind, r.code, r.out, r.err, r.elapsed_s), ('timed_out', None, b'late', b'e', 10.0))
        self.assertTrue(r.detail)
        self.assertEqual(self.ran(clock=(5.0, 14.99), returncode=0).kind, 'exited')

    def test_missing_executable_and_refused_spawn_are_unavailable(self):
        for error in (FileNotFoundError(2, 'No such file'), PermissionError(13, 'Access is denied'),
                      OSError(1455, 'The paging file is too small')):
            r = self.ran(error=error)
            self.assertEqual((r.kind, r.code, r.out, r.err), ('unavailable', None, b'', b''), error)
            self.assertIn(error.strerror, r.detail)

    def test_timeout_must_be_positive(self):
        for timeout in (0, -1, None, float('nan'), float('inf'), True, '10'):
            with self.assertRaises(ValueError, msg=timeout):
                Command(ARGV, timeout)
        with self.assertRaises(TypeError):
            Command(ARGV)                       # no default
        for argv in ((), 'tool --flag'):
            with self.assertRaises(ValueError, msg=argv):
                Command(argv, 10)
        self.assertEqual(Command(['tool', 'a'], 0.5).argv, ('tool', 'a'))

    def test_stdin_is_devnull_unless_a_payload_is_given(self):
        options = process_options(10)
        self.assertIs(options['stdin'], subprocess.DEVNULL)
        self.assertNotIn('input', options)
        self.assertEqual(self.ran().kind, 'exited')
        given = process_options(10, input=b'data')
        self.assertEqual(given['input'], b'data')
        self.assertNotIn('stdin', given)

    def test_str_and_bytes_payloads_reach_the_child(self):
        self.ran(Command(ARGV, 10, stdin='{"é": 1}'))
        self.ran(Command(ARGV, 10, stdin=b'\x00raw'))
        self.assertEqual([options['input'] for _, options in self.fake.calls], ['{"é": 1}'.encode('utf-8'), b'\x00raw'])

    def test_cwd_and_env_are_passed_whole(self):
        self.ran(Command(ARGV, 10, cwd=str(self.fake.cwd), env=self.fake.env))
        (_, options), = self.fake.calls
        self.assertEqual((options['cwd'], options['env']), (str(self.fake.cwd), self.fake.env))

    def test_every_command_carries_no_shell_and_the_console_flag(self):
        done = subprocess.CompletedProcess(list(ARGV), 0, b'', b'')
        with mock.patch.object(process.subprocess, 'run', return_value=done) as spawn:
            run(Command(ARGV, 10))
        self.assertEqual(spawn.call_count, 1)
        seen = spawn.call_args.kwargs                       # what process.run really passed
        self.assertIs(seen['shell'], False)
        self.assertEqual(seen.get('creationflags'), process.NO_WINDOW.get('creationflags'))
        self.assertEqual(process.NO_WINDOW, {'creationflags': 0x08000000} if sys.platform == 'win32' else {})
        self.assertEqual(seen, process_options(10))
        self.assertEqual(spawn.call_args.args, (list(ARGV),))

    def test_base_exception_propagates(self):
        for error in (KeyboardInterrupt(), SystemExit(1)):
            with self.assertRaises(type(error)):
                self.ran(error=error)

    def test_a_process_failure_raises_nothing(self):
        for answer in (dict(returncode=-9), dict(returncode=1),
                       dict(error=subprocess.TimeoutExpired(list(ARGV), 10)), dict(error=OSError(5, 'denied'))):
            self.assertIsInstance(self.ran(**answer), process.Result, answer)
        self.assertTrue(issubclass(process.CommandTooLong, ValueError))

    def test_list2cmdline_quotes_as_subprocess_does_under_the_fake(self):
        argv = ['C:\\Program Files\\x.exe', 'a b', 'say "hi"', '']
        with self.fake.patch(process):
            self.assertEqual(process.list2cmdline(argv), subprocess.list2cmdline(argv))


if __name__ == '__main__':
    unittest.main()
