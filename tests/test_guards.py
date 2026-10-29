"""Guards that only ever tighten: file size, real-process test count, default-tier purity.

Each ceiling is pinned at today's value. Lower it (delete the entry / reduce the number) when the code improves;
the tests fail when a pinned value is now loose, so the pins cannot rot, and nothing here lets a new offender in.
"""
import os
import io
import json
import subprocess
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

import tests
from tests.fakeci import FakeCommands, InProcessLauncher
from tests.gitfix import sh
from tests.tiers import MARKER, enabled

ROOT = Path(__file__).resolve().parents[1]
MAX_LINES = 500
INTEGRATION_TARGET = 20
INTEGRATION_CEILING = 93  # pinned today; owner target is INTEGRATION_TARGET

# Files over MAX_LINES, pinned at their current line counts (wc -l). Shrink-only: never add a file.
OVER = {
    "hub/reload_protocol.py": 571,
    "hub/runtime.py": 612,
}

IMPURE_CEILING = 65  # len(tests.IMPURE): default-tier modules still allowed to start processes or sleep


def sources():
    """.py/.ps1 files under the source folders, skipping dot dirs, caches, node_modules and virtualenvs."""
    for top in ('drover', 'hub', 'scripts', 'tests'):
        for folder, dirs, names in os.walk(ROOT / top):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('__pycache__', 'node_modules')
                       and not (Path(folder, d, 'pyvenv.cfg')).exists()]
            for name in names:
                if name.endswith(('.py', '.ps1')):
                    yield Path(folder, name)


def integration_tests(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from integration_tests(test)
        elif getattr(type(test), MARKER, False) or getattr(getattr(test, test._testMethodName), MARKER, False):
            yield test


class FileSize(unittest.TestCase):
    def test_python_and_powershell_files_stay_small(self):
        lines = {p.relative_to(ROOT).as_posix(): p.read_bytes().count(b'\n') for p in sources()}
        print(f'\nfiles over 300 lines: {sum(n > 300 for n in lines.values())}')
        grown = {f: n for f, n in lines.items() if n > OVER.get(f, MAX_LINES)}
        self.assertFalse(grown, f'files over their limit ({MAX_LINES}, or the pinned count in OVER): {grown}')
        stale = [f for f, pinned in OVER.items() if lines.get(f, 0) <= MAX_LINES or lines[f] < pinned]
        self.assertFalse(stale, 'shrink-only: delete the OVER entry (now <= 500 lines) or lower its pinned count: '
                         + ', '.join(f'{f} is {lines.get(f, 0)}' for f in stale))


class RealProcessBudget(unittest.TestCase):
    def test_integration_tests_only_go_down(self):
        suite = unittest.TestLoader().discover(str(ROOT / 'tests'), top_level_dir=str(ROOT))
        count = len(list(integration_tests(suite)))
        print(f'\nintegration-tier tests: {count} (target {INTEGRATION_TARGET})')
        self.assertLessEqual(count, INTEGRATION_CEILING,
                             f'{count} integration tests exceed the ceiling {INTEGRATION_CEILING}; '
                             f'fake the process instead (target {INTEGRATION_TARGET})')
        self.assertGreaterEqual(count, INTEGRATION_CEILING,
                                f'shrink-only: lower INTEGRATION_CEILING to {count} (target {INTEGRATION_TARGET})')


class FakeCommandBoundaries(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='drover-fake-commands-')
        self.addCleanup(temporary.cleanup)
        self.fake = FakeCommands(temporary.name)
        self.options = dict(cwd=self.fake.cwd, env=self.fake.env, timeout=17, text=True,
                            capture_output=True, stdin=subprocess.DEVNULL)

    def test_pwsh_python_and_git_outcomes_start_no_process_and_never_sleep(self):
        seen = list(getattr(tests, 'VIOLATIONS', []))
        with mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('real process')) as starts, \
                mock.patch.object(time, 'sleep', side_effect=AssertionError('real sleep')) as sleeps:
            for executable in (self.fake.pwsh, self.fake.python, self.fake.git):
                argv = [executable, 'argument with spaces', self.fake.cwd / 'input.json']
                with self.subTest(executable=executable):
                    self.fake.expect(argv, output='{"ok":true}', **self.options)
                    result = self.fake.run(argv, **self.options)
                    self.assertEqual(json.loads(result.stdout), {'ok': True})
                    self.assertEqual((result.args, result.returncode, result.stderr),
                                     ([str(a) for a in argv], 0, ''))
                    self.fake.expect(argv, returncode=7, stderr_output='command failed', **self.options)
                    result = self.fake.run(argv, **self.options)
                    self.assertEqual((result.returncode, result.stderr), (7, 'command failed'))
                    error = subprocess.TimeoutExpired([str(a) for a in argv], 17, output=b'partial', stderr=b'stalled')
                    self.fake.expect(argv, error=error, **self.options)
                    with self.assertRaises(subprocess.TimeoutExpired) as caught:
                        self.fake.run(argv, **self.options)
                    self.assertIs(caught.exception, error)
                    self.assertEqual((caught.exception.timeout, caught.exception.stdout), (17, b'partial'))
                    self.fake.expect(argv, output='{malformed', **self.options)
                    with self.assertRaises(json.JSONDecodeError):
                        json.loads(self.fake.run(argv, **self.options).stdout)
                    self.fake.expect(argv, error=FileNotFoundError('missing command'), **self.options)
                    with self.assertRaises(FileNotFoundError):
                        self.fake.run(argv, **self.options)
            starts.assert_not_called()
            sleeps.assert_not_called()
        self.fake.assert_done()
        self.assertEqual(len(self.fake.calls), 15)
        self.assertEqual(getattr(tests, 'VIOLATIONS', []), seen)
        for argv, options in self.fake.calls:
            self.assertTrue(Path(argv[0]).is_relative_to(self.fake.root))
            self.assertEqual(options, self.options)
        for name in ('HOME', 'USERPROFILE', 'DROVER_HOME', 'PATH'):
            self.assertTrue(Path(self.fake.env[name]).is_relative_to(self.fake.root))

    def test_contract_rejects_changed_or_omitted_arguments_and_options(self):
        argv = [self.fake.python, '-V']
        self.fake.expect(argv, **self.options)
        variants = [([self.fake.python, '--version'], self.options)]
        for key, value in (('cwd', self.fake.root), ('env', {}), ('timeout', 18), ('text', False),
                           ('stdin', None), ('capture_output', False)):
            variants.append((argv, {**self.options, key: value}))
            variants.append((argv, {k: v for k, v in self.options.items() if k != key}))
        variants.append((argv, {**self.options, 'shell': True}))
        for command, options in variants:
            with self.subTest(command=command, options=options):
                with self.assertRaisesRegex(AssertionError, 'command contract mismatch'):
                    self.fake.run(command, **options)
        with self.assertRaisesRegex(AssertionError, 'unused command'):
            self.fake.assert_done()
        self.fake.run(argv, **self.options)
        self.fake.assert_done()
        with self.assertRaisesRegex(AssertionError, 'unexpected command'):
            self.fake.run(argv, **self.options)
        with self.assertRaisesRegex(AssertionError, 'argv sequence'):
            self.fake.run('python -V')

    def test_environment_expectations_and_recordings_are_snapshots(self):
        env = dict(self.fake.env)
        argv = [self.fake.python, '-V']
        self.fake.expect(argv, env=env)
        env['ADDED_LATER'] = 'unexpected'
        with self.assertRaisesRegex(AssertionError, 'command contract mismatch'):
            self.fake.run(argv, env=env)
        env.pop('ADDED_LATER')
        self.fake.run(argv, env=env)
        env.clear()
        self.assertEqual(self.fake.calls[-1][1]['env'], self.fake.env)
        self.fake.assert_done()

    def test_capture_redirection_and_checked_failure(self):
        argv = [self.fake.pwsh, '-File', self.fake.cwd / 'gate.ps1']
        options = {**self.options, 'check': True}
        self.fake.expect(argv, returncode=3, output='partial', stderr_output='failed', **options)
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.fake.run(argv, **options)
        self.assertEqual((caught.exception.returncode, caught.exception.stdout, caught.exception.stderr),
                         (3, 'partial', 'failed'))
        output = io.StringIO()
        options = dict(stdout=output, stderr=subprocess.STDOUT, text=True)
        self.fake.expect(argv, output='out\n', stderr_output='err\n', **options)
        result = self.fake.run(argv, **options)
        self.assertEqual(output.getvalue(), 'out\nerr\n')
        self.assertEqual((result.stdout, result.stderr), (None, None))
        self.fake.expect(argv, output='bytes', stderr_output=b'error', capture_output=True)
        result = self.fake.run(argv, capture_output=True)
        self.assertEqual((result.stdout, result.stderr), (b'bytes', b'error'))
        for mode, text in (('w', False), ('wb', True)):
            with self.subTest(mode=mode, text=text):
                log = self.fake.root / 'redirected.log'
                with log.open(mode) as stream:
                    options = dict(stdout=stream, stderr=subprocess.STDOUT, text=text)
                    self.fake.expect(argv, output='out\n', stderr_output='err\n', **options)
                    result = self.fake.run(argv, **options)
                    self.assertEqual(log.read_bytes(), b'out\nerr\n')
                    self.assertEqual((result.stdout, result.stderr), (None, None))
        self.fake.assert_done()

    def test_patch_is_local_and_production_git_checks_the_boundary(self):
        from drover import placement_git
        original, real_run = placement_git.subprocess, subprocess.run
        argv = ['git', '-C', str(self.fake.cwd), 'rev-parse', 'HEAD']
        options = dict(text=True, capture_output=True, encoding='utf-8', errors='replace',
                       timeout=23, env=self.fake.env, **placement_git.NO_WINDOW)
        self.fake.expect(argv, output='abc123\n', **options)
        with self.fake.patch(placement_git):
            result = placement_git.git(self.fake.cwd, 'rev-parse', 'HEAD', timeout=23, env=self.fake.env)
            self.assertEqual(result.stdout, 'abc123\n')
            self.assertIs(subprocess.run, real_run)
            with self.assertRaisesRegex(AssertionError, 'unscripted subprocess API'):
                placement_git.subprocess.Popen(argv)
        self.assertIs(placement_git.subprocess, original)
        with self.assertRaisesRegex(ValueError, 'consumer module'):
            with self.fake.patch(subprocess):
                pass
        self.fake.expect(argv, **options)
        with self.assertRaisesRegex(AssertionError, 'unused command'):
            with self.fake.patch(placement_git):
                pass
        self.assertIs(placement_git.subprocess, original)

    def test_git_helper_uses_injected_environment_and_timeout_without_retry_sleep(self):
        env = {**self.fake.env, 'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'never', 'GIT_ASKPASS': ''}
        argv = [self.fake.git, 'clone', 'fixture://remote', self.fake.cwd]
        options = dict(cwd=None, env={**env, 'GIT_TRACE': '1'}, text=True,
                       capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
        self.fake.expect(argv, error=subprocess.TimeoutExpired(argv, 60, stderr=b'stalled'), **options)
        self.fake.expect(argv, output='cloned', **options)
        with mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('real process')) as starts, \
                mock.patch.object(time, 'sleep', side_effect=AssertionError('real sleep')) as sleeps:
            result = sh(*argv, runner=self.fake.run, env=self.fake.env)
            self.assertEqual(result.stdout, 'cloned')
            starts.assert_not_called()
            sleeps.assert_not_called()
        self.assertEqual(len(self.fake.calls), 2)
        self.assertEqual(self.fake.env.get('GIT_TRACE'), None)
        self.fake.assert_done()

    def test_patched_git_preserves_missing_executable_result(self):
        from drover import placement_git
        argv = ['git', '-C', str(self.fake.cwd), 'rev-parse', 'HEAD']
        options = dict(text=True, capture_output=True, encoding='utf-8', errors='replace',
                       timeout=23, env=self.fake.env, **placement_git.NO_WINDOW)
        self.fake.expect(argv, error=FileNotFoundError('fixture git is missing'), **options)
        with self.fake.patch(placement_git):
            result = placement_git.git(self.fake.cwd, 'rev-parse', 'HEAD', timeout=23, env=self.fake.env)
        self.assertIsInstance(result, subprocess.CompletedProcess)
        self.assertEqual((result.returncode, result.stdout), (127, ''))
        self.assertIn('fixture git is missing', result.stderr)

    def test_in_process_ci_uses_fake_pwsh_python_and_git_without_processes_or_sleeps(self):
        from drover import ci, placement_exec, placement_git, runner
        repo, sha, base = str(self.fake.cwd), 'a' * 40, 'b' * 40
        runs = self.fake.root / 'runs'
        run_dir = runner.run_dir(runs, 'fake-gate')
        receipt, log = run_dir / 'receipt.json', run_dir / runner.OUT_FILE
        commands = [[str(self.fake.pwsh), '-NoProfile', '-File', str(self.fake.cwd / 'gate.ps1')],
                    [str(self.fake.python), '-m', 'unittest', 'tests.sample']]
        plan = dict(repo=repo, sha=sha, base='main', kind='full', platform='windows',
                    commands=commands, out=str(receipt), log=str(log))
        plan_file = self.fake.root / 'plan.json'
        plan_file.write_text(json.dumps(plan), encoding='utf-8')
        env = {**self.fake.env, **ci.GIT_PROMPT_ENV}
        options = dict(text=True, capture_output=True, encoding='utf-8', errors='replace',
                       timeout=60, env=env, **placement_git.NO_WINDOW)

        def expect_git(args, output=''):
            self.fake.expect(['git', '-C', repo, *args], output=output, **options)

        expect_git(['rev-parse', 'HEAD'], sha + '\n')
        expect_git(['status', '--porcelain', '--untracked-files=no'])
        expect_git(['ls-files', '--others', '--exclude-standard', '--directory', '--no-empty-directory'])
        expect_git(['rev-parse', '--verify', '--quiet', 'main^{commit}'], base + '\n')
        expect_git(['merge-base', '--is-ancestor', base, sha])
        for argv in commands:
            self.fake.expect(argv, cwd=repo, env=env, stdout=mock.ANY, stderr=subprocess.STDOUT,
                             output='gate output\n', stderr_output='gate diagnostic\n')
        expect_git(['rev-parse', 'HEAD'], sha + '\n')
        expect_git(['status', '--porcelain', '--untracked-files=no'])
        launcher = InProcessLauncher(runs, command_runner=self.fake.run)
        spec = dict(run_id='fake-gate', nonce='fixture-nonce',
                    argv=[str(self.fake.python), '-m', 'drover.ci', '--plan', str(plan_file)])
        seen = list(getattr(tests, 'VIOLATIONS', []))
        with mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('real process')) as starts, \
                mock.patch.object(time, 'sleep', side_effect=AssertionError('real sleep')) as sleeps, \
                mock.patch.dict(os.environ, self.fake.env, clear=True), \
                self.fake.patch(placement_git, placement_exec):
            handle = launcher.launch(spec)
            self.assertEqual(handle.poll()['status'], 'done')
            starts.assert_not_called()
            sleeps.assert_not_called()
        self.assertEqual(getattr(tests, 'VIOLATIONS', []), seen)
        saved = json.loads(receipt.read_text(encoding='utf-8'))
        self.assertTrue(saved['passed'])
        self.assertEqual([step['command'] for step in saved['steps']], commands)
        self.assertEqual(log.read_text(encoding='utf-8'), 'gate output\ngate diagnostic\n' * 2)
        self.assertEqual(json.loads((run_dir / runner.STATUS_FILE).read_text())['exit_code'], 0)
        self.assertEqual(len(self.fake.calls), 9)
        for _, observed in self.fake.calls[5:7]:
            self.assertEqual(Path(observed['stdout'].name), log)
            self.assertTrue(observed['stdout'].closed)


@unittest.skipIf(enabled(), 'the purity guard applies to the default tier only')
class DefaultTierPurity(unittest.TestCase):
    def test_guard_blocks_processes_and_real_sleeps_outside_the_list(self):
        with mock.patch.object(tests, 'IMPURE', set()):
            with self.assertRaises(AssertionError):
                subprocess.run(['true'])
            with self.assertRaises(AssertionError):
                os.system('true')
            with self.assertRaises(AssertionError):
                time.sleep(0.1)
            time.sleep(0.001)  # a yield is not a real sleep
        del tests.VIOLATIONS[:]  # these were deliberate

    def test_impure_list_only_shrinks(self):
        n = len(tests.IMPURE)
        self.assertLessEqual(n, IMPURE_CEILING, 'do not add to tests.IMPURE: fake the process or sleep')
        self.assertGreaterEqual(n, IMPURE_CEILING, f'shrink-only: lower IMPURE_CEILING to {n}')
