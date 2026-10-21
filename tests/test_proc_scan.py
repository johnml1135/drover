"""The Windows process scan resolves every owner in one batched query, not one CIM call per process."""
import json
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from drover import placement


class BatchedOwners(unittest.TestCase):
    def test_script_does_not_invoke_a_cim_method_per_process(self):
        seen = []

        def run(argv, **kwargs):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, json.dumps(dict(sid='S-1', processes=[
                dict(ProcessId=5, Name='a.exe', ExecutablePath=None, CommandLine='a', SessionId=1, OwnerSid='S-1'),
                dict(ProcessId=6, Name='b.exe', ExecutablePath=None, CommandLine='b', SessionId=0, OwnerSid=None),
                dict(ProcessId=7, Name='c.exe', ExecutablePath=None, CommandLine='c', SessionId=1, OwnerSid='S-2')])), '')

        with patch.object(placement.subprocess, 'run', run):
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


if __name__ == '__main__':
    unittest.main()
