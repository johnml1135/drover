"""Committed repository entry is the public policy boundary."""
import unittest
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
            (entry(run={'platforms': []}), 'run.platforms'),
            (entry(run={'env': {'X': 1}}), 'run.env'), (entry(brief=3), 'brief'),
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
            sha = self.fix.commit(ENTRY, text)
            with self.subTest(key=key, text=text):
                with self.assertRaises(repos.RepoError) as caught:
                    repos.load(self.machine, sha)
                for detail in (ENTRY, sha, key):
                    self.assertIn(detail, str(caught.exception))
