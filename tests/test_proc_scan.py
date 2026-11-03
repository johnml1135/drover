"""The Windows process scan resolves every owner in one batched query, not one CIM call per process."""
import json
import os
import signal
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
                dict(ProcessId=7, Name='c.exe', ExecutablePath=None, CommandLine='c', SessionId=1, OwnerSid='S-2')])).encode(),
                b'')

        with patch.object(process.subprocess, 'run', run), \
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
# The child leaves a descendant that holds no pipe and exits at once, as a daemon started by a tool would.
LINGER = ('import subprocess, sys\n'
          "descendant = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'],\n"
          '                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n'
          'print(descendant.pid, flush=True)\n')


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

    def test_a_descendant_survives_a_run_that_ends_in_time(self):
        result = process.run(process.Command((sys.executable, '-c', LINGER), 10))
        self.assertEqual((result.kind, result.code), ('exited', 0))
        pid = int(result.out)
        try:
            self.assertFalse(gone(pid, within=0.5), 'closing the job ended a descendant of a run that finished')
        finally:
            if not gone(pid, within=0):
                os.kill(pid, signal.SIGTERM)


if __name__ == '__main__':
    unittest.main()


class MarkerSweep(unittest.TestCase):
    """On POSIX the worker's proof also ends every live process of this user carrying the run's exact marker: a
    `process` child leads its own session, so the group sweep alone misses what it leaves behind."""
    def setUp(self):
        import tempfile
        from pathlib import Path
        from drover import placement_process
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.proc, self.pp = Path(temp.name), placement_process
        for item in (patch.object(placement_process, '_PROC', self.proc),
                     patch.object(os, 'getuid', return_value=os.stat(temp.name).st_uid, create=True)):
            item.start()
            self.addCleanup(item.stop)

    def task(self, pid, environ, parent=1, group=None, state='S'):
        entry = self.proc / str(pid)
        entry.mkdir()
        (entry / 'stat').write_text(f'{pid} (fake tool) {state} {parent} {group or pid} 0 0\n')
        if isinstance(environ, bytes):
            (entry / 'environ').write_bytes(environ)
        elif environ == 'unreadable':
            (entry / 'environ').mkdir()     # reading it raises, as EACCES does
        return entry

    def test_only_this_users_live_processes_with_the_exact_marker_match(self):
        me = os.getpid()
        self.task(me, b'DROVER_RUN_ID=tok\0', parent=999_999)       # the sweeper itself
        self.task(999_999, b'DROVER_RUN_ID=tok\0')                    # and its parent
        self.task(101, b'HOME=/h\0DROVER_RUN_ID=tok\0PATH=/bin\0')    # the only match
        self.task(102, b'DROVER_RUN_ID=tok2\0')
        self.task(103, b'XDROVER_RUN_ID=tok\0')
        self.task(104, b'DROVER_RUN_ID=to\0')
        self.task(105, b'DROVER_RUN_ID=tok\0', state='Z')
        self.task(106, 'unreadable')
        self.task(107, None)                                           # gone between listing and reading
        (self.proc / 'self').mkdir()
        self.assertEqual(self.pp._marked('tok'), [101])
        with patch.object(os, 'getuid', return_value=os.stat(self.proc).st_uid + 1, create=True):
            self.assertEqual(self.pp._marked('tok'), [])                # another user's processes are never read

    def test_a_surviving_match_leaves_the_proof_unconfirmed_and_a_killed_one_confirms(self):
        killed = []
        clock = iter(range(0, 1000))
        def kill(pid, sig, gone=True):
            killed.append((pid, sig))
            if gone:
                import shutil
                shutil.rmtree(self.proc / str(pid))
        for gone, confirmed in ((False, False), (True, True)):
            with self.subTest(gone=gone):
                killed.clear()
                self.task(101, b'DROVER_RUN_ID=tok\0')
                with patch.object(self.pp, 'IS_WINDOWS', False), \
                        patch.object(self.pp.signal, 'SIGKILL', 9, create=True), \
                        patch.object(self.pp.os, 'kill', side_effect=lambda pid, sig: kill(pid, sig, gone)), \
                        patch.object(self.pp.time, 'monotonic', side_effect=lambda: next(clock)), \
                        patch.object(self.pp.time, 'sleep'):
                    self.assertIs(self.pp.stop_ci_children(dict(pid=500, run_id='tok')), confirmed)
                self.assertEqual(killed[0], (101, 9))
                if not gone:
                    import shutil
                    shutil.rmtree(self.proc / '101')
