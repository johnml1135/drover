import unittest
from pathlib import Path

from drover import placement
from tests.gitfix import Repo, sh


class GardenCase(unittest.TestCase):
    def setUp(self):
        self.r = Repo()
        self.addCleanup(self.r.cleanup)
        (self.r.root / 'noproc').mkdir()
        self.proc = placement.cleanup_processes(proc_root=self.r.root / 'noproc', windows=False)
        self.state = self.r.root / 'state'
        import os
        self._env = os.environ.get('DROVER_STATE_DIR')
        os.environ['DROVER_STATE_DIR'] = str(self.state)
        self.addCleanup(lambda: os.environ.pop('DROVER_STATE_DIR') if self._env is None
                        else os.environ.__setitem__('DROVER_STATE_DIR', self._env))

    def worktree(self, name, lock=None):
        path = self.r.root / 'wt' / name
        self.r.git('worktree', 'add', '-q', '-b', name, path, 'origin/main')
        if lock:
            self.r.git('worktree', 'lock', '--reason', lock, path)
        return path

    def propose(self, **kw):
        from drover import garden  # P1–P4: fixture restored through disk_cleanup.run in T16–T18.
        return garden.propose(self.r.repo, proc=self.proc, min_age_days=0, **kw)

    def names(self, items):
        return sorted(Path(i['path']).name for i in items)

    def land(self, branch):
        self.r.git('push', '-q', 'origin', f'{branch}:main')
        self.r.git('fetch', '-q', 'origin')


class ClassifyTests(GardenCase):

    def test_merged_by_ancestry_is_removable(self):
        wt = self.worktree('m')
        self.r.commit('m.txt', cwd=wt)
        self.land('m')
        self.assertEqual(self.names(self.propose()['removable']), ['m'])

    def test_rebased_landing_is_patch_equivalent_via_cherry(self):
        wt = self.worktree('rb')
        self.r.commit('rb.txt', 'rb\n', cwd=wt)
        self.r.commit('other.txt', 'o\n')
        self.r.git('push', '-q', 'origin', 'HEAD:main')
        sh('git', '-C', self.r.repo, 'cherry-pick', 'rb', check=False)
        self.r.git('push', '-q', 'origin', 'HEAD:main')
        self.r.git('fetch', '-q', 'origin')
        self.assertEqual(self.r.git('cherry', 'origin/main', 'rb').count('+'), 0)
        self.assertEqual(self.names(self.propose()['removable']), ['rb'])

    def test_squash_merge_detected(self):
        wt = self.worktree('sq')
        self.r.commit('s1.txt', cwd=wt)
        self.r.commit('s2.txt', cwd=wt)
        self.r.git('merge', '--squash', 'sq')
        self.r.git('commit', '-q', '-m', 'squash')
        self.r.git('push', '-q', 'origin', 'HEAD:main')
        self.r.git('fetch', '-q', 'origin')
        self.assertEqual(self.names(self.propose()['removable']), ['sq'])


    def test_busy_note_locked_and_other_side_are_kept(self):
        busy = self.worktree('busy')
        self.r.commit('b.txt', cwd=busy)
        self.land('busy')
        proc = self.r.root / 'proc'
        (proc / '42').mkdir(parents=True)
        (proc / '42' / 'cmdline').write_bytes(b'sleep\0')
        (proc / '42' / 'cwd').symlink_to(busy)
        self.proc = placement.cleanup_processes(proc_root=proc, windows=False)
        note = self.worktree('note', lock='keep: reviewing')
        self.r.commit('n.txt', cwd=note)
        self.land('note')
        out = self.propose()
        self.assertEqual(out['removable'], [])
        self.assertEqual(self.names(out['keep']), ['busy', 'note'])


class BatchTests(GardenCase):

    def setup_items(self):
        self.merged = self.worktree('merged')
        self.r.commit('m.txt', cwd=self.merged)
        self.land('merged')
        self.unm = self.worktree('unm')
        self.r.commit('u.txt', cwd=self.unm)
        self.dirty = self.worktree('dirty')
        (self.dirty / 'wip.txt').write_text('precious')
        self.dirty_other = self.worktree('dirty2')
        self.r.commit('d2.txt', cwd=self.dirty_other)
        (self.dirty_other / 'wip.txt').write_text('precious2')


    def test_remove_merged_script_dry_run_then_apply_and_never_prunes(self):
        from drover import garden  # P12: retained until its interface move in T17.
        self.setup_items()
        gone = self.r.root / 'wt' / 'ghost'
        self.r.git('worktree', 'add', '-q', '-b', 'ghost', gone, 'origin/main')
        self.r.commit('g.txt', cwd=gone)
        self.land('ghost')
        import shutil
        shutil.rmtree(gone)   # a missing directory is another side's or a crash's: prune would forget it
        rows = garden.remove_merged(self.r.repo, proc=self.proc, min_age_days=0)
        self.assertEqual([Path(r['path']).name for r in rows], ['merged'])
        self.assertTrue(self.merged.is_dir())
        rows = garden.remove_merged(self.r.repo, apply=True, proc=self.proc, min_age_days=0)
        self.assertTrue(rows[0]['result']['ok'])
        self.assertFalse(self.merged.exists())
        self.assertIn('ghost', self.r.git('worktree', 'list', '--porcelain'))   # entry still there: no prune


if __name__ == '__main__':
    unittest.main()
