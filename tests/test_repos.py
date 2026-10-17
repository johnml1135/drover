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
                   'gate_platform': 'wsl', 'gate_command': '', 'push': True, 'base': base, 'existing': True}
        saved = tomllib.loads(wizard.repo_toml(answers, ''))
        self.assertEqual(saved['landing'], {**value, 'push': True})
