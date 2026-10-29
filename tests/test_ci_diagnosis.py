"""Diagnostic selection and failure metadata through the CI boundary."""
import io
import json
import os
import tempfile
from contextlib import redirect_stdout
from unittest.mock import patch
import unittest
from drover import ci
from tests import test_ci as fixture

class DiagnosticCI(unittest.TestCase):
    setUp = fixture.PolicyCITests.setUp
    raw_diff = fixture.PolicyCITests.raw_diff
    repo_config = fixture.PolicyCITests.repo_config
    configure = fixture.PolicyCITests.configure
    change = fixture.PolicyCITests.change

    def test_diagnostic_runs_only_selected_module_and_platform(self):
        prog = self.configure()
        self.repo.commit('.drover/scripts/check action.ps1', '# DROVER_CI_DIAGNOSTIC_V1\n')
        commit = self.repo.head()
        self.service.run(prog, self.repo.repo, commit, platforms=['wsl'], base=commit,
                         module='tests.test_ci', tier='integration')
        spec, = self.launcher.specs
        self.assertEqual(spec['placement'], 'wsl')
        self.assertEqual(spec['run_config']['env']['DROVER_CI_MODULE'], 'tests.test_ci')
        self.assertEqual(spec['run_config']['env']['DROVER_CI_TIER'], 'integration')
        self.assertEqual(json.loads((self.runs / spec['run_id'] / 'plan.json').read_text())['sha'], commit)

    def test_full_candidate_clears_inherited_diagnostic_selector(self):
        prog = self.configure()
        commit = self.change()
        with patch.dict(os.environ, {'DROVER_CI_MODULE': 'tests.test_ci', 'DROVER_CI_TIER': 'integration'}):
            self.service.run(prog, self.repo.repo, commit, platforms=['wsl'])
        env = self.launcher.specs[0]['run_config']['env']
        self.assertEqual((env['DROVER_CI_MODULE'], env['DROVER_CI_TIER']), ('', ''))

    def test_diagnostic_never_uses_docs_shortcut(self):
        prog = self.configure(docs=True)
        self.repo.commit('.drover/scripts/check action.ps1', '# DROVER_CI_DIAGNOSTIC_V1\n')
        self.repo.branches['main'] = self.repo.head()
        commit = self.change('docs/readme.md', 'documentation')
        self.service.run(prog, self.repo.repo, commit, platforms=['wsl'], module='tests.test_ci', tier='integration')
        plan = json.loads((self.runs / self.launcher.specs[0]['run_id'] / 'plan.json').read_text())
        self.assertEqual(plan['kind'], 'full')
        self.assertEqual(plan['commands'][0][-1], '.drover/scripts/check action.ps1')

    def test_diagnostic_refuses_base_without_selector(self):
        prog = self.configure()
        with self.assertRaises(ci.CIInfrastructure):
            self.service.run(prog, self.repo.repo, self.repo.head(), platforms=['wsl'],
                             module='tests.test_ci', tier='integration')
        self.assertEqual(self.launcher.specs, [])

    def test_test_runner_emits_structured_failure_for_one_module(self):
        from scripts import test as suite
        failed = dict(module='tests.test_ci', code=1, ran=1, skipped=0, seconds=1, output='failed')
        output = io.StringIO()
        with patch.dict(os.environ, {'DROVER_TEST_TMPDIR': str(self.repo.root / 'tmp')}), \
                patch.object(suite, 'load_timings', return_value={}), \
                patch.object(suite, 'run_module', return_value=failed), redirect_stdout(output):
            self.assertEqual(suite.main(['-j', '1', '--tier', 'integration', 'tests.test_ci']), 1)
        self.assertIn('DROVER_CI_FAILURE integration tests.test_ci', output.getvalue())

    def test_runner_termination_uncertainty_inside_failed_module_is_infrastructure(self):
        prog = self.configure()
        commit = self.change()
        self.launcher.code = 1
        self.launcher.output = ('drover.ci.CIInterrupted: termination-uncertain: missing nonce-matched runner status\n'
                                'DROVER_CI_FAILURE integration tests.test_ci\n')
        result = self.service.run(prog, self.repo.repo, commit, platforms=['wsl'])
        self.assertTrue(result['infra'])
        self.assertIn('termination-uncertain', result['reason'])
        self.assertEqual(result['platform'], 'wsl')

    def test_failed_module_metadata_survives_ci_result(self):
        prog = self.configure()
        commit = self.change()
        self.launcher.code = 1
        self.launcher.output = 'DROVER_CI_FAILURE integration tests.test_ci\n'
        result = self.service.run(prog, self.repo.repo, commit, platforms=['wsl'])
        self.assertEqual((result['module'], result['tier'], result['platform']),
                         ('tests.test_ci', 'integration', 'wsl'))


class HubDiagnostic(unittest.TestCase):
    def test_hub_diagnosis_uses_base_sha_platform_module_and_tier(self):
        from types import SimpleNamespace
        from hub import dispatch
        calls = []
        service = object.__new__(dispatch.LandingService)
        service.settings = SimpleNamespace(runs_dir=self.enterContext(tempfile.TemporaryDirectory()))
        service.ci = SimpleNamespace(resume=lambda *a, **k: None,
                                     run=lambda *a, **k: calls.append((a, k)) or {'status': 'passed'})
        prog = {'unused': True}
        def run_batch(conn, repo, gate, **kwargs):
            gate('work', 'a' * 40, [], platform='wsl', signature='failure',
                 module='tests.test_ci', tier='integration')
            with self.assertRaises(ci.CIInfrastructure):
                gate('work', 'a' * 40, [], platform='wsl', signature='unknown')
            return {'outcome': 'red'}
        with patch('drover.notifications.controls.admission', return_value={'epoch': 0}), \
                patch.object(dispatch.repos, 'repo_path', return_value='.'), \
                patch.object(dispatch.repos, 'native', side_effect=lambda x: x), \
                patch.object(dispatch.repos, 'landing_config', return_value={'push': False, 'policy': 'single'}), \
                patch.object(dispatch.repos, 'default_branch', return_value='main'), \
                patch.object(dispatch.repos, 'worktree_root', return_value='/tmp'), \
                patch.object(dispatch.repos, 'remote_name', return_value='origin'), \
                patch.object(dispatch.repos, 'gate_platforms', return_value=['windows', 'wsl']), \
                patch.object(dispatch.disk, 'read', return_value=[]), \
                patch.object(dispatch.disk, 'admission', return_value={'verdict': 'go'}), \
                patch.object(dispatch.ops, 'set_setting'), patch.object(service, '_write_gate'), \
                patch.object(dispatch.landing, 'run_batch', side_effect=run_batch):
            service._land_repo(None, 'p', prog)
        self.assertEqual(len(calls), 1)
        args, kwargs = calls[0]
        self.assertEqual(args[2], 'a' * 40)
        self.assertEqual((kwargs['base'], kwargs['platforms'], kwargs['module'], kwargs['tier']),
                         ('a' * 40, ['wsl'], 'tests.test_ci', 'integration'))
