"""Machine adoption migration runs against copies, preserving effective policy."""
import contextlib
import io
import os
from pathlib import Path
from unittest.mock import patch

from drover import config, repos
from tests.test_repo_entry import RepoCase


class Migration(RepoCase):
    def setUp(self):
        super().setUp()
        self.state = self.fix.root / 'state'
        self.path = self.state / 'repos' / 'alpha.toml'
        self.path.parent.mkdir(parents=True)
        self.policy = {
            'run': {'platforms': ['wsl', 'windows'], 'default': 'wsl'},
            'gate': {'platform': 'both', 'timeout_s': 1800, 'idle_s': 1500},
            'review': {'backend': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'high'},
            'land': {'policy': 'batch', 'push': False},
        }
        self.adopt(**self.policy)
        # Like the live file: old command tables, policy, and local facts coexist.
        self.raw = {**self.machine, 'worktrees': str(self.fix.root / 'trees'),
                    'remote': 'origin', 'default_branch': 'main', **self.policy,
                    'gate': {**self.policy['gate'], 'commands': {
                        'full': ['python3', '-m', 'unittest', 'discover', '-s', 'tests', '-t', '.'],
                        'windows': {'full': ['python', '-m', 'unittest', 'discover', '-s', 'tests', '-t', '.']}}}}
        self.path.write_text(config.dumps(self.raw), encoding='utf-8')
        self.env = patch.dict(os.environ, {'DROVER_HOME': str(self.state)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def cli(self, *args, command='migrate-registration'):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                status = config.main([command, 'alpha', *args])
            except SystemExit as error:
                self.fail(f'migration CLI is missing: {error}')
        return status, output.getvalue(), errors.getvalue()

    def test_deprecated_migrate_machine_alias_runs_new_command(self):
        new = self.cli(command='migrate-registration')
        old = self.cli(command='migrate-machine')
        self.assertEqual(old[0], new[0])
        self.assertEqual(old[1], new[1])
        self.assertIn('migrate-machine is deprecated; use migrate-registration.', old[2])
        self.assertNotIn('deprecated', new[2])

    def test_dry_run_apply_backup_and_reload(self):
        original = self.path.read_bytes()
        status, output, errors = self.cli()
        self.assertEqual(status, 0, errors)
        self.assertIn('review.model', output)
        self.assertIn('already', output)
        self.assertIn('gate.commands', output)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob('*.bak*')), [])
        status, output, errors = self.cli('--apply')
        self.assertEqual(status, 0, errors)
        backups = list(self.path.parent.glob('*.bak*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        facts = repos.read_machine(self.path)
        self.assertEqual(set(facts), {'name', 'repo', 'worktrees', 'remote', 'default_branch'})
        effective = repos.load_all(self.path.parent)['alpha']
        before = repos.normalize(self.raw)
        for key in ('run', 'gate', 'review', 'land', 'landing'):
            self.assertEqual(effective[key], {**before[key], **({'unknown': False} if key == 'gate' else {})})
        status, _, errors = self.cli('--apply')
        self.assertEqual(status, 0, errors)
        self.assertEqual(len(list(self.path.parent.glob('*.bak*'))), 1)

    def test_policy_difference_refuses_without_writes(self):
        self.adopt(**{**self.policy, 'review': {**self.policy['review'], 'model': 'different'}})
        original = self.path.read_bytes()
        status, output, errors = self.cli('--apply')
        self.assertEqual(status, 2)
        for detail in ('review.model', 'gpt-6.1-sol', 'different'):
            self.assertIn(detail, output + errors)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob('*.bak*')), [])

    def test_rejection_names_migration_command(self):
        with self.assertRaises(repos.RepoError) as caught:
            repos.load(self.raw, machine_path=self.path)
        self.assertIn('python -m drover.config migrate-registration alpha', str(caught.exception))

    def test_environment_facts_survive(self):
        self.raw['run']['env'] = {'LOCAL.PATH': 'local tools'}
        self.path.write_text(config.dumps(self.raw), encoding='utf-8')
        status, _, errors = self.cli('--apply')
        self.assertEqual(status, 0, errors)
        self.assertEqual(repos.read_machine(self.path)['run'], {'env': {'LOCAL.PATH': 'local tools'}})
        self.assertEqual(repos.load_all(self.path.parent)['alpha']['run']['env'], {'LOCAL.PATH': 'local tools'})

    def test_unknown_keys_cannot_be_silently_removed(self):
        for extra in ({'unknown': {}}, {'unknown': '<absent>'}, {'run': {'platforms': ['wsl', 'other']}}):
            with self.subTest(extra=extra):
                self.path.write_text(config.dumps({**self.raw, **extra}), encoding='utf-8')
                original = self.path.read_bytes()
                status, _, _ = self.cli('--apply')
                self.assertEqual(status, 2)
                self.assertEqual(self.path.read_bytes(), original)
                self.assertEqual(list(self.path.parent.glob('*.bak*')), [])
