"""`git_client`: yes, no or unknown from one place. Git is faked at the command (`process.subprocess`)."""
import inspect
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from drover import compaction_hold, git_client, placement_git, process
from drover.git_client import Answer, GitDone, GitFailed, GitReply, ask, argv, mutate, out
from tests.fakeci import FakeCommands, process_options

REPO = '/repo'
HEAD = ('rev-parse', 'HEAD')


class FakedGit(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name
        self.fake = FakeCommands(temporary.name)
        context = self.fake.patch(process)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def expect(self, args=HEAD, timeout=git_client.QUICK, repo=REPO, **answer):
        self.fake.expect(placement_git.git_argv(repo, args), **process_options(timeout), **answer)

    def timeout(self, args=HEAD, timeout=git_client.QUICK):
        self.expect(args, timeout, error=subprocess.TimeoutExpired(['git'], timeout, b'part', b'slow'))

    def missing(self, args=HEAD, timeout=git_client.QUICK):
        self.expect(args, timeout, error=FileNotFoundError(2, 'Datei nicht gefunden'))


class Answers(FakedGit):
    def test_answer_refuses_truth_testing(self):
        for use in (bool, lambda a: not a):
            self.assertRaises(TypeError, use, Answer.UNKNOWN)
            self.assertRaises(TypeError, use, Answer.YES)

    def test_ask_exit_zero_is_yes_listed_exit_is_no_other_is_unknown(self):
        self.expect(output='abc\r\n')
        reply = ask(REPO, *HEAD, timeout_s=git_client.QUICK, no=(1,))
        self.assertEqual(reply, GitReply(Answer.YES, 'abc\n', '', 0, None))
        self.expect(returncode=1, stderr_output='no\n')
        self.assertEqual(ask(REPO, *HEAD, timeout_s=git_client.QUICK, no=(1,)), GitReply(Answer.NO, '', 'no\n', 1, None))
        self.expect(returncode=128, stderr_output='fatal\n')
        self.assertEqual(ask(REPO, *HEAD, timeout_s=git_client.QUICK, no=(1,)),
                         GitReply(Answer.UNKNOWN, '', 'fatal\n', 128, 'exit'))

    def test_no_has_no_default(self):
        with self.assertRaisesRegex(TypeError, "'no'"):
            ask(REPO, *HEAD, timeout_s=git_client.QUICK)
        self.assertIs(inspect.signature(ask).parameters['no'].default, inspect.Parameter.empty)

    def test_timeout_s_has_no_default_on_any_verb(self):
        for verb in (ask, out, mutate):
            parameter = inspect.signature(verb).parameters['timeout_s']
            self.assertIs(parameter.default, inspect.Parameter.empty, verb.__name__)
            self.assertIs(parameter.kind, inspect.Parameter.KEYWORD_ONLY, verb.__name__)
            with self.assertRaisesRegex(TypeError, "'timeout_s'"):
                verb(REPO, *HEAD, **({'no': (1,)} if verb is ask else {}))
        self.assertEqual((git_client.QUICK, git_client.NORMAL, git_client.FETCH, git_client.LONG),
                         (30.0, 60.0, 120.0, 600.0))

    def test_why_is_set_exactly_when_unknown(self):
        self.expect()
        self.expect(returncode=1)
        self.expect(returncode=2)
        self.timeout()
        self.missing()
        replies = [ask(REPO, *HEAD, timeout_s=git_client.QUICK, no=(1,)) for _ in range(5)]
        self.assertEqual([(r.answer, r.why) for r in replies],
                         [(Answer.YES, None), (Answer.NO, None), (Answer.UNKNOWN, 'exit'),
                          (Answer.UNKNOWN, 'timeout'), (Answer.UNKNOWN, 'unavailable')])
        self.assertEqual((replies[3].out, replies[3].err), ('part', 'slow'))

    def test_wsl_offline_at_build_is_unavailable(self):
        offline = compaction_hold.WslOffline('WSL is down for the overnight compaction')
        with mock.patch.object(placement_git, '_git_command', side_effect=offline):
            reply = ask(REPO, *HEAD, timeout_s=git_client.QUICK, no=(1,))
            done = mutate(REPO, 'fetch', timeout_s=git_client.FETCH)
            with self.assertRaises(GitFailed) as caught:
                out(REPO, *HEAD, timeout_s=git_client.QUICK)
        self.assertEqual((reply.answer, reply.why, reply.code), (Answer.UNKNOWN, 'unavailable', None))
        self.assertEqual((done.ok, done.why), (False, 'unavailable'))
        self.assertEqual(caught.exception.why, 'unavailable')
        self.assertEqual(self.fake.calls, [])

    def test_bad_git_pointer_is_git_failed_unavailable(self):
        repo = Path(self.root) / 'broken'
        repo.mkdir()
        (repo / '.git').write_text('not a pointer\n', encoding='utf-8')
        if placement_git.routes_through_wsl(str(repo)):
            self.skipTest('a WSL route never reads the pointer on this side')
        with self.assertRaises(GitFailed) as caught:
            out(repo, *HEAD, timeout_s=git_client.QUICK)
        self.assertEqual((caught.exception.why, caught.exception.code), ('unavailable', None))
        self.assertIn('invalid git metadata pointer', caught.exception.err)
        reply = ask(repo, *HEAD, timeout_s=git_client.QUICK, no=(1,))
        self.assertEqual((reply.answer, reply.why), (Answer.UNKNOWN, 'unavailable'))
        self.assertEqual(self.fake.calls, [])

    def test_a_command_too_long_is_a_callers_bug_and_raises(self):
        with mock.patch.object(placement_git, '_git_command', side_effect=process.CommandTooLong('long')):
            with self.assertRaises(process.CommandTooLong):
                ask(REPO, *HEAD, timeout_s=git_client.QUICK, no=(1,))


class Out(FakedGit):
    def test_out_returns_stdout_and_raises_git_failed_with_why(self):
        self.expect(output='one\r\ntwo\n')
        self.assertEqual(out(REPO, *HEAD, timeout_s=git_client.QUICK), 'one\ntwo\n')
        self.expect(returncode=1, stderr_output='fatal: bad\n', output='partial')
        self.timeout()
        self.missing()
        failures = []
        for _ in range(3):
            with self.assertRaises(GitFailed) as caught:
                out(REPO, *HEAD, timeout_s=git_client.QUICK)
            failures.append(caught.exception)
        self.assertEqual([(f.args_, f.why, f.code) for f in failures],
                         [(HEAD, 'exit', 1), (HEAD, 'timeout', None), (HEAD, 'unavailable', None)])
        self.assertEqual((failures[0].err, failures[0].out), ('fatal: bad\n', 'partial'))

    def test_error_keeps_a_domain_type_with_todays_message(self):
        def error(failed):
            return ValueError(failed.err.strip() or 'git command failed')
        self.expect(returncode=128, stderr_output='fatal: not a repo\n')
        self.expect(returncode=1)
        with self.assertRaisesRegex(ValueError, '^fatal: not a repo$') as caught:
            out(REPO, *HEAD, timeout_s=git_client.QUICK, error=error)
        self.assertIsInstance(caught.exception.__cause__, GitFailed)
        with self.assertRaisesRegex(ValueError, '^git command failed$'):
            out(REPO, *HEAD, timeout_s=git_client.QUICK, error=error)

    def test_git_failed_is_an_oserror_and_str_holds_no_stderr(self):
        self.assertNotIn('https://token@host', str(GitFailed(('fetch',), 'exit', 1, 'https://token@host', '')))
        failed = GitFailed(('fetch', 'origin'), 'timeout', None, 'https://token@host', 'x')
        self.assertIsInstance(failed, OSError)
        self.assertEqual((failed.args_, failed.why, failed.code, failed.err, failed.out),
                         (('fetch', 'origin'), 'timeout', None, 'https://token@host', 'x'))
        self.assertNotIn('token', str(failed) + repr(failed))
        self.assertIn('fetch', str(failed))


class Mutate(FakedGit):
    def test_mutate_reports_ok_and_why_and_has_no_answer(self):
        args = ('update-ref', 'refs/heads/main', 'b', 'a')
        self.expect(args, git_client.NORMAL)
        self.expect(args, git_client.NORMAL, returncode=1, stderr_output='lock\n')
        self.timeout(args, git_client.NORMAL)
        done = [mutate(REPO, *args, timeout_s=git_client.NORMAL) for _ in range(3)]
        self.assertEqual(done[0], GitDone(True, '', '', 0, None))
        self.assertEqual(done[1], GitDone(False, '', 'lock\n', 1, 'exit'))
        self.assertEqual((done[2].ok, done[2].code, done[2].why), (False, None, 'timeout'))
        self.assertFalse(hasattr(done[0], 'answer'))
        self.assertNotIn('no', inspect.signature(mutate).parameters)


class Conventions(FakedGit):
    def test_nothing_retries(self):
        self.expect(returncode=128)
        self.timeout()
        self.missing()
        self.expect(returncode=1)
        ask(REPO, *HEAD, timeout_s=git_client.QUICK, no=(1,))
        ask(REPO, *HEAD, timeout_s=git_client.QUICK, no=(1,))
        ask(REPO, *HEAD, timeout_s=git_client.QUICK, no=(1,))
        mutate(REPO, *HEAD, timeout_s=git_client.QUICK)
        self.assertEqual(len(self.fake.calls), 4)

    def test_no_124_or_127_reaches_any_caller(self):
        self.timeout()
        self.missing()
        self.timeout()
        self.missing()
        replies = [ask(REPO, *HEAD, timeout_s=git_client.QUICK, no=(1,)) for _ in range(2)]
        replies += [mutate(REPO, *HEAD, timeout_s=git_client.QUICK) for _ in range(2)]
        self.assertEqual([r.code for r in replies], [None] * 4)
        self.assertEqual([r.why for r in replies], ['timeout', 'unavailable'] * 2)
        self.assertFalse(hasattr(git_client, 'GIT_TIMED_OUT'))

    def test_spawn_counts_stay_for_the_hub_performance_log(self):
        for moved in ('request_git', 'git_spawns', 'git_spawn_lock', 'git'):
            self.assertFalse(hasattr(placement_git, moved), moved)
        self.expect()
        self.expect()
        self.missing()
        before, counts = git_client.git_spawns, {'spawns': 0}
        token = git_client.request_git.set(counts)
        try:
            ask(REPO, *HEAD, timeout_s=git_client.QUICK, no=(1,))
            out(REPO, *HEAD, timeout_s=git_client.QUICK)
            mutate(REPO, *HEAD, timeout_s=git_client.QUICK)
        finally:
            git_client.request_git.reset(token)
        self.assertEqual((git_client.git_spawns - before, counts['spawns']), (3, 3))
        with mock.patch.object(placement_git, '_git_command', side_effect=compaction_hold.WslOffline('down')):
            ask(REPO, *HEAD, timeout_s=git_client.QUICK, no=(1,))
        self.assertEqual(git_client.git_spawns - before, 3, 'nothing spawned, nothing counted')

    def test_argv_equals_the_journals_restore_argv(self):
        args = ['worktree', 'add', '--detach', '/trees/one', 'abc']
        self.assertEqual(argv(REPO, args), placement_git.git_argv(REPO, args))
        self.assertEqual(argv(REPO, args, side='wsl'), placement_git.git_argv(REPO, args, side='wsl'))

    def test_the_pointer_env_reaches_git(self):
        extra = {'GIT_COMMON_DIR': '/common'}
        with mock.patch.object(placement_git, '_git_command', return_value=(['git', '-C', REPO, *HEAD], extra)):
            self.fake.expect(['git', '-C', REPO, *HEAD], **process_options(git_client.QUICK, env={'A': '1', **extra}))
            ask(REPO, *HEAD, timeout_s=git_client.QUICK, no=(1,), env={'A': '1'})


if __name__ == '__main__':
    unittest.main()
