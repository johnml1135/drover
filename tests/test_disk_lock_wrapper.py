"""Legacy cleaner execution holds exactly the same machine-wide cleanup lock."""
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from tests.disk_fixture import DiskCase


class Wrapper(DiskCase):
    def test_external_cleanup_shares_lock_and_argv(self):
        from drover import disk_cleanup, disk_state, placement
        output = self.root / 'record argv.json'
        args = ['space argument', 'quote"and\'literal', 'dollar$(never execute)', 'line\nbreak']
        program = ('import json,os,pathlib,sys; '
                   'p=pathlib.Path(sys.argv[1]); '
                   'assert pathlib.Path(os.environ["DROVER_PATHS_STATE_DIR"],"disk-cleanup.lock").is_dir(); '
                   'p.write_text(json.dumps({"argv":sys.argv[2:],"cwd":os.getcwd(),"env":os.environ["FIXTURE_LITERAL"]})); '
                   'raise SystemExit(17)')
        argv = [sys.executable, '-c', program, str(output), *args]
        with patch.dict(os.environ, {'FIXTURE_LITERAL': 'exact value', 'DROVER_PATHS_STATE_DIR': str(self.state)}):
            status = disk_cleanup.locked_command(self.cfg, argv)
        self.assertEqual(status, 17)
        recorded = json.loads(output.read_text())
        self.assertEqual(recorded['argv'], args)
        self.assertEqual(recorded['cwd'], os.getcwd())
        self.assertEqual(recorded['env'], 'exact value')
        self.assertFalse((self.state / 'disk-cleanup.lock').exists())
        output.unlink()
        with disk_state.CleanupLock(self.state):
            status = disk_cleanup.locked_command(self.cfg, argv)
            self.assertNotEqual(status, 0)
            self.assertFalse(output.exists(), 'contending wrapper must not start its child')
        def interrupted(argv, **kwargs):
            self.assertTrue((self.state / 'disk-cleanup.lock').is_dir())
            raise OSError('fixture spawn failed')
        with self.assertRaises(OSError):
            disk_cleanup.locked_command(self.cfg, argv, runner=interrupted)
        self.assertFalse((self.state / 'disk-cleanup.lock').exists())
        # Generate the two caller spellings; neither may choose a side-local lock.
        native = [r'C:\Python\python.exe', '-m', 'drover', 'disk-cleanup', 'lock', '--', 'cleaner.exe']
        wsl = placement.WSL('Fixture').command(['python3', '-m', 'drover', 'disk-cleanup', 'lock', '--', 'cleaner'])
        self.assertIn('lock', native)
        self.assertIn('lock', wsl)
        for spelling in (str(self.state), str(self.state / '..' / self.state.name)):
            cfg = {**self.cfg, 'paths': {**self.cfg['paths'], 'state_dir': spelling}}
            with disk_state.CleanupLock(self.state):
                for wrapper_argv in (native, wsl):
                    called = []
                    disk_cleanup.locked_command(cfg, wrapper_argv, runner=lambda *a, **kw: called.append(a))
                    self.assertEqual(called, [], 'native and WSL wrappers share the normalized state lock')
