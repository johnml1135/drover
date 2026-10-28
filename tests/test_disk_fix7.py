"""Identity and metadata regressions from cleanup review seven and ruling R12."""
import importlib.util
import json
import os
import subprocess
import sys
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

from drover import config, disk_state, placement, repos
from tests.disk_fixture import DiskCase
from tests.gitfix import Repo


RUNNER_RECORD_PROBE = r"""import json, os, sys, time
from pathlib import Path
root = Path(sys.argv[1]) / 'disk'
runner_pid = int(sys.argv[2])
deadline = time.monotonic() + 5
while True:
    rows = [json.loads(p.read_text()) for p in root.glob('process-*.json')]
    if runner_pid in {r['identity']['pid'] for r in rows} and any(
            r['identity']['pid'] in (os.getpid(), os.getppid()) and r['identity']['pid'] != runner_pid
            for r in rows):
        break
    if time.monotonic() >= deadline:
        raise AssertionError(rows)
    time.sleep(.01)  # Resolver: parent publishes the child's kernel identity.
assert next(r for r in rows if r['identity']['pid'] != runner_pid)['cwd'] == str(Path.cwd())
"""


R12_HELPERS = {'csrss.exe', 'winlogon.exe', 'dwm.exe', 'fontdrvhost.exe',
               'NVDisplay.Container.exe', 'RtkAudUService64.exe', 'WacomHost.exe', 'Wacom_Tablet.exe'}


class CachedIdentity(DiskCase):
    def test_native_creation_time_must_be_readable_and_match_on_every_recheck(self):
        for baseline, current, refresh in ((None, None, True), (10, None, True),
                                           (10, 10, False), (10, 20, True)):
            with self.subTest(baseline=baseline, current=current):
                request = dict(paths={}, state_dir=str(self.state), apply=True,
                               _inventory_id=f'{baseline}-{current}')
                cached = dict(pid=77, name='python.exe', same_user=False, cmd='', cwd=None)
                disk_state.save_file(self.state, 'processes-' + request['_inventory_id'] + '.json',
                                     dict(rows=[cached], identities={'77': ['python.exe', baseline]}))
                # PID reuse: stale foreign evidence must not hide the new unreadable process.
                fresh = dict(pid=77, name='python.exe', same_user=None, cmd='', cwd=None)
                with patch.object(placement, '_windows_processes', return_value={'77': ['python.exe', current]}) as _placement_patch_14, patch('drover.placement_proof._windows_processes', _placement_patch_14), \
                        patch.object(placement, '_cim_rows', return_value=[fresh]) as query, patch('drover.placement_process._cim_rows', query), patch('drover.placement_proof._cim_rows', query):
                    for _ in range(2):
                        proof = placement.cleanup_processes(windows=True, request=request)
                        self.assertEqual(proof.busy(r'H:\candidate'), refresh)
                    self.assertEqual(bool(query.call_count), refresh)

    def test_refreshed_owner_and_argv_replace_stale_foreign_evidence(self):
        request = dict(paths={}, state_dir=str(self.state), apply=True, _inventory_id='reused')
        disk_state.save_file(self.state, 'processes-reused.json', dict(
            rows=[dict(pid=77, name='python.exe', same_user=False, cmd='', cwd=None)],
            identities={'77': ['python.exe', 10]}))
        with patch.object(placement, '_windows_processes', return_value={'77': ['python.exe', 20]}) as _placement_patch_1, patch('drover.placement_proof._windows_processes', _placement_patch_1), \
                patch.object(placement, '_windows_cwd', return_value=r'C:\elsewhere') as _placement_patch_2, patch('drover.placement_proof._windows_cwd', _placement_patch_2), \
                patch.object(placement, '_cim_rows', return_value=[dict(
                    pid=77, name='python.exe', same_user=True, cmd=r'H:\candidate\tool')]) as _placement_patch_3, patch('drover.placement_process._cim_rows', _placement_patch_3), patch('drover.placement_proof._cim_rows', _placement_patch_3):
            proof = placement.cleanup_processes(windows=True, request=request)
        self.assertTrue(proof.busy(r'H:\candidate'))
        self.assertFalse(proof.busy(r'H:\unrelated'))


    def test_refreshed_same_user_with_unreadable_argv_is_conservative(self):
        request = dict(paths={}, state_dir=str(self.state), apply=True, _inventory_id='argv')
        disk_state.save_file(self.state, 'processes-argv.json', dict(
            rows=[dict(pid=77, name='python.exe', same_user=False, cwd=None, cmd='')],
            identities={'77': ['python.exe', 10]}))
        with patch.object(placement, '_windows_processes', return_value={'77': ['python.exe', 20]}) as _placement_patch_4, patch('drover.placement_proof._windows_processes', _placement_patch_4), \
                patch.object(placement, '_cim_rows', return_value=[dict(
                    pid=77, name='python.exe', same_user=True, cwd=r'C:\elsewhere', cmd=None)]) as _placement_patch_5, patch('drover.placement_process._cim_rows', _placement_patch_5), patch('drover.placement_proof._cim_rows', _placement_patch_5):
            self.assertTrue(placement.cleanup_processes(windows=True, request=request).busy(r'H:\candidate'))


    def test_failed_identity_refresh_retains_and_names_the_process(self):
        request = dict(paths={}, state_dir=str(self.state), apply=True, _inventory_id='failed')
        disk_state.save_file(self.state, 'processes-failed.json', dict(
            rows=[dict(pid=77, name='python.exe', same_user=False, cwd=None, cmd='')],
            identities={'77': ['python.exe', None]}))
        with patch.object(placement, '_windows_processes', return_value={'77': ['python.exe', None]}) as _placement_patch_6, patch('drover.placement_proof._windows_processes', _placement_patch_6), \
                patch.object(placement, '_cim_rows', side_effect=subprocess.TimeoutExpired('cim', 240)) as _placement_patch_7, patch('drover.placement_process._cim_rows', _placement_patch_7), patch('drover.placement_proof._cim_rows', _placement_patch_7):
            proof = placement.cleanup_processes(windows=True, request=request)
        self.assertTrue(proof.busy(r'H:\candidate'))
        self.assertEqual([p['pid'] for p in proof.unreadable], [77])
        self.assertEqual(proof.query_failure['reason_code'], 'process_query_timeout')
        self.assertIn('elapsed_s', proof.query_failure)

    def test_missing_birth_never_confirms_recorded_identity_on_either_side(self):
        for side in ('windows', 'wsl'):
            for birth in (None, '', 'absent'):
                with self.subTest(side=side, birth=birth), \
                        patch.object(placement, 'cleanup_side', return_value=side) as _placement_patch_16, patch('drover.placement_process.cleanup_side', _placement_patch_16), \
                        patch.object(placement, 'cleanup_identity', return_value=dict(pid=77, side=side, birth=birth)) as _placement_patch_17, patch('drover.placement_process.cleanup_identity', _placement_patch_17), patch('drover.placement_proof.cleanup_identity', _placement_patch_17):
                    alive = placement.cleanup_claim_alive(dict(pid=77, side=side, birth=birth))
                    self.assertIsNot(alive, True)


    def test_missing_pid_and_other_side_cannot_confirm_a_process_identity(self):
        for side in ('windows', 'wsl'):
            with patch.object(placement, 'cleanup_side', return_value=side) as _placement_patch_10, patch('drover.placement_process.cleanup_side', _placement_patch_10), \
                    patch.object(placement, 'cleanup_identity', return_value=dict(pid=None, side=side, birth='boot:10')) as _placement_patch_11, patch('drover.placement_process.cleanup_identity', _placement_patch_11), patch('drover.placement_proof.cleanup_identity', _placement_patch_11):
                self.assertIsNone(placement.cleanup_claim_alive(dict(pid=None, side=side, birth='boot:10')))
                other = 'wsl' if side == 'windows' else 'windows'
                self.assertIsNone(placement.cleanup_claim_alive(dict(pid=77, side=other, birth='absent')))

    @unittest.skipIf(os.name == 'nt', 'proc identity')
    def test_unreadable_boot_identity_is_unknown_and_not_a_dead_process(self):
        read = Path.read_text
        def unavailable(path, *args, **kwargs):
            if str(path) == '/proc/sys/kernel/random/boot_id':
                raise FileNotFoundError(str(path))
            return read(path, *args, **kwargs)
        with patch.object(Path, 'read_text', unavailable):
            identity = placement.cleanup_identity()
            self.assertIsNone(identity['birth'])
            self.assertIsNone(placement.cleanup_claim_alive(dict(identity, birth='old:10')))

    def test_missing_run_identity_cannot_authorize_child_recovery(self):
        with patch.object(placement, 'IS_WINDOWS', False) as _placement_patch_8, patch('drover.placement_exec.IS_WINDOWS', _placement_patch_8), patch('drover.placement_git.IS_WINDOWS', _placement_patch_8), patch('drover.placement_host.IS_WINDOWS', _placement_patch_8), patch('drover.placement_paths.IS_WINDOWS', _placement_patch_8), patch('drover.placement_process.IS_WINDOWS', _placement_patch_8), patch('drover.placement_proof.IS_WINDOWS', _placement_patch_8), patch('drover.placement_tree.IS_WINDOWS', _placement_patch_8), patch('drover.placement_worker.IS_WINDOWS', _placement_patch_8), \
                patch.object(Path, 'iterdir', side_effect=AssertionError('must not inspect an unidentified tree')):
            self.assertFalse(placement.stop_ci_children(dict(pid=77, run_id=None)))


    @unittest.skipIf(os.name == 'nt', 'proc identity')
    def test_legacy_birth_without_boot_identity_stays_unknown(self):
        with patch.object(placement, 'cleanup_identity', return_value=dict(pid=77, side='wsl', birth='boot:10')) as _placement_patch_9, patch('drover.placement_process.cleanup_identity', _placement_patch_9), patch('drover.placement_proof.cleanup_identity', _placement_patch_9):
            self.assertIsNone(placement.cleanup_claim_alive(dict(pid=77, side='wsl', birth='10')))

    @unittest.skipUnless(os.name == 'nt', 'native Windows CIM command')
    def test_composed_cim_refresh_reads_the_launched_process_argv(self):
        request = dict(paths={}, state_dir=str(self.state), apply=True, _inventory_id='native')
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)', str(self.root / 'candidate')])
        try:
            name = Path(sys.executable).name
            disk_state.save_file(self.state, 'processes-native.json', dict(
                rows=[dict(pid=child.pid, name=name, same_user=False, cwd=None, cmd='')],
                identities={str(child.pid): [name, 1]}))
            with patch.object(placement, '_windows_processes', return_value={str(child.pid): [name, 2]}) as _placement_patch_12, patch('drover.placement_proof._windows_processes', _placement_patch_12), \
                    patch.object(placement, '_windows_cwd', return_value=str(self.root / 'elsewhere')) as _placement_patch_13, patch('drover.placement_proof._windows_cwd', _placement_patch_13):
                proof = placement.cleanup_processes(windows=True, request=request)
            self.assertTrue(proof.busy(str(self.root / 'candidate')))
            self.assertFalse(proof.busy(str(self.root / 'unrelated')))
        finally:
            child.terminate()
            child.wait(timeout=10)



class HelperPolicy(DiskCase):
    def test_unknown_owner_helpers_and_executable_anti_spoof(self):
        roots = [r'H:\candidate', r'H:\repo', r'H:\trees', r'H:\temp', r'H:\cache']
        request = dict(paths=dict(temp_roots=[roots[3]], caches=[dict(path=roots[4])]),
                       repos=[dict(repo=roots[1], worktrees=roots[2])], approved_path=roots[0])
        for owner in (None, True):
            for exe, conservative in [(None, False), (r'C:\Windows\csrss.exe', False),
                                      *[(root + r'\csrss.exe', True) for root in roots]]:
                with self.subTest(owner=owner, exe=exe):
                    proof = placement.cleanup_processes(windows=True, request=request, cim=lambda: [dict(
                        pid=77, name='CSRSS.EXE', same_user=owner, session_id=1,
                        cwd=None, cmd='', exe=exe)])
                    self.assertEqual(proof.busy(r'H:\elsewhere'), conservative)
                    self.assertEqual(bool(proof.unreadable), conservative)
        # Foreign named paths still retain candidates even when a helper is ignored.
        proof = placement.cleanup_processes(windows=True, request=request, cim=lambda: [dict(
            pid=77, name='csrss.exe', same_user=None, session_id=1, cwd=None,
            cmd=r'H:\candidate\input', exe=r'C:\Windows\csrss.exe')])
        self.assertTrue(proof.busy(roots[0]))
        self.assertFalse(proof.busy(r'H:\elsewhere'))

    @unittest.skipIf(os.name == 'nt', 'proc fixture')
    def test_wsl_ignore_cannot_spoof_executable_under_repo(self):
        proc = self.root / 'proc'
        entry = proc / '123456789'
        entry.mkdir(parents=True)
        (entry / 'status').write_text(f'Name:\tsystemd\nUid:\t{os.getuid()}\n')
        (entry / 'cmdline').write_bytes(b'')
        executable = self.file(self.root / 'repo' / 'systemd')
        (entry / 'exe').symlink_to(executable)
        request = dict(paths={}, repos=[dict(repo=str(executable.parent), worktrees=str(self.root / 'trees'))])
        proof = placement.cleanup_processes(windows=False, proc_root=proc, request=request)
        self.assertTrue(proof.busy(str(self.root / 'elsewhere')))

    def test_defaults_seed_install_and_migration_idempotently(self):
        from drover.disk_schedule import install
        expected = set(placement.default_process_ignore())
        self.assertTrue(R12_HELPERS <= expected)
        raw = dict(disk_cleanup=dict(temp_roots=[]))
        migrated = config.migrate_disk_config(raw)
        self.assertEqual(set(migrated['disk_cleanup']['process_ignore']), expected)
        self.assertEqual(config.migrate_disk_config(migrated), migrated)
        path = self.state / 'config.toml'
        path.write_text(config.dumps(raw))
        with patch.object(placement, 'disk_schedule_plan', return_value=dict(files={}, argv=[])):
            install(self.cfg)
            saved = path.read_bytes()
            self.assertEqual(set(config._read(path)['disk_cleanup']['process_ignore']), expected)
            install(self.cfg)
        self.assertEqual(path.read_bytes(), saved)
        edited = dict(disk_cleanup=dict(process_ignore=['owner-helper']))
        self.assertEqual(config.migrate_disk_config(edited)['disk_cleanup']['process_ignore'], ['owner-helper'])


    def test_previous_factory_ignore_list_is_upgraded_without_overwriting_owner_edits(self):
        from drover.disk_schedule import install
        previous = ['ctfmon.exe', 'TabTip.exe', 'TextInputHost.exe', 'DefenderSessionHelper.exe',
                    'Wacom_TouchUser.exe', 'Taskmgr.exe', 'SecurityHealthSystray.exe', 'sihost.exe',
                    'systemd', '(sd-pam)']
        raw = dict(disk_cleanup=dict(temp_roots=[], process_ignore=previous))
        expected = set(previous) | R12_HELPERS | {'remoting_desktop.exe'}
        migrated = config.migrate_disk_config(raw)
        self.assertEqual(set(migrated['disk_cleanup']['process_ignore']), expected)
        self.assertEqual(config.migrate_disk_config(migrated), migrated)
        edited = dict(disk_cleanup=dict(process_ignore=previous + ['owner-helper']))
        self.assertEqual(config.migrate_disk_config(edited)['disk_cleanup']['process_ignore'],
                         previous + ['owner-helper'])
        path = self.state / 'config.toml'
        path.write_text(config.dumps(raw))
        with patch.object(placement, 'disk_schedule_plan', return_value=dict(files={}, argv=[])):
            install(self.cfg)
            saved = path.read_bytes()
            self.assertEqual(set(config._read(path)['disk_cleanup']['process_ignore']), expected)
            install(self.cfg)
        self.assertEqual(path.read_bytes(), saved)

    def test_report_count_agrees_with_every_conservative_process(self):
        from drover.disk_inventory import bound_report
        rows = [dict(pid=n, name='python.exe', same_user=None, session_id=1, cwd=None, cmd='')
                for n in range(35)]
        proof = placement.cleanup_processes(windows=True, cim=lambda: rows)
        report = dict(items=[dict(action='report', side='windows', reason_code='process_visibility_unavailable',
                                  count=len(proof.unreadable), processes=proof.unreadable)])
        bound_report(report)
        row = report['items'][0]
        self.assertEqual(row['count'], 35)
        self.assertEqual(len(row['processes']), row['count'])
        for p in row['processes']:
            self.assertEqual(p['owner_action'], f"close python.exe (pid {p['pid']}) or add it to [disk_cleanup] process_ignore")


class OwnProcesses(DiskCase):
    def test_recorded_pid_birth_and_cwd_are_required_on_both_placements(self):
        for side in ('windows', 'wsl'):
            for recorded, actual, conservative in ((None, None, True), ('10', None, True),
                                                     ('10', '20', True), ('10', '10', False)):
                with self.subTest(side=side, recorded=recorded, actual=actual):
                    identity = dict(pid=77, side=side, birth=recorded)
                    disk_state.save_file(self.state, f'process-{side}-77.json',
                                         dict(identity=identity, cwd='/known' if side == 'wsl' else r'C:\known'))
                    request = dict(state_dir=str(self.state), paths={})
                    with patch.object(placement, 'cleanup_identity', return_value=dict(pid=77, side=side, birth=actual)) as _placement_patch_21, patch('drover.placement_process.cleanup_identity', _placement_patch_21), patch('drover.placement_proof.cleanup_identity', _placement_patch_21):
                        if side == 'windows':
                            proof = placement.cleanup_processes(windows=True, request=request, cim=lambda: [dict(
                                pid=77, name='python.exe', same_user=True, cwd=None, cmd='')])
                        else:
                            proc = self.root / 'proc'
                            entry = proc / '77'
                            entry.mkdir(parents=True, exist_ok=True)
                            (entry / 'status').write_text('Name:\tpython\nUid:\t1000\n')
                            (entry / 'cmdline').write_bytes(b'')
                            with patch.object(os, 'getuid', return_value=1000, create=True):
                                proof = placement.cleanup_processes(windows=False, proc_root=proc, request=request)
                        self.assertEqual(proof.busy('/elsewhere' if side == 'wsl' else r'C:\elsewhere'), conservative)
                        self.assertTrue(proof.busy('/known' if side == 'wsl' else r'C:\known'))


    def test_new_runner_after_cim_snapshot_uses_its_recorded_native_identity(self):
        for reused in (False, True):
            with self.subTest(reused=reused):
                request = dict(paths={}, state_dir=str(self.state), apply=True, _inventory_id='new-runner')
                rows = [dict(pid=1, name='service.exe', same_user=False, cwd=None, cmd='')]
                identities = {'1': ['service.exe', 10]}
                if reused:
                    rows.append(dict(pid=77, name='python.exe', same_user=False, cwd=None, cmd=''))
                    identities['77'] = ['python.exe', 1]
                disk_state.save_file(self.state, 'processes-new-runner.json', dict(rows=rows, identities=identities))
                disk_state.save_file(self.state, 'process-windows-77.json', dict(
                    identity=dict(pid=77, side='windows', birth='20'), cwd=r'C:\known'))
                with patch.object(placement, '_windows_processes', return_value={
                        '1': ['service.exe', 10], '77': ['python.exe', 20]}) as _placement_patch_18, patch('drover.placement_proof._windows_processes', _placement_patch_18), \
                        patch.object(placement, '_windows_cwd', return_value=None) as _placement_patch_19, patch('drover.placement_proof._windows_cwd', _placement_patch_19), \
                        patch.object(placement, '_cim_rows', return_value=[dict(
                            pid=77, name='python.exe', same_user=True, cmd='', exe=r'C:\Python\python.exe')]) as _placement_patch_20, patch('drover.placement_process._cim_rows', _placement_patch_20), patch('drover.placement_proof._cim_rows', _placement_patch_20):
                    proof = placement.cleanup_processes(windows=True, request=request)
                self.assertFalse(proof.busy(r'H:\unrelated'))
                self.assertTrue(proof.busy(r'C:\known'))

    def test_process_registration_has_kernel_identity_and_is_released(self):
        with placement.cleanup_process_record(self.state, str(self.root)):
            record = json.loads((self.state / 'disk' / f'process-{placement.cleanup_side()}-{os.getpid()}.json').read_text())
            self.assertEqual(record['identity']['pid'], os.getpid())
            self.assertEqual(record['identity']['side'], placement.cleanup_side())
            self.assertTrue(record['identity']['birth'])
            if os.name == 'nt':
                self.assertEqual(record['identity']['birth'], str(placement._windows_birth(os.getpid())))
            else:
                self.assertEqual(record['identity'], placement.cleanup_identity())
            self.assertEqual(record['cwd'], str(self.root))
        self.assertFalse(list((self.state / 'disk').glob('process-*.json')))


    def test_unwritable_process_record_does_not_abort_hub_or_runner(self):
        with patch.object(disk_state, 'save_file', side_effect=PermissionError('unavailable')):
            with placement.cleanup_process_record(self.state):
                pass
        self.assertFalse(list((self.state / 'disk').glob('process-*.json')))

    @unittest.skipUnless(importlib.util.find_spec('uvicorn'), 'hub entrypoint needs the hub environment (uvicorn)')
    def test_hub_entrypoint_records_its_actual_process_for_cleanup(self):
        from hub import __main__ as hub_main
        app = SimpleNamespace(state=SimpleNamespace(start_background=lambda: None, shutdown=lambda: None))
        settings = SimpleNamespace(ledger_path=self.state / 'ledger.db', runs_dir=self.state / 'runs')
        async def serve(app, settings):
            record = json.loads(next((self.state / 'disk').glob('process-*.json')).read_text())
            self.assertEqual(record['identity']['pid'], os.getpid())
            self.assertEqual(record['cwd'], str(Path.cwd()))
        with patch.object(hub_main, 'Settings', return_value=settings), \
                patch.object(hub_main, 'create_app', return_value=app), \
                patch.object(hub_main, 'serve', side_effect=serve):
            hub_main.main([])
        self.assertFalse(list((self.state / 'disk').glob('process-*.json')))

    def test_runner_records_itself_and_launched_process_and_releases_both(self):
        from drover import runner

        result = runner.supervise('fixture', self.state / 'runs',
                                  [sys.executable, '-c', RUNNER_RECORD_PROBE, str(self.state), str(os.getpid())], cwd=str(self.root), poll_s=.01)
        self.assertEqual(result['exit_code'], 0, (self.state / 'runs' / 'fixture' / 'out.log').read_text())
        self.assertFalse(list((self.state / 'disk').glob('process-*.json')))



class MachineTransport(unittest.TestCase):
    def test_cleanup_transports_machine_facts_without_trusting_serialized_metadata(self):
        from drover.disk_cleanup import _repos
        from drover.disk_worktrees import outputs_at
        fixture = Repo()
        self.addCleanup(fixture.cleanup)
        head = fixture.commit('.drover/config.toml', 'schema_version=1\n[cleanup]\ndisposable=["out"]\n')
        state = fixture.root / 'state'
        directory = state / 'repos'
        directory.mkdir(parents=True)
        machine = dict(name='alpha', repo=str(fixture.repo))
        path = directory / 'alpha.toml'
        path.write_text(config.dumps(machine))
        cfg = config.validate(dict(paths=dict(state_dir=str(state)), repos_dir=str(directory)))
        loaded = _repos(cfg)
        self.assertEqual(len(loaded), 1)
        transported = json.loads(json.dumps(loaded))[0]
        self.assertFalse(any(k.startswith('_') for k in transported))
        self.assertEqual(outputs_at(transported, fixture.repo), ['out'])
        self.assertEqual(path.read_text(), config.dumps(machine))
        with self.assertRaises(repos.RepoError):
            repos.load(json.loads(json.dumps(repos.load(machine))))
        path.write_text(config.dumps(dict(machine, _adopted=True)))
        with self.assertRaises(repos.RepoError):
            repos.read_machine(path)
