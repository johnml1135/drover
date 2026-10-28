import itertools
from types import SimpleNamespace
from unittest.mock import patch

from drover import config, disk_paths, placement
from tests.disk_fixture import DiskCase


def evidence(rows, windows=False):
    proof = placement._CleanupProcesses(windows=windows, proc_root='/nonexistent', request={})
    proof.rows[:] = rows
    proof.unknown = False
    return proof


class BusyIndex(DiskCase):
    ROWS = [('/work/alpha', ['python', '/opt/tool/run.py', '--out=/data/build one'], '/usr/bin/python'),
            ('/', ['sh', '-c', 'x'], '/bin/sh')]

    def test_matches_cwd_executable_and_argument_paths(self):
        proof = evidence(self.ROWS)
        for path in ('/work/alpha', '/work', '/opt/tool/run.py', '/opt/tool', '/data/build one', '/data', '/usr/bin/python'):
            with self.subTest(path=path):
                self.assertTrue(proof.busy(path))
        for path in ('/work/alphabet', '/opt/other', '/data/build one two', '/srv'):
            with self.subTest(path=path):
                self.assertFalse(proof.busy(path))

    def test_each_row_is_normalized_once_not_once_per_path(self):
        proof = evidence(self.ROWS * 50)
        calls = []
        real = placement.cleanup_normalize
        with patch.object(placement, 'cleanup_normalize', lambda *a, **k: calls.append(a) or real(*a, **k)) as _placement_patch_1, patch('drover.placement_host.cleanup_normalize', _placement_patch_1), patch('drover.placement_paths.cleanup_normalize', _placement_patch_1), patch('drover.placement_proof.cleanup_normalize', _placement_patch_1):
            for number in range(200):
                proof.busy(f'/tmp/x{number}')
        self.assertLess(len(calls), 100 * 5 + 200 * 2)


class Budget(DiskCase):
    def test_config_key_default_and_validation(self):
        self.assertEqual(config.validate({})['disk_cleanup']['temp_root_budget_s'], 300)
        self.assertEqual(config.validate(dict(disk_cleanup=dict(temp_root_budget_s=5)))['disk_cleanup']['temp_root_budget_s'], 5)
        for bad in (0, -1, float('inf'), 'x', True):
            with self.subTest(bad=bad):
                with self.assertRaises(config.ConfigError):
                    config.validate(dict(disk_cleanup=dict(temp_root_budget_s=bad)))

    def entries(self, count):
        return [self.file(self.temp_root / f'old{number:03}') for number in range(count)]

    def run_with_clock(self, budget, apply):
        self.cfg['disk_cleanup']['temp_root_budget_s'] = budget
        if apply:
            self.approve_report()  # the receipt covers the configured scope, budget included
        ticks = itertools.count()
        clock = SimpleNamespace(monotonic=lambda: float(next(ticks)))
        with patch.object(disk_paths, 'time', clock):
            return self.run_cleanup(apply=apply)

    def test_exceeded_budget_records_partial_and_stops_scanning(self):
        self.entries(40)
        result = self.run_with_clock(30, apply=False)
        partial = [i for i in result['items'] if i.get('reason_code') == 'temp_root_partial' and i['action'] == 'report']
        self.assertEqual([i['path'] for i in partial], [str(self.temp_root)])
        scanned = [i for i in result['items'] if i.get('reason_code') == 'stale_temp']
        self.assertTrue(0 < len(scanned) < 40, len(scanned))

    def test_apply_never_deletes_beyond_what_was_proven(self):
        files = self.entries(40)
        result = self.run_with_clock(30, apply=True)
        removed = {i['path'] for i in result['items'] if i.get('action') == 'removed'}
        self.assertTrue(any(i.get('reason_code') == 'temp_root_partial' for i in result['items']))
        self.assertEqual({str(p) for p in files if not p.exists()}, removed)
        self.assertTrue(any(p.exists() for p in files))

    def test_default_budget_scans_a_small_root_completely(self):
        self.entries(5)
        result = self.run_cleanup()
        self.assertFalse(any(i.get('reason_code') == 'temp_root_partial' for i in result['items']))
        self.assertEqual(len([i for i in result['items'] if i.get('reason_code') == 'stale_temp']), 5)
