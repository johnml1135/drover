"""Disk cleanup pins itself to a few CPUs instead of stopping when the machine is busy."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from drover import config, disk_runtime, placement, placement_worker

ROOT = Path(__file__).resolve().parents[1]
EIGHT = range(8)
# Runs in its own process, which is the only one whose priority and affinity it sets. It reads them back through its own
# kernel32 with its own types, so a mistake in Drover's types cannot agree with itself.
WINDOWS_CHILD = r'''
import ctypes, json, subprocess, sys
from ctypes import wintypes as w
k = ctypes.WinDLL('kernel32', use_last_error=True)
k.GetCurrentProcess.restype = w.HANDLE
k.GetCurrentProcess.argtypes = []
k.GetPriorityClass.restype = w.DWORD
k.GetPriorityClass.argtypes = [w.HANDLE]
k.GetProcessAffinityMask.restype = w.BOOL
k.GetProcessAffinityMask.argtypes = [w.HANDLE, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)]
k.SetProcessAffinityMask.restype = w.BOOL
k.SetProcessAffinityMask.argtypes = [w.HANDLE, ctypes.c_size_t]
def mask():
    process, system = ctypes.c_size_t(), ctypes.c_size_t()
    assert k.GetProcessAffinityMask(k.GetCurrentProcess(), ctypes.byref(process), ctypes.byref(system))
    return process.value
def state():
    return dict(mask=mask(), priority=k.GetPriorityClass(k.GetCurrentProcess()))
if sys.argv[1] == 'report':
    print(json.dumps(state()))
    raise SystemExit
sys.path.insert(0, sys.argv[2])
from drover import config, disk_cleanup, disk_runtime, placement
start = state()
bits = [cpu for cpu in range(64) if start['mask'] >> cpu & 1]
if sys.argv[1] == 'skewed':          # an inherited affinity that does not start at CPU 0
    bits = bits[1:4]
    assert k.SetProcessAffinityMask(k.GetCurrentProcess(), sum(1 << cpu for cpu in bits))
    print(json.dumps(dict(bits=bits, kept=placement.limit_cpus(2), after=state())))
elif sys.argv[1] == 'direct':
    took = placement.lower_priority()
    kept = placement.limit_cpus(2)
    child = subprocess.run([sys.executable, '-c', sys.argv[3], 'report'], capture_output=True, text=True, check=True)
    print(json.dumps(dict(bits=bits, took=took, kept=kept, start=start, after=state(), child=json.loads(child.stdout))))
else:                                # the detached coordinator, up to the moment it would start work
    seen = []
    disk_cleanup.run = lambda cfg, **options: seen.append(state())
    disk_runtime.run_detached({'disk_cleanup': {'cpu_limit': 2}}, False)
    print(json.dumps(dict(bits=bits, ran=seen)))
'''


@unittest.skipUnless(os.name == 'nt', 'the real Windows calls')
class RealWindows(unittest.TestCase):
    """The API itself, which every other test here replaces. Only the child process changes."""
    BELOW_NORMAL = 0x4000

    def child(self, mode):
        done = subprocess.run([sys.executable, '-c', WINDOWS_CHILD, mode, str(ROOT), WINDOWS_CHILD], capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def needs_cpus(self):
        if (os.process_cpu_count() or 1) < 4:
            self.skipTest('needs 4 CPUs')

    def test_priority_and_cpus_really_change_and_children_inherit_them(self):
        self.needs_cpus()
        got = self.child('direct')
        self.assertTrue(got['took'])
        self.assertEqual(got['kept'], 2)
        self.assertEqual(got['after']['priority'], self.BELOW_NORMAL)
        first_two = sum(1 << cpu for cpu in got['bits'][:2])
        self.assertEqual(got['after']['mask'], first_two)
        self.assertNotEqual(got['start']['mask'], first_two)
        self.assertEqual(got['child'], got['after'])

    def test_the_first_cpus_are_the_first_of_the_current_affinity(self):
        self.needs_cpus()
        got = self.child('skewed')
        self.assertEqual(got['kept'], 2)
        self.assertEqual(got['after']['mask'], sum(1 << cpu for cpu in got['bits'][:2]))

    def test_the_detached_coordinator_is_capped_before_it_works(self):
        self.needs_cpus()
        got = self.child('coordinator')
        self.assertEqual(got['ran'], [dict(mask=sum(1 << cpu for cpu in got['bits'][:2]), priority=self.BELOW_NORMAL)])


class Coordinator(unittest.TestCase):
    def test_the_enqueued_process_runs_the_capped_entry_point(self):
        argv = {}

        def detach(command, env, stream):
            argv['command'] = command
            return type('Proc', (), dict(pid=1))(), None
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as state, \
                patch.object(placement, 'detach', side_effect=detach), \
                patch.object(placement, 'cleanup_identity', return_value=dict(pid=1, birth='b', side='windows')):
            disk_runtime.cleanup_enqueue({'paths': {'state_dir': state}}, dict(applied=False, id='c'))
        self.assertIn('run_detached', argv['command'][2])

    def test_no_cap_configured_runs_uncapped(self):
        with patch.object(placement, 'lower_priority') as lower, patch.object(placement, 'limit_cpus') as limit, \
                patch('drover.disk_cleanup.run', return_value='ran') as run:
            self.assertEqual(disk_runtime.run_detached({}, True), 'ran')
        lower.assert_called_once_with()
        limit.assert_not_called()
        run.assert_called_once_with({}, mode='triggered', apply=True)


class LimitCpus(unittest.TestCase):
    def test_limit_cpus_masks_first_four_of_eight(self):
        masks = []
        got = placement.limit_cpus(4, windows=True, set_mask=masks.append, available=EIGHT)
        self.assertEqual(masks, [0b1111])
        self.assertEqual(got, 4)

    def test_limit_cpus_takes_the_first_of_the_current_set(self):
        masks = []
        self.assertEqual(placement.limit_cpus(2, windows=True, set_mask=masks.append, available=[9, 4, 6, 12]), 2)
        self.assertEqual(masks, [1 << 4 | 1 << 6])

    def test_limit_cpus_never_exceeds_available(self):
        masks = []
        got = placement.limit_cpus(4, windows=True, set_mask=masks.append, available=range(2))
        self.assertEqual(got, 2)
        self.assertEqual(masks, [])                 # already within the cap: nothing to set

    def test_limit_cpus_wsl_uses_sched_setaffinity(self):
        with patch.object(os, 'sched_setaffinity', create=True) as setter:
            got = placement.limit_cpus(4, windows=False, available=EIGHT)
        setter.assert_called_once_with(0, {0, 1, 2, 3})
        self.assertEqual(got, 4)

    def test_failure_to_limit_never_fails_cleanup(self):
        def refuse(mask):
            raise OSError('denied')
        self.assertEqual(placement.limit_cpus(4, windows=True, set_mask=refuse, available=EIGHT), 8)
        with patch.object(os, 'sched_setaffinity', side_effect=OSError, create=True):
            self.assertEqual(placement.limit_cpus(4, windows=False, available=EIGHT), 8)

    def test_a_refused_priority_change_is_reported_not_raised(self):
        def refuse(value):
            raise OSError('denied')
        self.assertFalse(placement.lower_priority(windows=True, set_class=refuse))
        self.assertTrue(placement.lower_priority(windows=True, set_class=lambda value: None))

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
