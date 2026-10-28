import unittest
from pathlib import Path

from drover import placement
from tests.gitfix import Repo, sh


from tests.test_disk_worktrees import WorktreeCase


class GardenCase(WorktreeCase):
    def setUp(self):
        super().setUp()
        (self.r.root / 'noproc').mkdir()
        self.proc = placement.cleanup_processes(proc_root=self.r.root / 'noproc', windows=False)

    def worktree(self, name, lock='native'):
        path = super().worktree(name, lock=lock)
        self.r.git('branch', '-m', name, cwd=path)
        return path

    def busy(self, path):
        return self.proc.busy(str(path))

    def propose(self, **kw):
        rows = self.cleanup()['items']
        rows = [row for row in rows if row['stage'] == 4 and row['path'] != str(self.r.repo)]
        return {'removable': [row for row in rows if row['action'] == 'candidate'],
                'keep': [row for row in rows if row['action'] != 'candidate']}

    def names(self, items):
        return sorted(Path(i['path']).name for i in items)

    def land(self, branch):
        self.r.git('push', '-q', 'origin', f'{branch}:main')


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
        self.assertEqual(self.r.git('cherry', 'origin/main', 'rb').count('+'), 0)
        self.assertEqual(self.names(self.propose()['removable']), ['rb'])

    def test_squash_merge_detected(self):
        wt = self.worktree('sq')
        self.r.commit('s1.txt', cwd=wt)
        self.r.commit('s2.txt', cwd=wt)
        self.r.git('merge', '--squash', 'sq')
        self.r.git('commit', '-q', '-m', 'squash')
        self.r.git('push', '-q', 'origin', 'HEAD:main')
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


if __name__ == '__main__':
    unittest.main()
