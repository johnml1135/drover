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
