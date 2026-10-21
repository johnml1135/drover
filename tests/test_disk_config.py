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
            with patch.object(placement, 'vhdx_dir', return_value='/mnt/h/WSL/Fixture'):
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
            with patch.object(placement, 'vhdx_dir', return_value=None):
                self.assertEqual(cli('--apply')[0], 2)
            self.assertEqual(path.read_bytes(), unresolved_bytes)
            self.assertEqual(list(state.glob('*bak*')), backups)

    def test_native_windows_virtual_source_delegates_host_resolution(self):
        raw = {'trackers': [disk('legacy', '~/repos', wsl_source='vhdx', queue_gb=95, hold_gb=35)]}
        with patch.object(placement, 'IS_WINDOWS', True), \
                patch.object(placement, 'detect_vhdx_drives', return_value={'Fixture': 'H:'}), \
                patch.object(placement, 'distro', return_value='Fixture'), \
                patch.object(placement, 'vhdx_dir', side_effect=AssertionError('WSL-only helper')):
            row = config.migrate_disk_config(raw)['trackers'][0]
        self.assertEqual(placement.drive(row['path']), 'H')
        self.assertEqual(levels(row), (150, 95, 35, 25))

    def test_malformed_legacy_levels_refuse_as_configuration_errors(self):
        for tracker in (disk(wsl_source='unknown'), disk(wsl_source='path', queue_gb='95'), 'not a table'):
            with self.subTest(tracker=tracker):
                with self.assertRaises(config.ConfigError):
                    config.migrate_disk_config({'trackers': [tracker]})
