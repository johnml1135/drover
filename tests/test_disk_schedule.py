"""Schedule generation is inspected without invoking any operating-system installer."""
import copy
import json
import subprocess
import xml.etree.ElementTree as ET
from unittest.mock import patch

from tests.disk_fixture import DiskCase


class Windows(DiskCase):
    def plan(self, apply=False):
        from drover import placement
        with patch.object(placement, 'IS_WINDOWS', True):
            return placement.disk_schedule_plan(self.cfg, apply=apply)

    def test_current_user_15min_and_daily_argv(self):
        from drover import placement
        with patch.object(subprocess, 'run', side_effect=AssertionError('generation executed a command')):
            plan = self.plan()
        self.assertEqual(len(plan['argv']), 2)
        names = set()
        xmls = []
        for argv in plan['argv']:
            self.assertIsInstance(argv, list)
            self.assertEqual(argv[0].lower(), 'schtasks.exe')
            self.assertIn('/Create', argv)
            self.assertIn('/F', argv)
            names.add(argv[argv.index('/TN') + 1])
            key = argv[argv.index('/XML') + 1]
            self.assertIn(key, plan['files'])
            xmls.append(plan['files'][key])
        self.assertEqual(names, {'DroverDiskTracker', 'DroverDiskCleanup'})
        for text in xmls:
            root = ET.fromstring(text)
            values = {node.tag.rsplit('}', 1)[-1]: node.text for node in root.iter()}
            self.assertEqual(values['LogonType'], 'InteractiveToken')
            self.assertEqual(values['RunLevel'], 'LeastPrivilege')
            self.assertEqual(values['MultipleInstancesPolicy'], 'IgnoreNew')
            self.assertEqual(values['StartWhenAvailable'], 'true')
            self.assertTrue(values['UserId'])
            self.assertTrue(placement.WIN_RE.match(values['Command']) or values['Command'].startswith('/'))
            action = values.get('Arguments', '')
            self.assertIn('--dry-run', action)
            self.assertIn(str(self.state), text)
            self.assertNotIn('wsl.exe', action.lower())
            self.assertNotIn('hub.', action.lower())
        self.assertTrue(any('PT15M' in text for text in xmls))
        self.assertTrue(any('03:00' in text for text in xmls))
        self.assertTrue(any('daily' in text for text in xmls))
        applied = json.dumps(self.plan(apply=True))
        self.assertIn('--apply', applied)
        self.assertNotIn('--dry-run', applied)

    def test_install_dry_run_and_first_live_report_receipt(self):
        from drover import disk_schedule, disk_cleanup, placement
        commands = []
        def runner(argv, **kwargs):
            commands.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, '', '')
        with patch.object(placement, 'IS_WINDOWS', True), \
                patch.object(placement, 'physical_disk_path', return_value=str(self.root)):
            before = set(self.root.rglob('*'))
            plan = disk_schedule.install(self.cfg, dry_run=True, runner=runner)
            self.assertTrue(plan['argv'])
            self.assertEqual(commands, [])
            self.assertEqual(set(self.root.rglob('*')), before)
            with self.assertRaises((ValueError, RuntimeError)):
                disk_schedule.install(self.cfg, apply=True, runner=runner)
            self.assertEqual(commands, [])
            candidate = self.temp_root / 'inspect before apply.txt'
            candidate.write_text('keep')
            report = disk_cleanup.run(self.cfg, mode='on-demand', apply=False, runtime=self.runtime())
            self.assertFalse(report['applied'])
            self.assertEqual(candidate.read_text(), 'keep')
            disk_schedule.install(self.cfg, apply=True, runner=runner)
            self.assertEqual(len(commands), 2)
            changed = copy.deepcopy(self.cfg)
            changed['disk_cleanup']['temp_roots'].append(str(self.root / 'new scope'))
            with self.assertRaises((ValueError, RuntimeError)):
                disk_schedule.install(changed, apply=True, runner=runner)
            self.assertEqual(len(commands), 2, 'receipt must match the exact configured scope')

    def test_install_seeds_missing_policy_and_persists_automation(self):
        from drover import config, disk_schedule, disk_state, placement
        raw = {'paths': self.cfg['paths'], 'trackers': self.cfg['trackers']}
        target = self.state / 'config.toml'
        target.write_text(config.dumps(raw))
        seeded = dict(temp_roots=[str(self.temp_root)], report_roots=[], caches=[
            dict(path=str(self.root / 'detected cache'), placement='windows', max_age_days=30)])
        runner = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, '', '')
        with patch.object(placement, 'IS_WINDOWS', True), \
                patch.object(placement, 'detected_cleanup_policy', return_value=seeded):
            before = target.read_bytes()
            plan = disk_schedule.install(self.cfg, dry_run=True, runner=runner)
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(plan['disk_cleanup'], seeded)
            disk_schedule.install(self.cfg, runner=runner)
        self.assertEqual(config._read(target)['disk_cleanup'], seeded)
        self.assertEqual(disk_state.read(self.state)['automation']['apply'], False)

    def test_applied_scheduled_tick_is_cheap_when_healthy(self):
        from drover import disk, disk_cleanup, placement
        with patch.object(placement, 'physical_disk_path', return_value=str(self.root)), \
                patch('drover.disk._free', return_value=180), \
                patch.object(disk_cleanup, 'scope_key', side_effect=AssertionError('healthy tick inventoried scope')), \
                patch.object(placement, 'cleanup_enqueue', side_effect=AssertionError('healthy tick launched cleanup')):
            self.assertEqual(disk.main(['tick', '--apply']), 0)

    def test_calendar_trigger_base_elements_precede_daily_schedule(self):
        # Task Scheduler's calendarTriggerType extends triggerBaseType before its schedule sequence.
        plan = self.plan()
        calendar = next(element for body in plan['files'].values()
                        for element in ET.fromstring(body).iter()
                        if element.tag.endswith('}CalendarTrigger'))
        children = [element.tag.rsplit('}', 1)[-1] for element in calendar]
        self.assertEqual(children, ['StartBoundary', 'Enabled', 'ScheduleByDay'])
