"""Cleanup obtains fail-safe process and owning-side facts through Placement."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from drover import placement


class Facts(unittest.TestCase):
    def test_unknown_busy_and_other_side(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'work tree'
            target.mkdir()
            absent = placement.cleanup_processes(proc_root=root / 'missing', windows=False)
            self.assertTrue(absent.busy(str(target)))
            empty_proc = root / 'proc'
            empty_proc.mkdir()
            idle = placement.cleanup_processes(proc_root=empty_proc, windows=False)
            self.assertFalse(idle.busy(str(target)))
            process = empty_proc / '123456789'
            process.mkdir()
            (process / 'cwd').symlink_to(target, target_is_directory=True)
            (process / 'cmdline').write_bytes(b'python\0worker\0')
            busy = placement.cleanup_processes(proc_root=empty_proc, windows=False)
            self.assertTrue(busy.busy(str(target)))
            self.assertFalse(busy.busy(str(root / 'work')))
            cim = placement.cleanup_processes(windows=True, cim=lambda: [
                (r'H:\elsewhere', 'python "H:\\trees\\topic\\worker.py"')])
            self.assertTrue(cim.busy(r'H:\trees\topic'))
            self.assertFalse(cim.busy(r'H:\trees\top'))
            def unavailable():
                raise subprocess.TimeoutExpired('CIM', 1)
            self.assertTrue(placement.cleanup_processes(windows=True, cim=unavailable).busy('anything'))
        self.assertEqual(placement.worktree_side('H:/trees/topic', 'wsl'), 'wsl')
        self.assertEqual(placement.worktree_side('/home/user/topic', 'windows'), 'windows')
        self.assertEqual(placement.worktree_side('H:/trees/topic'), 'windows')
        self.assertEqual(placement.worktree_side('/home/user/topic'), 'wsl')
        for ambiguous in ('../trees/topic', 'trees/topic', '/mnt/h/trees/topic'):
            with self.subTest(path=ambiguous):
                self.assertIsNone(placement.worktree_side(ambiguous))
        self.assertIsNone(placement.worktree_side('H:/trees/topic', 'keep for review'))
        with patch.object(placement, 'IS_WINDOWS', False), \
                patch.dict(os.environ, {'WSL_DISTRO_NAME': 'Fixture'}), \
                patch.object(placement, 'vhdx_dir', return_value=None):
            self.assertIsNone(placement.physical_disk_path('/home/user/topic'))
        with patch.object(placement, 'IS_WINDOWS', False), \
                patch.dict(os.environ, {'WSL_DISTRO_NAME': 'Fixture'}), \
                patch.object(placement, 'vhdx_dir', return_value='/mnt/h/WSL/Ubuntu'):
            self.assertEqual(placement.drive(placement.physical_disk_path('/home/user/topic')), 'H')
            self.assertEqual(placement.drive(placement.physical_disk_path('C:/repo')), 'C')

    def test_process_with_unavailable_cwd_cannot_prove_idle(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory) / 'proc'
            process = proc / '123456789'
            process.mkdir(parents=True)
            (process / 'cmdline').write_bytes(b'python\0worker\0')
            self.assertTrue(placement.cleanup_processes(proc_root=proc, windows=False).busy('/candidate'))

    def test_native_windows_vhdx_host_is_a_physical_root(self):
        with patch.object(placement, 'IS_WINDOWS', True), \
                patch.object(placement, 'distro', return_value='Fixture'), \
                patch.object(placement, 'detect_vhdx_drives', return_value={'Fixture': 'H:'}):
            self.assertEqual(placement.physical_disk_path('/home/user/topic'), 'H:/')

    def test_windows_shell_cwd_and_missing_visibility_retain_candidates(self):
        visible = placement.cleanup_processes(windows=True, cim=lambda: [
            (r'H:\trees\topic', 'pwsh.exe')])
        self.assertTrue(visible.busy(r'H:\trees\topic'))
        self.assertFalse(visible.busy(r'H:\trees\other'))
        hidden = placement.cleanup_processes(windows=True, cim=lambda: [(None, 'pwsh.exe')])
        self.assertTrue(hidden.busy(r'H:\trees\other'))
        self.assertEqual(hidden.reason, 'process visibility unavailable')
