"""Configured repo/path scope and owning-side isolation through real handlers."""
import copy
import os
import tempfile
import unittest
from unittest import mock

from drover import disk_runtime, placement, placement_process, placement_proof
from tests.disk_memory import MemoryWorktreeCase as WorktreeCase


class Scope(WorktreeCase):
    def test_configured_repos_and_extra_paths_this_side_other_side_untouched(self):
        from drover import disk_cleanup, ownership
        from tests.disk_fixture import served
        for side, other in (('wsl', 'windows'), ('windows', 'wsl')):
            with self.subTest(side=side):
                extra = self.new_repo('beta-' + side)
                trees = extra.root / 'beta.worktrees'
                self.register(extra, 'beta-' + side, trees)
                own = self.worktree('own-' + side, lock=side)
                self.r.commit('own-' + side, cwd=own)
                self.merge(own)
                foreign = self.worktree('foreign-' + side, lock=other)
                self.r.commit('foreign-' + side, cwd=foreign)
                self.merge(foreign)
                second = trees / ('own-' + side)
                extra.add(second, branch='work-' + side, lock=side)
                extra.commit('unmerged.txt', 'pending change', cwd=second)
                ownership.tag(str(second), 'beta-' + side, 'session:beta',
                              self.now + 86400, now=self.now)
                temp = self.temp_root / ('old-' + side)
                temp.write_text('old declared scratch')
                os.utime(temp, (self.now - 8 * 86400,) * 2)
                report = self.report_root / ('owner-data-' + side)
                report.write_text('report only')
                sibling = self.root / ('undeclared-' + side)
                sibling.mkdir()
                (sibling / 'keep').write_text('outside scope')
                self.cfg['disk_cleanup']['temp_roots'] = [str(self.temp_root)]
                self.cfg['disk_cleanup']['report_roots'] = [str(self.report_root)]
                self.tasks = {'at': self.now, 'tasks': [], 'active_worktrees': []}
                before_lock = self.registrations()
                requests = []
                base_runtime = self.runtime(real=True)
                def call(target, request):
                    request = request.to_wire()
                    requests.append((target, copy.deepcopy(request)))
                    if target != side:
                        return {'items': [], 'errors': [], 'reclaimed_bytes': 0}
                    if request['stage'] == 1:
                        return {'items': [], 'errors': [], 'reclaimed_bytes': 0}
                    request = dict(request, tasks=self.tasks)
                    return served(request)
                runtime = disk_cleanup.CleanupRuntime(base_runtime.now, base_runtime.read,
                                                       call, base_runtime.toast)
                # Simulated owning side and path mapping; Git always uses the real native platform.
                with mock.patch.object(placement, 'cleanup_side', return_value=side) as _placement_patch_1, mock.patch('drover.placement_process.cleanup_side', _placement_patch_1), \
                     mock.patch.object(placement, 'cleanup_path_side', return_value=side), \
                     mock.patch.object(disk_runtime, 'cleanup_runtime', return_value=runtime), \
                     mock.patch.object(placement, 'cleanup_processes',
                                       return_value=type('Idle', (), {'busy': lambda _, p: False})()), \
                     mock.patch('tempfile.gettempdir', return_value=str(self.temp_root)), \
                     mock.patch.dict(os.environ, {'TMPDIR': str(self.temp_root),
                                                  'TMP': str(self.temp_root),
                                                  'TEMP': str(self.temp_root)}), \
                     mock.patch.object(placement, 'physical_disk_path',
                                       return_value=self.cfg['trackers'][0]['path']):
                    disk_cleanup.run(self.cfg, runtime=runtime)
                    result = disk_cleanup.run(self.cfg, apply=True, runtime=runtime)
                self.assertFalse(own.exists())
                self.assertFalse(temp.exists())
                self.assertTrue(second.exists(), 'unmerged leased repo is visited but preserved')
                self.assertTrue(foreign.exists())
                self.assertTrue(report.exists())
                self.assertEqual((sibling / 'keep').read_text(), 'outside scope')
                listed = self.registrations()
                self.assertIn(str(foreign), listed)
                self.assertIn('locked ' + other, listed)
                self.reported(result, report)
                visited = {r['name'] for target, req in requests if target == side for r in req['repos']}
                self.assertTrue({'alpha', 'beta-' + side} <= visited)
                self.assertTrue(requests)
                if side == 'windows':
                    applied = [(target, req) for target, req in requests if req['apply']]
                    self.assertEqual([req['stage'] for _, req in applied],
                                     sorted(req['stage'] for _, req in applied))
                    for stage in range(1, 7):
                        targets = [target for target, req in applied if req['stage'] == stage]
                        self.assertEqual(len(targets), 2, (stage, targets))
                        self.assertEqual(set(targets), {'windows', 'wsl'})
                self.assertTrue(all(req['drive']['path'] == self.cfg['trackers'][0]['path']
                                    for _, req in requests))

    def test_worktree_is_charged_to_its_own_physical_drive(self):
        wt = self.worktree('other drive')
        self.r.commit('settled.txt', cwd=wt)
        self.merge(wt)
        other = self.root / 'other drive'
        other.mkdir()
        tracker = dict(self.cfg['trackers'][0], name='other', path=str(other))
        self.cfg['trackers'].append(tracker)
        from drover import disk_cleanup
        from tests.disk_fixture import served
        runtime = self.runtime(real=True)
        def call(side, request):
            request = request.to_wire()
            if side != self.side or request['stage'] == 1:
                return {'items': [], 'errors': [], 'reclaimed_bytes': 0}
            return served(request)
        runtime = disk_cleanup.CleanupRuntime(runtime.now, lambda path: 180 if path == '/simulated/other' else 80,
                                               call, runtime.toast)
        def physical(path):
            from pathlib import Path
            if str(path) == '/simulated/other':
                return '/simulated/other'
            path = Path(path)
            # Distinct physical drives, even when both fixtures live on Windows C:.
            return '/simulated/other' if path == wt or wt in path.parents or path == other else '/simulated/primary'
        idle = type('Idle', (), {'busy': lambda _, p: False})()
        with mock.patch.object(placement, 'physical_disk_path', side_effect=physical), \
             mock.patch.object(placement, 'cleanup_processes', return_value=idle), \
             mock.patch('tempfile.gettempdir', return_value=str(self.temp_root)):
            disk_cleanup.run(self.cfg, runtime=runtime)
            disk_cleanup.run(self.cfg, apply=True, runtime=runtime)
            self.assertTrue(wt.exists(), 'healthy unrelated physical drive must survive')
            self.cfg['trackers'] = [tracker]
            # The repo remains configured on the first drive. Its worktree is visited on its own low drive.
            low = disk_cleanup.CleanupRuntime(runtime.now, lambda path: 80, call, runtime.toast)
            disk_cleanup.run(self.cfg, runtime=low)
            disk_cleanup.run(self.cfg, apply=True, runtime=low)
        self.assertFalse(wt.exists(), 'worktree-only drive still includes its configured repo')


class PlainArguments(unittest.TestCase):
    """Placement takes plain arguments: the drive's path, the policy and the proof's settings, never a request dict."""
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.state = folder.name

    def test_cleanup_scope_takes_plain_arguments_and_returns_errors(self):
        hosts = {'H:/': 'H', 'H:/temp': 'H', 'H:/report': 'H', 'C:/temp': 'C', 'H:/cache': 'H', 'C:/cache': 'C'}
        paths = dict(temp_roots=['H:/temp', 'C:/temp', 'X:/unknown', 'H:/temp'], report_roots=['H:/report'],
                     caches=[dict(path='H:/cache', placement='windows'), dict(cwd='C:/cache', argv=['x'], placement='windows'),
                             dict(path='H:/cache', placement='wsl')], worker_timeout_s=60)
        before = copy.deepcopy(paths)
        with mock.patch.object(placement, 'physical_disk_path', side_effect=hosts.get), \
                mock.patch.object(placement, 'cleanup_side', return_value='windows'), \
                mock.patch.object(placement, 'cleanup_path_side', return_value='windows'), \
                mock.patch.object(placement, 'native', side_effect=str):
            scoped, errors = placement.cleanup_scope('H:/', paths)
            self.assertTrue(placement.cleanup_contains('H:/', 'H:/temp'))
            self.assertFalse(placement.cleanup_contains('H:/', 'C:/temp'))
            _, unknown = placement.cleanup_scope('X:/unknown', dict(temp_roots=[]))
        self.assertEqual(paths, before)
        self.assertEqual(scoped, dict(before, temp_roots=['H:/temp'], report_roots=['H:/report'],
                                      caches=[dict(path='H:/cache', placement='windows')]))
        self.assertEqual(errors, [dict(path='X:/unknown', reason_code='physical_disk_unknown')])
        self.assertEqual(unknown, [dict(path='X:/unknown', reason_code='physical_disk_unknown')])

    def test_cleanup_processes_cache_key_keeps_stage_and_drive(self):
        """Two stages, or two drives, of one inventory do not share a snapshot; rechecks within one reuse it."""
        settings = dict(paths={}, state_dir=self.state, inventory_id='decisions', stage=2, drive=dict(path='H:/'))
        row = dict(pid=77, name='python.exe', same_user=True, cmd='', cwd=r'C:\elsewhere')
        with mock.patch.object(placement_proof, '_windows_processes', return_value={'77': ['python.exe', 20]}), \
                mock.patch.object(placement_process, '_cim_rows', return_value=[row]) as query, \
                mock.patch.object(placement_proof, '_cim_rows', query):
            placement.cleanup_processes(windows=True, **settings)
            placement.cleanup_processes(windows=True, **settings)
            self.assertEqual(query.call_count, 1)
            placement.cleanup_processes(windows=True, **dict(settings, stage=4))
            self.assertEqual(query.call_count, 2)
            placement.cleanup_processes(windows=True, **dict(settings, drive=dict(path='C:/')))
            self.assertEqual(query.call_count, 3)

    def blocked(self, **settings):
        """An ignored image whose executable cannot be read, as Windows shows a non-elevated hub csrss.exe."""
        rows = [dict(pid=600, name='csrss.exe', same_user=True, cwd=None, cmd='', session_id=1, exe=None)]
        with mock.patch.object(placement_proof, '_windows_processes', return_value={'600': ['csrss.exe', 3]}):
            return placement.cleanup_processes(windows=True, cim=lambda: rows,
                                               paths=dict(process_ignore=['csrss.exe']), **settings)

    def test_cleanup_processes_budget_and_clear_ignored_change_what_counts(self):
        self.assertTrue(self.blocked().unknown)
        for settings in (dict(budget=True), dict(clear_ignored=True)):
            with self.subTest(**settings):
                proof = self.blocked(**settings)
                self.assertFalse(proof.unknown)
                self.assertEqual(proof.out_of_scope, 1)

    def test_cleanup_processes_returns_diagnostics_and_mutates_nothing(self):
        paths = dict(process_ignore=['csrss.exe'])
        before = copy.deepcopy(paths)
        proof = self.blocked(state_dir=self.state, inventory_id='run', stage=4, drive=dict(path='H:/'))
        self.assertEqual(paths, before)
        self.assertEqual([(row['pid'], row['name']) for row in proof.unreadable], [(600, 'csrss.exe')])
        self.assertIsNone(proof.query_failure)
        def fail():
            raise OSError('no cim')
        failed = placement.cleanup_processes(windows=True, cim=fail, paths=paths)
        self.assertEqual(failed.query_failure, dict(reason_code='process_visibility_unavailable', error='no cim'))
        self.assertEqual(paths, before)
        self.assertEqual(os.listdir(self.state), [])

    def test_cleanup_windows_proof_takes_plain_arguments(self):
        rows = [dict(pid=600, name='csrss.exe', same_user=True, cwd=None, cmd='', session_id=1, exe=None)]
        answer = dict(rows=rows, identities={'600': ['csrss.exe', 3]})
        with mock.patch.object(placement_proof, 'IS_WINDOWS', True), \
                mock.patch.object(placement_proof, '_windows_processes', return_value={'600': ['csrss.exe', 3]}), \
                mock.patch.object(placement_proof, '_cleanup_cim', return_value=answer) as cim:
            proof = placement.cleanup_windows_proof(paths=dict(process_ignore=['csrss.exe'], process_query_timeout_s=7),
                                                    state_dir=self.state, budget=True)
        self.assertFalse(proof.unknown)
        self.assertEqual(proof.out_of_scope, 1)
        self.assertEqual(cim.call_args.kwargs['timeout_s'], 7)
        self.assertEqual(cim.call_args.kwargs['state_dir'], self.state)
        with mock.patch.object(placement_proof, 'IS_WINDOWS', False):
            self.assertIsNone(placement.cleanup_windows_proof(paths={}, state_dir=self.state, budget=True))
