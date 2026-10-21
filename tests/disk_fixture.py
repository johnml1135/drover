"""Hermetic public-interface fixtures for the disk contract.

No new product module is imported until a test actually exercises its interface.
"""
import copy
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from drover import config, placement

DAY = 86400
GIB = 1024 ** 3


def empty_result():
    return {'items': [], 'errors': [], 'reclaimed_bytes': 0}


class DiskCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='disk-contract-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.repos_dir = self.state / 'repos'
        self.temp_root = self.root / 'declared temp'
        self.report_root = self.root / 'reports'
        for path in (self.state, self.repos_dir, self.temp_root, self.report_root):
            path.mkdir(parents=True, exist_ok=True)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg['paths'].update(state_dir=str(self.state), repos_root=str(self.root / 'repos'),
                                 worktrees=str(self.root / 'worktrees'), wsl_distro='Disk Test Distro')
        self.cfg['repos_dir'] = str(self.repos_dir)
        self.cfg['trackers'] = [dict(kind='disk', name='test-drive', enabled=True,
                                    path=str(self.root), warn_gb=150., queue_gb=100.,
                                    hold_gb=50., critical_gb=25.)]
        self.cfg['disk_cleanup'] = dict(temp_roots=[str(self.temp_root)],
                                        report_roots=[str(self.report_root)], caches=[])
        self.now = 2_000_000_000.
        self.free = 80.
        self.calls, self.toasts, self.busy_paths = [], [], set()
        self.tasks = {}
        self.side = placement.cleanup_side()
        self.other_side = 'wsl' if self.side == 'windows' else 'windows'
        for context in (patch.dict(os.environ, {'DROVER_HOME': str(self.state)}),
                        patch.object(config, 'get', return_value=self.cfg)):
            context.start()
            self.addCleanup(context.stop)

    def busy(self, path):
        path = Path(path)
        return any(path == Path(p) or path in Path(p).parents or Path(p) in path.parents
                   for p in self.busy_paths)

    def age(self, path, seconds):
        path = Path(path)
        if path.is_dir() and not path.is_symlink():
            for child in path.rglob('*'):
                if not child.is_symlink():
                    os.utime(child, (self.now - seconds, self.now - seconds))
        if not path.is_symlink():
            os.utime(path, (self.now - seconds, self.now - seconds))
        return path

    def file(self, path, text='sentinel', age=8 * DAY):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        self.age(path, age)
        return path

    def runtime(self, real=False, handler=None):
        from drover.disk_cleanup import CleanupRuntime

        def call(side, request):
            self.calls.append((side, copy.deepcopy(request)))
            if handler is not None:
                return handler(side, request)
            if not real or side != self.side or request['stage'] == 1:
                return empty_result()
            from drover.disk_side import serve
            # Tests declare every temp root explicitly, never inspect host temp.
            request = {**request, 'tasks': self.tasks,
                       'paths': {**request['paths'], 'temp_roots': self.cfg['disk_cleanup']['temp_roots'],
                                 'report_roots': self.cfg['disk_cleanup']['report_roots'],
                                 'caches': self.cfg['disk_cleanup']['caches']}}
            return serve(request)

        return CleanupRuntime(lambda: self.now, lambda path: self.free, call, self.toasts.append)

    def run_cleanup(self, *, apply=False, mode='on-demand', real=True, handler=None):
        from drover import disk_cleanup, placement
        proof = SimpleNamespace(busy=self.busy)
        with patch.object(placement, 'cleanup_processes', return_value=proof), \
             patch.object(placement, 'physical_disk_path', return_value=str(self.root)), \
             patch('tempfile.gettempdir', return_value=str(self.temp_root)), \
             patch.dict(os.environ, {name: str(self.temp_root) for name in ('TMPDIR', 'TMP', 'TEMP')}):
            return disk_cleanup.run(self.cfg, mode=mode, apply=apply,
                                    runtime=self.runtime(real=real, handler=handler))

    def approve_report(self):
        """Obtain matching dry receipt via the same public interface as an owner."""
        return self.run_cleanup(apply=False)


class Fixture(DiskCase):
    def test_hermetic_roots(self):
        self.assertEqual(config.state_dir(self.cfg), self.state)
        self.assertEqual(config.repos_dir(self.cfg), self.repos_dir)
        self.assertEqual(Path(os.environ['DROVER_HOME']), self.state)
        for path in (self.state, self.repos_dir, self.temp_root, self.report_root,
                     Path(self.cfg['trackers'][0]['path'])):
            self.assertTrue(path.is_relative_to(self.root))
        self.assertEqual(self.cfg['disk_cleanup']['caches'], [])
        self.assertEqual(self.calls, [])
