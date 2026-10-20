"""Only committed output declarations authorize stage-five deletion."""
from pathlib import Path

from tests.test_disk_worktrees import WorktreeCase


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
        self.r.git('add', '-f', 'generated/result', cwd=paths['tracked'])
        self.r.git('commit', '-q', '-m', 'tracked generated data', cwd=paths['tracked'])
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'keep').write_text('link target')
        (paths['linked'] / 'generated' / 'result').unlink()
        (paths['linked'] / 'generated' / 'result').symlink_to(outside, target_is_directory=True)
        self.busy_paths.add(str(paths['busy']))
        # Dirty declarations cannot widen the committed cleanup scope.
        policy = paths['uncommitted-policy'] / '.drover/config.toml'
        policy.write_text('schema_version=1\n[cleanup]\nbuild_outputs=["data"]\n')
        uncommitted = paths['uncommitted-policy'] / 'data' / 'keep'
        uncommitted.parent.mkdir()
        uncommitted.write_text('not authorized')
        self.cleanup()
        self.cleanup(apply=True)
        for name in ('dirty', 'tracked', 'linked', 'busy'):
            self.assertTrue((paths[name] / 'generated' / 'result').exists(), name)
        self.assertEqual(uncommitted.read_text(), 'not authorized')
        self.assertEqual((outside / 'keep').read_text(), 'link target')
        self.assertTrue((paths['tracked'] / '.git').is_file())
