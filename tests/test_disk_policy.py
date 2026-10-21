"""Only committed repository outputs and declared machine paths authorize cleanup."""
import contextlib
import io
import os
import tomllib
import unittest
from unittest.mock import patch

from drover import config, repos
from tests.gitfix import Repo

PROCESS_IGNORE = ['ctfmon.exe', 'TabTip.exe', 'TextInputHost.exe', 'DefenderSessionHelper.exe',
                  'Wacom_TouchUser.exe', 'Taskmgr.exe', 'SecurityHealthSystray.exe', 'sihost.exe',
                  'csrss.exe', 'winlogon.exe', 'dwm.exe', 'fontdrvhost.exe', 'NVDisplay.Container.exe',
                  'RtkAudUService64.exe', 'WacomHost.exe', 'Wacom_Tablet.exe',
                  'systemd', '(sd-pam)']


class Policy(unittest.TestCase):
    def setUp(self):
        self.fix = Repo()
        self.addCleanup(self.fix.cleanup)
        self.addCleanup(config.reset)
        env = patch.dict(os.environ, {'DROVER_HOME': str(self.fix.root / 'state')})
        env.start()
        self.addCleanup(env.stop)
        config.use(config.validate({'paths': {'state_dir': str(self.fix.root / 'state')}}))
        self.machine = {'name': 'alpha', 'repo': str(self.fix.repo), 'budget_gb': 12.5}

    def adopt(self, outputs):
        return self.fix.commit('.drover/config.toml', config.dumps({
            'schema_version': 1, 'cleanup': {'disposable': outputs}}))

    def test_committed_outputs_and_machine_facts(self):
        cleanup_config = repos.cleanup_config
        old = self.adopt(['build output', 'artifacts/reproducible'])
        self.adopt(['new-output'])
        (self.fix.repo / '.drover/config.toml').write_text('schema_version=1\n[cleanup]\ndisposable=["dirty"]\n')
        sources = {}
        prog = repos.load(self.machine, old, sources=sources)
        self.assertEqual(cleanup_config(prog), {'disposable': ['build output', 'artifacts/reproducible'], 'budget_gb': 12.5})
        self.assertIn('repo:.drover/config.toml@' + old, sources['cleanup.disposable'])
        self.assertTrue(sources['budget_gb'].startswith('machine:'))
        self.assertEqual(cleanup_config(repos.load(self.machine))['disposable'], ['new-output'])
        with self.assertRaises(repos.RepoError):
            repos.load({**self.machine, 'cleanup': {'disposable': ['machine-injection']}})
        self.assertEqual(cleanup_config(repos.normalize({'name': 'empty', 'repo': str(self.fix.repo)})),
                         {'disposable': [], 'budget_gb': None})
        for bad in (0, -1, True, float('nan'), float('inf'), '12'):
            with self.subTest(budget=bad):
                with self.assertRaises(repos.RepoError):
                    repos.load({**self.machine, 'budget_gb': bad})
        root = str(self.fix.root)
        policy = {'temp_roots': [root + '/temp'], 'report_roots': [root + '/inventory'],
                  'caches': [{'argv': ['fixture-tool', '--literal', 'a b;$(echo nope)'],
                              'cwd': root, 'placement': 'wsl'}]}
        self.assertEqual(config.validate({'disk_cleanup': policy})['disk_cleanup'], dict(
            policy, worker_timeout_s=1800, process_query_timeout_s=240, process_ignore=PROCESS_IGNORE))
        self.assertEqual(config.validate({})['disk_cleanup'], {'temp_roots': [], 'report_roots': [], 'caches': [],
                         'worker_timeout_s': 1800, 'process_query_timeout_s': 240, 'process_ignore': PROCESS_IGNORE})
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
                     'a//b', 'a/', '.git', '.git/config', '.GIT', 'build/.Git/config', r'a\b'):
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

    def test_detected_machine_defaults_are_editable_and_absent_tools_omitted(self):
        from drover import placement
        scratch = self.fix.root / 'agent scratch'
        scratch.mkdir()
        with patch.object(placement, 'CLEANUP_SCRATCH_ROOTS', (str(scratch),)), \
                patch.object(placement.shutil, 'which', side_effect=lambda tool: '/tools/' + tool if tool in ('uv', 'npm') else None), \
                patch('tempfile.gettempdir', return_value=str(self.fix.root / 'os temp')):
            raw = {'paths': {'state_dir': str(self.fix.root / 'state')}}
            seeded = config.migrate_disk_config(raw)
            policy = seeded['disk_cleanup']
            self.assertEqual(policy['temp_roots'], [str(self.fix.root / 'os temp'),
                             str(self.fix.root / 'state' / 'scratch'), str(scratch)])
            self.assertEqual([item['argv'] for item in policy['caches']],
                             [['uv', 'cache', 'prune'], ['npm', 'cache', 'clean', '--force']])
            self.assertTrue(all(item['cwd'] == str(self.fix.root) or item['cwd'] == str(self.fix.root / 'state')
                                for item in policy['caches']))
            config.validate(seeded)
            self.assertEqual(config.migrate_disk_config(seeded), seeded)
            explicit = {**raw, 'disk_cleanup': {'temp_roots': [], 'report_roots': [], 'caches': []}}
            self.assertEqual(config.migrate_disk_config(explicit), {**explicit, 'disk_cleanup': {
                **explicit['disk_cleanup'], 'process_ignore': PROCESS_IGNORE, 'process_query_timeout_s': 240}})

    def test_r8_disposable_globs_and_folder_cache_schema(self):
        revision = self.fix.commit('.drover/config.toml', config.dumps({
            'schema_version': 1, 'cleanup': {'disposable': ['*.log', '**/obj', 'literal path']}}))
        self.assertEqual(repos.cleanup_config(repos.load(self.machine, revision))['disposable'],
                         ['*.log', '**/obj', 'literal path'])
        entry = dict(path=str(self.fix.root / 'cache'), placement='wsl')
        cfg = config.validate({'disk_cleanup': {'caches': [entry]}})
        self.assertEqual(cfg['disk_cleanup']['caches'][0], dict(entry, max_age_days=30))
        for extra in ({'max_age_days': 0}, {'max_age_days': True}, {'max_age_days': float('inf')},
                      {'argv': ['mixed']}, {'path': '../escape'}):
            with self.subTest(extra=extra), self.assertRaises(config.ConfigError):
                config.validate({'disk_cleanup': {'caches': [{**entry, **extra}]}})

    def test_r8_detected_folder_only_for_tool_without_prune(self):
        from drover import placement
        cache = self.fix.root / 'cargo-cache'
        cache.mkdir()
        with patch.object(placement.shutil, 'which', side_effect=lambda tool: '/tools/cargo' if tool == 'cargo' else None), \
                patch.dict(os.environ, {'CARGO_HOME': str(cache)}):
            policy = placement.detected_cleanup_policy(self.fix.root / 'state')
        self.assertEqual(policy['caches'], [
            dict(path=str(cache / 'registry' / 'cache'), placement=placement.cleanup_side(), max_age_days=30)])
