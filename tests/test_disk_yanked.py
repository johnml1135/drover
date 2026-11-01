"""A yanked worktree is cleaned by the settled rule: builds purge when idle, the tree goes when committed, quiet and old."""
from tests import test_disk_worktrees as base
from tests.tiers import integration

DAY = 86400


class Yanked(base.WorktreeCase):
    def yank(self, name, *, idle=4 * DAY, lease=True):
        """A committed worktree owned by a yanked task whose run has stopped, last touched `idle` seconds ago."""
        from drover import ownership
        wt = self.worktree(name, lease=lease)
        self.r.commit(name + '.txt', name, cwd=wt)
        ownership.tag(str(wt), 'alpha', 'drover:' + name, self.now + 86400, now=self.now)
        self.age(wt, idle)
        task = dict(id=name, state='yanked', terminal=True, live=False, worktree=str(wt), owner='drover:' + name)
        self.tasks = dict(at=self.now, active_worktrees=[], tasks=[*self.tasks.get('tasks', []), task])
        return wt

    def sweep(self):
        self.cleanup()
        return self.cleanup(apply=True)

    @integration
    def test_builds_of_a_yanked_worktree_purge_on_the_idle_rule(self):
        wt = self.yank('builds', idle=3 * 3600)
        fresh = self.yank('fresh', idle=0)
        outputs = {}
        for path in (wt, fresh):
            out = path / 'generated' / 'result'
            out.parent.mkdir()
            out.write_text('rebuildable')
            outputs[path] = out
        self.age(wt / 'generated', 3 * 3600)
        self.age(fresh / 'generated', 0)
        self.sweep()
        self.assertFalse(outputs[wt].exists())
        self.assertTrue(outputs[fresh].exists(), 'inside the idle window')

    @integration
    def test_committed_idle_three_days_removes_worktree_keeps_branch(self):
        wt = self.yank('old', idle=4 * DAY)
        before = self.branches()
        self.sweep()
        self.assertFalse(wt.exists())
        self.assertEqual(self.branches(), before)

    @integration
    def test_dirty_yanked_worktree_is_kept(self):
        wt = self.yank('dirty')
        (wt / 'dirty.txt').write_text('uncommitted edit')
        (wt / 'scratch.txt').write_text('untracked')
        self.age(wt, 4 * DAY)
        self.sweep()
        self.assertEqual((wt / 'dirty.txt').read_text(), 'uncommitted edit')
        self.assertEqual((wt / 'scratch.txt').read_text(), 'untracked')

    @integration
    def test_unpushed_commits_alone_do_not_block_removal_because_branch_is_kept(self):
        wt = self.yank('unpushed')
        branch = self.r.git('symbolic-ref', '--short', 'HEAD', cwd=wt)
        head = self.r.git('rev-parse', 'HEAD', cwd=wt)
        self.assertNotEqual(self.r.git('rev-parse', 'origin/main'), head)
        self.sweep()
        self.assertFalse(wt.exists())
        self.assertEqual(self.r.git('rev-parse', 'refs/heads/' + branch), head)

    @integration
    def test_running_process_keeps_it(self):
        wt = self.yank('running')
        self.busy_paths.add(str(wt))
        self.sweep()
        self.assertTrue(wt.exists())

    @integration
    def test_idle_two_days_is_kept(self):
        wt = self.yank('recent', idle=2 * DAY)
        self.sweep()
        self.assertTrue(wt.exists())

    @integration
    def test_unmerged_yanked_worktree_needs_no_approval(self):
        wt = self.yank('unmerged')
        merged = self.r.git('branch', '--merged', 'origin/main')
        self.assertNotIn(self.r.git('symbolic-ref', '--short', 'HEAD', cwd=wt), merged)
        self.cleanup()
        self.assertTrue(wt.exists(), 'a report never removes')
        self.cleanup(apply=True)
        self.assertFalse(wt.exists())
