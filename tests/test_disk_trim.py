import json
import os
import subprocess
import sys
from unittest.mock import patch
from tests.disk_fixture import DiskCase, GIB, empty_result


class Trim(DiskCase):
    def test_root_argv_and_retrim_threshold(self):
        from drover import placement
        with patch.object(placement, 'IS_WINDOWS', True):
            argv = placement.trim_argv()
        self.assertEqual(argv[0].lower().removesuffix('.exe'), 'wsl')
        self.assertEqual(argv[argv.index('-u') + 1], 'root')
        self.assertEqual(argv[argv.index('-d') + 1], 'Disk Test Distro')
        self.assertEqual(argv[-4:], ['--', 'fstrim', '-v', '/'])
        self.assertFalse(any(word in argv for word in ('sudo', 'RunAs', '-Verb')))
        for reclaimed, retrims in ((GIB - 1, 1), (GIB, 2)):
            with self.subTest(reclaimed=reclaimed):
                def stage(side, request):
                    result = empty_result()
                    if side == 'wsl' and request['stage'] == 2:
                        result['reclaimed_bytes'] = reclaimed
                    return result
                self.approve_report()
                self.calls.clear()
                self.run_cleanup(apply=True, real=False, handler=stage)
                self.assertEqual(sum(side == 'windows' and request['stage'] == 1
                                     for side, request in self.calls), retrims)

    def test_composed_side_command_runs(self):
        from drover import placement
        marker = self.root / "record ' side.json"
        script = self.root / 'side cache.py'
        script.write_text('import json,os,sys\nfrom pathlib import Path\n'
                          'Path(sys.argv[1]).write_text(json.dumps([sys.argv[2:],os.getcwd()]))\n')
        literal = ['space value', 'line\nbreak', 'a;$(literal)']
        request = dict(stage=3, apply=True, now=self.now,
                       drive=dict(name='test-drive', path=str(self.root), free_gb=80., level='queue'),
                       repos=[], state_dir=str(self.state), tasks={},
                       paths=dict(temp_roots=[], report_roots=[], caches=[dict(
                           argv=[sys.executable, str(script), str(marker), *literal],
                           cwd=str(self.temp_root), placement=self.side)]))
        # This public native-side call must run its composed worker and command.
        # Block a cross-platform escape; no WSL or root process may run in tests.
        original = subprocess.run
        seen = []
        def execute(argv, *args, **kwargs):
            self.assertNotIn('wsl.exe', [str(a).lower() for a in argv])
            self.assertNotIn('fstrim', [str(a).lower() for a in argv])
            seen.append(list(argv))
            return original(argv, *args, **kwargs)
        with patch.object(subprocess, 'run', side_effect=execute), \
                patch.dict(os.environ, {'WSL_DISTRO_NAME': ''}):
            result = placement.cleanup_call(self.side, request)
        self.assertTrue(seen)
        self.assertEqual(json.loads(marker.read_text()), [literal, str(self.temp_root)])
        self.assertEqual(result['errors'], [])

    def test_retrim_returns_to_windows_before_next_destructive_stage(self):
        def stage(side, request):
            answer = empty_result()
            if side == 'wsl' and request['stage'] == 2:
                answer['reclaimed_bytes'] = GIB
            return answer
        self.approve_report()
        self.calls.clear()
        self.run_cleanup(apply=True, handler=stage)
        steps = [(side, r['stage']) for side, r in self.calls]
        reclaimed = steps.index(('wsl', 2))
        self.assertEqual(steps[reclaimed + 1], ('windows', 1))
