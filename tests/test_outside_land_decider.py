"""What git leaves unproved goes to the decider and the owner, and real git has the last word on what they cite."""
import unittest
from unittest.mock import patch

from drover import git_client, outside_land
from drover.ledger import ops, states
from tests.outside_land_fixture import Decider, DeciderCase, GitCase


class LiveCitationTests(DeciderCase):

    def test_live_answer_on_a_pushing_repo_reads_the_remote_base(self):
        self.use_config(push=True)
        tid, _, commit = self.unproved(hand='f.txt')
        self.r.git('push', '-q', 'origin', 'main')
        self.assertEqual(self.scan(Decider(self.conn, acted=True, answer=commit))['landed'], [tid])
        self.assertEqual(self.external(tid)['base_ref'], 'origin/main')

    def test_live_answer_holds_when_the_local_branch_is_ahead_of_the_remote(self):
        self.use_config(push=True)
        tid, _, commit = self.unproved(hand='f.txt')
        self.r.git('push', '-q', 'origin', 'main')
        self.r.commit('local.txt')
        self.assertEqual(self.scan(Decider(self.conn, acted=True, answer=commit))['landed'], [tid])

    def test_live_citation_for_a_task_with_no_recorded_commit_asks_the_owner(self):
        tid = self.task()
        self.r.branch('feat', {'g.txt': 'y'})
        commit = self.r.commit('f.txt', msg='hand work')
        self.assertEqual(self.scan(Decider(self.conn, acted=True, answer=commit))['landed'], [])
        self.assertEqual(self.status(tid), states.NEEDS_OWNER)
        self.assertEqual(len(ops.open_questions(self.conn)), 1)

    def two_runs(self):
        """A task whose first run changed f.txt and whose second, on the same branch, changed g.txt."""
        tid = self.task()
        first = self.r.branch('feat', {'f.txt': 'x'})
        self.r.git('checkout', '-q', 'feat')
        second = self.r.commit('g.txt', 'y')
        self.r.git('checkout', '-q', 'main')
        self.worker_done(tid, first, run=1)
        self.worker_done(tid, second, run=2, base=first)
        return tid

    def test_live_citation_reads_the_files_of_the_newest_run(self):
        tid = self.two_runs()
        commit = self.r.commit('g.txt', 'hand', msg='hand work')
        with patch.object(outside_land, 'ambiguous', return_value=[commit]):
            self.assertEqual(self.scan(Decider(self.conn, acted=True, answer=commit))['landed'], [tid])

    def test_live_citation_of_only_an_earlier_runs_files_asks_the_owner(self):
        tid = self.two_runs()
        commit = self.r.commit('f.txt', 'hand', msg='hand work')
        self.assertEqual(self.scan(Decider(self.conn, acted=True, answer=commit))['landed'], [])
        self.assertEqual((self.status(tid), len(ops.open_questions(self.conn))), (states.NEEDS_OWNER, 1))

    def test_live_citation_of_a_merge_commit_reads_its_whole_change(self):
        tid = self.task()
        self.worker_done(tid, self.r.branch('feat', {'f.txt': 'x'}))
        self.r.git('checkout', '-q', '-b', 'hand', 'main')
        self.r.commit('f.txt', msg='hand work')
        self.r.git('checkout', '-q', 'main')
        self.r.commit('other.txt')
        self.r.git('merge', '-q', '--no-ff', '--no-edit', 'hand')
        merge = self.main()
        with patch.object(outside_land, 'ambiguous', return_value=[merge]):
            self.assertEqual(self.scan(Decider(self.conn, acted=True, answer=merge))['landed'], [tid])

    def test_live_answer_for_a_commit_since_reverted_is_ignored(self):
        tid, _, commit = self.unproved(hand='f.txt')
        self.r.git('revert', '--no-edit', commit)
        self.scan(Decider(self.conn, acted=True, answer=commit))
        self.assertEqual(self.status(tid), states.NEEDS_OWNER)
        self.assertEqual(len(ops.open_questions(self.conn)), 1)


class UnknownTests(DeciderCase):
    def test_git_failure_asks_nothing_and_the_next_interval_retries(self):
        self.unproved()
        failed = git_client.GitReply(git_client.Answer.UNKNOWN, '', 'fatal: broken', 128, 'exit')
        with patch.object(git_client, 'ask', return_value=failed), self.assertLogs('drover.outside_land', 'WARNING'):
            self.scan()
        self.assertEqual((self.decider.calls, ops.open_questions(self.conn)), ([], []))
        self.scan()
        self.assertEqual(len(ops.open_questions(self.conn)), 1)

    def test_git_timeout_asks_nothing_and_the_next_interval_retries(self):
        self.unproved()
        timed_out = git_client.GitReply(git_client.Answer.UNKNOWN, '', '', None, 'timeout')
        with patch.object(git_client, 'ask', return_value=timed_out), self.assertLogs('drover.outside_land', 'WARNING'):
            self.scan()
        self.assertEqual((self.decider.calls, ops.open_questions(self.conn)), ([], []))
        self.scan()
        self.assertEqual(len(ops.open_questions(self.conn)), 1)

    def fail_only(self, command, returncode=128):
        real = git_client.ask
        def broken(repo, *args, **kw):
            if args and args[0] == command:
                return git_client.GitReply(git_client.Answer.UNKNOWN, '', 'fatal: disk full', returncode, 'exit')
            return real(repo, *args, **kw)
        return patch.object(git_client, 'ask', side_effect=broken)

    def test_failure_of_one_git_step_asks_nothing_and_the_next_interval_proves_it(self):
        tid = self.task('yanked')
        self.worker_done(tid, self.landed_by_hand())
        self.r.commit('h.txt')
        with self.fail_only('merge-tree'), self.assertLogs('drover.outside_land', 'WARNING'):
            self.assertEqual(self.scan()['landed'], [])
        self.assertEqual((self.decider.calls, ops.open_questions(self.conn)), ([], []))
        self.assertEqual(self.scan()['landed'], [tid])

    def test_failed_cherry_is_unknown(self):
        tip = self.r.branch('feat', {'f.txt': 'x'})
        self.r.commit('other.txt')
        self.r.git('cherry-pick', 'feat')
        with self.fail_only('cherry'), self.assertRaises(git_client.GitFailed):
            outside_land._proof(self.r.repo, 'feat', 'main', [tip], self.fork)

    def test_no_git_inside_a_transaction(self):
        _, _, commit = self.unproved()
        real, seen = git_client.ask, []
        def spy(*args, **kw):
            seen.append(self.conn.in_transaction)
            return real(*args, **kw)
        with patch.object(git_client, 'ask', side_effect=spy):
            self.scan()
            self.answer(f'Landed at {commit}')
            self.scan()
        self.assertTrue(seen)
        self.assertFalse(any(seen))


class RetractionTests(DeciderCase):
    def test_question_retracts_when_git_later_proves_it(self):
        tid, _, _ = self.unproved()
        self.scan()
        question = self.question()['id']
        self.r.git('merge', '-q', '--no-edit', 'feat')
        self.assertEqual(self.scan()['landed'], [tid])
        closed = ops.get_question(self.conn, question)
        self.assertEqual((closed['status'], closed['answer_choice']), ('answered', None))
        self.assertIn('landed', closed['answer_text'])
        self.assertEqual(self.external(tid)['review'], 'ancestor')


class AmbiguousTests(GitCase):
    def task(self, **kw):
        return {'id': 'T-1', 'title': 'T', 'branch': 'feat', 'scope_files': ['f.txt', 'src/*.py'],
                'commits': [], 'start': self.fork, **kw}

    def ambiguous(self, task=None):
        return outside_land.ambiguous(self.r.repo, task or self.task(), 'main')

    def test_changed_files_overlap_newest_first(self):
        older = self.r.commit('f.txt')
        self.r.commit('other.txt')
        (self.r.repo / 'src').mkdir()
        newer = self.r.commit('src/a.py')
        self.assertEqual(self.ambiguous(), [newer, older])

    def test_subject_overlap_needs_two_words_of_the_title(self):
        hit = self.r.commit('other.txt', msg='Rework the scanner budget')
        self.r.commit('more.txt', msg='Rework the hub')
        self.assertEqual(self.ambiguous(self.task(title='Scanner budget rework')), [hit])

    def test_subject_naming_the_branch_or_the_task_overlaps(self):
        named = self.r.commit('other.txt', msg="Merge branch 'feat-x'")
        tagged = self.r.commit('more.txt', msg='T-1: done by hand')
        self.assertEqual(self.ambiguous(self.task(branch='feat-x')), [tagged, named])

    def test_the_list_candidates_gives_starts_at_its_first_base(self):
        hit = self.r.commit('f.txt')
        self.assertEqual(self.ambiguous(self.task(start=[self.fork, 'a' * 40, None])), [hit])

    def test_nothing_overlaps(self):
        self.r.commit('other.txt')
        self.assertEqual(self.ambiguous(), [])

    def test_every_overlapping_commit_is_listed_newest_first(self):
        shas = [self.r.commit('f.txt', str(n)) for n in range(3)]
        self.assertEqual(self.ambiguous(), shas[::-1])

    def test_commits_before_the_work_began_do_not_count(self):
        self.r.commit('f.txt')
        self.fork = self.main()
        self.assertEqual(self.ambiguous(), [])

    def test_branch_merge_base_when_no_run_recorded_a_start(self):
        self.r.commit('f.txt')
        self.r.git('branch', '-q', 'feat', 'main')
        hit = self.r.commit('f.txt', 'later')
        self.assertEqual(self.ambiguous(self.task(start=None)), [hit])

    def test_deleted_branch_falls_back_to_the_recorded_commit(self):
        tip = self.r.branch('feat', {'g.txt': 'y'})
        self.r.git('branch', '-q', '-D', 'feat')
        hit = self.r.commit('f.txt')
        self.assertEqual(self.ambiguous(self.task(start=None, commits=[tip])), [hit])

    def test_no_anchor_at_all_means_nothing_to_ask(self):
        self.r.commit('f.txt')
        self.assertEqual(self.ambiguous(self.task(start=None, branch='gone')), [])

    def test_failed_git_is_not_an_empty_answer(self):
        failed = git_client.GitReply(git_client.Answer.UNKNOWN, '', 'fatal', 128, 'exit')
        with patch.object(git_client, 'ask', return_value=failed), self.assertRaises(git_client.GitFailed):
            self.ambiguous()

    def test_a_subject_cannot_forge_a_changed_file(self):
        self.r.commit('unrelated.txt', msg='evil\u2028f.txt\u2028more')
        found = outside_land._log(self.r.repo, 'main', '-1')[0]
        self.assertEqual((found[1], found[2]), ('evil\u2028f.txt\u2028more', ['unrelated.txt']))


if __name__ == '__main__':
    unittest.main()
