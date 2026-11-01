"""A task with several runs lands only with every run's change on the base, as real git shows it."""
import unittest

from drover.ledger import ops, states
from tests.outside_land_fixture import ScanCase


class RunHistoryTests(ScanCase):
    """A task with several runs: every run's commit counts, not only the net change."""
    def setUp(self):
        super().setUp()
        self.fork = self.r.commit('s.txt', self.text())

    def text(self, **lines):
        return ''.join(lines.get(f'l{n}', f'line {n}') + '\n' for n in range(40))

    def runs(self, *changes, base=None):
        wt = self.r.root / 'runs-wt'
        self.r.git('worktree', 'add', '-q', '-b', 'feat', wt, base or 'main')
        shas = []
        for change in changes:
            for name, text in change.items():
                if text is None:
                    self.r.git('rm', '-q', name, cwd=wt)
                else:
                    (wt / name).write_text(text)
                    self.r.git('add', name, cwd=wt)
            self.r.git('commit', '-q', '-m', 'run', cwd=wt)
            shas.append(self.r.git('rev-parse', 'HEAD', cwd=wt))
        self.r.git('worktree', 'remove', '--force', wt)
        return shas

    def task_with(self, shas):
        tid = self.task('yanked')
        for run, sha in enumerate(shas, 1):
            self.worker_done(tid, sha, run=run)
        return tid

    def removal(self, same_file=True):
        """Run 1 adds a feature and a debug line; run 2 only removes the debug line."""
        if same_file:
            return {'s.txt': self.text(l5='FEATURE', l30='DEBUG')}, {'s.txt': self.text(l5='FEATURE')}
        return {'feature.txt': 'x', 'debug.txt': 'd'}, {'debug.txt': None}

    def pick(self, *shas):
        self.r.commit('other.txt')
        self.r.git('cherry-pick', *shas)

    def squash(self, sha):
        self.r.git('merge', '-q', '--squash', sha)
        self.r.git('commit', '-q', '-m', 'squash')

    def assert_stays(self, tid):
        self.assertEqual(self.scan()['landed'], [])
        self.assertEqual(self.status(tid), states.YANKED)

    def assert_lands(self, tid):
        self.assertEqual(self.scan()['landed'], [tid])

    def test_first_run_picked_and_removal_never_on_main(self):
        shas = self.runs(*self.removal())
        self.pick(shas[0])
        self.assertIn('DEBUG', self.r.git('show', 'main:s.txt'))
        self.assert_stays(self.task_with(shas))

    def test_first_run_picked_and_removal_never_on_main_two_files(self):
        shas = self.runs(*self.removal(same_file=False))
        self.pick(shas[0])
        self.assertTrue((self.r.repo / 'debug.txt').exists())
        self.assert_stays(self.task_with(shas))

    def test_first_run_squashed_and_removal_never_on_main(self):
        shas = self.runs(*self.removal())
        self.squash(shas[0])
        self.assertIn('DEBUG', self.r.git('show', 'main:s.txt'))
        self.assert_stays(self.task_with(shas))

    def test_reverted_removal_is_not_proved(self):
        shas = self.runs(*self.removal())
        self.r.git('merge', '-q', '--ff-only', 'feat')
        self.r.git('revert', '--no-edit', 'HEAD')
        self.assertIn('DEBUG', self.r.git('show', 'main:s.txt'))
        self.assert_stays(self.task_with(shas))

    def test_reverted_removal_is_not_proved_two_files(self):
        shas = self.runs(*self.removal(same_file=False))
        self.r.git('merge', '-q', '--ff-only', 'feat')
        self.r.git('revert', '--no-edit', 'HEAD')
        self.assertTrue((self.r.repo / 'debug.txt').exists())
        self.assert_stays(self.task_with(shas))

    def test_three_runs_with_the_removal_never_on_main(self):
        shas = self.runs(*self.removal(), {'y.txt': 'y'})
        self.pick(shas[0], shas[2])
        self.assertIn('DEBUG', self.r.git('show', 'main:s.txt'))
        self.assert_stays(self.task_with(shas))

    def test_undo_on_a_moved_base_that_was_reverted_is_not_proved(self):
        moved = self.r.commit('m.txt')
        run_base = self.r.commit('other.txt')
        tid = self.task('yanked')
        self.launched(tid, 1, self.fork)                  # run 1 made no commit
        shas = self.runs({'m.txt': None}, base=run_base)
        self.worker_done(tid, shas[0], run=2, base=run_base)
        self.r.git('merge', '-q', '--ff-only', 'feat')
        self.r.git('revert', '--no-edit', 'HEAD')
        self.assertEqual(self.r.git('show', 'main:m.txt'), self.r.git('show', f'{moved}:m.txt'))
        self.assert_stays(tid)

    def test_newest_run_with_no_recorded_base_is_not_proved(self):
        shas = self.runs(*self.removal())
        self.r.git('merge', '-q', '--ff-only', 'feat')
        tid = self.task('yanked')
        self.worker_done(tid, shas[0], run=1)
        self.launched(tid, 2, None)
        ops.event(self.conn, 'worker-done', 'dispatcher', {'run': 2, 'sha': shas[1]}, task_id=tid)
        self.assert_stays(tid)

    def test_every_run_kept_lands(self):
        shas = self.runs(*self.removal(), {'y.txt': 'y'})
        self.r.git('merge', '-q', '--ff-only', 'feat')
        self.r.commit('later.txt')
        self.assert_lands(self.task_with(shas))

    def test_every_run_squashed_lands(self):
        shas = self.runs(*self.removal(), {'y.txt': 'y'})
        self.squash(shas[-1])
        self.r.commit('later.txt')
        self.assert_lands(self.task_with(shas))

    def test_every_run_picked_lands(self):
        shas = self.runs(*self.removal(), {'y.txt': 'y'})
        self.pick(*shas)
        self.r.commit('later.txt')
        self.assert_lands(self.task_with(shas))

    def test_removed_then_restored_lands(self):
        kept = {'s.txt': self.text(l5='FEATURE', l30='X')}
        shas = self.runs(kept, {'s.txt': self.text(l5='FEATURE')}, kept)
        self.r.git('merge', '-q', '--ff-only', 'feat')
        self.assert_lands(self.task_with(shas))

    def test_removed_then_restored_squashed_lands(self):
        kept = {'s.txt': self.text(l5='FEATURE', l30='X')}
        shas = self.runs(kept, {'s.txt': self.text(l5='FEATURE')}, kept)
        self.squash(shas[-1])
        self.assert_lands(self.task_with(shas))


if __name__ == '__main__':
    unittest.main()
