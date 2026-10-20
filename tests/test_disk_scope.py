"""Configured repo/path scope and owning-side isolation through real handlers."""
import copy
import os
from unittest import mock

from drover import placement
from tests.gitfix import Repo
from tests.test_disk_worktrees import WorktreeCase


class Scope(WorktreeCase):
    def test_configured_repos_and_extra_paths_this_side_other_side_untouched(self):
        from drover import disk_cleanup, disk_side, ownership
        for side, other in (('wsl', 'windows'), ('windows', 'wsl')):
            with self.subTest(side=side):
                extra = Repo()
                self.addCleanup(extra.cleanup)
                trees = extra.root / 'beta.worktrees'
                self.register(extra, 'beta-' + side, trees)
                own = self.worktree('own-' + side, lock=side)
                self.r.commit('own-' + side, cwd=own)
                self.merge(own)
                foreign = self.worktree('foreign-' + side, lock=other)
                self.r.commit('foreign-' + side, cwd=foreign)
                self.merge(foreign)
                second = trees / ('own-' + side)
                extra.git('worktree', 'add', '-q', '-b', 'work-' + side, second, 'origin/main')
                extra.git('worktree', 'lock', '--reason', side, second)
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
                before_lock = self.r.git('worktree', 'list', '--porcelain', '-z')
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
                real_metadata = placement.worktree_metadata
                def native_metadata(*args, **kwargs):
                    with mock.patch.object(placement, 'IS_WINDOWS', False):
                        return real_metadata(*args, **kwargs)
                real_git = placement.git
                def native_git(repo, *args, **kwargs):
                    with mock.patch.object(placement, 'IS_WINDOWS', False):
                        return real_git(repo, *args, **kwargs)
                real_side = placement.worktree_side
                def owning_side(path, lock=None):
                    if lock in ('windows', 'wsl'):
                        return lock
                    return real_side(path, lock)
                # The sole platform seam supplies the simulated current side; no OS calls.
                with mock.patch.object(placement, 'IS_WINDOWS', side == 'windows'), \
                     mock.patch.object(placement, 'git', side_effect=native_git), \
                     mock.patch.object(placement, 'worktree_metadata', side_effect=native_metadata), \
                     mock.patch.object(placement, 'worktree_side', side_effect=owning_side), \
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
