"""Review eight: canonical safety, fresh process evidence and bounded diagnostics."""
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from drover import disk_state, placement
from tests.disk_fixture import DiskCase


class Processes(DiskCase):
    def cached(self, rows, identities):
        request = dict(paths={}, state_dir=str(self.state), _inventory_id='probe')
        disk_state.save_file(self.state, 'processes-probe.json', dict(rows=rows, identities=identities))
        return request

    def test_own_record_supplies_only_cwd_and_preserves_new_exe_and_argv(self):
        for field in ('exe', 'cmd'):
            with self.subTest(field=field):
                request = self.cached([], {})
                disk_state.save_file(self.state, 'process-windows-77.json', dict(
                    identity=dict(pid=77, side='windows', birth='20'), cwd=r'C:\known'))
                row = dict(pid=77, name='python.exe', same_user=True, cmd='', exe=None)
                row[field] = r'H:\candidate\tool.exe'
                with patch.object(placement, '_windows_processes', return_value={'77': ['python.exe', 20]}), \
                        patch.object(placement, '_windows_cwd', return_value=None), \
                        patch.object(placement, '_cim_rows', return_value=[row]):
                    proof = placement.cleanup_processes(windows=True, request=request)
                self.assertTrue(proof.busy('/mnt/h/candidate'))
                self.assertTrue(proof.busy(r'C:\known'))
                self.assertFalse(proof.busy(r'H:\unrelated'))

    def test_new_pid_gets_targeted_read_instead_of_retaining_everything(self):
        request = self.cached([dict(pid=1, name='service.exe', same_user=False, cwd=None, cmd='')],
                              {'1': ['service.exe', 10]})
        row = dict(pid=77, name='git.exe', same_user=True, cmd=r'H:\candidate\input', exe=r'C:\tools\git.exe')
        with patch.object(placement, '_windows_processes', return_value={
                '1': ['service.exe', 10], '77': ['git.exe', 20]}), \
                patch.object(placement, '_windows_cwd', return_value=r'C:\elsewhere'), \
                patch.object(placement, '_cim_rows', return_value=[row]) as query:
            proof = placement.cleanup_processes(windows=True, request=request)
        self.assertEqual(query.call_args.args[1], ['77'])
        self.assertTrue(proof.busy(r'H:\candidate'))
        self.assertFalse(proof.busy(r'H:\unrelated'))

    def test_failed_transient_read_does_not_tell_owner_to_close_it(self):
        request = self.cached([], {})
        with patch.object(placement, '_windows_processes', return_value={'77': ['git.exe', 20]}), \
                patch.object(placement, '_cim_rows', side_effect=OSError('gone')):
            proof = placement.cleanup_processes(windows=True, request=request)
        self.assertTrue(proof.busy(r'H:\candidate'))
        self.assertEqual([p['pid'] for p in proof.unreadable], [77])
        self.assertNotIn('close ', proof.unreadable[0].get('owner_action', ''))

    def test_r13_only_trusted_os_locations_allow_helper_exemption(self):
        unsafe = [r'H:\repo\csrss.exe', '/mnt/h/repo/csrss.exe', r'\\?\H:\repo\csrss.exe',
                  r'\\server\share\repo\csrss.exe', r'\\?\UNC\server\share\repo\csrss.exe',
                  r'H:\external-git-worktree\csrss.exe', r'C:\Windows-lookalike\csrss.exe']
        safe = [None, r'C:\Windows\System32\csrss.exe', r'\\?\C:\Windows\System32\csrss.exe',
                '/mnt/c/Windows/System32/csrss.exe', r'C:\Program Files\helper\csrss.exe']
        with patch.dict(os.environ, {'SystemRoot': r'C:\Windows', 'ProgramFiles': r'C:\Program Files'}):
            for exe in unsafe + safe:
                with self.subTest(exe=exe):
                    proof = placement.cleanup_processes(windows=True, cim=lambda: [dict(
                        pid=77, name='csrss.exe', same_user=True, cwd=None, cmd='', exe=exe)])
                    self.assertEqual(proof.busy(r'H:\unrelated'), exe in unsafe)

    def test_named_argv_cwd_and_exe_share_normalization(self):
        for spelling in (r'H:\Repo\child', '/mnt/h/Repo/child', r'\\?\H:\Repo\child'):
            for field in ('cmd', 'cwd', 'exe'):
                with self.subTest(spelling=spelling, field=field):
                    row = dict(pid=77, name='foreign.exe', same_user=False, cwd=None, cmd='', exe=None)
                    row[field] = spelling
                    proof = placement.cleanup_processes(windows=True, cim=lambda: [row])
                    self.assertTrue(proof.busy('/mnt/h/repo'))
                    self.assertFalse(proof.busy(r'H:\repository'))
        for spelling in (r'\\server\share\repo\child', r'\\?\UNC\server\share\repo\child'):
            proof = placement.cleanup_processes(windows=True, cim=lambda: [dict(
                pid=77, name='foreign.exe', same_user=False, cwd=spelling, cmd='')])
            self.assertTrue(proof.busy('//SERVER/share/repo'))


class SafetyPaths(DiskCase):
    def test_containment_and_live_task_paths_accept_drive_and_extended_spellings(self):
        from drover.disk_paths import within, live_tree
        from drover.disk_worktrees import task_facts
        for root in (r'H:\Repo', '/mnt/h/repo', r'\\?\H:\repo'):
            with self.subTest(root=root):
                self.assertTrue(within('/mnt/h/repo/child', root))
                self.assertFalse(within('/mnt/h/repository', root))
                request = dict(now=self.now, tasks=dict(at=self.now, active_worktrees=[root], tasks=[]))
                self.assertTrue(task_facts(request, '/mnt/h/repo', None)[0])
                self.assertTrue(live_tree('/mnt/h/repo/child', request))
        self.assertTrue(within(r'\\?\UNC\server\share\repo\child', '//server/share/repo'))

    @unittest.skipIf(os.name == 'nt', 'Linux realpath semantics')
    def test_linux_realpath_alias_is_same_but_case_is_distinct(self):
        from drover.disk_paths import within
        real = self.root / 'real'
        real.mkdir()
        alias = self.root / 'alias'
        try:
            alias.symlink_to(real, target_is_directory=True)
        except OSError:
            self.skipTest('symlinks unavailable')
        self.assertTrue(within(alias / 'child', real))
        if os.name != 'nt':
            self.assertFalse(within(real / 'Child', real / 'child'))


class InventoryBounds(DiskCase):
    def test_inventory_intermediate_is_bounded_and_peak_is_reported(self):
        from drover.disk_inventory import Inventory
        request = dict(_inventory_id='bounded', state_dir=str(self.state), drive=dict(path='H:/'))
        inv = Inventory(self.now, request)
        # A broad directory tree repeats its largest-file lists in ancestor summaries.
        largest = [dict(path='x' * 200, bytes=1024) for _ in range(20)]
        inv.directories = {f'/tree/{n}': dict(files=20, bytes=20480, newest=0, largest=largest,
                                             repository=False) for n in range(1500)}
        inv.save()
        actual = (self.state / 'disk' / inv.cache[1]).stat().st_size
        self.assertLessEqual(actual, 4 * 1024 * 1024)
        self.assertEqual(inv.peak_intermediate_bytes, actual)
        # A dropped cache entry must be measured afresh, never treated as empty.
        fresh = self.file(self.root / 'fresh', 'facts')
        restored = Inventory(self.now, request)
        self.assertEqual(restored.measure(fresh)['bytes'], 5)


class VhdxReport(DiskCase):
    def test_sparse_and_nonsparse_readings_are_report_only(self):
        paths = {'Ubuntu': r'H:\WSL\Ubuntu\ext4.vhdx', 'Other': r'C:\WSL\ext4.vhdx'}
        def read(path):
            return dict(sparse=path == paths['Other'], allocated_bytes=590, apparent_bytes=1024)
        rows = placement.cleanup_vhdx_report(paths=paths, read=read)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row['action'], 'report')
            self.assertEqual(row['allocated_bytes'], 590)
            self.assertEqual(row['apparent_bytes'], 1024)
        row = next(r for r in rows if r['distro'] == 'Ubuntu')
        self.assertEqual(row['owner_action'], 'wsl --shutdown, then wsl --manage Ubuntu --set-sparse true (and compact once with Optimize-VHD)')
        self.assertNotIn('owner_action', next(r for r in rows if r['distro'] == 'Other'))

    def test_unreadable_vhdx_reports_unknown_without_advising_a_change(self):
        rows = placement.cleanup_vhdx_report(paths={'Ubuntu': r'H:\ext4.vhdx'},
                                             read=lambda path: (_ for _ in ()).throw(PermissionError('unreadable')))
        self.assertIn('error', rows[0])
        self.assertNotIn('owner_action', rows[0])


class OwnerActions(DiskCase):
    def test_every_drover_owner_action_in_disk_modules_parses_real_cli(self):
        import argparse
        import ast
        import shlex
        from drover import __main__ as cli
        class Parsed(BaseException):
            pass
        parse = argparse.ArgumentParser.parse_args
        def parse_only(parser, *args, **kwargs):
            parse(parser, *args, **kwargs)
            raise Parsed()
        commands = []
        for source in (Path(__file__).parents[1] / 'drover').glob('disk_*.py'):
            tree = ast.parse(source.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.keyword) and node.arg == 'owner_action':
                    # The only interpolation in the migration hint is a machine name.
                    value = eval(compile(ast.Expression(node.value), str(source), 'eval'),
                                 {'path': Path('alpha.toml')})
                    if 'drover ' in value:
                        commands.append(shlex.split(value.split('drover ', 1)[1]))
        self.assertTrue(commands)
        for command in commands:
            with self.subTest(command=command), patch.object(argparse.ArgumentParser, 'parse_args', parse_only):
                with self.assertRaises(Parsed):
                    cli.main(command)


class FreshDecisions(DiskCase):
    def test_cim_snapshot_is_per_stage_and_drive_but_rechecks_reuse_it(self):
        request = dict(paths={}, state_dir=str(self.state), _inventory_id='decisions',
                       stage=2, drive=dict(path='H:/'))
        row = dict(pid=77, name='python.exe', same_user=True, cmd='', cwd=r'C:\elsewhere')
        with patch.object(placement, '_windows_processes', return_value={'77': ['python.exe', 20]}), \
                patch.object(placement, '_cim_rows', return_value=[row]) as query:
            placement.cleanup_processes(windows=True, request=request)
            placement.cleanup_processes(windows=True, request=request)
            self.assertEqual(query.call_count, 1)
            placement.cleanup_processes(windows=True, request=dict(request, stage=4))
            self.assertEqual(query.call_count, 2)
            placement.cleanup_processes(windows=True, request=dict(request, drive=dict(path='C:/')))
            self.assertEqual(query.call_count, 3)

    def test_r12_default_list_upgrades_to_include_remote_desktop_without_overwriting_edits(self):
        from drover import config
        old = [name for name in placement.default_process_ignore() if name != 'remoting_desktop.exe']
        new = config.migrate_disk_config(dict(disk_cleanup=dict(process_ignore=old)))
        self.assertIn('remoting_desktop.exe', new['disk_cleanup']['process_ignore'])
        edited = old + ['custom.exe']
        self.assertEqual(config.migrate_disk_config(dict(disk_cleanup=dict(process_ignore=edited)))
                         ['disk_cleanup']['process_ignore'], edited)

    @unittest.skipIf(os.name == 'nt', 'proc fixture')
    def test_posix_argv_retains_paths_containing_spaces(self):
        proc = self.root / 'proc'
        entry = proc / '123456789'
        entry.mkdir(parents=True)
        (entry / 'status').write_text('Name:\tforeign\nUid:\t0\n')
        (entry / 'cmdline').write_bytes(b'/usr/bin/tool\0/candidate space/input\0')
        with patch.object(os, 'getuid', return_value=1000):
            proof = placement.cleanup_processes(windows=False, proc_root=proc)
        self.assertTrue(proof.busy('/candidate space'))
        self.assertFalse(proof.busy('/unrelated'))


class RunnerPids(DiskCase):
    def test_fixture_accepts_windows_venv_launcher_between_runner_and_interpreter(self):
        import sys
        from tests.test_disk_fix7 import RUNNER_RECORD_PROBE
        # Runner 31156 launched stub 17700; interpreter 17701 is its child.
        for pid, cwd in ((31156, '/runner/cwd'), (17700, str(self.root))):
            disk_state.save_file(self.state, f'process-windows-{pid}.json',
                                 dict(identity=dict(pid=pid, side='windows', birth=str(pid)), cwd=cwd))
        with patch.object(sys, 'argv', ['probe', str(self.state), '31156']), \
                patch.object(os, 'getpid', return_value=17701), \
                patch.object(os, 'getppid', return_value=17700), \
                patch.object(Path, 'cwd', return_value=self.root):
            exec(RUNNER_RECORD_PROBE, {})

    def test_process_record_uses_explicit_spawned_pid_even_when_self_and_parent_differ(self):
        seen = []
        def birth(pid):
            seen.append(pid)
            return 20
        with patch.object(placement, 'IS_WINDOWS', True), patch.object(placement, '_windows_birth', side_effect=birth), \
                patch.object(os, 'getpid', return_value=31156), patch.object(os, 'getppid', return_value=1):
            with placement.cleanup_process_record(self.state, self.root, pid=17700):
                row = json.loads((self.state / 'disk' / 'process-windows-17700.json').read_text())
                self.assertEqual(row['identity']['pid'], 17700)
                self.assertEqual(row['cwd'], str(self.root))
        self.assertEqual(seen, [17700])
        self.assertFalse(list((self.state / 'disk').glob('process-*.json')))


class SafetyDecisions(DiskCase):
    def test_main_checkout_protection_uses_canonical_identity_in_both_stages(self):
        from drover.disk_worktrees import inspect
        from drover.disk_paths import output_evidence
        request = dict(now=self.now, repos=[], paths={})
        machine = dict(repo=r'\\?\H:\Repo', name='alpha')
        entry = dict(worktree='/mnt/h/repo', HEAD='unused')
        with patch.object(placement, 'cleanup_side', return_value='windows'), \
                patch.object(placement, 'worktree_side', return_value='windows'), \
                patch.object(placement, 'cleanup_contains', return_value=True):
            self.assertEqual(inspect(request, machine, entry)['reason_code'], 'main_checkout')
            with self.assertRaisesRegex(ValueError, 'not an eligible'):
                output_evidence(request, machine, entry)

    def test_nested_registrations_and_approved_selection_normalize_spelling(self):
        from drover import disk_worktrees
        request = dict(repos=[dict(repo=r'H:\repo')], apply=False, approved_path=r'\\?\H:\tree')
        with patch.object(disk_worktrees, 'entries', return_value=[dict(worktree='/mnt/h/tree/nested')]):
            self.assertTrue(disk_worktrees.overlaps_worktrees(request, r'H:\TREE', '/mnt/h/tree'))
        with patch.object(disk_worktrees, 'entries', return_value=[dict(worktree='/mnt/h/tree')]):
            self.assertFalse(disk_worktrees.overlaps_worktrees(request, r'H:\tree/out', r'\\?\H:\TREE'))
        row = dict(path='/mnt/h/tree', reason_code='orphan', action='keep')
        with patch.object(disk_worktrees, 'entries', return_value=[dict(worktree='/mnt/h/tree')]), \
                patch.object(disk_worktrees, 'inspect', return_value=row), \
                patch.object(disk_worktrees, 'quarantine', side_effect=lambda req, machine, value: value):
            result = disk_worktrees.settled(request)
        self.assertEqual(result['items'][0]['action'], 'candidate')

    def test_remove_revalidation_finds_registration_with_variant_spelling(self):
        from drover import disk_worktrees
        # Reach the inspect seam, then deliberately refuse removal. This proves
        # canonical selection without touching any filesystem or Git metadata.
        machine = dict(repo=r'H:\REPO')
        request = dict(repos=[machine], now=self.now)
        selected = dict(path=r'H:\tree', repo='/mnt/h/repo', reason_code='unsettled')
        listed = dict(worktree=r'\\?\H:\TREE')
        with patch.object(disk_worktrees, 'entries', side_effect=[[listed], [dict(worktree='/mnt/h/tree')]]), \
                patch.object(disk_worktrees, 'inspect', return_value=dict(action='keep', reason_code='dirty')) as check:
            result = disk_worktrees.remove(request, selected)
        self.assertTrue(check.called)
        self.assertEqual(result['reason_code'], 'revalidation_changed')

    def test_unlock_matches_extended_drive_spelling_and_preserves_owner_note(self):
        from types import SimpleNamespace
        for marker, calls in (('wsl', 2), ('owner note', 1)):
            listing = 'worktree /mnt/h/repo\0locked ' + marker + '\0\0'
            with patch.object(placement, 'IS_WINDOWS', False), \
                    patch.object(placement, 'git', return_value=SimpleNamespace(returncode=0, stdout=listing)) as git:
                placement.unlock_worktree('/tmp/repo', r'\\?\H:\Repo')
            self.assertEqual(git.call_count, calls)

    def test_repository_boundary_accepts_symlink_alias_but_raw_link_is_still_unsafe(self):
        from drover.disk_paths import repository_tree, safe_path
        real = self.root / 'real'
        real.mkdir()
        alias = self.root / 'alias'
        try:
            alias.symlink_to(real, target_is_directory=True)
        except OSError:
            self.skipTest('symlinks unavailable')
        child = self.file(real / 'out' / 'file')
        self.assertFalse(safe_path(alias / 'out'))
        self.assertFalse(repository_tree(child.parent, boundary=alias))

    def test_physical_scope_uses_canonical_mount_identity(self):
        request = dict(drive=dict(path='/physical/disk'))
        with patch.object(placement, 'cleanup_path_side', return_value='wsl'), \
                patch.object(placement, 'cleanup_side', return_value='wsl'), \
                patch.object(placement, 'physical_disk_path', side_effect=['/physical/disk/.', '/physical/disk']):
            self.assertTrue(placement.cleanup_contains(request, '/candidate'))


class RemainingSafety(DiskCase):
    def test_activity_exclusion_normalizes_equivalent_paths(self):
        from drover.disk_paths import facts
        target = self.file(self.root / 'generated' / 'payload', age=0)
        spelling = target.parent / 'sub' / '..' / target.name
        (target.parent / 'sub').mkdir()
        row = facts(target, self.now, ignore_activity=[spelling])
        self.assertEqual(row['age_s'], self.now)

    @unittest.skipIf(os.name == 'nt', 'Linux mount evidence')
    def test_mount_lookup_resolves_alias_before_selecting_physical_root(self):
        real = self.root / 'real'
        real.mkdir()
        alias = self.root / 'alias'
        alias.symlink_to(real, target_is_directory=True)
        mountinfo = f'1 0 1:1 / / rw - ext4 /dev/root rw\n2 1 2:2 / {real} rw - ext4 /dev/other rw\n'
        with patch.object(Path, 'read_text', return_value=mountinfo):
            self.assertEqual(placement.linux_mount(alias / 'child'), str(real))

    def test_helper_in_actual_git_worktree_outside_configured_trees_is_not_exempt(self):
        from tests.gitfix import Repo
        repo = Repo()
        self.addCleanup(repo.cleanup)
        tree = repo.root / 'outside-configured-trees'
        repo.git('worktree', 'add', '--detach', str(tree), 'main')
        exe = self.file(tree / 'csrss.exe')
        self.assertIn(str(tree).replace('\\', '/'), repo.git('worktree', 'list', '--porcelain').replace('\\', '/'))
        proof = placement.cleanup_processes(windows=True,
            request=dict(repos=[dict(repo=str(repo.repo), worktrees=str(repo.root / 'configured'))], paths={}),
            cim=lambda: [dict(pid=77, name='csrss.exe', same_user=True, cwd=None, cmd='', exe=str(exe))])
        self.assertTrue(proof.busy(r'H:\unrelated'))


class ReportingIntegration(DiskCase):
    def test_windows_stage_reports_sparse_diagnostics_without_running_commands(self):
        from drover.disk_side import serve
        rows = [dict(action='report', reason_code='wsl_vhdx', path=r'H:\ext4.vhdx', distro='Ubuntu',
                     sparse=False, allocated_bytes=590, apparent_bytes=1024, owner_action='owner action')]
        request = dict(stage=1, apply=False, now=self.now, repos=[], paths={}, state_dir=str(self.state),
                       drive=dict(path=str(self.root)))
        with patch.object(placement, 'cleanup_scope', side_effect=lambda value: dict(value, scope_errors=[])), \
                patch.object(placement, 'trim_argv', return_value=['never-execute']), \
                patch.object(placement, 'cleanup_vhdx_report', return_value=rows), \
                patch.object(subprocess, 'run', side_effect=AssertionError('report must not run a command')):
            answer = serve(request)
        self.assertEqual(answer['items'], rows)
        self.assertEqual(answer['errors'], [])

    def test_registry_paths_are_expanded_and_queried_through_windows_placement(self):
        import ntpath
        import sys
        from contextlib import nullcontext
        from types import SimpleNamespace
        registry = SimpleNamespace(HKEY_CURRENT_USER='HKCU', OpenKey=lambda *args: nullcontext(args[-1]),
            QueryInfoKey=lambda key: (2,), EnumKey=lambda key, i: str(i),
            QueryValueEx=lambda key, field: ({'DistributionName': 'Distro ' + key,
                                            'BasePath': '%LOCALAPPDATA%\\' + key}[field], 1),
            ExpandEnvironmentStrings=lambda value: value.replace('%LOCALAPPDATA%', r'C:\Users\fixture\AppData'))
        observed = []
        def read(path):
            observed.append(path)
            return dict(sparse=True, allocated_bytes=4, apparent_bytes=8)
        with patch.dict(sys.modules, winreg=registry), patch.object(placement, 'IS_WINDOWS', True), \
                patch.object(placement, '_vhdx_reading', side_effect=read):
            rows = placement.cleanup_vhdx_report()
        self.assertEqual({row['distro'] for row in rows}, {'Distro 0', 'Distro 1'})
        self.assertEqual(observed, [ntpath.join(r'C:\Users\fixture\AppData', key, 'ext4.vhdx') for key in ('0', '1')])

    def test_parent_report_keeps_peak_across_stages_and_placements(self):
        def call(side, request):
            return dict(items=[], errors=[], reclaimed_bytes=0,
                        peak_intermediate_bytes=4096 if request['stage'] == 4 and side == 'wsl' else 12)
        report = self.run_cleanup(real=False, handler=call)
        self.assertEqual(report['peak_intermediate_bytes'], 4096)
        persisted = json.loads((self.state / 'disk' / 'last-report.json').read_text())
        self.assertEqual(persisted['peak_intermediate_bytes'], 4096)


class ProcessReadFailures(DiskCase):
    def test_failed_post_query_identity_enumeration_still_names_conservative_pid(self):
        request = dict(state_dir=str(self.state), paths={}, _inventory_id='post-query')
        disk_state.save_file(self.state, 'processes-post-query.json', dict(rows=[], identities={}))
        with patch.object(placement, '_windows_processes', side_effect=[{'77': ['git.exe', 20]}, OSError('denied')]), \
                patch.object(placement, '_cim_rows', return_value=[dict(pid=77, name='git.exe', same_user=False, cmd='', cwd=None)]):
            proof = placement.cleanup_processes(windows=True, request=request)
        self.assertTrue(proof.busy(r'H:\candidate'))
        self.assertEqual([row['pid'] for row in proof.unreadable], [77])
        self.assertNotIn('owner_action', proof.unreadable[0])

    def test_changed_birth_during_targeted_read_discards_new_evidence(self):
        request = dict(state_dir=str(self.state), paths={}, _inventory_id='race')
        disk_state.save_file(self.state, 'processes-race.json', dict(rows=[], identities={}))
        with patch.object(placement, '_windows_processes', side_effect=[{'77': ['git.exe', 20]}, {'77': ['git.exe', 21]}]), \
                patch.object(placement, '_cim_rows', return_value=[dict(pid=77, name='git.exe', same_user=False, cmd='', cwd=None)]):
            proof = placement.cleanup_processes(windows=True, request=request)
        self.assertTrue(proof.busy(r'H:\candidate'))
        self.assertEqual([row['pid'] for row in proof.unreadable], [77])

    def test_confirmed_own_cwd_cannot_hide_unreadable_argv(self):
        disk_state.save_file(self.state, 'process-windows-77.json', dict(
            identity=dict(pid=77, side='windows', birth='20'), cwd=r'C:\known'))
        with patch.object(placement, 'cleanup_identity', return_value=dict(pid=77, side='windows', birth='20')):
            proof = placement.cleanup_processes(windows=True, request=dict(state_dir=str(self.state)), cim=lambda: [dict(
                pid=77, name='python.exe', same_user=True, cwd=None, cmd=None, exe=r'C:\Python\python.exe')])
        self.assertTrue(proof.busy(r'H:\candidate'))
        self.assertNotIn('owner_action', proof.unreadable[0])

    def test_quoted_windows_argv_normalizes_option_paths_with_spaces(self):
        proof = placement.cleanup_processes(windows=True, cim=lambda: [dict(
            pid=77, name='foreign.exe', same_user=False, cwd=None,
            cmd='tool --output="\\\\?\\H:\\repo space\\out"')])
        self.assertTrue(proof.busy('/mnt/h/REPO SPACE'))
        self.assertFalse(proof.busy(r'H:\unrelated'))


class CrossPlacementPaths(unittest.TestCase):
    def test_windows_coordinator_keeps_linux_path_case_distinct(self):
        with patch.object(placement, 'IS_WINDOWS', True):
            self.assertNotEqual(placement.cleanup_parts('/home/owner/Case'),
                                placement.cleanup_parts('/home/owner/case'))
            self.assertEqual(placement.cleanup_parts('/mnt/h/Case'), placement.cleanup_parts(r'\\?\H:\case'))


class LiteralPathNames(unittest.TestCase):
    def test_canonical_identity_preserves_literal_apostrophes_in_paths(self):
        proof = placement.cleanup_processes(windows=True, cim=lambda: [dict(
            pid=77, name='foreign.exe', same_user=False, cwd="H:\\repo'\\child", cmd='')])
        self.assertTrue(proof.busy("/mnt/h/repo'"))
        self.assertFalse(proof.busy('/mnt/h/repo'))


class ReadRootSafety(unittest.TestCase):
    def test_native_windows_read_root_still_resolves_directory_links(self):
        from drover import task_access
        from types import SimpleNamespace
        def path(value):
            resolved = r'H:\work' if str(value).casefold() == r'h:\alias' else str(value)
            return SimpleNamespace(resolve=lambda: resolved)
        with patch.object(placement, 'IS_WINDOWS', True), patch.object(placement, 'Path', side_effect=path):
            with self.assertRaisesRegex(ValueError, 'read-only checkout'):
                task_access.check_read_roots(r'H:\work', [r'H:\alias'])
