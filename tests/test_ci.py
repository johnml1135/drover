"""CI's public interface, using repo scripts and the real composed runner command."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

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
        self.assertEqual(result['steps'][0]['command'][-1], 'scripts/docs.ps1')
        self.assertIn('docs script', result['log_tail'])

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
            caller = subprocess.Popen([sys.executable, '-c', command], stdout=output, stderr=output)
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
