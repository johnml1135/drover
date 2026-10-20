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
                (None, 'python "H:\\trees\\topic\\worker.py"')])
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
