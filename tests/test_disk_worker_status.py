"""A worker that dies at startup says how: its exit status and stderr reach the report."""
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from drover import disk_runtime, placement


class WorkerExitStatusTests(unittest.TestCase):
    def call(self, returncode, stderr):
        with tempfile.TemporaryDirectory() as state:
            request = dict(stage=1, apply=False, now=time.time(), state_dir=state, repos=[], tasks={},
                           drive=dict(path=state), paths={})
            done = subprocess.CompletedProcess(['worker'], returncode, '', stderr)
            with patch.object(subprocess, 'run', return_value=done):
                return disk_runtime.cleanup_call('windows', request)['errors'][0]

    def test_silent_startup_death_reports_its_exit_status(self):
        error = self.call(0xC0000142, '')
        self.assertEqual(error['reason_code'], 'worker_error')
        self.assertEqual(error['exit_status'], 0xC0000142)
        self.assertIn('0xc0000142', error['error'].lower())

    def test_stderr_is_kept_beside_the_status(self):
        error = self.call(1, 'ImportError: boom')
        self.assertIn('ImportError: boom', error['error'])
        self.assertEqual(error['exit_status'], 1)
        self.assertNotEqual(error['error'].strip(), '')

    def test_worker_starts_without_inheriting_a_console(self):
        """A console that died with the shell must not reach the worker."""
        flags = dict(creationflags=0x08000000)
        with tempfile.TemporaryDirectory() as state, patch.object(placement, 'NO_WINDOW', flags), \
                patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess(['worker'], 1, '', '')) as run:
            disk_runtime.cleanup_call('windows', dict(stage=1, apply=False, now=time.time(), state_dir=state, repos=[],
                                                      tasks={}, drive=dict(path=state), paths={}))
        self.assertEqual(run.call_args_list[0].kwargs['creationflags'], 0x08000000)


if __name__ == '__main__':
    unittest.main()
