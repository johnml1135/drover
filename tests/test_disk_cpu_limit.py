"""Disk cleanup pins itself to a few CPUs instead of stopping when the machine is busy."""
import os
import tempfile
import unittest
from unittest.mock import patch

from drover import config, placement, placement_worker


class LimitCpus(unittest.TestCase):
    def test_limit_cpus_masks_first_four_of_eight(self):
        masks = []
        got = placement.limit_cpus(4, windows=True, set_mask=masks.append, available=8)
        self.assertEqual(masks, [0b1111])
        self.assertEqual(got, 4)

    def test_limit_cpus_never_exceeds_available(self):
        masks = []
        got = placement.limit_cpus(4, windows=True, set_mask=masks.append, available=2)
        self.assertEqual(got, 2)
        self.assertTrue(all(mask == 0b11 for mask in masks))

    def test_limit_cpus_wsl_uses_sched_setaffinity(self):
        with patch.object(os, 'sched_setaffinity', create=True) as setter:
            got = placement.limit_cpus(4, windows=False, available=8)
        setter.assert_called_once_with(0, {0, 1, 2, 3})
        self.assertEqual(got, 4)

    def test_failure_to_limit_never_fails_cleanup(self):
        def refuse(mask):
            raise OSError('denied')
        self.assertEqual(placement.limit_cpus(4, windows=True, set_mask=refuse, available=8), 8)
        with patch.object(os, 'sched_setaffinity', side_effect=OSError, create=True):
            self.assertEqual(placement.limit_cpus(4, windows=False, available=8), 8)

    def test_the_worker_applies_the_configured_cap_beside_the_priority(self):
        calls = []
        with tempfile.TemporaryDirectory() as state,                 patch.object(placement_worker, 'IS_WINDOWS', False),                 patch.object(placement_worker, 'lower_priority'),                 patch.object(placement_worker, 'limit_cpus', side_effect=calls.append),                 patch.object(os, 'setsid', create=True), patch('sys.stdout'):
            with self.assertRaises(KeyError):   # the stage itself is not under test
                placement_worker.cleanup_worker(dict(_worker_id='cap', state_dir=state, paths=dict(cpu_limit=3)))
        self.assertEqual(calls, [3])


class CpuLimitConfig(unittest.TestCase):
    def test_default_is_four(self):
        self.assertEqual(config.validate({})['disk_cleanup']['cpu_limit'], 4)

    def test_must_be_a_positive_int(self):
        self.assertEqual(config.validate({'disk_cleanup': {'cpu_limit': 2}})['disk_cleanup']['cpu_limit'], 2)
        for bad in (0, -1, 1.5, True, '4', None):
            with self.subTest(bad), self.assertRaises(config.ConfigError):
                config.validate({'disk_cleanup': {'cpu_limit': bad}})


if __name__ == '__main__':
    unittest.main()
