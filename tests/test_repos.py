"""Private repo discovery, compatibility and explicit examples."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from drover import config, repos


class Discovery(unittest.TestCase):
    def test_private_configs_take_precedence_and_examples_require_opt_in(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {'DROVER_EXAMPLES': '0'}):
            state = Path(d)
            with patch.object(config, 'repos_dir', return_value=state / 'repos'):
                self.assertEqual(repos.load_all(), {})
                with patch.dict(os.environ, {'DROVER_EXAMPLES': '1'}):
                    self.assertEqual(set(repos.load_all()), {'acme-lib', 'acme-app'})
                legacy = state / 'programs'
                legacy.mkdir()
                text = 'name="mine"\nrepo="~/repos/mine"\n'
                (legacy / 'mine.toml').write_text(text)
                with self.assertLogs('drover.repos', 'WARNING') as logs:
                    self.assertEqual(set(repos.load_all()), {'mine'})
                self.assertIn('deprecated', logs.output[0])
                self.assertFalse((state / 'repos').exists())
                (state / 'repos').mkdir()
                self.assertEqual(repos.load_all(), {})


class LandingConfig(unittest.TestCase):
    def test_landing_defaults_and_explicit_options(self):
        raw = {'name': 'acme-lib', 'repo': '~/repos/acme-lib'}
        self.assertEqual(repos.normalize(raw)['landing'],
                         {'mode': 'local', 'push': False, 'remote': 'origin', 'main': 'main'})
        value = {'mode': 'local', 'push': True, 'remote': 'upstream', 'main': 'trunk'}
        prog = repos.normalize({**raw, 'landing': value})
        self.assertEqual(prog['landing'], value)
        self.assertEqual((prog['remote'], prog['default_branch'], prog['land']['push']), ('upstream', 'trunk', True))

    def test_pr_mode_and_invalid_push_are_rejected(self):
        for value, error in (({'mode': 'pr'}, 'not implemented'), ({'mode': 'other'}, 'landing.mode'),
                             ({'push': 'false'}, 'landing.push')):
            with self.assertRaisesRegex(repos.RepoError, error):
                repos.normalize({'name': 'acme-lib', 'repo': '~/repos/acme-lib', 'landing': value})

    def test_setup_preserves_landing_names_and_updates_canonical_push(self):
        import tomllib
        from drover import wizard
        value = {'mode': 'local', 'push': False, 'remote': 'upstream', 'main': 'trunk'}
        base = {'name': 'acme-lib', 'repo': '~/repos/acme-lib', 'landing': value}
        answers = {'name': 'acme-lib', 'repo': '~/repos/acme-lib', 'platforms': ['wsl'],
                   'push': True, 'base': base, 'existing': True}
        saved = tomllib.loads(wizard.repo_toml(answers, ''))
        self.assertEqual(saved['landing'], {'push': True, 'mode': 'local'})
        self.assertEqual((saved['remote'], saved['default_branch']), ('upstream', 'trunk'))


class VersionedRepoConfig(unittest.TestCase):
    def setUp(self):
        from tests.gitfix import Repo
        self.fix = Repo()
        self.addCleanup(self.fix.cleanup)
        self.machine = {'name': 'example', 'repo': str(self.fix.repo),
                        'run': {'env': {'MACHINE_VALUE': 'override'}}}

    def test_commit_then_machine_merge_and_dirty_config_is_ignored(self):
        first = self.fix.commit('.drover/config.toml', 'schema_version=1\n[scripts]\ntest=".drover/scripts/check.ps1"\n'
                                '[run]\nplatforms=["wsl"]\ndefault="wsl"\n'
                                '[run.env]\nMACHINE_VALUE="repo"\nREPO_VALUE="kept"\n')
        second = self.fix.commit('.drover/config.toml', 'schema_version=1\n[scripts]\ntest=".drover/scripts/next.ps1"\n')
        (self.fix.repo / '.drover/config.toml').write_text('invalid = [')
        old = repos.load(self.machine, first)
        self.assertEqual(old['scripts'], {'test': '.drover/scripts/check.ps1'})
        self.assertEqual(old['run']['env'], {'MACHINE_VALUE': 'override', 'REPO_VALUE': 'kept'})
        self.assertEqual(old['run']['default'], 'wsl')
        self.assertEqual(repos.load(old, second)['scripts']['test'], '.drover/scripts/next.ps1')

    def test_changed_files_preserve_unicode_and_control_characters(self):
        import json
        from drover import ci, pipeline
        names = ['src/space name.txt', 'src/café.txt']
        if os.name != 'nt':
            names.append('src/line\nbreak.txt')
        self.fix.git('checkout', '-b', 'topic')
        for name in names:
            sha = self.fix.commit(name, 'changed')
        prog = repos.load({**self.machine, 'scripts': {'test': '.drover/scripts/check.ps1'}})
        pipe = pipeline.Pipeline(None, repos_map={'example': prog})
        platform = 'windows' if os.name == 'nt' else 'wsl'
        plan = pipe.ci.prepare(prog, self.fix.repo, sha, 'ci', platform=platform)
        self.assertCountEqual(json.loads(plan['spec']['run_config']['env']['DROVER_CHANGED_FILES']), names)

    def test_missing_repo_file_uses_the_same_machine_layer(self):
        prog = repos.load({**self.machine, 'scripts': {'test': '.drover/scripts/check.ps1'}})
        self.assertEqual(repos.script_argv(prog),
                         ['pwsh', '-NoLogo', '-NoProfile', '-NonInteractive', '-File', '.drover/scripts/check.ps1'])

    def test_missing_machine_repo_is_a_configuration_error(self):
        with self.assertRaisesRegex(repos.RepoError, 'repo is required'):
            repos.load({'name': 'example'})

    def test_exact_commit_config_refuses_an_unreachable_checkout(self):
        sha = self.fix.commit('.drover/config.toml', 'schema_version=1\n[scripts]\ntest=".drover/scripts/check.ps1"\n')
        with self.assertRaises(repos.RepoError):
            repos.load(self.machine, sha, str(self.fix.root / 'missing'))

    def test_script_and_environment_contract_is_validated(self):
        for script in ('../escape.ps1', '/absolute.ps1', 'C:/check.ps1', 'check.sh', 'check.ps1 -Flag'):
            with self.subTest(script=script), self.assertRaises(repos.RepoError):
                repos.normalize({**self.machine, 'scripts': {'test': script}})
        for roots in (['/var/cache'], ['/tmp/../escape'], ['/tmp']):
            with self.subTest(roots=roots), self.assertRaises(repos.RepoError):
                repos.normalize({**self.machine, 'run': {'sandbox_writable_roots': roots}})

    def test_composed_gate_command_executes_the_versioned_script_and_environment(self):
        import json
        import subprocess
        import sys
        from drover import ci, pipeline, runner
        script = '.drover/scripts/drover/check action.ps1'
        self.fix.commit(script, 'Write-Output "script:$env:MACHINE_VALUE"\n'
                        'Write-Output "kind:$env:DROVER_CI_KIND"\n'
                        'Write-Output "changed:$env:DROVER_CHANGED_FILES"\nexit 0\n')
        self.fix.commit('.drover/config.toml', f'schema_version=1\n[scripts]\ntest="{script}"\n[gate]\ndocs_only=false\n')
        self.fix.git('checkout', '-b', 'topic')
        sha = self.fix.commit('source.txt', 'a change\n')
        prog = repos.load(self.machine, sha)
        pipe = pipeline.Pipeline(None, repos_map={'example': prog}, runs_dir=self.fix.root / 'runs')
        platform = 'windows' if os.name == 'nt' else 'wsl'
        plan = pipe.ci.prepare(prog, self.fix.repo, sha, 'ci', platform=platform)
        argv, receipt = plan['spec']['argv'], Path(plan['receipt'])
        result = subprocess.run([sys.executable, '-m', 'drover.runner', '--run-id', 'ci', '--runs-dir',
                                 str(self.fix.root / 'runs'), '--cwd', str(self.fix.repo), '--run-config', json.dumps(plan['spec']['run_config']), '--', *argv],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(receipt.read_text())
        self.assertEqual(data['sha'], sha)
        self.assertTrue(data['passed'])
        tail = '\n'.join(data['log_tail'])
        self.assertIn('script:override', tail)
        self.assertIn('kind:checks', tail)
        self.assertIn('source.txt', tail)
        self.assertEqual(data['steps'][0]['command'], repos.script_argv(prog))
        self.assertEqual(ci.problems(data, self.fix.repo, 'main'), [])
