"""CI policy against an in-memory repository: what kind of run a change gets, and what a saved run becomes."""
import json
import os
import sqlite3
from pathlib import Path
import subprocess
import time
import unittest
from unittest.mock import patch
import tempfile

from drover import ci, config, durable, repos, runner
from drover.ledger import db, ci as slots
from tests import test_ci
from tests.fakeci import resources_go
from tests.ci_fixture import Launcher
from tests.rcw_repo import MemoryRepo, MemoryRepository
from drover import placement, process
PLATFORM = 'windows' if os.name == 'nt' else 'wsl'


def probe_answers(code):
    """`process.subprocess` answering every command, the systemd-run probe among them, with exit `code`."""
    from types import SimpleNamespace
    return SimpleNamespace(run=lambda argv, **kw: subprocess.CompletedProcess(argv, code, b'', b''),
                           DEVNULL=subprocess.DEVNULL, TimeoutExpired=subprocess.TimeoutExpired)


class PolicyCITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = MemoryRepo(Path(temporary.name))
        self.source = MemoryRepository(self.repo)
        self.runs = self.repo.root / 'runs'
        self.launcher = Launcher(self.runs)
        self.service = ci.CI(self.runs, self.launcher, poll_s=0, resource_check=resources_go)
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
            patch.object(ci, '_out', side_effect=self.raw_diff),
            patch.object(disk, 'read', return_value=[dict(name='fixture', path=str(self.repo.root), free_gb=1000., level='ok')]),
            patch('subprocess.Popen', side_effect=AssertionError('policy tests must not start a process')),
            patch('time.sleep', side_effect=AssertionError('policy tests must not sleep')),
        )
        for context in contexts:
            context.start()
            self.addCleanup(context.stop)

    def raw_diff(self, repo, *args, **kw):
        self.assertEqual(args[:4], ('diff', '--raw', '--no-renames', '-z'))
        return self.raw_modes

    repo_config = test_ci.CITests.repo_config

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
                result = self.service.run(prog, self.repo.repo, commit)
                self.assertEqual((result['outcome'], result['passed']), ('infrastructure-failed', False))

    def test_docs_classification_is_shared_by_checks_and_full_ci(self):
        prog = self.configure(docs=True)
        commit = self.change('docs/readme.md', 'documentation\n')
        for kind in ('checks', 'full'):
            result = self.service.run(prog, self.repo.repo, commit, kind=kind)
            self.assertTrue(result['passed'])
            self.assertEqual(result['kind'], kind)
            if kind == 'full':
                self.assertEqual(result['steps'][-1]['command'][-1], '.drover/scripts/check action.ps1')
        self.repo.commit('.drover/scripts/docs.ps1', '# docs command\n')
        self.repo.commit('.drover/config.toml', self.repo_config(scripts={
            'test': '.drover/scripts/check action.ps1', 'docs': '.drover/scripts/docs.ps1'}))
        self.repo.branches['main'] = self.repo.head()
        commit = self.change('docs/next.md', 'more docs\n')
        self.launcher.output = 'docs script\n'
        result = self.service.run(prog, self.repo.repo, commit, kind='checks')
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
                self.assertEqual(context['kind'], 'checks')
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
        result = self.service.run(prog, self.repo.repo, commit, kind='checks')
        self.assertTrue(result['passed'])
        self.assertEqual(result['kind'], 'checks')
        self.assertEqual(result['steps'][0]['command'],
                         ['git', '-c', 'core.whitespace=-blank-at-eol', 'diff', '--check', 'main...' + commit])

    def test_a_gate_the_compaction_stopped_runs_again_at_no_retrys_cost(self):
        prog = self.configure()
        commit = self.change()
        conn = self.conn()
        finish = self.launcher.finish

        def stopped(spec):      # what the real runner leaves after a stop file with this reason
            durable.write_json(runner.run_dir(self.runs, spec['run_id']) / runner.STATUS_FILE,
                               dict(state='killed', reason=ci.COMPACTION_STOP, exit_code=None, nonce=spec['nonce'],
                                    run_id=spec['run_id']), sync=False)
            return dict(status='error', returncode=None, record=None, text='', handoff=None, timeout=None, usage={})
        self.launcher.finish = stopped
        with self.assertRaises(ci.CIDeferred) as raised:       # landing queues this; a retry is not spent
            self.service.run(prog, self.repo.repo, commit, conn=conn, platforms=['wsl'])
        self.assertEqual((raised.exception.detail['outcome'], raised.exception.detail['sha']), ('deferred', commit))
        requests = list(self.runs.glob('ci-*.json'))
        self.assertEqual(len(requests), 1)
        self.assertEqual({'context', 'execution', 'ticket', 'held_reason'} & json.loads(requests[0].read_text()).keys(), set())
        self.assertEqual(slots.lease_queue(conn), [])
        self.assertEqual(ci.active_wsl(self.runs), {})          # the next compaction has nothing to wait for
        self.launcher.finish = finish
        resumed = ci.CI(self.runs, self.launcher, poll_s=0, resource_check=resources_go)
        resumed.resume(prog, self.repo.repo, conn=conn)
        self.assertTrue(resumed.run(prog, self.repo.repo, commit, conn=conn, platforms=['wsl'])['passed'])
        first, second = self.launcher.specs
        self.assertNotEqual(first['run_id'], second['run_id'])
        self.assertFalse(list(self.runs.glob('ci-*.json')))

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
                ci.CI(self.runs, self.launcher, resource_check=resources_go).resume(prog, self.repo.repo, conn=conn)
        self.assertEqual(len(list(self.runs.glob('ci-*.json'))), 1)
        self.assertEqual(len(slots.lease_queue(conn)), 1)
        self.launcher.released = True
        resumed = ci.CI(self.runs, self.launcher, resource_check=resources_go)
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
                    patch.object(process, 'subprocess', probe_answers(0)):
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
                patch.object(process, 'subprocess', probe_answers(0)):
            result = self.service.run(prog, self.repo.repo, commit, priority='P4')
        self.assertEqual((result['passed'], result['exit_code']), (False, 124))
        self.assertIn('1800', self.launcher.commands[-1])
        self.launcher.code, self.launcher.output = 0, 'unwrapped\n'
        with patch.object(placement.shutil, 'which', return_value='systemd-run'), \
                patch.object(process, 'subprocess', probe_answers(1)):
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
        result = self.service.run(prog, self.repo.repo, commit, conn=conn)
        self.assertEqual(result['outcome'], 'deferred')
        self.assertIn('uncommitted', result['reason'])
        self.assertEqual(self.launcher.commands, [])
        self.assertEqual(slots.lease_queue(conn), [])


class GitFailures(unittest.TestCase):
    """head, dirty, contains and changed_paths have no entry to catch at: landing.py:209 needs CIError from each."""

    def test_head_dirty_contains_changed_paths_raise_ci_error_with_todays_type(self):
        from tests.fakeci import git_answers
        repo = tempfile.mkdtemp()
        self.addCleanup(__import__('shutil').rmtree, repo, True)
        calls = (('head', lambda: ci.head(repo)), ('dirty', lambda: ci.dirty(repo)),
                 ('contains', lambda: ci.contains(repo, 'main', 'abc')),
                 ('changed_paths', lambda: ci.changed_paths(repo, 'main')))

        def timeout(repo, *args, timeout):
            raise subprocess.TimeoutExpired(['git', *args], timeout)

        def missing(repo, *args, timeout):
            raise FileNotFoundError(2, 'No such file or directory')

        def exits(repo, *args, timeout):
            return subprocess.CompletedProcess(args, 128, '', 'fatal: bad object\n')
        for kind, reply, message in (('timeout', timeout, 'timed out after 60 seconds'),
                                     ('unavailable', missing, 'unavailable'),
                                     ('exit', exits, 'fatal: bad object')):
            for name, call in calls:
                with self.subTest(kind=kind, name=name), git_answers(reply):
                    if (kind, name) == ('exit', 'contains'):
                        self.assertEqual(call(), (None, False))      # a base that does not resolve is not found
                        continue
                    with self.assertRaisesRegex(ci.CIError, message) as raised:
                        call()
                    self.assertIs(type(raised.exception), ci.CIError)


if __name__ == '__main__':
    unittest.main()
