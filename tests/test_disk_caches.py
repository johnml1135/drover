import json
import os
import sys
from tests.disk_fixture import DiskCase


class Caches(DiskCase):
    def commands(self):
        script = self.root / 'cache recorder.py'
        script.write_text('import json,os,sys\nfrom pathlib import Path\n'
                          'Path(sys.argv[1]).write_text(json.dumps({"argv":sys.argv[2:],'
                          '"cwd":os.getcwd(),"env":os.environ.get("DISK_CACHE_TEST")}))\n'
                          'sys.exit(int(sys.argv[2]))\n')
        first, second = self.root / 'first.json', self.root / 'second.json'
        suffix = ['space value', "quote'\"", 'semi;colon', 'line\nbreak', '$(literal)']
        self.cfg['disk_cleanup']['caches'] = [
            dict(argv=[sys.executable, str(script), str(out), code, *suffix],
                 cwd=str(self.temp_root), placement='wsl')
            for out, code in ((first, '7'), (second, '0'))]
        return first, second, suffix

    def test_declared_argv_verbatim_and_failure_continues(self):
        first, second, suffix = self.commands()
        self.approve_report()
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        from unittest.mock import patch
        with patch.dict(os.environ, {'DISK_CACHE_TEST': 'unchanged value'}):
            result = self.run_cleanup(apply=True)
        for path, code in ((first, '7'), (second, '0')):
            self.assertEqual(json.loads(path.read_text()),
                             dict(argv=[code, *suffix], cwd=str(self.temp_root), env='unchanged value'))
        self.assertTrue(any(error.get('stage') == 3 for error in result['errors']))
        self.assertIn(4, [request['stage'] for side, request in self.calls])

    def test_dry_run_executes_no_commands(self):
        first, second, _ = self.commands()
        result = self.run_cleanup()
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertTrue(any(item['stage'] == 3 for item in result['items']))
        self.assertFalse(result['applied'])

    def test_r8_folder_routine_age_and_aggressive_lru_stop(self):
        folder = self.root / 'folder cache'
        old = self.file(folder / 'old', age=31 * 86400)
        young = self.file(folder / 'young', age=2 * 86400)
        self.cfg['disk_cleanup']['caches'] = [
            dict(path=str(folder), placement='wsl', max_age_days=30)]
        self.approve_report()
        self.run_cleanup(apply=True, mode='daily')
        self.assertFalse(old.exists())
        self.assertTrue(young.exists())
        oldest = self.file(folder / 'oldest', age=5 * 86400)
        newer = self.file(folder / 'newer', age=86400)
        self.approve_report()
        runtime = self.runtime(real=True)
        from drover import disk_cleanup, placement
        from unittest.mock import patch
        from types import SimpleNamespace
        def read(path):
            return 80 if oldest.exists() else 150
        from drover.disk_cleanup import CleanupRuntime
        with patch.object(placement, 'cleanup_processes', return_value=SimpleNamespace(busy=self.busy)),                 patch.object(placement, 'physical_disk_path', return_value=str(self.root)),                 patch.object(placement, 'cleanup_free', side_effect=read), \
                patch('tempfile.gettempdir', return_value=str(self.temp_root)):
            result = disk_cleanup.run(self.cfg, apply=True,
                runtime=CleanupRuntime(runtime.now, read, runtime.call, runtime.toast))
        self.assertFalse(oldest.exists())
        self.assertTrue(young.exists())
        self.assertTrue(newer.exists())
        self.assertFalse(result['exhausted'])

    def test_r8_folder_atime_and_mtime_fallback(self):
        folder = self.root / 'cache clocks'
        fallback = self.file(folder / 'fallback', age=31 * 86400)
        used = self.file(folder / 'used', age=40 * 86400)
        os.utime(used, (self.now - 86400, self.now - 40 * 86400))
        self.cfg['disk_cleanup']['caches'] = [dict(path=str(folder), placement='wsl')]
        self.approve_report()
        self.run_cleanup(apply=True, mode='daily')
        self.assertFalse(fallback.exists())
        self.assertTrue(used.exists())
