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



TOKEN = '0123456789abcdef0123456789abcdef'     # a disk worker's run id: uuid4 hex, unique without a nonce


class MarkerSweep(unittest.TestCase):
    """On POSIX the worker's proof also ends every live process of this user carrying the run's exact marker: a
    `process` child leads its own session, so the group sweep alone misses what it leaves behind."""
    def setUp(self):
        import tempfile
        from pathlib import Path
        from drover import placement_process, placement_sweep
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.proc, self.pp = Path(temp.name), placement_process
        self.killed, self.groups = [], []
        self.clock = iter(range(0, 1000))
        self.sweep = placement_sweep
        for item in (patch.object(placement_sweep, 'PROC', self.proc),
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

    def gone(self, pid):
        import shutil
        shutil.rmtree(self.proc / str(pid), ignore_errors=True)

    def stop(self, status, kill=None):
        """stop_ci_children on a POSIX host over the fake table; by default a signal ends its targets at once."""
        def default_kill(pid, sig):
            self.gone(pid)
        def killpg(group, sig):
            self.groups.append((group, sig))
            for entry in list(self.proc.iterdir()):
                if entry.name.isdigit() and (entry / 'stat').read_text().split()[5] == str(group):
                    self.gone(entry.name)
        def record(pid, sig):
            self.killed.append((pid, sig))
            (kill or default_kill)(pid, sig)
        with patch.object(self.pp, 'IS_WINDOWS', False), \
                patch.object(self.pp.signal, 'SIGKILL', 9, create=True), \
                patch.object(self.pp.os, 'kill', side_effect=record), \
                patch.object(self.pp.os, 'killpg', side_effect=killpg, create=True), \
                patch.object(self.pp.time, 'monotonic', side_effect=lambda: next(self.clock)), \
                patch.object(self.pp.time, 'sleep'):
            return self.pp.stop_ci_children(status)

    def test_only_this_users_live_processes_with_the_exact_marker_match(self):
        me, marker = os.getpid(), f'DROVER_RUN_ID={TOKEN}'.encode()
        self.task(me, marker + b'\0', parent=999_999)                # the sweeper itself
        self.task(999_999, marker + b'\0')                             # and its parent
        self.task(101, b'HOME=/h\0' + marker + b'\0PATH=/bin\0')       # the only match
        self.task(102, marker + b'2\0')
        self.task(103, b'X' + marker + b'\0')
        self.task(104, marker[:-1] + b'\0')
        self.task(105, marker + b'\0', state='Z')
        self.task(106, 'unreadable')
        self.task(107, None)                                           # gone between listing and reading
        (self.proc / 'self').mkdir()
        entries = self.sweep.marks(dict(run_id=TOKEN))
        self.assertEqual(self.sweep.marked(entries), [101])
        with patch.object(os, 'getuid', return_value=os.stat(self.proc).st_uid + 1, create=True):
            self.assertEqual(self.sweep.marked(entries), [])        # another user's processes are never read

    def test_a_surviving_match_leaves_the_proof_unconfirmed_and_a_killed_one_confirms(self):
        for gone, confirmed in ((False, False), (True, True)):
            with self.subTest(gone=gone):
                self.killed.clear()
                self.task(101, f'DROVER_RUN_ID={TOKEN}\0'.encode())
                kill = None if gone else (lambda pid, sig: None)
                self.assertIs(self.stop(dict(pid=500, run_id=TOKEN), kill=kill), confirmed)
                self.assertEqual(self.killed[0], (101, 9))
                self.gone(101)

    def test_group_ownership_is_read_before_any_kill(self):
        """A killed member still exiting shows an empty environ; the group stays this run's, from the read before."""
        self.task(501, f'DROVER_RUN_ID={TOKEN}\0'.encode(), group=500)
        def emptied(pid, sig):
            (self.proc / str(pid) / 'environ').write_bytes(b'')        # exit_mm: still listed, environ empty
        self.assertIs(self.stop(dict(pid=500, run_id=TOKEN), kill=emptied), True)
        self.assertEqual(self.groups, [(500, 9)])

    def test_a_member_gone_on_the_ownership_read_is_gone_not_an_error(self):
        self.task(502, None, group=500)                                # listed, then reaped before its environ read
        self.assertIs(self.stop(dict(pid=500, run_id=TOKEN)), True)
        self.assertEqual(self.groups, [])

    def test_a_gate_marker_needs_its_nonce(self):
        """A gate's run id is a per-ledger integer: only the process with this launch's nonce is this run's."""
        self.task(601, b'DROVER_RUN_ID=7\0DROVER_RUN_ID_NONCE=aaa\0')
        self.task(602, b'DROVER_RUN_ID=7\0DROVER_RUN_ID_NONCE=bbb\0')
        self.task(603, b'DROVER_RUN_ID=7\0')
        self.assertIs(self.stop(dict(pid=500, run_id=7, nonce='aaa')), True)
        self.assertEqual(self.killed, [(601, 9)])
        self.killed.clear()
        self.assertIs(self.stop(dict(pid=500, run_id=7)), True)      # no nonce: an integer id alone sweeps nothing
        self.assertEqual(self.killed, [])

    def test_a_killed_process_still_exiting_keeps_the_proof_open(self):
        """Once killed, a process drops out of the marker read (its environ empties); it counts until it is gone."""
        self.task(101, f'DROVER_RUN_ID={TOKEN}\0'.encode())
        def exiting(pid, sig):
            (self.proc / str(pid) / 'environ').write_bytes(b'')        # exit_mm: still listed, not yet a zombie
        self.assertIs(self.stop(dict(pid=500, run_id=TOKEN), kill=exiting), False)

    def test_group_ownership_needs_the_nonce_too(self):
        self.task(501, b'DROVER_RUN_ID=7\0DROVER_RUN_ID_NONCE=bbb\0', group=500)     # same id, another ledger
        self.assertIs(self.stop(dict(pid=500, run_id=7, nonce='aaa')), False)
        self.assertEqual(self.groups, [])

    def test_an_unreadable_member_is_not_taken_for_gone(self):
        self.task(501, 'unreadable', group=500)                        # EACCES is not absence
        with self.assertRaises(OSError):
            self.stop(dict(pid=500, run_id=TOKEN))
        self.assertEqual(self.groups, [])

    def test_gate_children_carry_the_launch_nonce(self):
        import tempfile
        from drover import placement, runner_supervise
        seen = []
        def spawn(argv, out, stdin, cwd, env):
            seen.append(env)
            raise OSError('stop here')
        with tempfile.TemporaryDirectory() as runs, patch.object(placement, 'spawn_worker', side_effect=spawn), \
                patch.dict(os.environ, {'DROVER_RUN_NONCE': 'outer'}):
            runner_supervise.supervise('7', runs, ['gate'], nonce='n0nce')
        self.assertEqual((seen[0]['DROVER_RUN_ID'], seen[0]['DROVER_RUN_ID_NONCE']), ('7', 'n0nce'))
        self.assertNotIn('DROVER_RUN_NONCE', seen[0])      # a nested runner must not adopt this launch's nonce


if __name__ == '__main__':
    unittest.main()
