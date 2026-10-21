"""Linux services are user timers and contain no elevation or WSL invocation."""
import os
import subprocess
from unittest.mock import patch

from tests.disk_fixture import DiskCase


class Linux(DiskCase):
    def test_user_timer_install_argv(self):
        from drover import placement, disk_schedule
        xdg = self.root / 'xdg config with spaces'
        calls = []
        def runner(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, '', '')
        with patch.dict(os.environ, {'XDG_CONFIG_HOME': str(xdg), 'WSL_INTEROP': '', 'WSL_DISTRO_NAME': ''}), \
                patch.object(placement, 'IS_WINDOWS', False):
            plan = placement.disk_schedule_plan(self.cfg)
            self.assertEqual(calls, [])
            self.assertTrue(plan['files'])
            for path, body in plan['files'].items():
                self.assertTrue(str(path).startswith(str(xdg / 'systemd' / 'user')))
                for forbidden in ('sudo', 'loginctl', 'wsl.exe', 'User=root'):
                    self.assertNotIn(forbidden, body)
            timers = [v for k, v in plan['files'].items() if str(k).endswith('.timer')]
            self.assertEqual(len(timers), 2)
            self.assertTrue(all('Persistent=true' in value for value in timers))
            self.assertTrue(any('OnCalendar=*-*-* *:0/15:00' in value for value in timers))
            self.assertTrue(any('03:00' in value for value in timers))
            self.assertIn(['systemctl', '--user', 'daemon-reload'], plan['argv'])
            self.assertTrue(any(argv[:4] == ['systemctl', '--user', 'enable', '--now'] for argv in plan['argv']))
            disk_schedule.install(self.cfg, dry_run=True, runner=runner)
            self.assertFalse(xdg.exists())
            self.assertEqual(calls, [])
            disk_schedule.install(self.cfg, runner=runner)
            self.assertEqual(calls, plan['argv'])
            for path, body in plan['files'].items():
                self.assertEqual(__import__('pathlib').Path(path).read_text(), body)

    def test_linux_without_windows_scope_can_complete_report_and_install_apply(self):
        from drover import placement, disk_cleanup, disk_schedule
        rt = self.runtime()
        def call(side, request):
            return placement.cleanup_call(side, request) if side == 'windows' else {
                'items': [], 'errors': [], 'reclaimed_bytes': 0}
        from drover.disk_cleanup import CleanupRuntime
        with patch.dict(os.environ, {'WSL_DISTRO_NAME': '', 'WSL_INTEROP': '',
                                    'XDG_CONFIG_HOME': str(self.root / 'xdg')}), \
                patch.object(placement, 'IS_WINDOWS', False), \
                patch.object(placement, 'physical_disk_path', return_value=str(self.root)):
            report = disk_cleanup.run(self.cfg, runtime=CleanupRuntime(rt.now, rt.read, call, rt.toast))
            self.assertFalse(report['errors'])
            disk_schedule.install(self.cfg, apply=True,
                runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, '', ''))

    def test_wsl_install_requires_native_windows_reader(self):
        from drover import placement
        with patch.dict(os.environ, {'WSL_DISTRO_NAME': 'FixtureDistro'}), \
                patch.object(placement, 'IS_WINDOWS', False):
            with self.assertRaisesRegex(ValueError, 'Windows'):
                placement.disk_schedule_plan(self.cfg)
