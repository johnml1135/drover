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
from tests.gitfix import Repo
from tests.tiers import integration

from tests.ci_fixture import Launcher
from tests.rcw_repo import MemoryRepo, MemoryRepository
from drover import placement
import tempfile

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
        self.service = ci.CI(self.runs)
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
        command = f'from drover.ci import CI; CI({str(self.runs)!r}).run({machine!r}, {str(self.repo.repo)!r}, {commit!r})'
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
            resumed = ci.CI(self.runs)
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
        with self.assertRaisesRegex(ci.CIError, 'wall-clock limit'):
            self.service.run(self.machine, self.repo.repo, commit, conn=conn)
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
        passed = self.service.run(prog, self.repo.repo, commit)
        self.assertTrue(passed['passed'])
        self.assertEqual(passed['kind'], 'docs')
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


class PolicyCITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = MemoryRepo(Path(temporary.name))
        self.source = MemoryRepository(self.repo)
        self.runs = self.repo.root / 'runs'
        self.launcher = Launcher(self.runs)
        self.service = ci.CI(self.runs, self.launcher, poll_s=0)
        self.machine = {'name': 'fixture', 'repo': str(self.repo.repo), 'run': {'env': {}}}
        self.raw_modes = ''
        original = config.get()
        self.addCleanup(config.use, original)
        config.use(config.validate({'paths': {'state_dir': str(self.repo.root / 'state')}}))
        from drover import disk
        contexts = (
            patch.object(repos, 'load', self.source.load),
            patch.object(placement, 'prepare_worktree'),
            patch.object(ci, 'head', self.source.head),
            patch.object(ci, 'dirty', side_effect=lambda repo, **kw: self.source.dirty(repo)),
            patch.object(ci, 'contains', side_effect=lambda path, base, sha:
                         (self.repo.head(base), self.repo.ancestor(base, sha))),
            patch.object(ci, 'changed_paths', self.source.changed_paths),
            patch.object(ci.CI, '_diff_lines', side_effect=self.source.diff_lines),
            patch.object(ci, 'git', side_effect=self.raw_diff),
            patch.object(disk, 'read', return_value=[dict(name='fixture', path=str(self.repo.root), free_gb=1000., level='ok')]),
            patch('subprocess.Popen', side_effect=AssertionError('policy tests must not start a process')),
            patch('time.sleep', side_effect=AssertionError('policy tests must not sleep')),
        )
        for context in contexts:
            context.start()
            self.addCleanup(context.stop)

    def raw_diff(self, repo, *args, **kw):
        self.assertEqual(args[:4], ('diff', '--raw', '--no-renames', '-z'))
        return subprocess.CompletedProcess(args, 0, self.raw_modes, '')

    repo_config = CITests.repo_config

    def configure(self, *, docs=False, gate=None, scripts=None):
        self.repo.commit('.drover/scripts/check action.ps1', '# execution adapter supplies the outcome\n')
        self.repo.commit('.drover/config.toml', self.repo_config(
            gate={'docs_only': docs, **(gate or {})}, scripts=scripts))
        self.repo.checkout(self.repo.repo, 'topic', 'HEAD')
        return repos.load(self.machine)

    def change(self, name='source.txt', text='changed'):
        return self.repo.commit(name, text)

    def conn(self):
        conn = db.connect(self.repo.root / 'ledger.db')
        self.addCleanup(conn.close)
        return conn

    def test_exit_code_and_clean_tree_determine_the_result(self):
        prog = self.configure()
        commit = self.change()
        self.launcher.code, self.launcher.output = 7, 'PASSED summary\n'
        result = self.service.run(prog, self.repo.repo, commit)
        self.assertEqual((result['passed'], result['exit_code'], result['kind']), (False, 7, 'full'))
        self.assertEqual(result['log_tail'], ['PASSED summary'])
        self.launcher.code = 0
        self.launcher.after_command = lambda: (self.repo.repo / 'source.txt').write_text('changed-again')
        result = self.service.run(prog, self.repo.repo, commit)
        self.assertEqual((result['exit_code'], result['tree_unchanged'], result['passed']), (0, False, False))

    def test_a_passing_result_does_not_authorize_cancelled_execution(self):
        prog = self.configure()
        commit = self.change()
        for returncode in (-15, 0):
            with self.subTest(returncode=returncode):
                self.launcher.execution = dict(status='error', returncode=returncode)
                with self.assertRaises(ci.CIError):
                    self.service.run(prog, self.repo.repo, commit)

    def test_docs_classification_is_shared_by_checks_and_full_ci(self):
        prog = self.configure(docs=True)
        commit = self.change('docs/readme.md', 'documentation\n')
        for kind in ('checks', 'full'):
            result = self.service.run(prog, self.repo.repo, commit, kind=kind)
            self.assertTrue(result['passed'])
            self.assertEqual(result['kind'], 'docs')
        self.repo.commit('.drover/scripts/docs.ps1', '# docs command\n')
        self.repo.commit('.drover/config.toml', self.repo_config(scripts={
            'test': '.drover/scripts/check action.ps1', 'docs': '.drover/scripts/docs.ps1'}))
        self.repo.branches['main'] = self.repo.head()
        commit = self.change('docs/next.md', 'more docs\n')
        self.launcher.output = 'docs script\n'
        result = self.service.run(prog, self.repo.repo, commit)
        self.assertIn('--check', result['steps'][0]['command'])
        self.assertEqual(result['steps'][1]['command'][-1], '.drover/scripts/docs.ps1')
        self.assertIn('docs script', result['log_tail'])

    def test_docs_policy_through_prepare(self):
        self.configure(docs=True)
        original = self.repo.head('main')
        for files, policy, expected in (
                ({'docs/readme.md': 'docs\n', 'source.py': 'code\n'}, {}, 'checks'),
                ({'.github/readme.md': 'docs\n'}, {}, 'checks'),
                ({'docs/readme.md': 'docs\n'}, {'docs_only': False}, 'checks'),
                ({'guide/readme.rst': 'docs\n'}, {'docs_only_globs': ['guide/**']}, 'docs'),
                ({'docs/readme.md': 'docs\n'}, {'docs_only_globs': ['guide/**']}, 'checks'),
                ({'docs/readme.md': 'docs\n'}, {'docs_only_globs': []}, 'checks'),
                ({'guide/readme.md': 'docs\n'}, {'docs_only_globs': ['**/*.md', '!guide/**']}, 'checks')):
            with self.subTest(files=files, policy=policy):
                self.repo.checkout(self.repo.repo, 'case', original)
                self.repo.commit('.drover/config.toml', self.repo_config(gate=policy))
                self.repo.branches['main'] = self.repo.head()
                for path, content in files.items():
                    commit = self.change(path, content)
                context = self.service.prepare(self.machine, self.repo.repo, commit, 'docs-policy')
                self.assertEqual(context['kind'], expected)
                plan = json.loads((self.runs / 'docs-policy' / 'plan.json').read_text())
                self.assertEqual('--check' in plan['commands'][0], expected == 'docs')

    def test_docs_symlink_addition_and_removal_require_checks(self):
        self.configure(docs=True)
        for before, after, name in (('000000', '120000', 'add'), ('120000', '000000', 'remove')):
            with self.subTest(mode=name):
                commit = self.change('docs/link.md', name)
                self.raw_modes = f':{before} {after} old new A\0docs/link.md\0'
                context = self.service.prepare(self.machine, self.repo.repo, commit, 'symlink-' + name)
                self.assertEqual(context['kind'], 'checks')
                self.repo.branches['main'] = commit

    def test_docs_markdown_hard_breaks_pass_the_composed_command(self):
        prog = self.configure(docs=True)
        commit = self.change('docs/readme.md', 'line with hard break  \nnext line\n')
        result = self.service.run(prog, self.repo.repo, commit)
        self.assertTrue(result['passed'])
        self.assertEqual(result['kind'], 'docs')
        self.assertEqual(result['steps'][0]['command'],
                         ['git', '-c', 'core.whitespace=-blank-at-eol', 'diff', '--check', 'main...' + commit])

    def test_heartbeat_failure_keeps_the_live_runner_for_adoption(self):
        prog = self.configure()
        commit = self.change()
        conn = self.conn()
        count = self.repo.root / 'count'
        self.launcher.pending = True
        self.launcher.on_start = lambda: count.write_text(count.read_text() + 'start\n' if count.exists() else 'start\n')
        with patch.object(slots, 'lease_heartbeat', side_effect=sqlite3.OperationalError('database is locked')):
            with self.assertRaises(ci.CIInterrupted) as raised:
                self.service.run(prog, self.repo.repo, commit, conn=conn)
        self.assertIsInstance(raised.exception.__cause__, sqlite3.OperationalError)
        self.assertEqual(count.read_text(), 'start\n')
        self.assertEqual(len(list(self.runs.glob('ci-*.json'))), 1)
        self.assertEqual(len(slots.lease_queue(conn)), 1)
        with patch.object(slots, 'lease_acquire', side_effect=sqlite3.OperationalError('database is locked')):
            with self.assertRaises(ci.CIInterrupted):
                ci.CI(self.runs, self.launcher).resume(prog, self.repo.repo, conn=conn)
        self.assertEqual(len(list(self.runs.glob('ci-*.json'))), 1)
        self.assertEqual(len(slots.lease_queue(conn)), 1)
        self.launcher.released = True
        resumed = ci.CI(self.runs, self.launcher)
        resumed.resume(prog, self.repo.repo, conn=conn)
        self.assertTrue(resumed.run(prog, self.repo.repo, commit, conn=conn)['passed'])
        self.assertEqual(count.read_text(), 'start\n')
        self.assertEqual(len(self.launcher.specs), 1)
        self.assertEqual(slots.lease_queue(conn), [])
        self.assertFalse(list(self.runs.glob('ci-*.json')))

    def test_docs_script_cannot_bypass_diff_check(self):
        self.configure(docs=True, scripts={'test': '.drover/scripts/check action.ps1', 'docs': '.drover/scripts/docs.ps1'})
        for content, diagnostic in (('<<<<<<< ours\ntext\n=======\nother\n>>>>>>> theirs\n', 'conflict marker'),
                                    ('text\n\n', 'blank line at EOF')):
            commit = self.change('docs/readme.md', content)
            self.launcher.code, self.launcher.output = 2, diagnostic + '\n'
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
        from types import SimpleNamespace
        prog = self.configure()
        commit = self.change()
        command = repos.script_argv(prog, platform=PLATFORM)
        self.launcher.output = 'script executed\n'
        self.launcher.wrap_command = placement.ci_command
        for priority, weight in (('P0', 1000), ('P1', 1000), ('P2', 100), ('P3', 30), ('P4', 10)):
            with self.subTest(priority=priority), patch.object(placement.shutil, 'which', return_value='systemd-run'), \
                    patch.object(placement, 'subprocess', SimpleNamespace(run=lambda *a, **kw: SimpleNamespace(returncode=0))):
                result = self.service.run(prog, self.repo.repo, commit, priority=priority)
                invocation = self.launcher.commands[-1]
                self.assertEqual(invocation[:5], ['systemd-run', '--user', '--scope', '--quiet', '-p'])
                self.assertIn(f'CPUWeight={weight}', invocation)
                self.assertIn(f'IOWeight={weight}', invocation)
                inner = invocation[invocation.index('--')+1:]
                if priority == 'P4':
                    self.assertIn('MemoryMax=8G', invocation)
                    self.assertEqual(inner, ['timeout', '1800', *command])
                else:
                    self.assertFalse(any(a.startswith('MemoryMax=') for a in invocation))
                    self.assertEqual(inner, command)
                self.assertTrue(result['passed'])
                self.assertEqual(result['steps'][0]['command'], command)
                self.assertIn('script executed', result['log_tail'])
        self.launcher.code = 124
        with patch.object(placement.shutil, 'which', return_value='systemd-run'), \
                patch.object(placement, 'subprocess', SimpleNamespace(run=lambda *a, **kw: SimpleNamespace(returncode=0))):
            result = self.service.run(prog, self.repo.repo, commit, priority='P4')
        self.assertEqual((result['passed'], result['exit_code']), (False, 124))
        self.assertIn('1800', self.launcher.commands[-1])
        self.launcher.code, self.launcher.output = 0, 'unwrapped\n'
        with patch.object(placement.shutil, 'which', return_value='systemd-run'), \
                patch.object(placement, 'subprocess', SimpleNamespace(run=lambda *a, **kw: SimpleNamespace(returncode=1))):
            result = self.service.run(prog, self.repo.repo, commit, priority='P4')
        self.assertEqual(self.launcher.commands[-1], command)
        self.assertTrue(result['passed'])
        self.assertIn('unwrapped', result['log_tail'])

    def test_full_ci_requires_each_platform_and_stops_at_first_failure(self):
        prog = self.configure()
        commit = self.change()
        for outcomes, expected in (({'windows': 0, 'wsl': 0}, ['wsl', 'windows']),
                                   ({'windows': 3, 'wsl': 0}, ['wsl', 'windows']),
                                   ({'windows': 0, 'wsl': 4}, ['wsl'])):
            with self.subTest(outcomes=outcomes):
                self.launcher.specs.clear()
                self.launcher.outcomes = outcomes
                result = self.service.run(prog, self.repo.repo, commit, platforms=['windows', 'wsl'])
                self.assertEqual([s['placement'] for s in self.launcher.specs], expected)
                self.assertEqual(result['passed'], all(code == 0 for code in outcomes.values()))
                self.assertEqual(result['platform'], 'windows+wsl' if result['passed'] else expected[-1])

    def test_dirty_tree_is_refused_and_releases_its_ci_runner(self):
        prog = self.configure()
        commit = self.change()
        (self.repo.repo / 'untracked').write_text('not committed')
        conn = self.conn()
        with self.assertRaisesRegex(ci.CIError, 'uncommitted'):
            self.service.run(prog, self.repo.repo, commit, conn=conn)
        self.assertEqual(self.launcher.commands, [])
        self.assertEqual(slots.lease_queue(conn), [])
