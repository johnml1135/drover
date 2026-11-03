"""The Windows process scan resolves every owner in one batched query, not one CIM call per process."""
import json
import os
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from drover import placement, process


class BatchedOwners(unittest.TestCase):
    def test_script_does_not_invoke_a_cim_method_per_process(self):
        seen = []

        def run(argv, **kwargs):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, json.dumps(dict(sid='S-1', processes=[
                dict(ProcessId=5, Name='a.exe', ExecutablePath=None, CommandLine='a', SessionId=1, OwnerSid='S-1'),
                dict(ProcessId=6, Name='b.exe', ExecutablePath=None, CommandLine='b', SessionId=0, OwnerSid=None),
                dict(ProcessId=7, Name='c.exe', ExecutablePath=None, CommandLine='c', SessionId=1, OwnerSid='S-2')])), '')

        with patch.object(placement.subprocess, 'run', run), \
                patch('drover.placement_process._windows_image_path', return_value=None):
            rows = placement._cim_rows(5)
        self.assertEqual(len(seen), 1)
        self.assertNotIn('Invoke-CimMethod', seen[0][-1])
        self.assertEqual([r['same_user'] for r in rows], [True, None, False])

    @unittest.skipUnless(sys.platform == 'win32', 'real Windows process table')
    def test_real_scan_is_fast_and_knows_its_own_children(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            started = time.monotonic()
            rows = placement._cim_rows(60)
            elapsed = time.monotonic() - started
            mine = [r for r in rows if r['pid'] == child.pid]
            self.assertEqual(len(mine), 1)
            self.assertIs(mine[0]['same_user'], True)
            self.assertLess(elapsed, 15)
        finally:
            child.kill()
            child.wait()


# The child starts a grandchild that inherits the output pipes, as Git\cmd\git.exe starts mingw64's git.exe.
TREE = ('import subprocess, sys, time\n'
        "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(6)'])\n"
        'print(grandchild.pid, flush=True)\n'
        'time.sleep(6)\n')


def gone(pid, within):
    """Whether `pid` has exited, waiting up to `within` seconds."""
    if sys.platform == 'win32':
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x00100000, False, pid)      # SYNCHRONIZE
        if not handle:
            return True
        try:
            return kernel.WaitForSingleObject(ctypes.c_void_p(handle), int(within * 1000)) == 0
        finally:
            kernel.CloseHandle(ctypes.c_void_p(handle))
    deadline = time.monotonic() + within
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


class ProcessTree(unittest.TestCase):
    """Real processes, here because this module may spawn: `process.run`'s timeout ends the whole tree."""

    def test_a_timeout_ends_the_grandchild_and_returns_promptly(self):
        started = time.monotonic()
        result = process.run(process.Command((sys.executable, '-c', TREE), 1))
        took = time.monotonic() - started
        self.assertEqual(result.kind, 'timed_out')
        self.assertLess(took, 3.5)
        self.assertLess(result.elapsed_s, 3.5)
        self.assertTrue(result.out.strip(), 'the child printed no grandchild pid before the timeout')
        self.assertTrue(gone(int(result.out.split()[0]), within=2), 'the grandchild outlived the timeout')


if __name__ == '__main__':
    unittest.main()
