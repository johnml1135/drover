"""Regressions from the second report-only machine run."""
import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from drover import config, disk_state, placement
from tests.disk_fixture import DiskCase


class Recovery(unittest.TestCase):
    def test_large_result_never_enters_recovery_argv_or_environment(self):
        run = subprocess.run
        captured = []
        def execute(argv, **kw):
            captured.append((argv, kw))
            self.assertLess(len(subprocess.list2cmdline(argv).encode('utf-16-le')), 8192)
            self.assertNotIn('RESULT_SENTINEL', repr((argv, kw.get('env'))))
            # Execute the exact Python composed for WSL on this test host.
            return run([sys.executable, *argv[argv.index('-c'):]], **kw)
        with patch.object(placement, 'IS_WINDOWS', True), patch.object(placement, 'distro', return_value='Fixture'), \
                patch.object(subprocess, 'run', side_effect=execute):
            self.assertTrue(placement.recover_ci_children('wsl', dict(
                pid=123456789, runner_pid=123456789, run_id='fixture', result='RESULT_SENTINEL' * 6000), '/tmp'))
        self.assertEqual(json.loads(captured[0][0][-1]), dict(pid=123456789, runner_pid=123456789, run_id='fixture'))

    def test_all_wsl_builders_use_bounded_command_boundary(self):
        tree = ast.parse(Path(placement.__file__).read_text())
        builders = []
        for function in tree.body:
            if isinstance(function, (ast.FunctionDef, ast.ClassDef)):
                if any(isinstance(n, ast.Constant) and n.value == 'wsl.exe' for n in ast.walk(function)):
                    builders.append(function.name)
        self.assertEqual(builders, ['wsl_control'])
        for build in (lambda tail: placement.wsl_control(tail),
                      lambda tail: placement.WSL('Fixture').command(tail)):
            self.assertLess(len(subprocess.list2cmdline(build(['python3', '-c', 'pass'])).encode('utf-16-le')), 8192)
            with self.assertRaises(ValueError):
                build(['x' * 8192])


class ProcessPolicy(DiskCase):
    def test_windows_session_zero_and_helper_ignore_keep_named_paths(self):
        for owner, session, image, ignored, conservative in (
                (None, 0, 'service.exe', [], False),
                (None, 1, 'service.exe', [], True),
                (None, None, 'service.exe', [], True),
                (True, 1, 'CTFMON.EXE', ['ctfmon.exe'], False),
                (True, 1, 'ctfmon.exe', [], True),
                (True, 1, 'powershell.exe', None, True),
                (True, 1, 'conhost.exe', None, True),
                (False, 1, 'foreign.exe', [], False)):
            with self.subTest(owner=owner, session=session, image=image):
                policy = {} if ignored is None else dict(process_ignore=ignored)
                proof = placement.cleanup_processes(windows=True, request=dict(paths=policy), cim=lambda: [dict(
                    pid=77, name=image, same_user=owner, session_id=session, cwd=None, cmd=r'H:\candidate\tool.exe')])
                self.assertTrue(proof.busy(r'H:\candidate'))
                self.assertEqual(proof.busy(r'H:\unrelated'), conservative)
                if conservative:
                    self.assertEqual(proof.unreadable[0]['owner_action'],
                                     f'close {image} (pid 77) or add it to [disk_cleanup] process_ignore')

    @unittest.skipIf(os.name == 'nt', 'proc fixture')
    def test_wsl_helpers_are_case_sensitive_and_owner_editable(self):
        proc = self.root / 'proc'
        entry = proc / '123456789'
        entry.mkdir(parents=True)
        (entry / 'cmdline').write_bytes(b'/candidate/tool\0')
        for image, ignored, conservative in (('systemd', None, False), ('(sd-pam)', None, False),
                                             ('Systemd', None, True), ('systemd', [], True),
                                             ('bash', None, True)):
            (entry / 'status').write_text(f'Name:\t{image}\nUid:\t{os.getuid()}\n')
            policy = {} if ignored is None else dict(process_ignore=ignored)
            proof = placement.cleanup_processes(windows=False, proc_root=proc, request=dict(paths=policy))
            self.assertTrue(proof.busy('/candidate'))
            self.assertEqual(proof.busy('/elsewhere'), conservative)

    def test_defaults_migrate_partial_config_idempotently_and_preserve_edits(self):
        expected = {'ctfmon.exe', 'TabTip.exe', 'TextInputHost.exe', 'DefenderSessionHelper.exe',
                    'Wacom_TouchUser.exe', 'Taskmgr.exe', 'SecurityHealthSystray.exe', 'sihost.exe',
                    'csrss.exe', 'winlogon.exe', 'dwm.exe', 'fontdrvhost.exe', 'NVDisplay.Container.exe',
                    'RtkAudUService64.exe', 'WacomHost.exe', 'Wacom_Tablet.exe', 'remoting_desktop.exe',
                    'systemd', '(sd-pam)'}
        migrated = config.migrate_disk_config(dict(disk_cleanup=dict(temp_roots=[])))
        self.assertEqual(set(migrated['disk_cleanup']['process_ignore']), expected)
        self.assertNotIn('powershell.exe', expected)
        self.assertEqual(config.migrate_disk_config(migrated), migrated)
        migrated['disk_cleanup']['process_ignore'] = ['owner-helper']
        self.assertEqual(config.migrate_disk_config(migrated)['disk_cleanup']['process_ignore'], ['owner-helper'])
        from drover.disk_schedule import install
        path = self.state / 'config.toml'
        path.write_text(config.dumps(dict(disk_cleanup=dict(temp_roots=[]))))
        with patch.object(placement, 'disk_schedule_plan', return_value=dict(files={}, argv=[])):
            install(self.cfg)
            saved = path.read_bytes()
            self.assertEqual(set(config._read(path)['disk_cleanup']['process_ignore']), expected)
            install(self.cfg)
        self.assertEqual(path.read_bytes(), saved)

    def test_every_conservative_process_is_named_in_report_and_critical_notice(self):
        from drover import disk_cleanup
        rows = [dict(pid=i, name='powershell.exe', cwd=None, cmd='', same_user=True) for i in range(30)]
        proof = placement.cleanup_processes(windows=True, cim=lambda: rows)
        self.free = 1
        rt = self.runtime(real=True)
        with patch.object(placement, 'cleanup_processes', return_value=proof), \
                patch.object(placement, 'physical_disk_path', return_value=str(self.root)):
            disk_cleanup.run(self.cfg, runtime=rt)
            result = disk_cleanup.run(self.cfg, apply=True, runtime=rt)
        row = next(r for r in result['items'] if r['reason_code'] == 'process_visibility_unavailable' and r['action'] == 'report')
        self.assertEqual(len(row['processes']), 30)
        for process in row['processes']:
            action = f"close powershell.exe (pid {process['pid']}) or add it to [disk_cleanup] process_ignore"
            self.assertEqual(process['owner_action'], action)
            self.assertTrue(any(action in notice['body'] for notice in self.toasts))


class ProcessQuery(DiskCase):
    def test_timeout_is_configurable_and_reported_once_with_elapsed_time(self):
        from drover.disk_side import serve
        self.assertEqual(config.validate({})['disk_cleanup']['process_query_timeout_s'], 240)
        for value in (0, -1, True, float('inf'), '240'):
            with self.subTest(value=value), self.assertRaises(config.ConfigError):
                config.validate(dict(disk_cleanup=dict(process_query_timeout_s=value)))
        self.assertEqual(config.validate(dict(disk_cleanup=dict(process_query_timeout_s=300)))['disk_cleanup']['process_query_timeout_s'], 300)
        request = dict(stage=2, apply=False, now=self.now, paths=dict(process_query_timeout_s=300),
                       state_dir=str(self.state), repos=[], drive=dict(path=str(self.root)), _inventory_id='fixture')
        with patch.object(placement, 'IS_WINDOWS', True), \
                patch.object(placement, '_windows_processes', return_value={}), \
                patch.object(placement, 'cleanup_scope', side_effect=lambda r: dict(r, scope_errors=[])), \
                patch.object(subprocess, 'run', side_effect=subprocess.TimeoutExpired('cim', 300)) as query, \
                patch.object(placement.time, 'monotonic', side_effect=[10., 310.25, 400., 700.25]):
            first = serve(request)
            second = serve(dict(request, stage=3))
        self.assertEqual(query.call_count, 2)
        self.assertEqual(query.call_args.kwargs['timeout'], 300)
        for answer in (first, second):
            row = next(r for r in answer['items'] if r['reason_code'] == 'process_query_timeout')
            self.assertEqual(row['elapsed_s'], 300.25)
        from drover.disk_inventory import bound_report
        report = dict(items=[dict(row, side='windows') for a in (first, second) for row in a['items']])
        bound_report(report)
        self.assertEqual(len([r for r in report['items'] if r['reason_code'] == 'process_query_timeout']), 1)

    def test_one_narrow_query_per_stage_and_drive(self):
        rows = [dict(ProcessId=123456789, ParentProcessId=42, Name='service.exe', SessionId=0,
                     ExecutablePath=r'H:\candidate\tool.exe', CommandLine='', OwnerSid=None)]
        done = subprocess.CompletedProcess([], 0, json.dumps(dict(sid='invoker', processes=rows)), '')
        request = dict(paths={}, state_dir=str(self.state), _inventory_id='fixture')
        with patch.object(subprocess, 'run', return_value=done) as query, \
                patch.object(placement, '_windows_processes', return_value={'123456789': ['service.exe', 1]}), \
                patch.object(placement, '_windows_cwd', return_value=None):
            for stage, drive in ((2, 'H:/'), (4, 'H:/'), (5, 'C:/')):
                proof = placement.cleanup_processes(windows=True, request=dict(request, stage=stage, drive=dict(path=drive)))
                self.assertTrue(proof.busy(r'H:\candidate'))
                self.assertFalse(proof.busy(r'H:\unrelated'))
        self.assertEqual(query.call_count, 3)
        script = query.call_args.args[0][-1]
        self.assertIn('-Property ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine,SessionId', script)
        self.assertEqual(query.call_args.kwargs['timeout'], 240)


class TempTraversal(DiskCase):
    def test_retained_subtrees_are_walked_once_and_roots_have_timings(self):
        old = self.file(self.temp_root / 'retained' / 'deep' / 'deeper' / 'payload')
        self.age(old.parents[2], 86400)  # recent tree is retained
        self.cfg['disk_cleanup']['temp_roots'].append(str(old.parents[1]))
        visits = []
        iterdir = Path.iterdir
        def counted(path):
            if path == old.parents[0]:
                visits.append(str(path))
            return iterdir(path)
        with patch.object(Path, 'iterdir', counted):
            result = self.run_cleanup()
        self.assertEqual(len(visits), 1)
        timings = [r for r in result['items'] if r.get('reason_code') == 'temp_root_elapsed']
        self.assertEqual({r['path'] for r in timings}, set(self.cfg['disk_cleanup']['temp_roots']))
        self.assertTrue(all(r['elapsed_s'] >= 0 for r in timings))
        retained = next(r for r in result['items'] if r.get('path') == str(old.parents[2]))
        self.assertEqual(retained['files'], 1)
        self.assertEqual(retained['bytes'], len('sentinel'))
        self.assertTrue(old.exists())


class ReviewRegressions(DiskCase):
    def test_new_process_after_cim_snapshot_blocks_apply_recheck(self):
        request = dict(paths={}, state_dir=str(self.state), _inventory_id='fixture', apply=True)
        done = subprocess.CompletedProcess([], 0, json.dumps(dict(sid='invoker', processes=[dict(
            ProcessId=101, Name='old.exe', OwnerSid='invoker', SessionId=1, CommandLine='old.exe')])), '')
        with patch.object(subprocess, 'run', return_value=done) as query, \
                patch.object(placement, '_windows_cwd', return_value=r'C:\elsewhere'), \
                patch.object(placement, '_windows_processes', create=True,
                             side_effect=[{'101': ['old.exe', 1]}, {'101': ['old.exe', 1], '202': ['build.exe', 2]},
                                          {'101': ['old.exe', 1], '202': ['build.exe', 2]}]):
            first = placement.cleanup_processes(windows=True, request=request)
            second = placement.cleanup_processes(windows=True, request=request)
        self.assertFalse(first.busy(r'C:\candidate'))
        self.assertTrue(second.busy(r'C:\candidate'))
        self.assertEqual(query.call_count, 2)
        self.assertEqual(second.unreadable[0]['pid'], 202)
        self.assertNotIn('owner_action', second.unreadable[0])

    def test_unknown_owner_nonzero_session_is_conservative_even_with_cwd(self):
        proof = placement.cleanup_processes(windows=True, cim=lambda: [dict(
            pid=77, name='unknown.exe', same_user=None, session_id=1, cwd=r'C:\elsewhere', cmd='')])
        self.assertTrue(proof.busy(r'C:\candidate'))
        self.assertEqual(proof.unreadable[0]['pid'], 77)

    def test_process_report_unions_distinct_stage_snapshots(self):
        from drover.disk_inventory import bound_report
        report = dict(items=[dict(action='report', side='wsl', reason_code='process_visibility_unavailable',
                                  count=1, processes=[dict(pid=pid, name='bash', owner_action=f'close bash (pid {pid}) or add it to [disk_cleanup] process_ignore')])
                             for pid in (10, 20)])
        bound_report(report)
        self.assertEqual(report['items'][0]['count'], 2)
        self.assertEqual({p['pid'] for p in report['items'][0]['processes']}, {10, 20})

    def test_overlapping_roots_keep_nested_candidate_boundaries(self):
        old = self.file(self.temp_root / 'container' / 'nested' / 'old')
        self.age(old.parents[0], 10 * 86400)
        self.age(old.parents[1], 86400)
        # Restore nested age after making only the container recent.
        self.age(old.parents[0], 10 * 86400)
        self.cfg['disk_cleanup']['temp_roots'].append(str(old.parents[0]))
        result = self.run_cleanup()
        self.assertTrue(any(r.get('path') == str(old) and r['reason_code'] == 'stale_temp' for r in result['items']))

    def test_process_detected_by_deletion_recheck_is_named_in_stage_report(self):
        from drover.disk_side import serve
        old = self.file(self.temp_root / 'old')
        request = dict(stage=2, apply=True, now=self.now, repos=[], state_dir=str(self.state),
                       drive=dict(path=str(self.root)), paths=self.cfg['disk_cleanup'], _inventory_id='fixture')
        done = subprocess.CompletedProcess([], 0, json.dumps(dict(sid='invoker', processes=[dict(
            ProcessId=101, Name='old.exe', OwnerSid='invoker', SessionId=1, CommandLine='old.exe')])), '')
        with patch.object(placement, 'IS_WINDOWS', True), \
                patch.object(placement, 'cleanup_scope', side_effect=lambda r: dict(r, scope_errors=[])), \
                patch.object(placement, 'cleanup_contains', return_value=True), \
                patch.object(subprocess, 'run', return_value=done), \
                patch.object(placement, '_windows_cwd', return_value=r'C:\elsewhere'), \
                patch.object(placement, '_windows_processes',
                             side_effect=[{'101': ['old.exe', 1]}, {'101': ['old.exe', 1], '202': ['build.exe', 2]},
                                          {'101': ['old.exe', 1], '202': ['build.exe', 2]}]):
            answer = serve(request)
        self.assertTrue(old.exists())
        row = next(r for r in answer['items'] if r['reason_code'] == 'process_visibility_unavailable' and r['action'] == 'report')
        self.assertEqual(row['processes'][0]['pid'], 202)
