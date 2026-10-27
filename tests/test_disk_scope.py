"""Configured repo/path scope and owning-side isolation through real handlers."""
import copy
import os
from unittest import mock

from drover import placement
from tests.disk_memory import MemoryWorktreeCase as WorktreeCase


class Scope(WorktreeCase):
    def test_configured_repos_and_extra_paths_this_side_other_side_untouched(self):
        from drover import disk_cleanup, disk_side, ownership
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
                    requests.append((target, copy.deepcopy(request)))
                    if target != side:
                        return {'items': [], 'errors': [], 'reclaimed_bytes': 0}
                    if request['stage'] == 1:
                        return {'items': [], 'errors': [], 'reclaimed_bytes': 0}
                    request = dict(request, tasks=self.tasks)
                    return disk_side.serve(request)
                runtime = disk_cleanup.CleanupRuntime(base_runtime.now, base_runtime.read,
                                                       call, base_runtime.toast)
                # Simulated owning side and path mapping; Git always uses the real native platform.
                with mock.patch.object(placement, 'cleanup_side', return_value=side), \
                     mock.patch.object(placement, 'cleanup_path_side', return_value=side), \
                     mock.patch.object(placement, 'cleanup_runtime', return_value=runtime), \
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
        from drover import disk_cleanup, disk_side
        runtime = self.runtime(real=True)
        def call(side, request):
            if side != self.side or request['stage'] == 1:
                return {'items': [], 'errors': [], 'reclaimed_bytes': 0}
            return disk_side.serve(request)
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
