"""Private repo discovery, compatibility and explicit examples."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from drover import config, repos


class GateCommands(unittest.TestCase):
    def repo(self, commands, platform='both', **gate):
        return repos.normalize({'name': 'acme-app', 'repo': '~/repos/acme-app',
                                'gate': {'platform': platform, 'commands': commands, **gate}})

    def test_shared_commands_and_per_key_platform_overrides(self):
        shared = {'full': ['shared'], 'scoped': ['shared-scoped']}
        prog = self.repo(shared)
        for platform in repos.PLATFORMS:
            self.assertEqual(repos.gate_commands(prog, platform), shared)
        commands = {**shared, 'windows': {'full': ['python', 'check.py']},
                    'wsl': {'scoped': ['python3', 'scoped.py']}}
        prog = self.repo(commands)
        self.assertEqual(prog['gate']['commands'], commands)
        self.assertEqual(repos.gate_commands(prog, 'windows'),
                         {'full': ['python', 'check.py'], 'scoped': shared['scoped']})
        self.assertEqual(repos.gate_commands(prog, 'wsl'),
                         {'full': shared['full'], 'scoped': ['python3', 'scoped.py']})

    def test_platform_commands_can_supply_full_without_a_shared_command(self):
        commands = {'windows': {'full': ['python']}, 'wsl': {'full': ['python3']}}
        for platform in ('windows', 'wsl', 'both'):
            with self.subTest(platform=platform):
                prog = self.repo(commands, platform)
                self.assertFalse(prog['gate']['unknown'])
                for target in repos.gate_platforms(prog):
                    self.assertEqual(repos.gate_commands(prog, target), commands[target])

    def test_known_gate_requires_full_on_every_selected_platform(self):
        for missing in repos.PLATFORMS:
            commands = {p: {'full': ['check']} for p in repos.PLATFORMS if p != missing}
            with self.subTest(missing=missing):
                with self.assertRaises(repos.RepoError) as cm:
                    self.repo(commands)
                self.assertIn('acme-app', str(cm.exception))
                self.assertIn(f'gate.commands.{missing}.full', str(cm.exception))
        with self.assertRaisesRegex(repos.RepoError, 'acme-app.*gate.commands.wsl.full'):
            self.repo({}, 'wsl', unknown=False)
        self.assertTrue(self.repo({}, unknown=True)['gate']['unknown'])
        self.assertTrue(self.repo({'wsl': {'scoped': ['check']}}, unknown=True)['gate']['unknown'])

    def test_unknown_keys_and_malformed_commands_name_the_config_path(self):
        cases = [({'ful': ['x']}, 'gate.commands.ful'),
                 ({'linux': {'full': ['x']}}, 'gate.commands.linux'),
                 ({'windows': {'ful': ['x']}}, 'gate.commands.windows.ful'),
                 ({'wsl': {'windows': {'full': ['x']}}}, 'gate.commands.wsl.windows'),
                 ({'windows': ['x']}, 'gate.commands.windows'),
                 ({'wsl': {'full': []}}, 'gate.commands.wsl.full'),
                 ({'wsl': {'full': 'python3'}}, 'gate.commands.wsl.full'),
                 ({'wsl': {'scoped': ['x', 3]}}, 'gate.commands.wsl.scoped'),
                 ({'full': []}, 'gate.commands.full'),
                 (['x'], 'gate.commands')]
        for commands, path in cases:
            with self.subTest(commands=commands):
                with self.assertRaises(repos.RepoError) as cm:
                    self.repo(commands, unknown=True)
                self.assertIn(f'acme-app: {path}', str(cm.exception))

    def test_owner_override_replaces_platform_commands_with_shared_full(self):
        prog = self.repo({'windows': {'full': ['old']}}, unknown=True)
        overridden = repos.with_override(prog, 'python check.py')
        for platform in repos.PLATFORMS:
            self.assertEqual(repos.gate_commands(overridden, platform), {'full': ['python', 'check.py']})
        self.assertTrue(prog['gate']['unknown'])


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
                   'gate_platform': 'wsl', 'gate_command': '', 'push': True, 'base': base, 'existing': True}
        saved = tomllib.loads(wizard.repo_toml(answers, ''))
        self.assertEqual(saved['landing'], {**value, 'push': True})
