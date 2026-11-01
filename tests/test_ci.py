"""CI policy runs against scripted execution; bounded real adapter contracts."""
import json
import os
import sqlite3
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from drover import ci, config, repos, runner
from drover.ledger import db, ci as slots
from tests.fakeci import resources_go
from tests.gitfix import Repo
from tests.tiers import integration


PLATFORM = 'windows' if os.name == 'nt' else 'wsl'


class CITests(unittest.TestCase):
    def setUp(self):
        self.repo = Repo(ignore='__pycache__/\n')
        self.addCleanup(self.repo.cleanup)
        from drover import config, disk
        import copy
        import os
        environment = patch.dict(os.environ, {'DROVER_HOME': str(self.repo.root / 'state')})
        environment.start()
        self.addCleanup(environment.stop)
        original_config = config.get()
        self.addCleanup(config.use, original_config)
        cfg = copy.deepcopy(original_config)
        cfg['paths']['state_dir'] = str(self.repo.root / 'state')
        cfg['repos_dir'] = str(self.repo.root / 'state' / 'repos')
        config.use(cfg)
        # Detached callers cannot inherit the parent's disk.read mock. Give them
        # the same unrestricted fixture admission without consulting live drives.
        state = self.repo.root / 'state'
        state.mkdir()
        (state / 'config.toml').write_text(config.dumps({'trackers': [
            {'kind': 'disk', 'name': tracker['name'], 'enabled': False}
            for tracker in cfg['trackers'] if tracker['kind'] == 'disk']}), encoding='utf-8')
        for context in (patch.object(disk, 'read', return_value=[dict(name='fixture', path=str(self.repo.root), free_gb=1000., level='ok')]),):
            context.start()
            self.addCleanup(context.stop)
        self.runs = self.repo.root / 'runs'
        self.service = ci.CI(self.runs, resource_check=resources_go)
        self.machine = {'name': 'fixture', 'repo': str(self.repo.repo), 'run': {'env': {}}}

    def repo_config(self, *, scripts=None, gate=None):
        return config.dumps({'schema_version': 1,
                             'scripts': scripts if scripts is not None else {'test': '.drover/scripts/check action.ps1'},
                             'run': {'platforms': [PLATFORM], 'default': PLATFORM},
                             'gate': {'platform': PLATFORM, 'timeout_s': 30, 'idle_s': 10, **(gate or {})}})

    def configure(self, code, docs=False):
        exe = sys.executable.replace("'", "''")
        self.repo.commit('.drover/scripts/check action.ps1',
                         f"if ($args.Count) {{ exit 97 }}\n& '{exe}' -c '{code.replace(chr(39), chr(39)*2)}'\nexit $LASTEXITCODE\n")
        self.repo.commit('.drover/config.toml', self.repo_config(gate={'docs_only': docs}))
        self.repo.git('checkout', '-b', 'topic')
        return repos.load(self.machine)

    @integration
    def test_composed_wsl_command_runs_a_tiny_unittest_project_verbatim(self):
        exe = sys.executable.replace("'", "''")
        self.repo.commit('.drover/scripts/check action.ps1',
                         'if ($args.Count) { exit 97 }\n'
                         f"& '{exe}' -m unittest discover -s sample_tests\nexit $LASTEXITCODE\n")
        self.repo.commit('sample_tests/test_sample.py',
                         'import json,os,pathlib,unittest\nclass Sample(unittest.TestCase):\n'
                         ' def test_context(self):\n'
                         '  self.assertEqual(os.environ["DROVER_CI_KIND"], "checks")\n'
                         '  self.assertIn("source.txt", json.loads(os.environ["DROVER_CHANGED_FILES"]))\n'
                         '  self.assertTrue(pathlib.Path(".drover/config.toml").is_file())\n'
                         '  print("fixture: passed")\n')
        self.repo.commit('.drover/config.toml', self.repo_config(gate={'docs_only': False}))
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

    @integration
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
                ci.CI(self.runs, launcher, resource_check=resources_go).run(prog, self.repo.repo, commit, conn=conn)
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
                ci.CI(self.runs, launcher, resource_check=resources_go).resume(prog, self.repo.repo, conn=conn)
        self.assertEqual(len(list(self.runs.glob('ci-*.json'))), 1)
        self.assertEqual(len(slots.lease_queue(conn)), 1)
        resumed = ci.CI(self.runs, launcher, resource_check=resources_go)
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

    @integration
    def test_new_hub_adopts_full_ci_without_starting_the_script_twice(self):
        count, release = self.repo.root / 'count', self.repo.root / 'release'
        self.addCleanup(release.touch)
        code = ('import os,pathlib,time; p=pathlib.Path(os.environ["COUNT_FILE"]); '
                'p.write_text(p.read_text()+"start\\n" if p.exists() else "start\\n"); '
                'print("started",flush=True); '\
                '\nwhile not pathlib.Path(os.environ["RELEASE_FILE"]).exists(): time.sleep(.05)\nprint("released")')
        self.configure(code)
        commit = self.repo.commit('source.txt', 'changed')
        machine = {**self.machine, 'run': {**self.machine['run'], 'env': {
            'COUNT_FILE': str(count), 'RELEASE_FILE': str(release)}}}
        command = f'from drover.ci import CI; CI({str(self.runs)!r}, resource_check=lambda **_: dict(verdict="go", reasons=[])).run({machine!r}, {str(self.repo.repo)!r}, {commit!r})'
        with open(self.repo.root / 'caller.log', 'w') as output:
            caller = subprocess.Popen([sys.executable, '-c', command], stdout=output, stderr=output, stdin=subprocess.DEVNULL)
            self.addCleanup(lambda: caller.poll() is None and caller.kill())
            deadline = time.monotonic() + 30
            original = None
            while caller.poll() is None and time.monotonic() < deadline:
                requests = list(self.runs.glob('ci-*.json'))
                saved = ci.read_result(requests[0]) if requests else None
                spec = (saved or {}).get('context', {}).get('spec', {})
                status = runner.read_status(self.runs, spec['run_id']) if spec else None
                if (count.exists() and status and status.get('nonce') == spec['nonce']
                        and status.get('state') == 'running'):
                    original = status
                    break
                time.sleep(.05)
            self.assertIsNotNone(original, (self.repo.root / 'caller.log').read_text())
            status_file = self.runs / original['run_id'] / 'status.json'
            caller.terminate()
            caller.wait(timeout=10)
            self.assertEqual(count.read_text(), 'start\n')
            release.touch()
            resumed = ci.CI(self.runs, resource_check=resources_go)
            resumed.resume(machine, self.repo.repo)
            result = resumed.run(machine, self.repo.repo, commit)
        self.assertTrue(result['passed'])
        self.assertEqual(count.read_text(), 'start\n')
        self.assertEqual(json.loads(status_file.read_text())['runner_pid'], original['runner_pid'])
        self.assertFalse(list(self.runs.glob('ci-*.json')))


    @integration
    def test_wall_timeout_stops_a_script_that_keeps_printing_and_releases_its_ci_runner(self):
        pulse = self.repo.root / 'pulse'
        code = ('import pathlib,time; p=pathlib.Path(' + json.dumps(str(pulse)) + '); '
                '\nwhile True: p.write_text(str(time.time())); print("watchdog pulse",flush=True); time.sleep(.05)')
        self.configure(code)
        self.repo.commit('.drover/config.toml', self.repo_config(gate={'timeout_s': 5, 'idle_s': 10}))
        commit = self.repo.commit('source.txt', 'changed')
        conn = db.connect(self.repo.root / 'ledger.db')
        self.addCleanup(conn.close)
        result = self.service.run(self.machine, self.repo.repo, commit, conn=conn)
        self.assertEqual((result['outcome'], result['passed'], result['reason']), ('infrastructure-failed', False, 'timeout'))
        self.assertIn('wall-clock limit', '\n'.join(result['log_tail']))
        status_file = next(self.runs.glob('ci-*/status.json'))
        status = json.loads(status_file.read_text())
        self.assertEqual(status['state'], 'timeout')
        self.assertIn('wall-clock limit', status['reason'])
        self.assertGreaterEqual((status_file.parent / 'out.log').read_text().count('watchdog pulse'), 2)
        last_pulse = pulse.read_text()
        time.sleep(.2)
        self.assertEqual(pulse.read_text(), last_pulse, 'timed-out script is still executing')
        self.assertEqual(slots.lease_queue(conn), [])
        self.assertFalse(list(self.runs.glob('ci-*.json')))

    @integration
    def test_documentation_diff_guard_contract(self):
        prog = self.configure('raise SystemExit(99)', docs=True)
        self.repo.commit('.drover/scripts/docs.ps1', 'Write-Output "docs script ran"\nexit 0\n')
        self.repo.commit('.drover/config.toml', self.repo_config(scripts={
            'test': '.drover/scripts/check action.ps1', 'docs': '.drover/scripts/docs.ps1'}))
        self.repo.git('branch', '-f', 'main', 'HEAD')
        commit = self.repo.commit('docs/readme.md', 'line with hard break  \nnext line\n')
        passed = self.service.run(prog, self.repo.repo, commit, kind='checks')
        self.assertTrue(passed['passed'])
        self.assertEqual(passed['kind'], 'checks')
        for content, diagnostic in (('<<<<<<< ours\ntext\n=======\nother\n>>>>>>> theirs\n', 'conflict marker'),
                                    ('text\n\n', 'blank line at EOF')):
            commit = self.repo.commit('docs/readme.md', content)
            for kind in ('checks', 'full'):
                with self.subTest(content=diagnostic, kind=kind):
                    result = self.service.run(self.machine, self.repo.repo, commit, kind=kind)
                    self.assertFalse(result['passed'])
                    self.assertEqual(len(result['steps']), 1)
                    self.assertIn('--check', result['steps'][0]['command'])
                    self.assertIn(diagnostic, '\n'.join(result['log_tail']))
                    self.assertNotIn('docs script ran', result['log_tail'])
        blob = self.repo.git('hash-object', '-w', '--stdin', input='../base.txt\n')
        self.repo.git('update-index', '--add', '--cacheinfo', f'120000,{blob},docs/link.md')
        self.repo.git('commit', '-qm', 'add symlink')
        commit = self.repo.git('rev-parse', 'HEAD')
        self.assertEqual(self.service.prepare(self.machine, self.repo.repo, commit, 'symlink-add')['kind'], 'checks')
        self.repo.git('branch', '-f', 'main', commit)
        self.repo.git('update-index', '--force-remove', 'docs/link.md')
        self.repo.git('commit', '-qm', 'remove symlink')
        commit = self.repo.git('rev-parse', 'HEAD')
        self.assertEqual(self.service.prepare(self.machine, self.repo.repo, commit, 'symlink-remove')['kind'], 'checks')


if __name__ == '__main__':
    unittest.main()
