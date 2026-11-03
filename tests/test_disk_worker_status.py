"""A worker that dies at startup says how: its exit status and stderr reach the report. The worker's start and its
startup proof reach the one fake at `process`, with the console flag."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from drover import disk_runtime, placement, placement_exec, process
from tests.fakeci import FakeCommands, process_options

TOKEN = 't0k3n'
CODE = ('import json,sys;sys.path.insert(0,sys.argv[1]);'
        'from drover.placement import cleanup_worker;cleanup_worker(json.load(sys.stdin))')
ROOT = str(Path(disk_runtime.__file__).resolve().parents[1])
NATIVE = 'windows' if placement.IS_WINDOWS else 'wsl'    # this host's own side: no path translation


class WorkerExitStatusTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name) / 'state'
        self.state.mkdir()
        self.fake = FakeCommands(Path(temp.name) / 'fake')
        job = SimpleNamespace(terminate=lambda: True, close=lambda: None)
        for item in (mock.patch('subprocess.run', side_effect=AssertionError('real process')),
                     mock.patch('uuid.uuid4', return_value=SimpleNamespace(hex=TOKEN)),
                     mock.patch.object(placement, '_CleanupJob', return_value=job),
                     mock.patch.object(placement, 'recover_ci_children', return_value=True)):
            item.start()
            self.addCleanup(item.stop)

    def request(self, **paths):
        return dict(stage=1, apply=False, now=time.time(), state_dir=str(self.state), repos=[], tasks={},
                    drive=dict(path=str(self.state)), paths=paths)

    def expect_worker(self, timeout=1800, **answer):
        self.fake.expect([sys.executable, '-c', CODE, ROOT, TOKEN], **answer,
                         **process_options(timeout, env={**os.environ, 'DROVER_RUN_ID': TOKEN}, input=mock.ANY))

    def call(self, returncode, stderr):
        self.expect_worker(returncode=returncode, stderr_output=stderr)
        with self.fake.patch(process):
            return disk_runtime.cleanup_call(NATIVE, self.request())['errors'][0]

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

    def test_cleanup_worker_starts_with_the_console_flag(self):
        """A console that died with the shell must reach neither the worker (disk_runtime:92) nor its startup proof
        (:141): both go through `process`, which adds the flag."""
        self.fake.expect(['wsl.exe', '-d', 'Dist', '--', 'env', 'DROVER_RUN_ID=' + TOKEN, 'python3', '-c', CODE,
                          '/w/' + ROOT, TOKEN], returncode=3,
                         **process_options(11, env={**os.environ, 'DROVER_RUN_ID': TOKEN}, input=mock.ANY))
        self.fake.expect(['wsl.exe', '-d', 'Dist', '--exec', 'python3', '-c', placement._CLEANUP_STARTUP_PROOF, TOKEN],
                         **process_options(30))
        with self.fake.patch(process), mock.patch.object(placement, 'IS_WINDOWS', True), \
                mock.patch.object(placement, 'to_wsl_path', lambda p: '/w/' + str(p)), \
                mock.patch.object(placement, 'cleanup_path_side', lambda p: 'wsl'), \
                mock.patch.object(placement, 'distro', lambda: 'Dist'), \
                mock.patch.object(placement, 'detect_vhdx_drives', lambda: {}), \
                mock.patch.object(placement_exec, '_settings', lambda: {'wsl_distro': 'Dist'}):
            answer = disk_runtime.cleanup_call('wsl', self.request(worker_timeout_s=11))
        self.assertEqual(answer['errors'][0]['reason_code'], 'worker_error')
        self.assertTrue(answer['errors'][0]['termination_confirmed'])
        for _, options in self.fake.calls:
            self.assertEqual({key: options.get(key) for key in process.NO_WINDOW}, dict(process.NO_WINDOW))
        if sys.platform == 'win32':
            self.assertEqual(process.NO_WINDOW, {'creationflags': 0x08000000})
        self.assertEqual(json.loads(self.fake.calls[0][1]['input'])['_worker_id'], TOKEN)

    def test_worker_timeout_keeps_todays_error_row(self):
        """A timed-out worker that did claim its work: the row keeps the reason, the partial output and stderr."""
        claim = self.state / 'disk' / f'worker-{TOKEN}.json'
        claim.parent.mkdir()
        claim.write_text(json.dumps(dict(pid=4242, run_id=TOKEN)), encoding='utf-8')
        self.expect_worker(11, error=subprocess.TimeoutExpired('worker', 11, output=b'partial', stderr=b'late'))
        with self.fake.patch(process):
            answer = disk_runtime.cleanup_call(NATIVE, self.request(worker_timeout_s=11))
        row = dict(reason_code='worker_timeout', output='partial', error='late', termination_confirmed=True,
                   work_started=True)
        self.assertEqual(answer, dict(items=[], errors=[row], reclaimed_bytes=0))
        saved = self.state / 'disk' / f'worker-error-{TOKEN}.json'
        self.assertEqual(json.loads(saved.read_text(encoding='utf-8')), row)


if __name__ == '__main__':
    unittest.main()
