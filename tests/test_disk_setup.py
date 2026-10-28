"""Setup emits physical drive defaults, independent of drive capacity."""
import os
import unittest
from unittest.mock import patch

from drover import placement, wizard

KEYS = ('warn_gb', 'queue_gb', 'hold_gb', 'critical_gb')


class Setup(unittest.TestCase):
    def test_physical_defaults_without_virtual_disk_tracker(self):
        default_disk_trackers = placement.default_disk_trackers
        with patch.object(placement, 'IS_WINDOWS', True) as _placement_patch_1, patch('drover.placement_exec.IS_WINDOWS', _placement_patch_1), patch('drover.placement_git.IS_WINDOWS', _placement_patch_1), patch('drover.placement_host.IS_WINDOWS', _placement_patch_1), patch('drover.placement_paths.IS_WINDOWS', _placement_patch_1), patch('drover.placement_process.IS_WINDOWS', _placement_patch_1), patch('drover.placement_proof.IS_WINDOWS', _placement_patch_1), patch('drover.placement_tree.IS_WINDOWS', _placement_patch_1), patch('drover.placement_worker.IS_WINDOWS', _placement_patch_1), patch.dict(os.environ, {}, clear=True):
            windows = default_disk_trackers()
        with patch.object(placement, 'IS_WINDOWS', False) as _placement_patch_2, patch('drover.placement_exec.IS_WINDOWS', _placement_patch_2), patch('drover.placement_git.IS_WINDOWS', _placement_patch_2), patch('drover.placement_host.IS_WINDOWS', _placement_patch_2), patch('drover.placement_paths.IS_WINDOWS', _placement_patch_2), patch('drover.placement_process.IS_WINDOWS', _placement_patch_2), patch('drover.placement_proof.IS_WINDOWS', _placement_patch_2), patch('drover.placement_tree.IS_WINDOWS', _placement_patch_2), patch('drover.placement_worker.IS_WINDOWS', _placement_patch_2), \
                patch.dict(os.environ, {'WSL_DISTRO_NAME': 'Fixture'}, clear=True):
            wsl = default_disk_trackers()
        self.assertEqual(wsl, windows)
        self.assertEqual({placement.drive(t['path']) for t in windows}, {'H', 'C'})
        for row in windows:
            self.assertEqual(row['kind'], 'disk')
            self.assertNotIn('wsl_source', row)
            self.assertEqual(tuple(row[k] for k in KEYS),
                             (60, 40, 25, 15) if placement.drive(row['path']) == 'C' else (150, 100, 50, 25))
        with patch.object(placement, 'IS_WINDOWS', False) as _placement_patch_3, patch('drover.placement_exec.IS_WINDOWS', _placement_patch_3), patch('drover.placement_git.IS_WINDOWS', _placement_patch_3), patch('drover.placement_host.IS_WINDOWS', _placement_patch_3), patch('drover.placement_paths.IS_WINDOWS', _placement_patch_3), patch('drover.placement_process.IS_WINDOWS', _placement_patch_3), patch('drover.placement_proof.IS_WINDOWS', _placement_patch_3), patch('drover.placement_tree.IS_WINDOWS', _placement_patch_3), patch('drover.placement_worker.IS_WINDOWS', _placement_patch_3), patch.dict(os.environ, {}, clear=True):
            linux = default_disk_trackers()
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
