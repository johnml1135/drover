"""Setup emits physical drive defaults, independent of drive capacity."""
import unittest

from drover import config_defaults, placement, wizard

KEYS = ('warn_gb', 'queue_gb', 'hold_gb', 'critical_gb')


class Setup(unittest.TestCase):
    def test_physical_defaults_without_virtual_disk_tracker(self):
        def trackers(platform, env):
            return config_defaults.host_disk_trackers(config_defaults.windows_or_wsl(platform, env))

        windows = trackers('win32', {})
        wsl = trackers('linux', {'WSL_DISTRO_NAME': 'Fixture'})
        self.assertEqual(wsl, windows)
        self.assertNotEqual(trackers('linux', {}), windows)
        self.assertEqual({placement.drive(t['path']) for t in windows}, {'H', 'C'})
        for row in windows:
            self.assertEqual(row['kind'], 'disk')
            self.assertNotIn('wsl_source', row)
            self.assertEqual(tuple(row[k] for k in KEYS),
                             (60, 40, 25, 15) if placement.drive(row['path']) == 'C' else (150, 100, 50, 25))
        linux = trackers('linux', {})
        self.assertEqual(len(linux), 1)
        self.assertEqual(linux[0]['path'], '/')
        self.assertEqual(tuple(linux[0][k] for k in KEYS), (150, 100, 50, 25))
        for capacity in (128, 1000, 8000):
            rows, notes = wizard.scale_levels(64, [
                {'path': 'C:/', 'free_gb': 10, 'total_gb': capacity},
                {'path': 'R:/', 'free_gb': 500, 'total_gb': capacity}], 32, {'R'})
            disks = {t['name']: t for t in rows if t['kind'] == 'disk' and t.get('enabled', True)}
            self.assertEqual(set(disks), {'c', 'r'})
            self.assertEqual(tuple(disks['c'][k] for k in KEYS), (60, 40, 25, 15))
            self.assertEqual(tuple(disks['r'][k] for k in KEYS), (150, 100, 50, 25))
            self.assertTrue(all('wsl_source' not in t for t in rows))
            self.assertTrue(any('WARNING' in note and 'C:' in note for note in notes))
            ram = next(t for t in rows if t['kind'] == 'ram')
            self.assertEqual((ram['queue_base_gb'], ram['hold_gb']), (6, 4))
