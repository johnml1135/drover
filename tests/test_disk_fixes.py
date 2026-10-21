"""Safety regressions through Placement and the cleanup public interfaces."""
import json
import os
from types import SimpleNamespace
import subprocess
import sys
from unittest.mock import patch

from drover import placement
from tests.disk_fixture import DiskCase, empty_result


class PlacementSafety(DiskCase):
    def request(self, stage=2):
        return dict(stage=stage, apply=True, now=self.now, state_dir=str(self.state),
                    repos=[], tasks={}, drive=dict(path='H:/', level='queue', warn_gb=150),
                    paths=dict(temp_roots=['H:/temp', '/home/user/temp'], report_roots=[], caches=[]))

    def test_windows_roots_never_reach_linux_worker(self):
        observed = []
        def execute(argv, **kwargs):
            observed.append(json.loads(kwargs['input']))
            from drover.disk_state import save_file
            payload = observed[-1]
            save_file(self.state, 'worker-' + payload['_worker_id'] + '.json', dict(
                pid=123456789, run_id=payload['_worker_id']))
            return subprocess.CompletedProcess(argv, 0, json.dumps(empty_result()), '')
        with patch.object(placement, 'IS_WINDOWS', True), patch.object(subprocess, 'run', side_effect=execute):
            with patch.object(placement, 'recover_ci_children', return_value=True):
                placement.cleanup_call('wsl', self.request())
        self.assertEqual(observed[0]['paths']['temp_roots'], ['/home/user/temp'])

    def test_empty_temp_policy_stays_empty(self):
        from drover.disk_side import serve
        old = self.file(self.temp_root / 'unconfigured')
        request = self.request()
        request['paths']['temp_roots'] = []
        request['drive']['path'] = str(self.root)
        with patch.object(placement, 'physical_disk_path', return_value=str(self.root)), \
                patch('tempfile.gettempdir', return_value=str(self.temp_root)), \
                patch.object(placement, 'cleanup_processes', return_value=type('Idle', (), {'busy': lambda *a: False})()):
            serve(request)
        self.assertTrue(old.exists())

    def test_linux_longest_mount_is_used_for_measurement_and_scope(self):
        mountinfo = '1 0 8:1 / / rw - ext4 /dev/root rw\n2 1 8:2 / /data rw - ext4 /dev/data rw\n3 2 8:3 / /data/deep\\040space rw - ext4 /dev/extra rw\n'
        with patch.object(placement, 'IS_WINDOWS', False), \
                patch.dict('os.environ', {'WSL_DISTRO_NAME': ''}), \
                patch('pathlib.Path.read_text', return_value=mountinfo):
            self.assertEqual(placement.physical_disk_path('/data/build'), '/data')
            self.assertEqual(placement.physical_disk_path('/database/build'), '/')
            self.assertEqual(placement.physical_disk_path('/data/../home/build'), '/')
            self.assertEqual(placement.physical_disk_path('/data/deep space/build'), '/data/deep space')
            request = self.request()
            request['drive']['path'] = '/data'
            self.assertTrue(placement.cleanup_contains(request, '/data/build'))
            self.assertFalse(placement.cleanup_contains(request, '/home/build'))
            seen = []
            with patch('shutil.disk_usage', side_effect=lambda p: seen.append(p) or type('Usage', (), {'free': 10})()):
                placement.cleanup_free('/data/build')
            self.assertEqual(seen, ['/data'])

    def test_worker_timeout_is_reported_after_termination_confirmation(self):
        from drover import disk_cleanup, disk_state
        self.approve_report()
        confirmed = []
        def timeout(argv, **kwargs):
            request = json.loads(kwargs['input'])
            if '_worker_id' in request:
                disk_state.save_file(self.state, 'worker-' + request['_worker_id'] + '.json',
                                     dict(pid=123456789, runner_pid=123456789, run_id=request['_worker_id']))
            raise subprocess.TimeoutExpired(argv, 600, output='partial evidence', stderr='worker stalled')
        def confirm(*args):
            self.assertTrue((self.state / 'disk-cleanup.lock').exists())
            confirmed.append(True)
            return True
        rt = self.runtime()
        with patch.object(subprocess, 'run', side_effect=timeout), \
                patch.object(placement, 'physical_disk_path', return_value=str(self.root)), \
                patch.object(placement, 'process_cmdlines', return_value=[]), \
                patch.object(placement, 'recover_ci_children', side_effect=confirm), \
                patch.object(placement, '_CleanupJob', return_value=SimpleNamespace(
                    terminate=confirm, close=lambda: None)):
            result = disk_cleanup.run(self.cfg, apply=True, runtime=disk_cleanup.CleanupRuntime(
                rt.now, rt.read, placement.cleanup_call, rt.toast))
        self.assertTrue(confirmed)
        self.assertTrue(any(e.get('reason_code') == 'worker_timeout' for e in result['errors']))
        self.assertFalse((self.state / 'disk-cleanup.lock').exists())
        saved = json.loads((self.state / 'disk' / 'last-report.json').read_text())
        self.assertIn('partial evidence', json.dumps(saved))

    def test_unconfirmed_worker_loss_keeps_lock_and_error_evidence(self):
        from drover import disk_cleanup
        self.approve_report()
        rt = self.runtime()
        with patch.object(subprocess, 'run', side_effect=subprocess.TimeoutExpired(['worker'], 600)), \
                patch.object(placement, 'physical_disk_path', return_value=str(self.root)):
            with self.assertRaises(placement.CleanupWorkerError):
                disk_cleanup.run(self.cfg, apply=True, runtime=disk_cleanup.CleanupRuntime(
                    rt.now, rt.read, placement.cleanup_call, rt.toast))
        self.assertTrue((self.state / 'disk-cleanup.lock').exists())
        self.assertTrue(list((self.state / 'disk').glob('worker-error-*.json')))
        self.assertIn('worker_timeout', (self.state / 'disk' / 'last-report.json').read_text())

    def test_composed_worker_timeout_stops_its_process_group(self):
        request = self.request(stage=3)
        request['drive']['path'] = str(self.root)
        request['paths'] = dict(temp_roots=[], report_roots=[], caches=[dict(
            argv=[sys.executable, '-c', 'import time;time.sleep(30)'],
            cwd=str(self.temp_root), placement=self.side)])
        execute = subprocess.run
        def bounded(argv, **kwargs):
            if 'cleanup_worker' in ' '.join(map(str, argv)):
                kwargs['timeout'] = 3 if self.side == 'windows' else 1
            return execute(argv, **kwargs)
        with patch.object(subprocess, 'run', side_effect=bounded), \
                patch.dict(os.environ, {'WSL_DISTRO_NAME': ''}):
            result = placement.cleanup_call(self.side, request)
        self.assertEqual(result['errors'][0]['reason_code'], 'worker_timeout')
        self.assertTrue(result['errors'][0]['termination_confirmed'])
        self.assertTrue(list((self.state / 'disk').glob('worker-error-*.json')))

    def test_failed_termination_scan_keeps_lock_when_evidence_write_fails(self):
        from drover import disk_cleanup, disk_state
        self.approve_report()
        save = disk_state.save_file
        def timeout(argv, **kwargs):
            request = json.loads(kwargs['input'])
            save(self.state, 'worker-' + request['_worker_id'] + '.json', dict(
                pid=123456789, runner_pid=123456789, run_id=request['_worker_id']))
            raise subprocess.TimeoutExpired(argv, 600)
        def no_evidence(state, name, value):
            if name.startswith('worker-error-') or name == 'last-report.json':
                raise OSError('disk full')
            return save(state, name, value)
        rt = self.runtime()
        with patch.object(subprocess, 'run', side_effect=timeout), \
                patch.object(placement, 'physical_disk_path', return_value=str(self.root)), \
                patch.object(placement, 'process_cmdlines', return_value=[]), \
                patch.object(placement, 'recover_ci_children', side_effect=placement.ScanError('scan unavailable')), \
                patch.object(placement, '_CleanupJob', return_value=SimpleNamespace(
                    terminate=lambda: False, close=lambda: None)), \
                patch.object(disk_state, 'save_file', side_effect=no_evidence):
            with self.assertRaises(placement.CleanupWorkerError):
                disk_cleanup.run(self.cfg, apply=True, runtime=disk_cleanup.CleanupRuntime(
                    rt.now, rt.read, placement.cleanup_call, rt.toast))
        self.assertTrue((self.state / 'disk-cleanup.lock').exists())

    def test_corrupt_worker_identity_cannot_release_lock(self):
        from drover import disk_cleanup, disk_state
        self.approve_report()
        def timeout(argv, **kwargs):
            request = json.loads(kwargs['input'])
            disk_state.save_file(self.state, 'worker-' + request['_worker_id'] + '.json', [])
            raise subprocess.TimeoutExpired(argv, 600)
        rt = self.runtime()
        with patch.object(subprocess, 'run', side_effect=timeout), \
                patch.object(placement, 'physical_disk_path', return_value=str(self.root)):
            with self.assertRaises(placement.CleanupWorkerError):
                disk_cleanup.run(self.cfg, apply=True, runtime=disk_cleanup.CleanupRuntime(
                    rt.now, rt.read, placement.cleanup_call, rt.toast))
        self.assertTrue((self.state / 'disk-cleanup.lock').exists())

    def test_cleanup_keeps_descendants_on_a_different_mount(self):
        from drover import disk_cleanup
        from pathlib import Path
        from types import SimpleNamespace
        temp = self.file(self.temp_root / 'mounted' / 'valuable')
        cached = self.file(self.root / 'cache' / 'mounted' / 'valuable')
        self.age(temp.parent, 8 * 86400)
        self.cfg['disk_cleanup']['caches'] = [dict(path=str(cached.parents[1]), placement=self.side)]
        def physical(path):
            path = Path(path)
            return str(self.root / 'other drive') if any(
                path == p or p in path.parents for p in (temp.parent, cached.parent)) else str(self.root)
        with patch.object(placement, 'physical_disk_path', side_effect=physical), \
                patch.object(placement, 'cleanup_free', return_value=80), \
                patch.object(placement, 'cleanup_processes', return_value=SimpleNamespace(busy=lambda p: False)):
            disk_cleanup.run(self.cfg, runtime=self.runtime(real=True))
            disk_cleanup.run(self.cfg, apply=True, runtime=self.runtime(real=True))
        self.assertTrue(temp.exists())
        self.assertTrue(cached.exists())

    def test_windows_scope_uses_native_spelling_for_declared_paths(self):
        request = self.request()
        request['paths']['temp_roots'] = ['/mnt/h/temp with spaces']
        request['paths']['caches'] = [dict(path='/mnt/h/cache folder', placement='windows')]
        with patch.object(placement, 'IS_WINDOWS', True), \
                patch.object(placement, 'physical_disk_path', return_value='H:/'):
            scoped = placement.cleanup_scope(request)
        self.assertEqual(scoped['paths']['temp_roots'], [r'H:\temp with spaces'])
        self.assertEqual(scoped['paths']['caches'][0]['path'], r'H:\cache folder')
