"""CI's public interface, using repo scripts and the real composed runner command."""
import json
import os
import shutil
import sqlite3
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from drover import ci, repos, runner
from drover.ledger import db, ci as slots
from tests.gitfix import Repo

PLATFORM = 'windows' if os.name == 'nt' else 'wsl'


class CITests(unittest.TestCase):
    def setUp(self):
        self.repo = Repo(ignore='__pycache__/\n')
        self.addCleanup(self.repo.cleanup)
        self.runs = self.repo.root / 'runs'
        self.service = ci.CI(self.runs)
        self.machine = {'name': 'fixture', 'repo': str(self.repo.repo),
                        'run': {'platforms': [PLATFORM], 'default': PLATFORM},
                        'gate': {'platform': PLATFORM, 'timeout_s': 30, 'idle_s': 10}}

    def configure(self, code, docs=False):
        exe = sys.executable.replace("'", "''")
        self.repo.commit('scripts/check action.ps1',
                         f"if ($args.Count) {{ exit 97 }}\n& '{exe}' -c '{code.replace(chr(39), chr(39)*2)}'\nexit $LASTEXITCODE\n")
        self.repo.commit('drover.toml', '[scripts]\ntest="scripts/check action.ps1"\n[gate]\n'
                         f'docs_only={str(docs).lower()}\n')
        self.repo.git('checkout', '-b', 'topic')
        return repos.load(self.machine)

    def test_composed_wsl_command_runs_a_tiny_unittest_project_verbatim(self):
        exe = sys.executable.replace("'", "''")
        self.repo.commit('scripts/check action.ps1',
                         'if ($args.Count) { exit 97 }\n'
                         f"& '{exe}' -m unittest discover -s sample_tests\nexit $LASTEXITCODE\n")
        self.repo.commit('sample_tests/test_sample.py',
                         'import json,os,pathlib,unittest\nclass Sample(unittest.TestCase):\n'
                         ' def test_context(self):\n'
                         '  self.assertEqual(os.environ["DROVER_CI_KIND"], "checks")\n'
                         '  self.assertIn("source.txt", json.loads(os.environ["DROVER_CHANGED_FILES"]))\n'
                         '  self.assertTrue(pathlib.Path("drover.toml").is_file())\n'
                         '  print("fixture: passed")\n')
        self.repo.commit('drover.toml', '[scripts]\ntest="scripts/check action.ps1"\n[gate]\ndocs_only=false\n')
        self.repo.git('checkout', '-b', 'topic')
        commit = self.repo.commit('source.txt', 'changed')
        prog = repos.load(self.machine)
        result = self.service.run(prog, self.repo.repo, commit, kind='checks')
        self.assertTrue(result['passed'])
        self.assertEqual(result['sha'], commit)
        self.assertEqual(result['steps'][0]['command'], repos.script_argv(prog, platform=PLATFORM))
        self.assertIn('fixture: passed', '\n'.join(result['log_tail']))
        self.assertIn('Ran 1 test', '\n'.join(result['log_tail']))
        self.assertEqual(ci.problems(result, self.repo.repo, 'main'), [])
        status = json.loads(next(self.runs.glob('ci-*/status.json')).read_text())
        self.assertEqual((status['state'], status['exit_code']), ('exited', 0))
        self.assertIn('heartbeat', status)

    def test_exit_code_and_clean_tree_determine_the_result(self):
        prog = self.configure('print("PASSED summary"); raise SystemExit(7)')
        commit = self.repo.commit('source.txt', 'changed')
        result = self.service.run(prog, self.repo.repo, commit)
        self.assertEqual((result['passed'], result['exit_code'], result['kind']), (False, 7, 'full'))
        self.assertEqual(result['log_tail'], ['PASSED summary'])
        self.repo.commit('scripts/check action.ps1', 'Set-Content source.txt changed-again\nexit 0\n')
        commit = self.repo.git('rev-parse', 'HEAD')
        result = self.service.run(prog, self.repo.repo, commit)
        self.assertEqual((result['exit_code'], result['tree_unchanged'], result['passed']), (0, False, False))

    def test_a_passing_result_does_not_authorize_cancelled_execution(self):
        from drover.launch import RunnerLauncher
        class CancelledAfterResult(RunnerLauncher):
            def launch(self, spec):
                handle = super().launch(spec)
                class Follow:
                    def poll(self):
                        value = handle.poll()
                        return {**value, 'status': 'error', 'returncode': returncode} if value else None
                return Follow()
        prog = self.configure('print("finished")')
        commit = self.repo.commit('source.txt', 'changed')
        for returncode in (-15, 0):
            with self.subTest(returncode=returncode):
                service = ci.CI(self.runs, CancelledAfterResult(self.runs))
                with self.assertRaises(ci.CIError):
                    service.run(prog, self.repo.repo, commit)

    def test_docs_classification_is_shared_by_checks_and_full_ci(self):
        prog = self.configure('raise SystemExit(7)', docs=True)
        commit = self.repo.commit('docs/readme.md', 'documentation\n')
        for kind in ('checks', 'full'):
            result = self.service.run(prog, self.repo.repo, commit, kind=kind)
            self.assertTrue(result['passed'])
            self.assertEqual(result['kind'], 'docs')
        self.repo.commit('scripts/docs.ps1', 'Write-Output "docs script"\nexit 0\n')
        self.repo.commit('drover.toml', '[scripts]\ntest="scripts/check action.ps1"\ndocs="scripts/docs.ps1"\n')
        self.repo.git('branch', '-f', 'main', 'HEAD')
        commit = self.repo.commit('docs/next.md', 'more docs\n')
        result = self.service.run(prog, self.repo.repo, commit)
        self.assertIn('--check', result['steps'][0]['command'])
        self.assertEqual(result['steps'][1]['command'][-1], 'scripts/docs.ps1')
        self.assertIn('docs script', result['log_tail'])

    def test_docs_policy_through_prepare(self):
        self.configure('raise SystemExit(99)', docs=True)
        for files, policy, expected in (
                ({'docs/readme.md': 'docs\n', 'source.py': 'code\n'}, {}, 'checks'),
                ({'.github/readme.md': 'docs\n'}, {}, 'checks'),
                ({'docs/readme.md': 'docs\n'}, {'docs_only': False}, 'checks'),
                ({'guide/readme.rst': 'docs\n'}, {'docs_only_globs': ['guide/**']}, 'docs'),
                ({'docs/readme.md': 'docs\n'}, {'docs_only_globs': ['guide/**']}, 'checks'),
                ({'docs/readme.md': 'docs\n'}, {'docs_only_globs': []}, 'checks'),
                ({'guide/readme.md': 'docs\n'}, {'docs_only_globs': ['**/*.md', '!guide/**']}, 'checks')):
            with self.subTest(files=files, policy=policy):
                self.repo.git('checkout', '-B', 'case', 'main')
                for path, content in files.items():
                    commit = self.repo.commit(path, content)
                machine = {**self.machine, 'gate': {**self.machine['gate'], **policy}}
                context = self.service.prepare(machine, self.repo.repo, commit, 'docs-policy')
                self.assertEqual(context['kind'], expected)
                plan = json.loads((self.runs / 'docs-policy' / 'plan.json').read_text())
                self.assertEqual('--check' in plan['commands'][0], expected == 'docs')

    def test_docs_symlink_addition_and_removal_require_checks(self):
        self.configure('raise SystemExit(99)', docs=True)
        # Git's index represents symlinks even on machines without symlink privileges.
        blob = self.repo.git('hash-object', '-w', '--stdin', input='../base.txt\n')
        self.repo.git('update-index', '--add', '--cacheinfo', f'120000,{blob},docs/link.md')
        self.repo.git('commit', '-qm', 'add symlink')
        commit = self.repo.git('rev-parse', 'HEAD')
        context = self.service.prepare(self.machine, self.repo.repo, commit, 'symlink-add')
        self.assertEqual(context['kind'], 'checks')
        self.repo.git('branch', '-f', 'main', commit)
        self.repo.git('update-index', '--force-remove', 'docs/link.md')
        self.repo.git('commit', '-qm', 'remove symlink')
        commit = self.repo.git('rev-parse', 'HEAD')
        context = self.service.prepare(self.machine, self.repo.repo, commit, 'symlink-remove')
        self.assertEqual(context['kind'], 'checks')

    def test_docs_markdown_hard_breaks_pass_the_composed_command(self):
        prog = self.configure('raise SystemExit(99)', docs=True)
        commit = self.repo.commit('docs/readme.md', 'line with hard break  \nnext line\n')
        result = self.service.run(prog, self.repo.repo, commit)
        self.assertTrue(result['passed'])
        self.assertEqual(result['kind'], 'docs')

    def test_heartbeat_failure_keeps_the_live_runner_for_adoption(self):
        from drover.launch import RunnerLauncher
        count, release = self.repo.root / 'count', self.repo.root / 'release'
        self.addCleanup(release.touch)
        code = ('import pathlib,time; p=pathlib.Path("' + str(count).replace('\\', '/') + '"); '
                'p.write_text(p.read_text()+"start\\n" if p.exists() else "start\\n"); '
                'print("started",flush=True); '
                '\nwhile not pathlib.Path("' + str(release).replace('\\', '/') + '").exists(): time.sleep(.05)')
        prog = self.configure(code)
        commit = self.repo.commit('source.txt', 'changed')
        conn = db.connect(self.repo.root / 'ledger.db')
        self.addCleanup(conn.close)
        launcher = RunnerLauncher(self.runs)
        service = ci.CI(self.runs, launcher)
        def fail_heartbeat(*args):
            deadline = time.monotonic() + 20
            while not count.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(count.exists())
            raise sqlite3.OperationalError('database is locked')
        with patch.object(slots, 'lease_heartbeat', side_effect=fail_heartbeat):
            with self.assertRaises(ci.CIInterrupted) as raised:
                service.run(prog, self.repo.repo, commit, conn=conn)
        self.assertIsInstance(raised.exception.__cause__, sqlite3.OperationalError)
        self.assertEqual(count.read_text(), 'start\n')
        self.assertEqual(len(list(self.runs.glob('ci-*.json'))), 1)
        self.assertEqual(len(slots.lease_queue(conn)), 1)
        # A second database error before adoption must retain the same request too.
        with patch.object(slots, 'lease_acquire', side_effect=sqlite3.OperationalError('database is locked')):
            with self.assertRaises(ci.CIInterrupted):
                ci.CI(self.runs, launcher).resume(prog, self.repo.repo, conn=conn)
        self.assertEqual(len(list(self.runs.glob('ci-*.json'))), 1)
        self.assertEqual(len(slots.lease_queue(conn)), 1)
        release.touch()
        resumed = ci.CI(self.runs, launcher)
        resumed.resume(prog, self.repo.repo, conn=conn)
        self.assertTrue(resumed.run(prog, self.repo.repo, commit, conn=conn)['passed'])
        self.assertEqual(count.read_text(), 'start\n')
        self.assertEqual(slots.lease_queue(conn), [])
        self.assertFalse(list(self.runs.glob('ci-*.json')))

    def test_dead_runner_is_finalized_on_resume_and_retry_finishes(self):
        from drover.launch import RunnerLauncher
        count, pulse = self.repo.root / 'count', self.repo.root / 'pulse'
        code = ('import pathlib,time; p=pathlib.Path("' + str(count).replace('\\', '/') + '"); '
                'n=int(p.read_text())+1 if p.exists() else 1; p.write_text(str(n)); '
                'print("started",flush=True); '
                '\nwhile n == 1: pathlib.Path("' + str(pulse).replace('\\', '/') + '").write_text(str(time.time())); time.sleep(.05)')
        prog = self.configure(code)
        commit = self.repo.commit('source.txt', 'changed')
        conn = db.connect(self.repo.root / 'ledger.db')
        self.addCleanup(conn.close)
        launcher = RunnerLauncher(self.runs)
        def interrupted(*args):
            deadline = time.monotonic() + 20
            while not pulse.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(pulse.exists())
            raise sqlite3.OperationalError('database is locked')
        with patch.object(slots, 'lease_heartbeat', side_effect=interrupted):
            with self.assertRaises(ci.CIInterrupted):
                ci.CI(self.runs, launcher).run(prog, self.repo.repo, commit, conn=conn)
        status_file = next(self.runs.glob('ci-*/status.json'))
        status = json.loads(status_file.read_text())
        # Kill only the runner: POSIX children survive; Windows closes its job.
        if os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/PID', str(status['runner_pid'])],
                           check=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=20)
        else:
            import signal
            os.kill(status['runner_pid'], signal.SIGKILL)
        with patch.object(launcher, 'recover_ci', return_value=False):
            with self.assertRaises(ci.CIInterrupted):
                ci.CI(self.runs, launcher).resume(prog, self.repo.repo, conn=conn)
        self.assertEqual(len(list(self.runs.glob('ci-*.json'))), 1)
        self.assertEqual(len(slots.lease_queue(conn)), 1)
        resumed = ci.CI(self.runs, launcher)
        with self.assertRaises(ci.CIError) as raised:
            resumed.resume(prog, self.repo.repo, conn=conn)
        self.assertNotIsInstance(raised.exception, ci.CIInterrupted)
        last_pulse = pulse.read_text()
        time.sleep(.2)
        self.assertEqual(pulse.read_text(), last_pulse, 'orphan child is still executing')
        final = json.loads(status_file.read_text())
        self.assertEqual(final['state'], 'killed')
        self.assertIsNotNone(final['ended'])
        self.assertEqual(slots.lease_queue(conn), [])
        self.assertFalse(list(self.runs.glob('ci-*.json')))
        # A second resume must not adopt the dead runner again.
        resumed.resume(prog, self.repo.repo, conn=conn)
        self.assertTrue(resumed.run(prog, self.repo.repo, commit, conn=conn)['passed'])
        self.assertEqual(count.read_text(), '2')
        self.assertEqual(slots.lease_queue(conn), [])

    def test_docs_script_cannot_bypass_diff_check(self):
        self.configure('raise SystemExit(99)', docs=True)
        self.repo.commit('scripts/docs.ps1', 'Write-Output "docs script ran"\nexit 0\n')
        self.repo.commit('drover.toml', '[scripts]\ntest="scripts/check action.ps1"\ndocs="scripts/docs.ps1"\n')
        self.repo.git('branch', '-f', 'main', 'HEAD')
        for content, diagnostic in (('<<<<<<< ours\ntext\n=======\nother\n>>>>>>> theirs\n',
                                     'conflict marker'), ('text\n\n', 'blank line at EOF')):
            commit = self.repo.commit('docs/readme.md', content)
            for kind in ('checks', 'full'):
                with self.subTest(content=diagnostic, kind=kind):
                    result = self.service.run(self.machine, self.repo.repo, commit, kind=kind)
                    self.assertFalse(result['passed'])
                    self.assertEqual(len(result['steps']), 1)
                    self.assertIn('--check', result['steps'][0]['command'])
                    self.assertIn(diagnostic, '\n'.join(result['log_tail']))
                    self.assertNotIn('docs script ran', result['log_tail'])

    @unittest.skipIf(os.name == 'nt', 'Linux resource scope')
    def test_priority_controls_surround_the_verbatim_script_in_the_runner(self):
        # A machine executable stand-in lets the composed command run even where
        # the sandbox cannot access the systemd user bus. It execs the real script.
        bindir = self.repo.root / 'bin'
        bindir.mkdir()
        calls = self.repo.root / 'scopes.jsonl'
        scope = bindir / 'systemd-run'
        scope.write_text(f"#!{sys.executable}\n" +
                         'import json,os,sys\n'
                         'args=sys.argv[1:]\n'
                         'if args[-1:] == ["true"]: raise SystemExit(0)\n'
                         'with open(os.environ["SCOPE_CALLS"], "a") as f: f.write(json.dumps(args)+"\\n")\n'
                         'inner=args[args.index("--")+1:]\n'
                         'os.execvp(inner[0], inner)\n')
        scope.chmod(0o755)
        prog = self.configure('print("script executed")')
        commit = self.repo.commit('source.txt', 'changed')
        self.machine['run']['env'] = {'PATH': str(bindir) + os.pathsep + os.environ['PATH'],
                                      'SCOPE_CALLS': str(calls)}
        prog = repos.load(self.machine)
        command = repos.script_argv(prog, platform=PLATFORM)
        for priority, weight in (('P0', 1000), ('P1', 1000), ('P2', 100), ('P3', 30), ('P4', 10)):
            with self.subTest(priority=priority):
                result = self.service.run(prog, self.repo.repo, commit, priority=priority)
                self.assertTrue(result['passed'])
                self.assertEqual(result['steps'][0]['command'], command)
                self.assertIn('script executed', result['log_tail'])
                invocation = json.loads(calls.read_text().splitlines()[-1])
                self.assertEqual(invocation[:4], ['--user', '--scope', '--quiet', '-p'])
                self.assertIn(f'CPUWeight={weight}', invocation)
                self.assertIn(f'IOWeight={weight}', invocation)
                inner = invocation[invocation.index('--')+1:]
                if priority == 'P4':
                    self.assertIn('MemoryMax=8G', invocation)
                    self.assertEqual(inner, ['timeout', '1800', *command])
                else:
                    self.assertFalse(any(a.startswith('MemoryMax=') for a in invocation))
                    self.assertEqual(inner, command)

        # Shorten only the machine's timeout executable for a real expiry test;
        # the CI command above must still supply the historical 1800 seconds.
        timeout = bindir / 'timeout'
        timeout.write_text(f"#!{sys.executable}\nimport os,sys\n" +
                           'assert sys.argv[1] == "1800"\n' +
                           f'os.execv({shutil.which("timeout")!r}, ["timeout", ".3", *sys.argv[2:]])\n')
        timeout.chmod(0o755)
        commit = self.repo.commit('scripts/check action.ps1', 'Write-Output "started"\nStart-Sleep 60\nexit 0\n')
        result = self.service.run(prog, self.repo.repo, commit, priority='P4')
        self.assertEqual((result['passed'], result['exit_code']), (False, 124))
        # The old fallback remains valid on machines without a usable user bus.
        scope.write_text(f"#!{sys.executable}\nraise SystemExit(1)\n")
        calls.unlink()
        commit = self.repo.commit('scripts/check action.ps1', 'Write-Output "unwrapped"\nexit 0\n')
        result = self.service.run(prog, self.repo.repo, commit, priority='P4')
        self.assertTrue(result['passed'])
        self.assertIn('unwrapped', result['log_tail'])
        self.assertFalse(calls.exists())

    def test_wall_timeout_stops_a_script_that_keeps_printing(self):
        self.configure('import time; exec("while True: print(1,flush=True); time.sleep(.05)")')
        commit = self.repo.commit('source.txt', 'changed')
        prog = {**self.machine, 'gate': {**self.machine['gate'], 'timeout_s': 1, 'idle_s': 10}}
        conn = db.connect(self.repo.root / 'ledger.db')
        self.addCleanup(conn.close)
        with self.assertRaisesRegex(ci.CIError, 'wall-clock limit'):
            self.service.run(prog, self.repo.repo, commit, conn=conn)
        self.assertEqual(slots.lease_queue(conn), [])

    @unittest.skipIf(os.name == 'nt', 'both composed entrypoints execute locally on WSL')
    def test_full_ci_requires_each_platform_and_stops_at_first_failure(self):
        from drover.launch import RunnerLauncher
        prog = self.configure('import os; print("placement:"+os.environ["FIXTURE_PLACEMENT"]); '
                              'raise SystemExit(int(os.environ["FIXTURE_EXIT"]))')
        commit = self.repo.commit('source.txt', 'changed')
        for outcomes, expected in (({'windows': 0, 'wsl': 0}, ['windows', 'wsl']),
                                   ({'windows': 3, 'wsl': 0}, ['windows']),
                                   ({'windows': 0, 'wsl': 4}, ['windows', 'wsl'])):
            calls = []
            class OutcomeLauncher(RunnerLauncher):
                def launch(self, spec):
                    platform = spec['placement']
                    calls.append(platform)
                    cfg = spec['run_config']
                    spec = {**spec, 'run_config': {**cfg, 'env': {**cfg['env'],
                            'FIXTURE_PLACEMENT': platform, 'FIXTURE_EXIT': str(outcomes[platform])}}}
                    return super().launch(spec)
            with self.subTest(outcomes=outcomes):
                service = ci.CI(self.runs, OutcomeLauncher(self.runs))
                result = service.run(prog, self.repo.repo, commit, platforms=['windows', 'wsl'])
                self.assertEqual(calls, expected)
                self.assertEqual(result['passed'], all(code == 0 for code in outcomes.values()))
                self.assertEqual(result['platform'], 'windows+wsl' if result['passed'] else expected[-1])

    def test_dirty_tree_is_refused_and_releases_its_ci_runner(self):
        prog = self.configure('raise AssertionError("must not execute")')
        commit = self.repo.commit('source.txt', 'changed')
        (self.repo.repo / 'untracked').write_text('not committed')
        conn = db.connect(self.repo.root / 'ledger.db')
        self.addCleanup(conn.close)
        with self.assertRaisesRegex(ci.CIError, 'uncommitted'):
            self.service.run(prog, self.repo.repo, commit, conn=conn)
        self.assertEqual(slots.lease_queue(conn), [])

    def test_silent_script_times_out_and_releases_its_ci_runner(self):
        prog = self.configure('import time; time.sleep(60)')
        commit = self.repo.commit('source.txt', 'changed')
        prog = repos.load({**self.machine, 'gate': {**self.machine['gate'], 'idle_s': .4}})
        conn = db.connect(self.repo.root / 'ledger.db')
        self.addCleanup(conn.close)
        with self.assertRaisesRegex(ci.CIError, 'timed out'):
            self.service.run(prog, self.repo.repo, commit, conn=conn)
        self.assertEqual(slots.lease_queue(conn), [])

    def test_new_hub_adopts_full_ci_without_starting_the_script_twice(self):
        count, release = self.repo.root / 'count', self.repo.root / 'release'
        code = ('import os,pathlib,time; p=pathlib.Path(os.environ["COUNT_FILE"]); '
                'p.write_text(p.read_text()+"start\\n" if p.exists() else "start\\n"); '
                'print("started",flush=True); '\
                '\nwhile not pathlib.Path(os.environ["RELEASE_FILE"]).exists(): time.sleep(.05)\nprint("released")')
        self.configure(code)
        commit = self.repo.commit('source.txt', 'changed')
        machine = {**self.machine, 'run': {**self.machine['run'], 'env': {
            'COUNT_FILE': str(count), 'RELEASE_FILE': str(release)}}}
        command = f'from drover.ci import CI; CI({str(self.runs)!r}).run({machine!r}, {str(self.repo.repo)!r}, {commit!r})'
        with open(self.repo.root / 'caller.log', 'w') as output:
            caller = subprocess.Popen([sys.executable, '-c', command], stdout=output, stderr=output, stdin=subprocess.DEVNULL)
            self.addCleanup(lambda: caller.poll() is None and caller.kill())
            deadline = time.monotonic() + 30
            while not count.exists() and caller.poll() is None and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(count.exists(), (self.repo.root / 'caller.log').read_text())
            status_file = next(self.runs.glob('ci-*/status.json'))
            original = json.loads(status_file.read_text())
            caller.terminate()
            caller.wait(timeout=10)
            self.assertEqual(count.read_text(), 'start\n')
            release.touch()
            resumed = ci.CI(self.runs)
            resumed.resume(machine, self.repo.repo)
            result = resumed.run(machine, self.repo.repo, commit)
        self.assertTrue(result['passed'])
        self.assertEqual(count.read_text(), 'start\n')
        self.assertEqual(json.loads(status_file.read_text())['runner_pid'], original['runner_pid'])
        self.assertFalse(list(self.runs.glob('ci-*.json')))
