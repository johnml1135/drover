"""Four-level machine facts and explicit migration are validated without live state."""
import contextlib
import copy
import io
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from drover import config, placement

EXPECTED_REFUSALS = [
    ('plain',
     'accepted'),
    ('inside-repository',
     '<root>/home/config.toml: folder cache overlaps protected repository'),
    ('above-repository',
     '<root>/home/config.toml: folder cache overlaps protected repository'),
    ('git-part',
     '<root>/home/config.toml: folder cache overlaps protected repository'),
    ('venv-part',
     '<root>/home/config.toml: folder cache overlaps protected repository'),
    ('state',
     '<root>/home/config.toml: folder cache overlaps protected repository, worktree or state path'),
    ('repos-root',
     '<root>/home/config.toml: folder cache overlaps protected repository, worktree or state path'),
    ('worktrees',
     '<root>/home/config.toml: folder cache overlaps protected repository, worktree or state path'),
    ('machine-repo',
     '<root>/home/config.toml: folder cache overlaps protected repository, worktree or state path'),
    ('machine-worktrees',
     '<root>/home/config.toml: folder cache overlaps protected repository, worktree or state path'),
    ('derived-worktrees-unused',
     'accepted'),
    ('environment-moves-state',
     'configuration error: folder cache overlaps protected repository, worktree or state path'),
    ('derived-worktrees',
     '<root>/home/config.toml: folder cache overlaps protected repository, worktree or state path'),
    ('unreadable-machine-file',
     "<root>/home/config.toml: Expected ']' at the end of a table declaration (at end of document)"),
    ('internal-machine-key',
     '<root>/home/config.toml: <root>/machine-repos/broken.toml: _internal is internal metadata'),
]
KEYS = ('warn_gb', 'queue_gb', 'hold_gb', 'critical_gb')


def disk(name='test', path='H:/', **values):
    return {'kind': 'disk', 'name': name, 'path': path, **values}


def levels(row):
    return tuple(row[key] for key in KEYS)


class Levels(unittest.TestCase):
    def test_defaults_boundaries_and_validation(self):
        raw = {'trackers': [disk('h', 'H:/'), disk('c', 'C:/'), disk('archive', 'R:/')]}
        rows = {row['name']: row for row in config.validate(raw)['trackers']}
        self.assertEqual(levels(rows['h']), (150, 100, 50, 25))
        self.assertEqual(levels(rows['c']), (60, 40, 25, 15))
        self.assertEqual(levels(rows['archive']), (150, 100, 50, 25))
        equal = config.validate({'trackers': [disk(**dict.fromkeys(KEYS, 0))]})
        self.assertEqual(levels(next(t for t in equal['trackers'] if t['name'] == 'test')), (0, 0, 0, 0))
        for key in KEYS:
            for bad in (True, False, float('nan'), float('inf'), -float('inf'), -1, '25'):
                with self.subTest(key=key, value=bad):
                    with self.assertRaises(config.ConfigError):
                        config.validate({'trackers': [disk(**{key: bad})]})
        for values in ({'warn_gb': 99}, {'queue_gb': 49}, {'hold_gb': 24}, {'critical_gb': 51}):
            with self.subTest(values=values):
                with self.assertRaises(config.ConfigError):
                    config.validate({'trackers': [disk(**values)]})
        with self.assertRaises(config.ConfigError) as caught:
            config.validate({'trackers': [disk(wsl_source='path')]})
        self.assertIn('migrate-machine', str(caught.exception))
        self.assertTrue(any(t['kind'] == 'wsl' and t['name'] == 'wsl-up' for t in rows.values()))


class Migration(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='disk-migration-')
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        for context in (patch.dict(os.environ, {'DROVER_HOME': str(root / 'state')}),
                        patch.object(placement, 'detected_cleanup_policy', return_value=dict(
                            temp_roots=[str(root / 'temp')], report_roots=[], caches=[]))):
            context.start()
            self.addCleanup(context.stop)

    def test_global_dry_run_backup_and_idempotence(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            path = state / 'config.toml'
            raw = {'paths': {'state_dir': str(state)}, 'hub': {'ui_port': 9981}, 'trackers': [
                disk('h', 'H:/repos', wsl_source='vhdx', queue_gb=75.0, hold_gb=25.0),
                disk('c', 'C:/', wsl_source='vhdx', queue_gb=50.0, hold_gb=25.0, enabled=False),
                disk('wsl', '', wsl_source='home', queue_gb=75.0, hold_gb=25.0),
                disk('archive', 'R:/', wsl_source='path', queue_gb=120.0, hold_gb=30.0),
                disk('explicit', 'S:/', wsl_source='path', warn_gb=200.0, queue_gb=90.0,
                     hold_gb=40.0, critical_gb=10.0),
                {'kind': 'wsl', 'name': 'wsl-up', 'enabled': False, 'queue_mem_pct': 80.0}]}
            original_raw = copy.deepcopy(raw)
            proposed = config.migrate_disk_config(raw)
            self.assertEqual(raw, original_raw, 'the pure transform must not edit its input')
            rows = {t['name']: t for t in proposed['trackers']}
            self.assertNotIn('wsl', rows)
            self.assertEqual(levels(rows['h']), (150, 100, 50, 25))
            self.assertEqual(levels(rows['c']), (60, 40, 25, 15))
            self.assertFalse(rows['c']['enabled'])
            self.assertEqual(rows['c']['path'], 'C:/')
            self.assertEqual(levels(rows['archive']), (150, 120, 30, 25))
            self.assertEqual(levels(rows['explicit']), (200, 90, 40, 10))
            self.assertEqual(rows['wsl-up'], raw['trackers'][-1])
            self.assertEqual(proposed['hub'], raw['hub'])
            self.assertFalse(any('wsl_source' in t for t in proposed['trackers']))
            self.assertEqual(config.migrate_disk_config(proposed), proposed)
            config.validate(proposed)
            virtual_only = {'trackers': [disk('virtual-host', '~/repos', wsl_source='vhdx',
                                              queue_gb=95.0, hold_gb=35.0)]}
            with patch.object(placement, 'vhdx_dir', return_value='/mnt/h/WSL/Fixture') as _placement_patch_5, patch('drover.placement_host.vhdx_dir', _placement_patch_5):
                resolved = config.migrate_disk_config(virtual_only)['trackers'][0]
            self.assertEqual(placement.drive(resolved['path']), 'H')
            self.assertEqual(levels(resolved), (150, 95, 35, 25))
            path.write_bytes(('# exact original comment\r\n' + config.dumps(raw)).encode())
            original = path.read_bytes()
            with self.assertRaises(config.ConfigError):
                config.load(state, env={})

            def cli(*args):
                out, err = io.StringIO(), io.StringIO()
                with patch.dict(os.environ, {'DROVER_HOME': str(state)}), \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    result = config.main(['migrate-machine', *args])
                return result, out.getvalue() + err.getvalue()

            status, output = cli()
            self.assertEqual(status, 0, output)
            self.assertIn('warn_gb', output)
            self.assertIn('critical_gb', output)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(state.glob('*bak*')), [])
            status, output = cli('--apply')
            self.assertEqual(status, 0, output)
            backups = list(state.glob('*bak*'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)
            self.assertEqual(tomllib.loads(path.read_text()), proposed)
            loaded = config.load(state, env={})
            self.assertEqual(levels(next(t for t in loaded['trackers'] if t['name'] == 'h')), (150, 100, 50, 25))
            applied = path.read_bytes()
            self.assertEqual(cli('--apply')[0], 0)
            self.assertEqual(path.read_bytes(), applied)
            self.assertEqual(list(state.glob('*bak*')), backups)
            invalid = copy.deepcopy(raw)
            invalid['hub']['ui_port'] = -1
            path.write_text(config.dumps(invalid))
            invalid_bytes = path.read_bytes()
            self.assertEqual(cli('--apply')[0], 2)
            self.assertEqual(path.read_bytes(), invalid_bytes)
            self.assertEqual(list(state.glob('*bak*')), backups)
            unresolved = {'trackers': [disk('h', '~/repos', wsl_source='vhdx', queue_gb=75.0, hold_gb=25.0)]}
            path.write_text(config.dumps(unresolved))
            unresolved_bytes = path.read_bytes()
            with patch.object(placement, 'vhdx_dir', return_value=None) as _placement_patch_6, patch('drover.placement_host.vhdx_dir', _placement_patch_6), \
                    patch.object(placement, 'detect_vhdx_drives', return_value={}) as _placement_patch_7, patch('drover.placement_host.detect_vhdx_drives', _placement_patch_7):
                self.assertEqual(cli('--apply')[0], 2)
            self.assertEqual(path.read_bytes(), unresolved_bytes)
            self.assertEqual(list(state.glob('*bak*')), backups)

    def test_native_windows_virtual_source_delegates_host_resolution(self):
        raw = {'trackers': [disk('legacy', '~/repos', wsl_source='vhdx', queue_gb=95, hold_gb=35)]}
        with patch.object(placement, 'IS_WINDOWS', True) as _placement_patch_1, patch('drover.placement_exec.IS_WINDOWS', _placement_patch_1), patch('drover.placement_git.IS_WINDOWS', _placement_patch_1), patch('drover.placement_host.IS_WINDOWS', _placement_patch_1), patch('drover.placement_paths.IS_WINDOWS', _placement_patch_1), patch('drover.placement_process.IS_WINDOWS', _placement_patch_1), patch('drover.placement_proof.IS_WINDOWS', _placement_patch_1), patch('drover.placement_tree.IS_WINDOWS', _placement_patch_1), patch('drover.placement_worker.IS_WINDOWS', _placement_patch_1), \
                patch.object(placement, 'detect_vhdx_drives', return_value={'Fixture': 'H:'}) as _placement_patch_2, patch('drover.placement_host.detect_vhdx_drives', _placement_patch_2), \
                patch.object(placement, 'distro', return_value='Fixture') as _placement_patch_3, patch('drover.placement_exec.distro', _placement_patch_3), patch('drover.placement_git.distro', _placement_patch_3), patch('drover.placement_host.distro', _placement_patch_3), \
                patch.object(placement, 'vhdx_dir', side_effect=AssertionError('WSL-only helper')) as _placement_patch_4, patch('drover.placement_host.vhdx_dir', _placement_patch_4):
            row = config.migrate_disk_config(raw)['trackers'][0]
        self.assertEqual(placement.drive(row['path']), 'H')
        self.assertEqual(levels(row), (150, 95, 35, 25))

    def test_malformed_legacy_levels_refuse_as_configuration_errors(self):
        for tracker in (disk(wsl_source='unknown'), disk(wsl_source='path', queue_gb='95'), 'not a table'):
            with self.subTest(tracker=tracker):
                with self.assertRaises(config.ConfigError):
                    config.migrate_disk_config({'trackers': [tracker]})

class DiskConfig(unittest.TestCase):
    """Folder caches are refused at load when they could touch a repository, worktree, state or machine-repo path."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='disk-refusals-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.machine = self.root / 'machine-repos'
        self.machine.mkdir()
        self.repo = self.root / 'repo'
        (self.repo / '.git').mkdir(parents=True)
        self.plain = self.root / 'plain'
        self.plain.mkdir()

    def outcome(self, folder, *, env=None, trees=True):
        text = (f'repos_dir = "{self.machine.as_posix()}"\n[paths]\nstate_dir = "{(self.root / "state").as_posix()}"\n'
                f'repos_root = "{(self.root / "root").as_posix()}"\n'
                + (f'worktrees = "{(self.root / "trees").as_posix()}"\n' if trees else '') +
                f'[[disk_cleanup.caches]]\npath = "{Path(folder).as_posix()}"\nplacement = "wsl"\n')
        (self.home / 'config.toml').write_text(text, encoding='utf-8')
        try:
            config.load(self.home, env or {})
        except config.ConfigError as error:
            return str(error).replace(str(self.root), '<root>').replace(self.root.as_posix(), '<root>').replace('\\', '/')
        return 'accepted'

    def test_existing_safety_refusals(self):
        (self.machine / 'beta.toml').write_text(config.dumps(dict(
            name='beta', repo=str(self.root / 'beta'), worktrees=str(self.root / 'beta-trees'))), encoding='utf-8')
        (self.machine / 'gamma.toml').write_text(config.dumps(dict(name='gamma', repo=str(self.root / 'gamma'))),
                                                 encoding='utf-8')
        env = {'DROVER_PATHS_STATE_DIR': str(self.root / 'moved-state')}
        trace = [(name, self.outcome(folder, env=extra_env)) for name, folder, extra_env in (
            ('plain', self.plain / 'cache', None),
            ('inside-repository', self.repo / 'cache', None),
            ('above-repository', self.root, None),
            ('git-part', self.plain / '.git' / 'cache', None),
            ('venv-part', self.plain / '.venv', None),
            ('state', self.root / 'state' / 'cache', None),
            ('repos-root', self.root / 'root', None),
            ('worktrees', self.root / 'trees' / 'a', None),
            ('machine-repo', self.root / 'beta' / 'out', None),
            ('machine-worktrees', self.root / 'beta-trees', None),
            ('derived-worktrees-unused', self.root / 'gamma.worktrees' / 'x', None),
            ('environment-moves-state', self.root / 'moved-state' / 'cache', env))]
        trace.append(('derived-worktrees', self.outcome(self.root / 'gamma.worktrees' / 'x', trees=False)))
        (self.machine / 'broken.toml').write_text('[unterminated', encoding='utf-8')
        trace.append(('unreadable-machine-file', self.outcome(self.plain / 'cache')))
        (self.machine / 'broken.toml').write_text('_internal = 1\n', encoding='utf-8')
        trace.append(('internal-machine-key', self.outcome(self.plain / 'cache')))
        self.assertEqual(trace, EXPECTED_REFUSALS)
