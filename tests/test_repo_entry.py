"""Committed repository entry is the public policy boundary."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from drover import config, repos
from tests.gitfix import Repo

ENTRY = '.drover/config.toml'
SCRIPT = '.drover/scripts/check.ps1'


def entry(**updates):
    return config.dumps({'schema_version': 1, 'scripts': {'test': SCRIPT}, **updates})


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.fix = Repo()
        self.addCleanup(self.fix.cleanup)
        self.addCleanup(config.reset)
        config.use(config.validate({}))
        self.machine = {'name': 'alpha', 'repo': str(self.fix.repo)}

    def adopt(self, **updates):
        return self.fix.commit(ENTRY, entry(**updates))

    def call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (TypeError, AttributeError) as exc:
            self.fail(f'public configuration interface is missing: {exc}')


class Entry(RepoCase):
    def test_selected_entry_and_validation(self):
        old = self.adopt(brief='old')
        new = self.adopt(brief='new')
        self.fix.commit('drover.toml', 'brief="root"\n[scripts]\ntest="old/root.ps1"\n')
        self.fix.commit('.drover/unnamed.toml', 'this is invalid TOML [')
        self.fix.commit('.drover/workflows/unnamed.yaml', ': invalid: [')
        (self.fix.repo / ENTRY).write_text(entry(brief='dirty'), encoding='utf-8')
        for sha, brief in ((old, 'old'), (new, 'new'), ('HEAD', 'new')):
            with self.subTest(revision=sha):
                prog = repos.load(self.machine, sha)
                self.assertEqual(prog['scripts'], {'test': SCRIPT})
                self.assertEqual(prog['brief'], brief)
                self.assertEqual(prog.get('schema_version'), 1)
                self.assertIs(prog.get('_adopted'), True)

    def test_committed_errors_include_resolved_sha(self):
        for text, key in (('schema_version = [', '<syntax>'),
                          (entry(brief=3), 'brief'),
                          (entry(review={'seat': 'missing-seat'}), 'review.seat')):
            sha = self.fix.commit(ENTRY, text)
            with self.subTest(key=key), self.assertRaises(repos.RepoError) as caught:
                repos.load(self.machine)
            for detail in (ENTRY, sha, key):
                self.assertIn(detail, str(caught.exception))

    def test_legacy_fallback_keeps_permissive_defaults(self):
        prog = repos.load({**self.machine, 'run': {'default': []},
                           'gate': {'platform': [], 'timeout_s': True},
                           'review': {'model': False}, 'land': {'policy': []}})
        self.assertEqual(prog['run']['default'], 'windows')
        self.assertEqual(prog['gate']['timeout_s'], 1.0)
        self.assertEqual(prog['land']['policy'], 'batch')


SHA = 'a' * 40


class CommittedConfig(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('subprocess.Popen', side_effect=AssertionError('parser spawned a process')))
        self.enterContext(patch('drover.config.get', side_effect=AssertionError('parser read user config')))

    def test_valid_entry(self):
        raw = {'schema_version': 1, 'scripts': {'test': SCRIPT},
               'brief': '', 'run': {'platforms': ['wsl'], 'env': {'X': '1'}},
               'gate': {'platform': 'wsl', 'timeout_s': 12, 'docs_only': False},
               'landing': {'push': False}, 'land': {'policy': 'single'}}
        self.assertEqual(repos.parse_committed_config(config.dumps(raw), SHA), raw)

    def test_normalization_preserves_first_error(self):
        # These exact diagnostics come from the loader at dc0c884. Parsing must
        # defer these checks so cleanup and platform conflicts still win over env.
        cases = [
            ({'cleanup': {'disposable': ['../bad']}, 'run': {'env': {'X': 1}}},
             'cleanup.disposable must contain canonical relative paths or globs outside .git'),
            ({'cleanup': {'disposable': ['../bad']}, 'run': {'platforms': []}},
             'cleanup.disposable must contain canonical relative paths or globs outside .git'),
            ({'run': {'platforms': ['wsl'], 'env': {'X': 1}}, 'gate': {'platform': 'both'}},
             "alpha: gate.platform 'both' needs run.platforms to include ['windows', 'wsl']"),
            ({'run': {'platforms': ['wsl'], 'default': 'windows', 'env': {'X': 1}}},
             "alpha: run.default 'windows' is not in run.platforms ['wsl']"),
            ({'run': {'platforms': [], 'env': {'X': 1}}},
             "alpha: run.platforms must be a non-empty list drawn from ('windows', 'wsl')"),
            ({'run': {'platforms': []}},
             "alpha: run.platforms must be a non-empty list drawn from ('windows', 'wsl')"),
            ({'run': {'env': {'X': 1}}},
             'alpha: run.env must map environment variable names to strings'),
        ]
        machine = {'name': 'alpha', 'repo': '/repo', 'worktrees': '/trees'}
        for updates, error in cases:
            text = entry(**updates)
            with self.subTest(updates=updates):
                raw = repos.parse_committed_config(text, SHA)
                with self.assertRaises(repos.RepoError) as caught:
                    repos.normalize(config._merge(raw, machine))
                self.assertEqual(str(caught.exception), error)
                results = [SimpleNamespace(returncode=0, stdout=value)
                           for value in (SHA, ENTRY, text)]
                with patch.object(repos.placement, 'reachable', return_value=True), \
                        patch.object(repos.placement, 'git', side_effect=results), \
                        patch('drover.repos_normalize.worktrees_for', return_value=machine['worktrees']), \
                        self.assertRaises(repos.RepoError) as caught:
                    repos.load(machine, machine_path='/registration.toml')
                self.assertEqual(str(caught.exception), f'{ENTRY}@{SHA}: {error}')

    def test_invalid_entry(self):
        invalid = [
            ('schema_version = [', '<syntax>'),
            ('[scripts]\ntest=".drover/scripts/check.ps1"', 'schema_version'),
            ('schema_version=true', 'schema_version'), ('schema_version=2', 'schema_version'),
            ('schema_version="1"', 'schema_version'),
            (entry(repo='elsewhere'), 'repo'), (entry(remote='elsewhere'), 'remote'),
            (entry(default_branch='trunk'), 'default_branch'), (entry(worktrees='elsewhere'), 'worktrees'),
            (entry(name='renamed'), 'name'), (entry(workflows={}), 'workflows'),
            (entry(surprise=1), 'surprise'), (entry(run={'unknown': 1}), 'run.unknown'),
            (entry(gate={'unknown': 1}), 'gate.unknown'),
            (entry(review={'unknown': 1}), 'review.unknown'),
            (entry(landing={'remote': 'elsewhere'}), 'landing.remote'),
            (entry(land={'unknown': 1}), 'land.unknown'),
            (entry(brief=3), 'brief'),
            (entry(gate={'docs_only': 'yes'}), 'gate.docs_only'),
        ]
        for table in ('scripts', 'run', 'gate', 'review', 'landing', 'land'):
            invalid.append((entry(**{table: []}), table))
        for path in ('/check.ps1', 'C:/check.ps1', r'.drover\scripts\check.ps1',
                     '.drover/scripts/../check.ps1', '.drover/scripts/./check.ps1',
                     '.drover/scripts//check.ps1', 'scripts/check.ps1',
                     '.drover/scripts/check.py', '.drover/scripts/'):
            invalid.append((entry(scripts={'test': path}), 'scripts.test'))
        for text, key in invalid:
            with self.subTest(key=key, text=text):
                with self.assertRaises(repos.RepoError) as caught:
                    repos.parse_committed_config(text, SHA)
                for detail in (ENTRY, SHA, key):
                    self.assertIn(detail, str(caught.exception))

    def test_supplied_fields_validate_before_normalization(self):
        cases = {
            'run.default': [[], False, '', 'other'],
            'gate.platform': [[], False, '', 'other', 'WSL'],
            'review.model': [False, [], 1, ''],
            'review.high_risk_model': [False, [], 1, ''],
            'review.backend': [False, [], '', 'other'],
            'review.seat': [False, [], 1, ''],
            'review.effort': [False, [], 1, ''],
            'land.policy': [[], False, '', 'other'],
            'land.push': [[], 1, 'false'],
            'landing.push': [[], 1, 'false'],
            'landing.mode': [[], False, '', 'other', 'pr'],
            'gate.timeout_s': [True, [], '12'],
            'gate.idle_s': [False, [], '12'],
        }
        for key, values in cases.items():
            table, field = key.split('.')
            for value in values:
                text = entry(**{table: {field: value}})
                with self.subTest(key=key, value=value):
                    with self.assertRaises(repos.RepoError) as caught:
                        repos.parse_committed_config(text, SHA)
                    for detail in (ENTRY, SHA, key):
                        self.assertIn(detail, str(caught.exception))
