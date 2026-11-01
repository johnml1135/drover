"""What git leaves unproved goes to the decider, in shadow first, and then to the owner."""
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from drover import landing, outside_land, placement
from drover.ledger import landing as landing_storage
from drover.ledger import ops, states
from tests.test_outside_land import GitCase, ScanCase


class Decider:
    """A fake `decide` that also checks no ledger transaction is held across the call."""

    def __init__(self, conn, *, acted=False, answer=None, shadow=None, confidence=0.9, fallback='timeout'):
        self.conn, self.calls = conn, []
        self.result = {'name': 'landed_outside', 'answer': answer if acted else None, 'shadow_answer': shadow,
                       'acted': acted, 'confidence': confidence, 'fallback': None if acted else fallback,
                       'model': 'fake', 'state_hash': 'hash', 'criteria_version': 1, 'probabilities': None}

    def __call__(self, name, state, rule_answer=None, dynamic_criteria=None, **kw):
        assert not self.conn.in_transaction
        self.calls.append(SimpleNamespace(name=name, state=state, rule=rule_answer, criteria=dynamic_criteria,
                                          timeout=kw.get('timeout')))
        return self.result


class DeciderCase(ScanCase):
    def scan(self, decide=None):
        self.decider = decide or getattr(self, 'decider', None) or Decider(self.conn)
        return outside_land.tick(self.conn, {}, decide=self.decider)

    def unproved(self, status='needs-owner', hand='g.txt'):
        """A task whose branch never landed, and a hand commit on main that touches its scope."""
        tid = self.task(status, scope_files=['f.txt', 'g.txt'])
        tip = self.r.branch('feat', {'f.txt': 'x'})
        self.worker_done(tid, tip)
        return tid, tip, self.r.commit(hand, msg='hand work')

    def question(self, position=0):
        return ops.open_questions(self.conn)[position]

    def labels(self, question=None):
        return [o['label'] for o in (question or self.question())['options']]

    def answer(self, choice=None, text=None):
        ops.answer_question(self.conn, self.question()['id'], choice=choice, text=text)

    def external(self, tid):
        return ops.events(self.conn, task_id=tid, kind='status')[-1]['data']['external_landing']


class ShadowAndLiveTests(DeciderCase):
    def test_shadow_answer_is_recorded_and_changes_nothing(self):
        tid, _, commit = self.unproved()
        before = ops.get_task(self.conn, tid)
        self.assertEqual(self.scan(Decider(self.conn, shadow=commit, fallback='shadow'))['landed'], [])
        self.assertEqual(ops.get_task(self.conn, tid), before)
        rows = self.conn.execute('SELECT question_name,answer,shadow FROM decisions').fetchall()
        self.assertEqual([tuple(row) for row in rows], [('landed_outside', f'"{commit}"', 1)])
        self.assertIn(f'Landed at {commit}', self.labels())
        call = self.decider.calls[0]
        self.assertEqual((call.name, call.rule, set(call.criteria)), ('landed_outside', 'not_landed', {commit, 'not_landed'}))
        self.assertIn('hand work', call.criteria[commit])
        self.assertIn('hand work', call.state)

    def test_live_answer_citing_a_commit_on_main_lands(self):
        tid, _, commit = self.unproved(hand='f.txt')
        self.assertEqual(self.scan(Decider(self.conn, acted=True, answer=commit))['landed'], [tid])
        self.assertEqual(self.status(tid), states.LANDED)
        record = self.external(tid)
        self.assertEqual((record['sha'], record['gate'], record['review']), (commit, 'outside-drover', 'decider'))
        self.assertEqual(ops.events(self.conn, task_id=tid, kind='status')[-1]['actor'], 'detector')
        self.assertEqual(landing_storage.land_sha(self.conn, tid), commit)
        self.assertEqual(ops.open_questions(self.conn), [])
        self.assertEqual(self.conn.execute('SELECT shadow FROM decisions').fetchone()[0], 0)

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

    def test_live_answer_citing_an_unknown_sha_is_ignored(self):
        tid, _, commit = self.unproved()
        self.scan(Decider(self.conn, acted=True, answer='f' * 40))
        self.assertEqual(self.status(tid), states.NEEDS_OWNER)
        self.assertIn(f'Landed at {commit}', self.labels())

    def test_live_answer_for_a_commit_off_main_is_ignored(self):
        tid, tip, commit = self.unproved()
        with patch.object(outside_land, 'ambiguous', return_value=[commit, tip]):
            self.scan(Decider(self.conn, acted=True, answer=tip))
        self.assertEqual(self.status(tid), states.NEEDS_OWNER)
        self.assertEqual(len(ops.open_questions(self.conn)), 1)

    def test_live_citation_with_no_file_overlap_asks_the_owner(self):
        tid, _, commit = self.unproved(hand='g.txt')      # in scope, but the task's recorded commit changed only f.txt
        self.assertEqual(self.scan(Decider(self.conn, acted=True, answer=commit))['landed'], [])
        self.assertEqual(self.status(tid), states.NEEDS_OWNER)
        self.assertIn(f'Landed at {commit}', self.labels())

    def test_live_citation_for_a_task_with_no_recorded_commit_asks_the_owner(self):
        tid = self.task()
        self.r.branch('feat', {'g.txt': 'y'})
        commit = self.r.commit('f.txt', msg='hand work')
        self.assertEqual(self.scan(Decider(self.conn, acted=True, answer=commit))['landed'], [])
        self.assertEqual(self.status(tid), states.NEEDS_OWNER)
        self.assertEqual(len(ops.open_questions(self.conn)), 1)

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

    def test_live_not_landed_asks_the_owner(self):
        tid, _, _ = self.unproved()
        self.scan(Decider(self.conn, acted=True, answer='not_landed'))
        self.assertEqual(self.status(tid), states.NEEDS_OWNER)
        self.assertEqual(len(ops.open_questions(self.conn)), 1)

    def test_decider_down_asks_the_owner(self):
        tid, _, commit = self.unproved()
        self.scan(Decider(self.conn))
        self.assertEqual(self.status(tid), states.NEEDS_OWNER)
        self.assertEqual(self.labels(), [f'Landed at {commit}', 'Not landed'])
        self.assertTrue(self.question()['allow_text'])

    def test_low_confidence_asks_the_owner(self):
        tid, _, commit = self.unproved()
        self.scan(Decider(self.conn, shadow=commit, confidence=0.3, fallback='low_confidence'))
        self.assertEqual(self.status(tid), states.NEEDS_OWNER)
        self.assertEqual(len(ops.open_questions(self.conn)), 1)

    def test_task_moved_on_during_the_decision_is_not_asked_about(self):
        tid, _, commit = self.unproved()
        decider = Decider(self.conn, shadow=commit, fallback='shadow')
        def moved(*args, **kw):
            ops.set_status(self.conn, tid, states.READY, 'owner', reason='requeued')
            return decider(*args, **kw)
        self.scan(moved)
        self.assertEqual(ops.open_questions(self.conn), [])


class QuietTests(DeciderCase):
    def test_no_candidates_asks_nothing_for_needs_owner_tasks(self):
        self.worker_done(self.task(), self.r.branch('feat', {'f.txt': 'x'}))
        self.r.commit('unrelated.txt')
        self.scan()
        self.assertEqual(self.decider.calls, [])
        self.assertEqual(ops.open_questions(self.conn), [])

    def test_yanked_task_with_no_candidates_stays_quiet_and_yanked(self):
        tid = self.task('yanked')
        self.worker_done(tid, self.r.branch('feat', {'f.txt': 'x'}))
        self.scan()
        self.assertEqual((self.status(tid), self.decider.calls, ops.open_questions(self.conn)), (states.YANKED, [], []))

    def test_one_question_per_task_across_ticks(self):
        tid, _, _ = self.unproved()
        self.scan()
        self.scan()
        self.r.commit('g.txt', 'another hand edit')
        self.scan()
        self.assertEqual(len(self.decider.calls), 1)
        self.assertEqual(len(ops.open_questions(self.conn)), 1)
        self.assertEqual(self.status(tid), states.NEEDS_OWNER)

    def test_no_question_again_after_the_owner_answered_not_landed(self):
        tid, _, _ = self.unproved()
        self.scan()
        self.answer('Not landed')
        self.scan()
        self.assertEqual((len(self.decider.calls), ops.open_questions(self.conn)), (1, []))
        self.assertEqual(self.status(tid), states.NEEDS_OWNER)

    def test_git_failure_asks_nothing_and_the_next_interval_retries(self):
        self.unproved()
        failed = SimpleNamespace(returncode=128, stdout='', stderr='fatal: broken')
        with patch.object(landing, 'git', return_value=failed), self.assertLogs('drover.outside_land', 'WARNING'):
            self.scan()
        self.assertEqual((self.decider.calls, ops.open_questions(self.conn)), ([], []))
        self.scan()
        self.assertEqual(len(ops.open_questions(self.conn)), 1)

    def test_git_timeout_asks_nothing_and_the_next_interval_retries(self):
        self.unproved()
        timed_out = SimpleNamespace(returncode=landing.GIT_TIMED_OUT, stdout='', stderr='')
        with patch.object(landing, 'git', return_value=timed_out), self.assertLogs('drover.outside_land', 'WARNING'):
            self.scan()
        self.assertEqual((self.decider.calls, ops.open_questions(self.conn)), ([], []))
        self.scan()
        self.assertEqual(len(ops.open_questions(self.conn)), 1)

    def test_timeout_after_the_proof_also_asks_nothing(self):
        self.unproved()
        with patch.object(outside_land, 'ambiguous', side_effect=subprocess.TimeoutExpired('git', 1)), \
                self.assertLogs('drover.outside_land', 'WARNING'):
            self.scan()
        self.assertEqual((self.decider.calls, ops.open_questions(self.conn)), ([], []))

    def fail_only(self, command, returncode=128):
        real = landing.git
        def broken(repo, *args, **kw):
            if args and args[0] == command:
                return SimpleNamespace(returncode=returncode, stdout='', stderr='fatal: disk full')
            return real(repo, *args, **kw)
        return patch.object(landing, 'git', side_effect=broken)

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
        with self.fail_only('cherry'), self.assertRaises(outside_land.GitUnknown):
            outside_land._proof(self.r.repo, 'feat', 'main', [tip], self.fork)

    def test_failed_cited_commit_check_asks_nothing(self):
        _, _, commit = self.unproved()
        with patch.object(outside_land, '_survives', side_effect=outside_land.GitUnknown('broken')), \
                self.assertLogs('drover.outside_land', 'WARNING'):
            self.scan(Decider(self.conn, acted=True, answer=commit))
        self.assertEqual(ops.open_questions(self.conn), [])

    def test_pruned_recorded_commit_still_reaches_the_question(self):
        tid = self.task()
        self.r.branch('feat', {'g.txt': 'y'})
        self.worker_done(tid, 'a' * 40)
        self.r.commit('f.txt', msg='hand work')
        self.scan()
        self.assertEqual(len(ops.open_questions(self.conn)), 1)

    def test_unresolved_start_still_reaches_the_question(self):
        tid = self.task()
        tip = self.r.branch('feat', {'g.txt': 'y'})
        self.worker_done(tid, tip, base='b' * 40)
        self.r.commit('f.txt', msg='hand work')
        self.scan()
        self.assertEqual(len(ops.open_questions(self.conn)), 1)

    def test_no_git_inside_a_transaction(self):
        _, _, commit = self.unproved()
        real, seen = placement.git, []
        def spy(*args, **kw):
            seen.append(self.conn.in_transaction)
            return real(*args, **kw)
        with patch.object(placement, 'git', side_effect=spy):
            self.scan()
            self.answer(f'Landed at {commit}')
            self.scan()
        self.assertTrue(seen)
        self.assertFalse(any(seen))


class BudgetTests(DeciderCase):
    def test_decider_call_that_would_pass_the_budget_waits_for_the_next_interval(self):
        tid, _, _ = self.unproved()
        clock = iter([0, 0, outside_land.SCAN_BUDGET_S - outside_land.decide_module.TIMEOUT + 1])
        decider = Decider(self.conn)
        with self.assertLogs('drover.outside_land', 'WARNING'):
            outside_land.tick(self.conn, {}, decide=decider, now=lambda: next(clock))
        self.assertEqual((decider.calls, ops.open_questions(self.conn)), ([], []))
        self.assertEqual(ops.get_setting(self.conn, outside_land.RESUME_KEY), tid)
        self.scan(decider)
        self.assertEqual((len(decider.calls), ops.get_setting(self.conn, outside_land.RESUME_KEY)), (1, ''))

    def test_the_configured_decider_timeout_sets_the_margin(self):
        self.unproved()
        clock = iter([0, 0, outside_land.SCAN_BUDGET_S - 2])
        decider = Decider(self.conn)
        outside_land.tick(self.conn, {'decider': {'timeout_s': 1}}, decide=decider, now=lambda: next(clock, 60))
        self.assertEqual(len(decider.calls), 1)

    def test_a_timeout_past_the_budget_gets_the_time_left(self):
        self.unproved()
        clock = iter([0, 0, 10])
        decider = Decider(self.conn)
        outside_land.tick(self.conn, {'decider': {'timeout_s': 600}}, decide=decider, now=lambda: next(clock, 60))
        self.assertEqual(decider.calls[0].timeout, outside_land.SCAN_BUDGET_S - 10)
        self.assertEqual(len(ops.open_questions(self.conn)), 1)

    def test_git_before_the_decider_counts_against_its_time(self):
        self.unproved()
        spent = {'now': 0.0}
        real = outside_land._described
        def slow(*args):
            spent['now'] = outside_land.SCAN_BUDGET_S - 1
            return real(*args)
        decider = Decider(self.conn)
        with patch.object(outside_land, '_described', side_effect=slow), self.assertLogs('drover.outside_land', 'WARNING'):
            outside_land.tick(self.conn, {}, decide=decider, now=lambda: spent['now'])
        self.assertEqual(decider.calls, [])

    def test_decider_time_counts_against_the_budget(self):
        first, _, _ = self.unproved()
        second = self.task(scope_files=['g.txt'], branch='feat')
        spent = {'now': 0.0}
        decider = Decider(self.conn)
        def slow(*args, **kw):
            spent['now'] = outside_land.SCAN_BUDGET_S
            return decider(*args, **kw)
        with self.assertLogs('drover.outside_land', 'WARNING'):
            outside_land.tick(self.conn, {}, decide=slow, now=lambda: spent['now'])
        self.assertEqual(len(decider.calls), 1)
        self.assertEqual(ops.get_setting(self.conn, outside_land.RESUME_KEY), second)
        self.assertEqual(self.status(first), states.NEEDS_OWNER)


class OwnerAnswerTests(DeciderCase):
    def test_owner_answer_landed_records_the_named_commit(self):
        tid, _, commit = self.unproved()
        self.scan()
        self.answer(f'Landed at {commit}')
        self.assertEqual(self.scan()['landed'], [tid])
        self.assertEqual(self.status(tid), states.LANDED)
        record = self.external(tid)
        self.assertEqual((record['sha'], record['base_ref'], record['review']), (commit, 'main', 'owner-answer'))
        self.assertEqual(ops.events(self.conn, task_id=tid, kind='status')[-1]['actor'], 'owner')

    def test_owner_answer_landed_moves_a_yanked_task(self):
        tid, _, commit = self.unproved('yanked')
        self.scan()
        self.answer(f'Landed at {commit}')
        self.assertEqual(self.scan()['landed'], [tid])
        self.assertEqual(self.status(tid), states.LANDED)
        self.assertEqual(ops.events(self.conn, task_id=tid, kind='status')[-1]['data']['from'], states.YANKED)

    def test_owner_answer_not_landed_abandons_a_yanked_task(self):
        tid, _, _ = self.unproved('yanked')
        self.scan()
        self.answer('Not landed')
        self.assertEqual(self.scan()['landed'], [])
        self.assertEqual(self.status(tid), states.CANCELLED)
        event = ops.events(self.conn, task_id=tid, kind='status')[-1]
        self.assertEqual((event['actor'], event['data']['reason']), ('owner', 'abandoned'))

    def test_owner_answer_not_landed_leaves_a_needs_owner_task(self):
        tid, _, _ = self.unproved()
        self.scan()
        before = ops.get_task(self.conn, tid)
        self.answer('Not landed')
        self.scan()
        self.assertEqual(ops.get_task(self.conn, tid), before)

    def test_free_text_naming_a_commit_is_verified_and_recorded(self):
        tid, _, _ = self.unproved()
        self.scan()
        elsewhere = self.r.commit('elsewhere.txt')
        self.answer(text=f'  landed at {elsewhere} ')
        self.assertEqual(self.scan()['landed'], [tid])
        self.assertEqual(self.external(tid)['sha'], elsewhere)

    def test_other_free_text_leaves_the_task_alone(self):
        tid, _, _ = self.unproved()
        self.scan()
        self.answer(text='ask me next week')
        self.scan()
        self.assertEqual((self.status(tid), ops.open_questions(self.conn)), (states.NEEDS_OWNER, []))

    def refused(self, sha):
        answered = self.question()['id']
        self.answer(f'Landed at {sha}')
        self.scan()
        self.assertEqual(ops.get_question(self.conn, answered)['status'], 'answered')
        return answered

    def test_owner_answer_off_the_base_opens_one_question_saying_why(self):
        tid, tip, commit = self.unproved()
        with patch.object(outside_land, 'ambiguous', return_value=[commit, tip]):
            self.scan()
        answered = self.refused(tip)
        self.assertEqual(self.status(tid), states.NEEDS_OWNER)
        again = self.question()
        self.assertNotEqual(again['id'], answered)
        self.assertIn(tip, again['body'])
        self.assertIn('is not on main', again['body'])
        self.assertEqual(self.labels(again), [f'Landed at {commit}', 'Not landed'])
        self.scan()
        self.assertEqual(len(ops.open_questions(self.conn)), 1)

    def test_free_text_for_a_missing_commit_is_refused(self):
        tid, _, _ = self.unproved()
        self.scan()
        self.answer(text=f'Landed at {"f" * 40}')
        self.scan()
        self.assertEqual(self.status(tid), states.NEEDS_OWNER)
        self.assertIn('not a commit', self.question()['body'])

    def test_free_text_with_a_short_sha_is_refused(self):
        tid, _, commit = self.unproved()
        self.scan()
        self.answer(text=f'Landed at {commit[:8]}')
        self.scan()
        self.assertEqual(self.status(tid), states.NEEDS_OWNER)
        self.assertIn('full', self.question()['body'])

    def test_a_refused_task_can_still_be_answered(self):
        tid, tip, commit = self.unproved()
        with patch.object(outside_land, 'ambiguous', return_value=[commit, tip]):
            self.scan()
        self.refused(tip)
        self.answer(f'Landed at {commit}')
        self.assertEqual(self.scan()['landed'], [tid])

    def test_unknown_verification_is_no_refusal_and_retries(self):
        tid, _, commit = self.unproved()
        self.scan()
        self.answer(f'Landed at {commit}')
        with patch.object(outside_land, '_refusal', side_effect=subprocess.TimeoutExpired('git', 1)), \
                self.assertLogs('drover.outside_land', 'WARNING'):
            self.assertEqual(self.scan()['landed'], [])
        self.assertEqual(ops.open_questions(self.conn), [])
        self.assertEqual(self.scan()['landed'], [tid])

    def test_answer_waits_while_wsl_is_down(self):
        tid, _, commit = self.unproved()
        self.scan()
        self.answer(f'Landed at {commit}')
        with patch.object(placement, 'routes_through_wsl', return_value=True), \
                patch.object(placement, 'wsl_running', return_value=False), patch.object(placement, 'git') as git:
            self.assertEqual(self.scan(), {'scanned': 0, 'landed': []})
        git.assert_not_called()
        self.assertEqual(self.status(tid), states.NEEDS_OWNER)


class AnswerOnceTests(DeciderCase):
    def taken(self, tid):
        return [e['data']['outcome'] for e in ops.events(self.conn, task_id=tid, kind='landing.answer')]

    def test_not_landed_then_later_yank_does_not_cancel(self):
        tid, _, _ = self.unproved()
        self.scan()
        self.answer('Not landed')
        self.scan()
        ops.yank(self.conn, tid)
        self.scan()
        self.scan()
        self.assertEqual(self.status(tid), states.YANKED)
        self.assertEqual(self.taken(tid), ['not-landed'])

    def test_not_landed_with_a_yank_before_the_next_tick_does_not_cancel(self):
        tid, _, _ = self.unproved()
        self.scan()
        self.answer('Not landed')
        ops.yank(self.conn, tid)
        self.scan()
        self.assertEqual(self.status(tid), states.YANKED)
        self.assertEqual(self.taken(tid), ['void'])

    def test_void_answer_for_a_yanked_task_asks_once_and_the_new_answer_counts(self):
        tid, _, _ = self.unproved()
        self.scan()
        first = self.question()['id']
        self.answer('Not landed')
        ops.yank(self.conn, tid)
        self.scan()
        self.scan()
        self.scan()
        again = self.question()
        self.assertNotEqual(again['id'], first)
        self.assertEqual(len(ops.open_questions(self.conn)), 1)
        self.assertIn('not used', again['body'])
        self.answer('Not landed')
        self.scan()
        self.assertEqual(self.status(tid), states.CANCELLED)
        self.assertEqual(self.taken(tid), ['void', 'not-landed'])

    def test_void_answer_for_a_yanked_task_can_still_land_it(self):
        tid, _, commit = self.unproved()
        self.scan()
        self.answer(f'Landed at {commit}')
        ops.yank(self.conn, tid)
        self.scan()
        self.assertEqual(self.status(tid), states.YANKED)
        self.answer(f'Landed at {commit}')
        self.assertEqual(self.scan()['landed'], [tid])

    def test_void_answer_for_a_yanked_task_with_no_candidates_stays_quiet(self):
        tid, _, _ = self.unproved()
        self.scan()
        self.answer('Not landed')
        ops.yank(self.conn, tid)
        with patch.object(outside_land, 'ambiguous', return_value=[]):
            self.scan()
        self.assertEqual((self.status(tid), ops.open_questions(self.conn)), (states.YANKED, []))
        self.assertEqual(self.taken(tid), ['void'])

    def rerun(self, tid):
        ops.set_status(self.conn, tid, states.READY, 'owner', reason='requeued')
        self.assertEqual(ops.claim_next(self.conn, {})['id'], tid)
        ops.set_status(self.conn, tid, states.NEEDS_OWNER, 'worker', reason='stuck again')

    def test_stale_landed_answer_does_not_land_a_rerun_task(self):
        tid, _, commit = self.unproved()
        self.scan()
        question = self.question()['id']
        ops.set_status(self.conn, tid, states.READY, 'owner', reason='requeued')
        self.answer(f'Landed at {commit}')
        self.assertEqual(ops.claim_next(self.conn, {})['id'], tid)
        ops.set_status(self.conn, tid, states.NEEDS_OWNER, 'worker', reason='stuck again')
        self.assertEqual(self.scan()['landed'], [])
        self.assertEqual(self.status(tid), states.NEEDS_OWNER)
        self.assertEqual(self.taken(tid), ['void'])
        self.assertEqual(ops.open_questions(self.conn), [])
        self.assertEqual(ops.get_question(self.conn, question)['status'], 'answered')

    def test_void_answer_naming_a_bad_commit_opens_no_refusal(self):
        tid, _, _ = self.unproved()
        self.scan()
        self.answer(text=f'Landed at {"f" * 40}')
        self.rerun(tid)
        self.scan()
        self.assertEqual((self.taken(tid), ops.open_questions(self.conn)), (['void'], []))

    def test_answer_is_consumed_once_across_ticks(self):
        tid, _, _ = self.unproved()
        self.scan()
        self.answer(text='ask me next week')
        self.scan()
        self.scan()
        self.assertEqual(self.taken(tid), ['no-action'])

    def test_landing_and_its_consumption_commit_together(self):
        tid, _, commit = self.unproved()
        self.scan()
        self.answer(f'Landed at {commit}')
        with patch('drover.ledger.landing.record_merged', side_effect=ValueError('no')), \
                self.assertLogs('drover.outside_land', 'WARNING'):
            self.assertEqual(self.scan()['landed'], [])
        self.assertEqual((self.status(tid), self.taken(tid)), (states.NEEDS_OWNER, []))
        self.assertEqual(self.scan()['landed'], [tid])
        self.assertEqual(self.taken(tid), ['landed'])

    def test_refusal_and_its_consumption_commit_together(self):
        tid, tip, commit = self.unproved()
        with patch.object(outside_land, 'ambiguous', return_value=[commit, tip]):
            self.scan()
        self.answer(f'Landed at {tip}')
        with patch.object(outside_land, '_ask_owner', side_effect=ValueError('no')), \
                self.assertLogs('drover.outside_land', 'WARNING'):
            self.scan()
        self.assertEqual((self.taken(tid), ops.open_questions(self.conn)), ([], []))
        self.scan()
        self.assertEqual(self.taken(tid), ['refused'])
        self.assertEqual(len(ops.open_questions(self.conn)), 1)

    def test_abandon_and_its_consumption_commit_together(self):
        tid, _, _ = self.unproved('yanked')
        self.scan()
        self.answer('Not landed')
        with patch('drover.ledger.ops_bypass.abandon', side_effect=ValueError('no')), \
                self.assertLogs('drover.outside_land', 'WARNING'):
            self.scan()
        self.assertEqual((self.status(tid), self.taken(tid)), (states.YANKED, []))
        self.scan()
        self.assertEqual((self.status(tid), self.taken(tid)), (states.CANCELLED, ['not-landed']))

    def test_a_task_moved_since_it_was_asked_about_loses_its_open_question(self):
        tid, _, _ = self.unproved()
        self.scan()
        ops.yank(self.conn, tid)
        self.scan()
        self.assertEqual((self.status(tid), ops.open_questions(self.conn)), (states.YANKED, []))
        self.scan()
        self.scan()
        self.assertEqual(self.taken(tid), ['void'])
        self.assertEqual(self.status(tid), states.YANKED)
        self.assertEqual(len(ops.open_questions(self.conn)), 1)


class QuestionBodyTests(DeciderCase):
    def test_the_body_names_each_candidate_and_the_options_stay(self):
        _, _, commit = self.unproved()
        self.scan()
        body = self.question()['body']
        self.assertIn(f'{commit[:12]} hand work', body)
        self.assertEqual(self.labels(), [f'Landed at {commit}', 'Not landed'])

    def test_subjects_are_truncated_plain_text(self):
        self.unproved()
        subject = 'tag <b>\u202e' + 'x' * 200
        self.r.commit('g.txt', 'again', msg=subject)
        self.scan()
        body = self.question()['body']
        self.assertNotIn('\u202e', body)
        self.assertNotIn('x' * 100, body)
        self.assertIn('tag <b> xxx', body)
        self.assertIn('...', body)

    def test_a_subject_cannot_forge_a_changed_file(self):
        self.task(scope_files=['f.txt'])
        self.r.commit('unrelated.txt', msg='evil\u2028f.txt\u2028more')
        found = outside_land._log(self.r.repo, 'main', '-1')[0]
        self.assertEqual((found[1], found[2]), ('evil\u2028f.txt\u2028more', ['unrelated.txt']))


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

    def test_every_open_question_of_the_task_retracts(self):
        tid, tip, commit = self.unproved()
        with patch.object(outside_land, 'ambiguous', return_value=[commit, tip]):
            self.scan()
        self.answer(f'Landed at {tip}')
        self.scan()
        self.assertEqual(len(ops.open_questions(self.conn)), 1)
        self.r.git('merge', '-q', '--no-edit', 'feat')
        self.scan()
        self.assertEqual(ops.open_questions(self.conn), [])

    def test_task_settled_elsewhere_closes_its_question(self):
        tid, _, _ = self.unproved('yanked')
        self.scan()
        ops.abandon(self.conn, tid)
        self.scan()
        self.assertEqual(ops.open_questions(self.conn), [])

    def test_retraction_is_not_read_as_an_owner_answer(self):
        tid, _, _ = self.unproved('yanked')
        self.scan()
        self.r.git('merge', '-q', '--no-edit', 'feat')
        self.scan()
        self.scan()
        self.assertEqual(self.status(tid), states.LANDED)


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

    def test_nothing_overlaps(self):
        self.r.commit('other.txt')
        self.assertEqual(self.ambiguous(), [])

    def test_at_most_twenty(self):
        shas = [self.r.commit('f.txt', str(n)) for n in range(25)]
        self.assertEqual(self.ambiguous(), shas[:4:-1])

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
        failed = SimpleNamespace(returncode=128, stdout='', stderr='fatal')
        with patch.object(landing, 'git', return_value=failed), self.assertRaises(outside_land.GitUnknown):
            self.ambiguous()


if __name__ == '__main__':
    unittest.main()
