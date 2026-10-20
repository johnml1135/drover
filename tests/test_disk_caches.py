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
