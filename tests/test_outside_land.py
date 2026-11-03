"""Git proves a landing made outside Drover, and nothing weaker does."""
import unittest
from unittest.mock import patch

from drover import git_client, landing, outside_land
from tests.outside_land_fixture import GitCase


class GitProofTests(GitCase):
    def test_fast_forward_is_ancestor(self):
        tip = self.r.branch('feat', {'f.txt': 'x'})
        self.r.git('merge', '-q', '--ff-only', 'feat')
        self.assertEqual(self.proof([tip]), {'sha': tip, 'proof': 'ancestor'})

    def test_rebased_is_patch_equivalent(self):
        tip = self.r.branch('feat', {'f.txt': 'x'})
        self.r.commit('other.txt')
        self.r.git('cherry-pick', 'feat')
        self.assertEqual(self.proof([tip]), {'sha': self.main(), 'proof': 'patch-equivalent'})

    def test_branch_rebased_then_fast_forwarded_is_patch_equivalent(self):
        recorded = self.r.branch('feat', {'f.txt': 'x'})
        self.r.commit('other.txt')
        wt = self.r.root / 'rebase-wt'
        self.r.git('worktree', 'add', '-q', wt, 'feat')
        self.r.git('rebase', '-q', 'main', cwd=wt)
        self.r.git('worktree', 'remove', '--force', wt)
        self.r.git('merge', '-q', '--ff-only', 'feat')
        self.assertEqual(self.proof([recorded]), {'sha': self.main(), 'proof': 'patch-equivalent'})

    def test_squash_merge_adds_nothing(self):
        tip = self.r.branch('feat', {'a.txt': 'a', 'b.txt': 'b'})
        self.r.git('merge', '-q', '--squash', 'feat')
        self.r.git('commit', '-q', '-m', 'squash')
        self.assertEqual(self.proof([tip]), {'sha': self.main(), 'proof': 'adds-nothing'})

    def test_no_recorded_commit_is_never_a_proof(self):
        self.r.branch('feat', {'a.txt': 'a', 'b.txt': 'b'})
        self.r.git('merge', '-q', '--squash', 'feat')
        self.r.git('commit', '-q', '-m', 'squash')
        self.assertIsNone(self.proof())                       # adds nothing
        self.r.git('merge', '-q', '--no-edit', 'feat')
        self.assertIsNone(self.proof())                       # ancestor

    def test_no_recorded_commit_is_no_patch_proof(self):
        self.r.branch('feat', {'f.txt': 'x'})
        self.r.commit('other.txt')
        self.r.git('cherry-pick', 'feat')
        self.assertIsNone(self.proof())

    def test_branch_past_the_recorded_commit_is_not_proved(self):
        recorded = self.r.branch('feat', {'f.txt': 'x'})
        wt = self.r.root / 'past-wt'
        self.r.git('worktree', 'add', '-q', wt, 'feat')
        self.r.commit('more.txt', cwd=wt)
        self.r.git('worktree', 'remove', '--force', wt)
        self.r.commit('other.txt')
        self.r.git('cherry-pick', recorded, 'feat')             # all landed, but the ledger knows only the first
        self.assertIsNone(self.proof([recorded]))

    def revert_last(self):
        self.r.git('revert', '--no-edit', 'HEAD')
        self.assertFalse((self.r.repo / 'f.txt').exists())

    def test_reverted_on_main_is_not_proved(self):
        tip = self.r.branch('feat', {'f.txt': 'x'})
        self.r.git('merge', '-q', '--ff-only', 'feat')
        self.assertEqual(self.proof([tip])['proof'], 'ancestor')
        self.revert_last()
        self.assertIsNone(self.proof([tip]))

    def config(self, second):
        section = '[{}]\nhost = h\nport = 1\nretries = 3\nenabled = {}\ntimeout = 5\nname = n\nlimit = 2\n'
        return section.format('one', 'true') + section.format('two', second)

    def config_section(self):
        self.fork = self.r.commit('app.cfg', self.config('false'))

    def flip(self, branch):
        wt = self.r.root / f'{branch}-wt'
        self.r.git('worktree', 'add', '-q', '-b', branch, wt, 'main')
        tip = self.r.commit('app.cfg', self.config('true'), cwd=wt)
        self.r.git('worktree', 'remove', '--force', wt)
        return tip

    def test_reverted_section_flip_is_not_proved(self):
        self.config_section()
        tip = self.flip('feat')
        self.r.git('merge', '-q', '--ff-only', 'feat')
        self.assertEqual(self.proof([tip])['proof'], 'ancestor')
        self.r.git('revert', '--no-edit', 'HEAD')
        self.assertIsNone(self.proof([tip]))
        self.fork = self.main()
        tip = self.flip('picked')
        self.r.commit('other.txt')
        self.r.git('cherry-pick', 'picked')
        self.assertEqual(self.proof([tip], branch='picked')['proof'], 'patch-equivalent')
        self.r.git('revert', '--no-edit', 'HEAD')
        self.assertIsNone(self.proof([tip], branch='picked'))

    def test_reverted_mode_change_is_not_proved(self):
        wt = self.r.root / 'mode-wt'
        self.r.git('worktree', 'add', '-q', '-b', 'feat', wt, 'main')
        self.r.git('update-index', '--chmod=+x', 'base.txt', cwd=wt)
        self.r.git('commit', '-q', '-m', 'executable', cwd=wt)
        tip = self.r.git('rev-parse', 'HEAD', cwd=wt)
        self.r.git('worktree', 'remove', '--force', wt)
        self.r.git('merge', '-q', '--ff-only', 'feat')
        self.assertEqual(self.proof([tip])['proof'], 'ancestor')
        self.r.git('revert', '--no-edit', 'HEAD')
        self.assertIn('100644', self.r.git('ls-tree', 'main', 'base.txt'))
        self.assertIsNone(self.proof([tip]))

    def test_reverted_replay_is_not_proved(self):
        tip = self.r.branch('feat', {'f.txt': 'x'})
        self.r.commit('other.txt')
        self.r.git('cherry-pick', 'feat')
        self.revert_last()
        self.assertIsNone(self.proof([tip]))

    def test_reverted_squash_is_not_proved(self):
        tip = self.r.branch('feat', {'f.txt': 'x', 'g.txt': 'y'})
        self.r.git('merge', '-q', '--squash', 'feat')
        self.r.git('commit', '-q', '-m', 'squash')
        self.assertEqual(self.proof([tip])['proof'], 'adds-nothing')
        self.revert_last()
        self.assertIsNone(self.proof([tip]))

    def test_partly_reverted_landing_is_not_proved(self):
        tip = self.r.branch('feat', {'f.txt': 'x', 'g.txt': 'y'})
        self.r.git('merge', '-q', '--ff-only', 'feat')
        self.r.git('rm', '-q', 'f.txt')
        self.r.git('commit', '-q', '-m', 'take part of it back')
        self.assertIsNone(self.proof([tip]))

    def test_unrelated_commits_after_the_landing_keep_the_proof(self):
        tip = self.r.branch('feat', {'f.txt': 'x'})
        self.r.git('merge', '-q', '--ff-only', 'feat')
        self.r.commit('later.txt')
        self.assertEqual(self.proof([tip]), {'sha': tip, 'proof': 'ancestor'})

    def test_later_edit_of_the_same_lines_is_not_proved(self):
        tip = self.r.branch('feat', {'f.txt': 'x'})
        self.r.git('merge', '-q', '--ff-only', 'feat')
        self.r.commit('f.txt', 'rewritten')
        self.assertIsNone(self.proof([tip]))                  # missed, so it reaches the owner instead

    def test_recorded_commit_that_changed_nothing_is_not_proved(self):
        self.r.git('commit', '-q', '--allow-empty', '-m', 'nothing')
        self.r.git('branch', '-q', 'feat', 'main')
        self.assertIsNone(self.proof([self.main()]))

    def test_unknown_run_base_is_not_proved(self):
        tip = self.r.branch('feat', {'f.txt': 'x'})
        self.r.git('merge', '-q', '--ff-only', 'feat')
        self.assertIsNone(outside_land.git_proof(self.r.repo, 'feat', 'main', [tip]))

    def test_survival_check_leaves_the_index_and_worktree_alone(self):
        tip = self.r.branch('feat', {'f.txt': 'x'})
        self.r.git('merge', '-q', '--ff-only', 'feat')
        before = self.r.git('status', '--porcelain'), (self.r.repo / '.git' / 'index').read_bytes()
        self.assertIsNotNone(self.proof([tip]))
        self.assertEqual((self.r.git('status', '--porcelain'), (self.r.repo / '.git' / 'index').read_bytes()), before)

    def test_partial_squash_is_not_proved(self):
        tip = self.r.branch('feat', {'a.txt': 'a', 'b.txt': 'b'})
        self.r.git('checkout', '-q', 'feat', '--', 'a.txt')
        self.r.git('commit', '-q', '-m', 'part of the branch')
        self.assertIsNone(self.proof([tip]))

    def test_unlanded_branch_is_not_proved(self):
        tip = self.r.branch('feat', {'f.txt': 'x'})
        self.r.commit('other.txt')
        self.assertIsNone(self.proof([tip]))

    def test_branch_at_base_is_not_a_landing(self):
        self.r.git('branch', '-q', 'feat', 'main')
        self.assertIsNone(self.proof())
        self.r.commit('later.txt')                  # now behind the base as well
        self.assertIsNone(self.proof())

    def test_branch_with_no_own_commits_after_rebase_needs_task_evidence(self):
        unlanded = self.r.branch('feat', {'f.txt': 'x'})
        other = self.r.commit('other.txt')
        self.r.git('branch', '-q', '-f', 'feat', 'main')   # rebased: every task commit dropped
        self.assertIsNone(self.proof())
        self.assertIsNone(self.proof([unlanded]))          # a recorded commit that never reached the base
        self.assertEqual(self.proof([unlanded, other]), {'sha': other, 'proof': 'ancestor'})

    def test_earlier_attempt_on_the_base_does_not_land_the_later_one(self):
        first = self.r.branch('feat', {'f.txt': 'x'})
        self.r.git('merge', '-q', '--ff-only', 'feat')
        wt = self.r.root / 'retry-wt'
        self.r.git('worktree', 'add', '-q', wt, 'feat')
        second = self.r.commit('g.txt', cwd=wt)
        self.r.git('worktree', 'remove', '--force', wt)
        self.r.git('branch', '-q', '-f', 'feat', 'main')
        self.assertIsNone(self.proof([first, second]))

    def test_own_commits_block_the_ancestor_shortcut(self):
        landed = self.r.branch('feat', {'f.txt': 'x'})
        self.r.git('merge', '-q', '--ff-only', 'feat')
        self.r.git('branch', '-q', 'wt', 'feat')
        self.r.git('worktree', 'add', '-q', self.r.root / 'wt', 'wt')
        self.r.commit('more.txt', cwd=self.r.root / 'wt')
        self.assertIsNone(self.proof([landed], branch='wt'))

    def test_branch_that_undoes_itself_is_not_proved(self):
        self.r.branch('feat', {'f.txt': 'x'})
        self.r.git('checkout', '-q', 'feat')
        self.r.git('revert', '--no-edit', 'HEAD')
        tip = self.r.git('rev-parse', 'HEAD')
        self.r.git('checkout', '-q', 'main')
        self.r.commit('other.txt')
        self.assertIsNone(self.proof([tip]))

    def test_merge_commit_hides_nothing_from_the_patch_check(self):
        self.r.branch('feat', {'f.txt': 'x'})
        self.r.branch('side', {'s.txt': 's'})
        self.r.commit('other.txt')
        self.r.git('cherry-pick', 'feat', 'side')
        wt = self.r.root / 'merge-wt'
        self.r.git('worktree', 'add', '-q', wt, 'feat')
        self.r.git('merge', '-q', '--no-edit', 'side', cwd=wt)
        (wt / 'unlanded.txt').write_text('in the merge only')
        self.r.git('add', 'unlanded.txt', cwd=wt)
        self.r.git('commit', '-q', '--amend', '--no-edit', cwd=wt)
        tip = self.r.git('rev-parse', 'HEAD', cwd=wt)
        self.assertIs(landing.contained(self.r.repo, tip, self.main()), git_client.Answer.YES)  # cherry skips merges
        self.assertIsNone(self.proof([tip]))

    def test_branch_with_only_a_merge_of_the_base_has_no_commits_of_its_own(self):
        self.r.git('branch', '-q', 'feat', 'main')
        self.r.commit('other.txt')
        self.r.git('checkout', '-q', 'feat')
        self.r.git('merge', '-q', '--no-ff', '--no-edit', 'main')
        self.r.git('checkout', '-q', 'main')
        self.r.commit('later.txt')
        self.assertIsNone(self.proof([]))

    def test_missing_branch_is_unknown(self):
        self.assertIsNone(self.proof(branch='no-such-branch'))

    def test_missing_base_is_unknown(self):
        self.r.branch('feat', {'f.txt': 'x'})
        self.assertIsNone(self.proof(base='no-such-base'))

    def test_git_failure_is_unknown(self):
        self.assertIsNone(outside_land.git_proof(self.r.root / 'not-a-repo', 'feat', 'main', ['a' * 40], 'b' * 40))
        tip = self.r.branch('feat', {'f.txt': 'x'})
        self.r.git('merge', '-q', '--ff-only', 'feat')
        self.assertIsNotNone(self.proof([tip]))
        for why, code in (('unavailable', None), ('exit', 128)):
            with patch.object(git_client, 'ask', return_value=git_client.GitReply(
                    git_client.Answer.UNKNOWN, '', 'git unavailable: no git', code, why)):
                self.assertIsNone(self.proof([tip]))

    def test_timeout_is_unknown(self):
        tip = self.r.branch('feat', {'f.txt': 'x'})
        self.r.git('merge', '-q', '--ff-only', 'feat')
        timed_out = git_client.GitReply(git_client.Answer.UNKNOWN, '', '', None, 'timeout')
        with patch.object(git_client, 'ask', return_value=timed_out):
            self.assertIsNone(self.proof([tip]))
            self.assertIs(landing.contained(self.r.repo, 'feat', 'main', timeout=1), git_client.Answer.UNKNOWN)


if __name__ == '__main__':
    unittest.main()
