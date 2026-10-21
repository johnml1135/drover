"""Only committed repository outputs and declared machine paths authorize cleanup."""
import contextlib
import io
import os
import tomllib
import unittest
from unittest.mock import patch

from drover import config, repos
from tests.gitfix import Repo


class Policy(unittest.TestCase):
    def setUp(self):
        self.fix = Repo()
        self.addCleanup(self.fix.cleanup)
        self.addCleanup(config.reset)
        config.use(config.validate({'paths': {'state_dir': str(self.fix.root / 'state')}}))
        self.machine = {'name': 'alpha', 'repo': str(self.fix.repo), 'budget_gb': 12.5}

    def adopt(self, outputs):
        return self.fix.commit('.drover/config.toml', config.dumps({
            'schema_version': 1, 'cleanup': {'build_outputs': outputs}}))

    def test_committed_outputs_and_machine_facts(self):
        cleanup_config = repos.cleanup_config
        old = self.adopt(['build output', 'artifacts/reproducible'])
        self.adopt(['new-output'])
        (self.fix.repo / '.drover/config.toml').write_text('schema_version=1\n[cleanup]\nbuild_outputs=["dirty"]\n')
        sources = {}
        prog = repos.load(self.machine, old, sources=sources)
        self.assertEqual(cleanup_config(prog), {'build_outputs': ['build output', 'artifacts/reproducible'], 'budget_gb': 12.5})
        self.assertIn('repo:.drover/config.toml@' + old, sources['cleanup.build_outputs'])
        self.assertTrue(sources['budget_gb'].startswith('machine:'))
        self.assertEqual(cleanup_config(repos.load(self.machine))['build_outputs'], ['new-output'])
        with self.assertRaises(repos.RepoError):
            repos.load({**self.machine, 'cleanup': {'build_outputs': ['machine-injection']}})
        self.assertEqual(cleanup_config(repos.normalize({'name': 'empty', 'repo': str(self.fix.repo)})),
                         {'build_outputs': [], 'budget_gb': None})
        for bad in (0, -1, True, float('nan'), float('inf'), '12'):
            with self.subTest(budget=bad):
                with self.assertRaises(repos.RepoError):
                    repos.load({**self.machine, 'budget_gb': bad})
        root = str(self.fix.root)
        policy = {'temp_roots': [root + '/temp'], 'report_roots': [root + '/inventory'],
                  'caches': [{'argv': ['fixture-tool', '--literal', 'a b;$(echo nope)'],
                              'cwd': root, 'placement': 'wsl'}]}
        self.assertEqual(config.validate({'disk_cleanup': policy})['disk_cleanup'], policy)
        self.assertEqual(config.validate({})['disk_cleanup'], {'temp_roots': [], 'report_roots': [], 'caches': []})
        state = self.fix.root / 'state'
        machine_file = state / 'repos' / 'alpha.toml'
        machine_file.parent.mkdir(parents=True)
        machine_file.write_text(config.dumps(self.machine))
        output = io.StringIO()
        with patch.dict(os.environ, {'DROVER_HOME': str(state)}), contextlib.redirect_stdout(output):
            self.assertEqual(config.main(['migrate-machine', 'alpha', '--apply']), 0, output.getvalue())
        self.assertEqual(tomllib.loads(machine_file.read_text())['budget_gb'], 12.5)

    def test_rejects_escape_and_invalid_cache_argv(self):
        cleanup_config = repos.cleanup_config
        for path in ('../outside', '/absolute', 'C:/drive', 'a/../b', 'a/./b', './a', '.',
                     'a//b', 'a/', '.git', '.git/config', '.GIT', 'build/.Git/config', 'a/*', 'a/?', 'a/[x]', r'a\b'):
            with self.subTest(path=path):
                revision = self.adopt([path])
                with self.assertRaises(repos.RepoError):
                    cleanup_config(repos.load(self.machine, revision))
        valid = {'argv': ['fixture', 'literal argument'], 'cwd': str(self.fix.root), 'placement': 'wsl'}
        for changed in ({'argv': 'fixture --all'}, {'argv': []}, {'argv': ['fixture', 1]},
                        {'argv': ['']}, {'cwd': 'relative'}, {'placement': 'auto'},
                        {'shell': True}, {'argv': ['fixture', '\x00']}):
            with self.subTest(cache=changed):
                with self.assertRaises(config.ConfigError):
                    config.validate({'disk_cleanup': {'caches': [{**valid, **changed}]}})
        for key in ('temp_roots', 'report_roots'):
            for value in (['relative'], ['~/guessed'], 'absolute-looking', [1]):
                with self.subTest(key=key, value=value):
                    with self.assertRaises(config.ConfigError):
                        config.validate({'disk_cleanup': {key: value}})
