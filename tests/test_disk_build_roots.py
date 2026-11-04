"""External build roots: each immediate subfolder of a declared `[[disk_cleanup.build_roots]]` entry is one unit, removed
once idle past its `max_idle_hours`, on a process proof with the budget treatment and a re-measure that finds it
unchanged."""
import itertools
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from drover import config, disk_preflight, placement, placement_proof
from drover.disk_stage import StageClock
from drover.disk_temp import temporary
from tests.disk_fixture import DAY, DiskCase, placement_processes, staged

HOUR = 3600
IGNORED = 'csrss.exe'
READABLE = dict(pid=7, name='editor.exe', same_user=True, cwd='C:\\elsewhere', cmd='editor.exe', session_id=1,
                exe='C:\\elsewhere\\editor.exe')


def unreadable(name):
    """A Windows process whose owner and executable cannot be read, in a user session: unknown to a strict proof."""
    return dict(pid=4242, name=name, same_user=None, cwd=None, cmd=None, session_id=1, exe=None)


class BuildRootsCase(DiskCase):
    """A build root `cargo-targets` under the test tree, and units made in it."""
    def setUp(self):
        super().setUp()
        self.build = self.root / 'cargo-targets'
        self.build.mkdir()
        self.cfg['disk_cleanup']['build_roots'] = [dict(path=str(self.build), placement=self.side, max_idle_hours=2)]
        self.asked = []

    def unit(self, name, age, files=('debug/app.exe', 'debug/deps/lib.rlib')):
        for relative in files:
            self.file(self.build / name / relative, 'x' * 10, age=age)
        return self.age(self.build / name, age)

    def rows(self, result, path):
        return [i for i in result['items'] if i.get('path') == str(path)]


class BuildRoots(BuildRootsCase):
    # --- the whole cleanup, through scope and the stage wiring

    def test_idle_unit_is_a_candidate_and_removed_on_apply(self):
        idle = self.unit('agent-one', 3 * HOUR)
        dry = self.run_cleanup()
        [row] = self.rows(dry, idle)
        self.assertEqual((row['action'], row['reason_code'], row['files'], row['bytes']),
                         ('candidate', 'build_root_idle', 2, 20))
        self.assertTrue(idle.exists())
        applied = self.run_cleanup(apply=True)
        [row] = self.rows(applied, idle)
        self.assertEqual((row['action'], row['reason_code']), ('removed', 'build_root_idle'))
        self.assertFalse(idle.exists())
        self.assertTrue(self.build.is_dir())

    def test_recent_unit_is_kept(self):
        recent = self.unit('agent-two', 2 * HOUR - 60)
        self.age(recent / 'debug' / 'deps' / 'lib.rlib', 10 * DAY)
        self.approve_report()
        result = self.run_cleanup(apply=True)
        [row] = self.rows(result, recent)
        self.assertEqual((row['action'], row['reason_code']), ('keep', 'build_root_recent'))
        self.assertTrue(recent.exists())

    def test_one_new_file_deep_inside_keeps_the_unit(self):
        unit = self.unit('agent-three', 5 * DAY)
        self.age(unit / 'debug' / 'app.exe', HOUR)
        self.approve_report()
        result = self.run_cleanup(apply=True)
        self.assertEqual(self.rows(result, unit)[0]['reason_code'], 'build_root_recent')
        self.assertTrue(unit.exists())

    def test_unit_busy_with_a_readable_process_is_kept(self):
        busy = self.unit('agent-four', 3 * DAY)
        self.busy_paths.add(str(busy / 'debug'))
        self.approve_report()
        result = self.run_cleanup(apply=True)
        [row] = self.rows(result, busy)
        self.assertEqual((row['action'], row['reason_code']), ('keep', 'process_busy'))
        self.assertTrue(busy.exists())

    def test_a_folder_in_both_temp_and_build_roots_is_handled_once_as_a_build_root(self):
        """A temp root that is a build root is not scanned as temp; a temp entry that is one has no temp row; a temp entry
        holding one deeper stays whole, as protected. The build roots inside are judged once, by their own rule."""
        self.cfg['disk_cleanup']['temp_roots'].append(str(self.build))
        idle = self.unit('agent-five', 8 * DAY)
        outer = self.root / 'outer temp'
        self.cfg['disk_cleanup']['temp_roots'].append(str(outer))
        self.cfg['disk_cleanup']['build_roots'] += [dict(path=str(outer / 'direct'), placement=self.side),
                                                    dict(path=str(outer / 'inner' / 'targets'), placement=self.side)]
        direct = self.file(outer / 'direct' / 'unit' / 'x', age=8 * DAY).parent
        deep = self.file(outer / 'inner' / 'targets' / 'unit' / 'x', age=8 * DAY).parent
        self.approve_report()
        result = self.run_cleanup(apply=True)
        for unit in (idle, direct, deep):
            with self.subTest(unit=unit):
                [row] = self.rows(result, unit)
                self.assertEqual((row['action'], row['reason_code']), ('removed', 'build_root_idle'))
        for entry in (self.build, outer / 'direct'):
            self.assertEqual(self.rows(result, entry), [])
        self.assertFalse(any(i.get('reason_code') in ('stale_temp', 'temp_retained') and str(self.build) in i['path']
                             for i in result['items']))
        [inner] = self.rows(result, outer / 'inner')
        self.assertEqual((inner['action'], inner['reason_code']), ('keep', 'protected'))
        self.assertTrue((outer / 'inner' / 'targets').is_dir())

    def test_link_unit_is_kept_and_not_followed(self):
        target = self.file(self.root / 'precious' / 'keep.txt', age=30 * DAY).parent
        self.age(target, 30 * DAY)
        link = self.build / 'linked'
        link.symlink_to(target, target_is_directory=True)
        self.approve_report()
        result = self.run_cleanup(apply=True)
        [row] = self.rows(result, link)
        self.assertEqual(row['action'], 'keep')
        self.assertTrue(link.is_symlink())
        self.assertTrue((target / 'keep.txt').exists())

    def test_link_inside_a_unit_keeps_the_unit_and_its_target(self):
        target = self.file(self.root / 'precious' / 'keep.txt', age=30 * DAY)
        unit = self.unit('agent-six', 3 * DAY)
        (unit / 'out').symlink_to(target.parent, target_is_directory=True)
        self.approve_report()
        result = self.run_cleanup(apply=True)
        self.assertEqual(self.rows(result, unit)[0]['reason_code'], 'protected')
        self.assertTrue(target.exists())
        self.assertTrue(unit.exists())

    def test_repository_inside_a_unit_keeps_it(self):
        unit = self.unit('agent-seven', 3 * DAY, files=('src/.git/HEAD',))
        self.approve_report()
        result = self.run_cleanup(apply=True)
        self.assertEqual(self.rows(result, unit)[0]['action'], 'keep')
        self.assertTrue(unit.exists())

    def test_a_folder_holding_a_bare_repository_inside_a_unit_keeps_it(self):
        unit = self.unit('agent-bare', 3 * DAY, files=('mirror/HEAD', 'mirror/objects/pack/p', 'mirror/refs/heads/main'))
        self.approve_report()
        result = self.run_cleanup(apply=True)
        [row] = self.rows(result, unit)
        self.assertEqual((row['action'], row['reason_code']), ('keep', 'protected'))
        self.assertTrue((unit / 'mirror' / 'HEAD').exists())

    def test_a_unit_exactly_max_idle_hours_old_is_the_first_age_that_is_idle(self):
        edge, inside = self.unit('exactly', 2 * HOUR), self.unit('a-second-short', 2 * HOUR - 1)
        result = self.run_cleanup()
        self.assertEqual([(r['action'], r['reason_code']) for r in self.rows(result, edge)], [('candidate', 'build_root_idle')])
        self.assertEqual([(r['action'], r['reason_code']) for r in self.rows(result, inside)], [('keep', 'build_root_recent')])

    # --- one stage run, with the process proofs as the stage asks for them

    def request(self, apply, **paths):
        return dict(stage=2, now=self.now, apply=apply, drive=dict(path=str(self.root)), state_dir=str(self.state),
                    paths=dict(build_roots=self.cfg['disk_cleanup']['build_roots'], process_ignore=[IGNORED], **paths))

    def stage(self, apply, rows=(), clock=None, **paths):
        """Stage 2 on this tree; each proof is a real Windows proof over `rows` and one readable process elsewhere (an
        empty answer is unknown), made as the stage asks for it."""
        rows = [READABLE, *rows]

        def processes(**options):
            self.asked.append(options['budget'])
            return placement_proof._CleanupProcesses(windows=True, cim=lambda: [dict(r) for r in rows],
                                                     paths=options['paths'], budget=options['budget'])
        with patch.object(placement, 'cleanup_processes', processes), \
                patch.object(placement, 'physical_disk_path', return_value=str(self.root)):
            return staged(temporary, self.request(apply, **paths), **({'clock': clock} if clock else {}))

    def test_unreadable_process_in_process_ignore_does_not_block(self):
        idle = self.unit('agent-eight', 3 * HOUR)
        answer = self.stage(True, rows=[unreadable(IGNORED)])
        [row] = self.rows(answer, idle)
        self.assertEqual((row['action'], row['reason_code']), ('removed', 'build_root_idle'))
        self.assertFalse(idle.exists())
        self.assertEqual(answer['reclaimed_bytes'], 20)
        self.assertEqual(self.asked, [False, True, True])     # temp's strict proof; the build first pass and fresh

    def test_unreadable_process_not_in_process_ignore_blocks(self):
        idle = self.unit('agent-nine', 3 * HOUR)
        answer = self.stage(True, rows=[unreadable('mystery.exe')])
        [row] = self.rows(answer, idle)
        self.assertEqual((row['action'], row['reason_code']), ('keep', 'process visibility unavailable'))
        self.assertTrue(idle.exists())

    def test_temp_roots_keep_the_strict_proof(self):
        stale = self.file(self.temp_root / 'old', age=8 * DAY)
        answer = self.stage(True, rows=[unreadable(IGNORED)], temp_roots=[str(self.temp_root)])
        self.assertEqual(self.rows(answer, stale)[0]['reason_code'], 'process visibility unavailable')
        self.assertTrue(stale.exists())

    def test_unit_changed_between_scan_and_removal_is_kept(self):
        idle = self.unit('agent-ten', 3 * HOUR)
        taken = []

        def processes(**options):
            taken.append(options)
            if len(taken) == 3:         # the strict first pass, the build one, then the fresh one before removal
                self.file(idle / 'debug' / 'new.o', 'grown', age=3 * HOUR)
            return SimpleNamespace(busy=lambda path: False)
        with patch.object(placement, 'cleanup_processes', processes), \
                patch.object(placement, 'physical_disk_path', return_value=str(self.root)):
            answer = staged(temporary, self.request(True))
        [row] = self.rows(answer, idle)
        self.assertEqual((row['action'], row['reason_code']), ('keep', 'revalidation_changed'))
        self.assertTrue((idle / 'debug' / 'new.o').exists())
        self.assertEqual(len(taken), 3)

    def test_past_the_stage_line_the_stage_stops_and_removes_nothing_more(self):
        units = [self.unit(f'unit{number:02}', 3 * DAY) for number in range(12)]
        ticks = itertools.count()
        answer = self.stage(True, clock=lambda: float(next(ticks)), worker_timeout_s=60)
        self.assertTrue(answer['partial'])
        stops = [i for i in answer['items'] if i['reason_code'] == 'temp_deadline']
        self.assertEqual(len(stops), 1)
        removed = {i['path'] for i in answer['items'] if i['action'] == 'removed'}
        self.assertTrue(0 < len(removed) < len(units), removed)
        self.assertEqual({str(u) for u in units if not u.exists()}, removed)
        after = answer['items'].index(stops[0])
        self.assertFalse(any(i['action'] == 'removed' for i in answer['items'][after:]))

    def test_past_the_line_a_unit_not_reached_gets_no_row(self):
        first, second = self.unit('unit00', 3 * DAY), self.unit('unit01', 3 * DAY)
        clock = lambda: 0.0 if first.exists() else 100.0        # the line (48 s of 60) is crossed once the first unit is gone
        answer = self.stage(True, clock=clock, worker_timeout_s=60)
        self.assertTrue(answer['partial'])
        self.assertEqual([i['reason_code'] for i in answer['items']], ['build_root_idle', 'temp_deadline'])
        self.assertEqual(self.rows(answer, second), [])
        self.assertTrue(second.exists())

    def test_a_removal_the_clock_will_not_let_begin_is_a_row_and_a_stop_and_leaves_the_unit(self):
        idle = self.unit('agent-late', 3 * DAY)
        with patch.object(StageClock, 'can_mutate', return_value=False):      # the scan was on time; a removal would not end
            answer = self.stage(True)
        self.assertEqual([i['reason_code'] for i in answer['items']], ['removal_deadline', 'temp_deadline'])
        [row] = self.rows(answer, idle)
        self.assertNotEqual(row['action'], 'removed')
        self.assertTrue(answer['partial'])
        self.assertTrue((idle / 'debug' / 'app.exe').exists())
        self.assertEqual(answer['reclaimed_bytes'], 0)

    def on_another_device(self, target):
        """Patch `Path.stat` so `target` and everything below it report another device, as a mount there would."""
        real = Path.stat

        def stat(path, *args, **options):
            info = real(path, *args, **options)
            if Path(path) != target and target not in Path(path).parents:
                return info
            fields = {name: getattr(info, name) for name in dir(info) if name.startswith('st_')}
            return SimpleNamespace(**dict(fields, st_dev=info.st_dev + 1))
        return patch.object(Path, 'stat', stat)

    def test_a_unit_that_is_itself_a_mount_is_kept(self):
        unit = self.unit('mounted', 3 * DAY)
        with self.on_another_device(unit):
            answer = self.stage(True)
        [row] = self.rows(answer, unit)
        self.assertEqual((row['action'], row['reason_code']), ('keep', 'protected'))
        self.assertIn('device', row['error'])
        self.assertTrue((unit / 'debug' / 'app.exe').exists())

    def test_a_deep_path_on_another_device_keeps_the_unit(self):
        unit = self.unit('deep mount', 3 * DAY)
        with self.on_another_device(unit / 'debug' / 'deps' / 'lib.rlib'):
            answer = self.stage(True)
        [row] = self.rows(answer, unit)
        self.assertEqual((row['action'], row['reason_code']), ('keep', 'protected'))
        self.assertTrue((unit / 'debug' / 'app.exe').exists())

    def test_a_build_root_not_made_yet_gives_no_row(self):
        self.build.rmdir()
        answer = self.stage(True)
        self.assertEqual([i for i in answer['items'] if str(self.build) in i['path']], [])
        self.assertEqual(answer['errors'], [])

    def test_files_directly_in_a_build_root_are_not_units(self):
        loose = self.file(self.build / 'stray.log', age=30 * DAY)
        answer = self.stage(True)
        self.assertEqual(self.rows(answer, loose), [])
        self.assertTrue(loose.exists())



@unittest.skipUnless(placement.cleanup_side() == 'windows', 'the build-roots run serves Windows-placement roots')
class BuildRootsRun(BuildRootsCase):
    """`mode='build-roots'`: only the Windows build roots, in this process, applied at any drive level on the owner's
    declaration alone; no worker, no dry report, no other stage."""

    def setUp(self):
        super().setUp()
        self.free = 500.        # far above warn: a healthy drive

    def build_run(self):
        with placement_processes():
            return self.run_cleanup(mode='build-roots', apply=True)

    def test_a_healthy_drive_loses_an_idle_unit_and_keeps_a_recent_one_without_a_report_or_automation(self):
        from drover import disk_state
        idle, recent = self.unit('idle', 3 * HOUR), self.unit('recent', HOUR)
        result = self.build_run()
        self.assertTrue(result['applied'])
        self.assertEqual([(r['action'], r['reason_code']) for r in self.rows(result, idle)], [('removed', 'build_root_idle')])
        self.assertEqual([(r['action'], r['reason_code']) for r in self.rows(result, recent)], [('keep', 'build_root_recent')])
        self.assertFalse(idle.exists())
        self.assertTrue(recent.exists())
        state = disk_state.read(self.state)
        self.assertNotIn('report_scope', state)
        self.assertNotIn('automation', state)
        self.assertFalse((self.state / 'disk' / 'last-report.json').exists())
        self.assertTrue((self.state / 'disk' / 'last-build-roots.json').exists())

    def test_no_worker_is_started_and_no_other_stage_has_a_row(self):
        self.unit('idle', 3 * HOUR)
        self.file(self.temp_root / 'old temp', age=30 * DAY)
        result = self.build_run()
        self.assertEqual(self.calls, [])       # no worker on either side: the runtime was never asked
        self.assertEqual({r.get('stage') for r in result['items'] + result['errors']}, {2})
        self.assertTrue((self.temp_root / 'old temp').exists())
        self.assertEqual({s['stage'] for s in result['stages']}, {2})

    def test_a_busy_unit_is_kept(self):
        busy = self.unit('busy', 3 * DAY)
        self.busy_paths.add(str(busy / 'debug'))
        result = self.build_run()
        self.assertEqual([(r['action'], r['reason_code']) for r in self.rows(result, busy)], [('keep', 'process_busy')])
        self.assertTrue(busy.exists())

    def test_a_root_on_the_other_side_is_reported_not_removed(self):
        self.cfg['disk_cleanup']['build_roots'].append(dict(path='/home/someone/targets', placement='wsl', max_idle_hours=2))
        result = self.build_run()
        [row] = [r for r in result['items'] if r['path'] == '/home/someone/targets']
        self.assertEqual((row['action'], row['reason_code'], row['side']), ('keep', 'build_root_other_side', 'wsl'))
        self.assertEqual(self.calls, [])

    def test_the_command_line_applies_unless_it_is_a_dry_run(self):
        from drover import disk_cleanup
        for argv, apply in ((['--mode', 'build-roots'], True), (['--mode', 'build-roots', '--apply'], True),
                            (['--mode', 'build-roots', '--dry-run'], False)):
            with self.subTest(argv=argv):
                with patch.object(disk_cleanup, 'run', return_value=dict(
                        mode='build-roots', applied=apply, items=[], errors=[])) as ran, patch('sys.stdout'):
                    self.assertEqual(disk_cleanup.main(argv), 0)
                ran.assert_called_once_with(mode='build-roots', apply=apply)

    def test_a_report_only_run_names_the_candidate_and_removes_nothing(self):
        idle = self.unit('idle', 3 * HOUR)
        with placement_processes():
            result = self.run_cleanup(mode='build-roots', apply=False)
        self.assertFalse(result['applied'])
        self.assertEqual([(r['action'], r['reason_code']) for r in self.rows(result, idle)],
                         [('candidate', 'build_root_idle')])
        self.assertTrue(idle.exists())

    def test_an_unreadable_process_is_named_in_the_report(self):
        from drover import disk_cleanup
        idle = self.unit('idle', 3 * HOUR)
        self.cfg['disk_cleanup']['process_ignore'] = [IGNORED]
        def processes(**options):
            return placement_proof._CleanupProcesses(windows=True, cim=lambda: [dict(READABLE), unreadable('mystery.exe')],
                                                     paths=options['paths'], budget=options['budget'])
        with placement_processes(), patch.object(placement, 'cleanup_processes', processes), \
                patch.object(placement, 'physical_disk_path', return_value=str(self.root)):
            result = disk_cleanup.run(self.cfg, mode='build-roots', apply=True, runtime=self.runtime())
        self.assertTrue(idle.exists())
        self.assertIn('process_visibility_unavailable', [r['reason_code'] for r in result['items']])

    def test_a_root_on_no_tracked_drive_has_a_row(self):
        idle = self.unit('idle', 3 * HOUR)
        self.cfg['trackers'][0]['enabled'] = False
        result = self.build_run()
        [row] = self.rows(result, self.build)
        self.assertEqual((row['action'], row['reason_code']), ('keep', 'build_root_untracked_drive'))
        self.assertTrue(idle.exists())

    def test_a_link_that_appears_above_the_unit_before_removal_keeps_it(self):
        from drover import disk_temp
        idle = self.unit('idle', 3 * HOUR)
        answers = iter((True, False))       # the root as listed, then the unit just before it goes
        with patch.object(disk_temp, 'safe_path', side_effect=lambda path: next(answers)):
            result = self.build_run()
        [row] = self.rows(result, idle)
        self.assertEqual((row['action'], row['reason_code']), ('keep', 'protected'))
        self.assertTrue(idle.exists())

    def test_a_held_cleanup_lock_skips_the_run_without_error(self):
        from drover import disk_state
        idle = self.unit('idle', 3 * HOUR)
        with disk_state.CleanupLock(self.state):
            result = self.build_run()
        self.assertEqual((result['items'], result['errors'], result.get('skipped')), ([], [], 'locked'))
        self.assertTrue(idle.exists())

    def test_unit_depth_two_removes_an_idle_session_and_keeps_an_active_one_and_the_parent_files(self):
        self.cfg['disk_cleanup']['build_roots'][0]['unit_depth'] = 2
        project = self.build / 'project'
        idle = self.file(project / 'old-session' / 'tasks' / 'out.txt', age=3 * HOUR).parents[1]
        active = self.file(project / 'live-session' / 'out.txt', age=60).parent
        loose = self.file(project / 'notes.txt', age=30 * DAY)
        self.age(idle, 3 * HOUR)
        self.age(project / 'live-session', 60)
        result = self.build_run()
        self.assertEqual([(r['action'], r['reason_code']) for r in self.rows(result, idle)], [('removed', 'build_root_idle')])
        self.assertEqual([(r['action'], r['reason_code']) for r in self.rows(result, active)], [('keep', 'build_root_recent')])
        self.assertEqual(self.rows(result, project), [])
        self.assertFalse(idle.exists())
        self.assertTrue(active.exists())
        self.assertTrue(loose.exists())

    def test_unit_depth_two_leaves_an_emptied_parent_and_never_follows_a_link_parent(self):
        self.cfg['disk_cleanup']['build_roots'][0]['unit_depth'] = 2
        target = self.file(self.root / 'precious' / 'session' / 'keep.txt', age=30 * DAY).parents[1]
        self.age(target, 30 * DAY)
        (self.build / 'linked').symlink_to(target, target_is_directory=True)
        lone = self.file(self.build / 'project' / 'only-session' / 'x', age=3 * DAY).parent
        self.age(lone, 3 * DAY)
        result = self.build_run()
        self.assertEqual([r['action'] for r in self.rows(result, lone)], ['removed'])
        self.assertTrue((self.build / 'project').is_dir())
        [link] = self.rows(result, self.build / 'linked')
        self.assertEqual((link['action'], link['reason_code']), ('keep', 'protected'))
        self.assertTrue((target / 'session' / 'keep.txt').exists())


class BuildRootConfig(DiskCase):
    def validated(self, *entries):
        return config.validate(dict(disk_cleanup=dict(build_roots=list(entries))))['disk_cleanup']['build_roots']

    def test_entries_and_default(self):
        root = str(self.root / 'targets')
        self.assertEqual(self.validated(dict(path=root, placement=self.side)),
                         [dict(path=root, placement=self.side, max_idle_hours=2)])
        self.assertEqual(self.validated(dict(path=root, placement=self.side, max_idle_hours=0.5))[0]['max_idle_hours'], 0.5)
        self.assertNotIn('build_roots', config.validate({})['disk_cleanup'])     # an unset key leaves the scope as it was
        self.assertEqual(self.validated(dict(path=root, placement=self.side, unit_depth=2))[0]['unit_depth'], 2)
        self.assertNotIn('unit_depth', self.validated(dict(path=root, placement=self.side))[0])     # nor an unset depth

    def test_build_root_interval(self):
        self.assertNotIn('build_root_interval_s', config.validate({})['disk_cleanup'])   # unset: the scope is as it was
        self.assertEqual(config.validate(dict(disk_cleanup=dict(build_root_interval_s=600)))['disk_cleanup']
                         ['build_root_interval_s'], 600)
        for bad in (0, -1, float('inf'), float('nan'), True, '3600'):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(config.ConfigError, 'build_root_interval_s'):
                    config.validate(dict(disk_cleanup=dict(build_root_interval_s=bad)))

    @unittest.skipUnless(placement.cleanup_side() == 'windows', 'a Windows path')
    def test_the_documented_claude_temp_root_loads_inside_its_temp_root(self):
        home = self.root / 'home'
        home.mkdir()
        (home / 'config.toml').write_text(
            "[disk_cleanup]\n"
            "temp_roots = ['C:\\Users\\johnm\\AppData\\Local\\Temp']\n"
            "[[disk_cleanup.build_roots]]\n"
            "path = 'C:\\Users\\johnm\\AppData\\Local\\Temp\\claude'\n"
            "placement = \"windows\"\n"
            "max_idle_hours = 24\n"
            "unit_depth = 2\n", encoding='utf-8')
        loaded = config.load(home)
        self.assertEqual(loaded['disk_cleanup']['build_roots'],
                         [dict(path='C:\\Users\\johnm\\AppData\\Local\\Temp\\claude', placement='windows',
                               max_idle_hours=24, unit_depth=2)])

    def test_validation_errors(self):
        root = str(self.root / 'targets')
        for bad, words in (('x', 'list'), (['x'], 'table'),
                           ([dict(path='relative/targets', placement=self.side)], 'absolute'),
                           ([dict(placement=self.side)], 'absolute'),
                           ([dict(path=root)], 'placement'),
                           ([dict(path=root, placement='mars')], 'placement'),
                           ([dict(path=root, placement=self.other_side)], 'placement'),
                           ([dict(path=root, placement=self.side, max_age_days=3)], 'unknown key'),
                           ([dict(path=root, placement=self.side, max_idle_hours=0)], 'max_idle_hours'),
                           ([dict(path=root, placement=self.side, max_idle_hours=float('inf'))], 'max_idle_hours'),
                           ([dict(path=root, placement=self.side, max_idle_hours=True)], 'max_idle_hours'),
                           ([dict(path=root, placement=self.side, unit_depth=0)], 'unit_depth'),
                           ([dict(path=root, placement=self.side, unit_depth=3)], 'unit_depth'),
                           ([dict(path=root, placement=self.side, unit_depth='2')], 'unit_depth'),
                           ([dict(path=root, placement=self.side, unit_depth=True)], 'unit_depth'),
                           ([dict(path=root, placement=self.side), dict(path=root + '/inner', placement=self.side)], 'nest')):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(config.ConfigError, words):
                    config.validate(dict(disk_cleanup=dict(build_roots=bad)))

    def preflight(self, folder):
        self.cfg['disk_cleanup']['build_roots'] = [dict(path=str(folder), placement=placement.cleanup_path_side(str(folder)))]
        disk_preflight.check_cache_folders(config.validate(self.cfg))

    def refused(self, folders, words):
        for folder in folders:
            with self.subTest(folder=folder):
                with self.assertRaisesRegex(config.ConfigError, 'build root .*' + words):
                    self.preflight(folder)

    def test_preflight_refuses_drive_and_filesystem_roots(self):
        self.refused(('C:\\', 'D:\\', 'C:/', '/', '/mnt/c'), 'drive or filesystem root')

    def test_preflight_refuses_system_folders(self):
        self.refused(('C:\\Windows', 'C:\\Program Files', 'C:\\Program Files (x86)', 'C:\\ProgramData', 'C:\\Users',
                      'c:\\windows', '/usr', '/etc', '/home', '/var'), 'system folder')

    def test_preflight_refuses_home_on_either_side_by_its_shape(self):
        self.refused(('/home/someone', '/root', 'C:\\Users\\someone'), 'home folder')
        for side in ('windows', 'wsl'):         # a Windows root checked from WSL, too, is known by its shape
            with patch.object(placement, 'cleanup_side', return_value=side):
                self.refused(('C:\\Users\\someone', '/home/someone'), 'home folder')
        self.refused((Path.home(),), 'home folder')

    def test_preflight_refuses_protected_roots_and_git_trees(self):
        repo = self.root / 'checkout'
        self.file(repo / '.git' / 'HEAD', 'ref: refs/heads/main\n')
        (self.repos_dir / 'repo.toml').write_text(config.dumps(dict(name='repo', repo=str(repo),
                                                                    worktrees=str(self.root / 'trees'))))
        for folder in (repo / 'target', repo, self.root, self.state, self.root / 'trees', Path.home(),
                       Path(Path.home().anchor)):
            with self.subTest(folder=folder):
                with self.assertRaisesRegex(config.ConfigError, 'build root'):
                    self.preflight(folder)

    def test_preflight_refuses_a_git_tree_that_is_not_a_registered_repository(self):
        checkout = self.root / 'unregistered'
        self.file(checkout / '.git' / 'HEAD', 'ref: refs/heads/main\n')
        self.refused((checkout / 'target', checkout / 'target' / 'deep'), 'git tree')

    def test_preflight_accepts_a_plain_folder(self):
        self.preflight(self.root / 'cargo-targets')
        self.preflight(Path.home() / 'cargo-targets')       # inside home is fine; home itself is not


if __name__ == '__main__':
    import unittest
    unittest.main()
