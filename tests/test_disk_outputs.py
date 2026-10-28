"""Only committed output declarations authorize stage-five deletion."""
from pathlib import Path

from tests.disk_memory import MemoryWorktreeCase as WorktreeCase
from tests.test_disk_worktrees import WorktreeCase as GitWorktreeCase
from tests.tiers import integration


class Outputs(WorktreeCase):
    def test_only_declared_ignored_untracked_idle_outputs(self):
        wt = self.worktree('unmerged outputs')
        self.r.commit('pending.txt', cwd=wt)
        output = wt / 'generated' / 'result'
        output.parent.mkdir()
        output.write_text('rebuildable')
        undeclared = wt / 'data' / 'precious'
        undeclared.parent.mkdir()
        undeclared.write_text('preserve')
        branches = self.branches()
        self.cleanup()
        self.assertTrue(output.exists())
        self.cleanup(apply=True, mode='daily')
        self.assertTrue(output.exists(), 'daily maintenance stops before stage five')
        result = self.cleanup(apply=True)
        self.assertFalse(output.exists())
        self.assertTrue(wt.is_dir())
        self.assertEqual(undeclared.read_text(), 'preserve')
        self.assertEqual(self.branches(), branches)
        self.assertTrue(any(i['stage'] == 5 for i in result['items']))

    def test_dirty_tracked_links_and_undeclared_survive(self):
        paths = {}
        for name in ('dirty', 'tracked', 'linked', 'busy', 'uncommitted-policy'):
            wt = self.worktree(name)
            self.r.commit('pending.txt', cwd=wt)
            paths[name] = wt
            output = wt / 'generated' / 'result'
            output.parent.mkdir()
            output.write_text(name)
        (paths['dirty'] / 'pending.txt').write_text('uncommitted edit')
        self.r.track(paths['tracked'], 'generated/result')
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'keep').write_text('link target')
        (paths['linked'] / 'generated' / 'result').unlink()
        (paths['linked'] / 'generated' / 'result').symlink_to(outside, target_is_directory=True)
        self.busy_paths.add(str(paths['busy']))
        # Dirty declarations cannot widen the committed cleanup scope.
        policy = paths['uncommitted-policy'] / '.drover/config.toml'
        policy.write_text('schema_version=1\n[cleanup]\ndisposable=["data"]\n')
        uncommitted = paths['uncommitted-policy'] / 'data' / 'keep'
        uncommitted.parent.mkdir()
        uncommitted.write_text('not authorized')
        self.cleanup()
        self.cleanup(apply=True)
        self.assertFalse((paths['dirty'] / 'generated' / 'result').exists(), 'R8 permits disposable files in dirty worktrees')
        self.assertEqual((paths['dirty'] / 'pending.txt').read_text(), 'uncommitted edit')
        for name in ('tracked', 'linked', 'busy'):
            self.assertTrue((paths[name] / 'generated' / 'result').exists(), name)
        self.assertEqual(uncommitted.read_text(), 'not authorized')
        self.assertEqual((outside / 'keep').read_text(), 'link target')
        self.assertTrue((paths['tracked'] / '.git').is_file())

    def test_output_rechecks_marker_after_final_status(self):
        from unittest import mock
        from drover import disk_worktrees
        wt = self.worktree('output-race')
        self.r.commit('pending.txt', cwd=wt)
        output = wt / 'generated' / 'result'
        output.parent.mkdir()
        output.write_text('preserve on ownership change')
        self.cleanup()
        real_status = disk_worktrees.status
        checks = []
        def status(repo, **kw):
            if str(repo) == str(wt):
                checks.append(kw)
                if len(checks) == 3:
                    self.r.lock(wt, self.other_side)
            return real_status(repo, **kw)
        with mock.patch.object(disk_worktrees, 'status', side_effect=status):
            self.cleanup(apply=True)
        self.assertGreaterEqual(len(checks), 3)
        self.assertEqual(output.read_text(), 'preserve on ownership change')
        self.assertIn('locked ' + self.other_side, self.registrations())

    def test_nested_registered_worktree_survives_declared_output_cleanup(self):
        wt = self.worktree('outer output')
        self.r.commit('unmerged.txt', cwd=wt)
        inner = wt / 'generated' / 'foreign-inner'
        self.r.add(inner, branch='foreign-inner', lock=self.other_side)
        (inner / '.git').chmod(0o666)
        (inner / '.git').unlink()
        keep = inner / 'keep'
        keep.write_text('foreign owner data')
        self.cleanup()
        self.cleanup(apply=True)
        self.assertEqual(keep.read_text(), 'foreign owner data')
        self.assertIn(str(inner), self.registrations())
        self.assertIn('locked ' + self.other_side, self.registrations())

    def test_changed_default_branch_output_scope_requires_another_report(self):
        from drover import config
        wt = self.worktree('changed output scope')
        self.r.commit('unmerged.txt', cwd=wt)
        data = wt / 'data' / 'keep'
        data.parent.mkdir()
        data.write_text('not in original report')
        self.cleanup()
        self.r.commit('.drover/config.toml', config.dumps({
            'schema_version': 1, 'cleanup': {'disposable': ['data']}}))
        with self.assertRaisesRegex(ValueError, 'dry report'):
            self.cleanup(apply=True)
        self.assertEqual(data.read_text(), 'not in original report')
        self.cleanup()
        self.cleanup(apply=True)
        self.assertFalse(data.exists())

    def test_r8_pressure_age_dirty_and_globs(self):
        from drover import config
        wt = self.worktree('disposable pressure')
        self.r.commit('.drover/config.toml', config.dumps({
            'schema_version': 1, 'cleanup': {'disposable': ['**/obj', '*.log']}}))
        source = wt / 'source.txt'
        self.r.commit('source.txt', 'committed', cwd=wt)
        source.write_text('owner edit')
        log = wt / 'recent.log'
        log.write_text('declared untracked')
        nested = wt / 'src' / 'obj' / 'result'
        nested.parent.mkdir(parents=True)
        nested.write_text('regenerated')
        tracked = wt / 'tracked.log'
        self.r.commit('tracked.log', 'tracked', cwd=wt)
        tracked.write_text('tracked edit')
        keep = wt / 'precious'
        keep.write_text('undeclared')
        self.free = 120
        self.age(wt, 2 * 86400)
        self.cleanup()
        self.cleanup(apply=True)
        self.assertTrue(log.exists())
        self.age(wt, 3 * 86400)
        self.cleanup(apply=True)
        self.assertFalse(log.exists())
        self.assertFalse(nested.exists())
        self.assertEqual(source.read_text(), 'owner edit')
        self.assertEqual(tracked.read_text(), 'tracked edit')
        self.assertEqual(keep.read_text(), 'undeclared')
        log.write_text('young under hold')
        self.age(wt, 0)
        self.free = 40
        self.busy_paths.add(str(wt))
        self.cleanup(apply=True)
        self.assertTrue(log.exists())
        self.busy_paths.clear()
        self.cleanup(apply=True)
        self.assertFalse(log.exists())

    def test_warn_removes_multiple_disposable_files_without_resetting_own_idle_age(self):
        import time
        self.now = time.time()
        wt = self.worktree('real clock idle')
        self.r.commit('unmerged.txt', cwd=wt)
        paths = [wt / 'generated' / name for name in ('first', 'second')]
        for path in paths:
            path.parent.mkdir(exist_ok=True)
            path.write_text('regenerated')
        self.age(wt, 4 * 86400)
        self.free = 120
        self.cleanup()
        self.cleanup(apply=True)
        self.assertTrue(all(not path.exists() for path in paths))


@integration
class OutputsContracts(GitWorktreeCase):
    def test_output_rechecks_marker_after_final_status(self):
        from unittest import mock
        from drover import placement
        wt = self.worktree('output-race')
        self.r.commit('pending.txt', cwd=wt)
        output = wt / 'generated' / 'result'
        output.parent.mkdir()
        output.write_text('preserve on ownership change')
        self.cleanup()
        real_git = placement.git
        checks = []
        def git(repo, *args, **kw):
            if str(repo) == str(wt) and 'status' in args:
                checks.append(args)
                if len(checks) == 3:
                    self.r.git('worktree', 'unlock', wt)
                    self.r.git('worktree', 'lock', '--reason', self.other_side, wt)
            return real_git(repo, *args, **kw)
        with mock.patch.object(placement, 'git', side_effect=git) as _placement_patch_1, mock.patch('drover.placement_git.git', _placement_patch_1):
            self.cleanup(apply=True)
        self.assertGreaterEqual(len(checks), 3)
        self.assertEqual(output.read_text(), 'preserve on ownership change')
        self.assertIn('locked ' + self.other_side, self.registrations())

    def test_changed_default_branch_output_scope_requires_another_report(self):
        from drover import config
        wt = self.worktree('changed output scope')
        self.r.commit('unmerged.txt', cwd=wt)
        data = wt / 'data' / 'keep'
        data.parent.mkdir()
        data.write_text('not in original report')
        self.cleanup()
        self.r.commit('.drover/config.toml', config.dumps({
            'schema_version': 1, 'cleanup': {'disposable': ['data']}}))
        with self.assertRaisesRegex(ValueError, 'dry report'):
            self.cleanup(apply=True)
        self.assertEqual(data.read_text(), 'not in original report')
        self.cleanup()
        self.cleanup(apply=True)
        self.assertFalse(data.exists())
