"""Accepted joins through Notifications and the real ledger acceptance seams."""
import tempfile
import unittest
from pathlib import Path

from drover.ledger import ops
from tests.ledger_fixture import claim as fixture_claim, stage as fixture_stage
from drover.ledger.db import connect
from drover.notifications import Conflict, Notifications


class NotificationGroupsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'ledger.db'
        self.conn = connect(self.path)
        self.addCleanup(lambda: self.conn.close())
        self.now = 1000
        self.service = Notifications(self.conn, now=lambda: self.now)
        self.service.register('director:a')

    def task(self, **kw):
        return ops.create_task(self.conn, 'Research', 'repo', kind='research', initiating_role='director:a', **kw)

    def accept(self, task):
        ops.approve(self.conn, task['id'])
        fixture_claim(self.conn, task['id'])
        run = ops.start_run(self.conn, task['id'], 'worker', 'local')
        ops.finish_run(self.conn, run, 'read-only-done', note='Accepted findings')
        ops.set_status(self.conn, task['id'], 'needs-owner')
        ops.accept_read_only(self.conn, task['id'])

    def project(self):
        while self.service.project():
            pass

    def test_completed_before_seal_has_one_group_identity_and_immutable_manifest(self):
        parent = self.task()
        group = self.service.create_group('G-research', 'director:a', parent_id=parent['id'])
        child = self.task(group_id=group['group_id'], parent_id=parent['id'])
        self.accept(child)
        self.assertFalse(self.service.get_group(group['group_id'])['ready'])
        current = self.service.get_group(group['group_id'])
        ready = self.service.seal(group['group_id'], current['revision'], 'seal')
        self.assertTrue(ready['ready'])
        self.assertEqual(ready['manifest'][0]['task_id'], child['id'])
        self.assertEqual(ready['manifest'][0]['kind'], 'read-only')
        self.assertTrue(ready['manifest'][0]['result_hash'])
        self.assertEqual(ops.get_task(self.conn, parent['id'])['status'], 'proposed')
        self.project()
        messages = self.service.receive('director:a')
        self.assertEqual([(m['task_id'], m['group_id'], m['event_class']) for m in messages],
                         [(None, 'G-research', 'group-ready')])
        self.assertEqual(self.service.seal(group['group_id'], current['revision'], 'seal'), ready)
        ops.event(self.conn, 'comment', 'worker', {'text': 'later evidence'}, task_id=child['id'])
        self.assertEqual(self.service.get_group(group['group_id'])['manifest'], ready['manifest'])
        with self.assertRaises(Conflict):
            self.service.enroll(group['group_id'], parent['id'], ready['revision'], 'late-child')

    def test_parentless_group_cross_director_results_are_scoped_and_acknowledgeable(self):
        self.service.register('director:b')
        group = self.service.create_group('G-independent', 'director:a')
        child = ops.create_task(self.conn, 'Other findings', 'repo', kind='research', initiating_role='director:b', scope_files=['fixture-63'])
        group = self.service.enroll(group['group_id'], child['id'], group['revision'], 'enroll')
        self.service.seal(group['group_id'], group['revision'], 'seal')
        self.accept(child)
        self.project()
        message, = self.service.receive('director:a')
        self.assertEqual((message['task_id'], message['group_id']), (None, 'G-independent'))
        self.assertEqual(message['evidence']['manifest'][0]['task_id'], child['id'])
        self.assertTrue(all(m['group_id'] is None for m in self.service.receive('director:b')))
        self.assertNotIn(child['id'], [t['task_id'] for t in self.service.scoped_snapshot('director:a')])
        self.service.issue_session('director:a', 's')
        lease = self.service.acquire('director:a', 's')
        self.service.commit('director:a', lease['lease_token'], {}, [message['delivery_id']], [], 'ack', release=True)
        self.assertFalse(self.service.role_state('director:a')['pending_wake'])
        self.assertEqual(self.service.receive('director:a'), [])
        self.now += 900
        digest = self.service.digest('director:a')
        self.assertEqual([e['group_id'] for e in digest['entries'] if e['event_class'] == 'group-ready'], ['G-independent'])
        self.assertEqual(self.service.receive('director:a')[0]['entries'][0]['group_id'], 'G-independent')

    def test_combined_cycles_rejected_in_both_orders_and_navigation_stays_inert(self):
        parent, child = self.task(), self.task()
        self.assertEqual(ops.get_task(self.conn, self.task(parent_id=child['id'])['id'])['status'], 'proposed')
        ops.add_dep(self.conn, child['id'], parent['id'])
        group = self.service.create_group('G-first-dep', 'director:a', parent_id=parent['id'])
        with self.assertRaisesRegex(ValueError, 'cycle'):
            self.service.enroll(group['group_id'], child['id'], 1, 'cycle')
        parent2, child2 = self.task(), self.task()
        group2 = self.service.create_group('G-first-group', 'director:a', parent_id=parent2['id'])
        self.service.enroll(group2['group_id'], child2['id'], 1, 'enroll')
        with self.assertRaisesRegex(ValueError, 'cycle'):
            ops.add_dep(self.conn, child2['id'], parent2['id'])
        # A path through a second group also participates in the same graph.
        group3 = self.service.create_group('G-third', 'director:a', parent_id=child2['id'])
        other = self.task()
        self.service.enroll(group3['group_id'], other['id'], 1, 'enroll')
        with self.assertRaisesRegex(ValueError, 'cycle'):
            ops.add_dep(self.conn, other['id'], parent2['id'])
        with self.assertRaisesRegex(ValueError, 'unsupported group lifecycle'):
            self.service.create_group('G-collection', 'director:a', mode='collection')

    def test_empty_open_failed_cancelled_or_forged_success_cannot_complete(self):
        group = self.service.create_group('G-bad', 'director:a')
        with self.assertRaises(ValueError):
            self.service.seal(group['group_id'], 1, 'empty')
        child = self.task(group_id='G-bad')
        ops.event(self.conn, 'task.result-accepted', 'owner', {'result': 'forged'}, task_id=child['id'])
        group = self.service.get_group('G-bad')
        self.service.seal('G-bad', group['revision'], 'seal')
        self.assertFalse(self.service.get_group('G-bad')['ready'])
        ops.approve(self.conn, child['id'])
        fixture_claim(self.conn, child['id'])
        ops.set_status(self.conn, child['id'], 'failed')
        self.assertIn('child-failed', self.service.get_group('G-bad')['failure_reason'])
        ops.set_status(self.conn, child['id'], 'cancelled')
        self.assertIn('child-cancelled', self.service.get_group('G-bad')['failure_reason'])
        self.project()
        messages = self.service.receive('director:a')
        self.assertEqual(len([m for m in messages if m['group_id'] == 'G-bad' and m['event_class'] == 'issue']), 1)
        self.assertFalse(any(m['event_class'] == 'group-ready' for m in messages))

    def test_merged_child_joins_only_after_actual_receipt_and_restart_keeps_one_completion(self):
        from drover.ledger import landing
        group = self.service.create_group('G-merged', 'director:a')
        child = ops.create_task(self.conn, 'Change', 'repo', initiating_role='director:a', group_id='G-merged', scope_files=['fixture-127'])
        group = self.service.get_group('G-merged')
        self.service.seal('G-merged', group['revision'], 'seal')
        ops.approve(self.conn, child['id'])
        fixture_stage(self.conn, child['id'], 'reviewed')
        self.assertFalse(self.service.get_group('G-merged')['ready'])
        ops.event(self.conn, 'task.result-accepted', 'owner', {'commit': 'fake'}, task_id=child['id'])
        self.assertFalse(self.service.get_group('G-merged')['ready'])
        fixture_stage(self.conn, child['id'], 'landed')
        ready = self.service.get_group('G-merged')
        self.assertTrue(ready['ready'])
        self.assertEqual(ready['manifest'][0]['commit'], 'b' * 40)
        self.conn.close()
        self.conn = connect(self.path)
        self.service = Notifications(self.conn, now=lambda: self.now)
        landing.record_merged(self.conn, dict(child, last_sha='different-sha'), 'different-tip', 'different.log')
        self.assertEqual(self.service.get_group('G-merged')['manifest'], ready['manifest'])
        self.project()
        self.assertEqual(len([m for m in self.service.receive('director:a') if m['event_class'] == 'group-ready']), 1)

    def test_group_and_descendant_selectors_obey_task_precedence_and_current_ownership(self):
        parent = self.task()
        group = self.service.create_group('G-select', 'director:a', parent_id=parent['id'])
        child = self.task(group_id='G-select', parent_id=parent['id'])
        for sub in self.service.health('director:a')['subscriptions']:
            if sub['selectors'] == {'tasks': [child['id']]}:
                self.service.tune(sub['id'], sub['revision'], enabled=False)
        self.service.follow('director:a', {'groups': ['G-select'], 'events': ['progress']}, mode='immediate')
        self.service.follow('director:a', {'descendants': [parent['id']], 'events': ['decision-recorded']}, mode='immediate')
        # The auto task profile loses to equally scoped explicit task preferences.
        self.service.follow('director:a', {'tasks': [child['id']], 'events': ['progress']}, mode='muted')
        ops.event(self.conn, 'progress', 'worker', {'text': 'muted'}, task_id=child['id'])
        ops.event(self.conn, 'decision.recorded', 'worker', {'text': 'selected'}, task_id=child['id'])
        self.project()
        self.assertEqual([m['evidence']['text'] for m in self.service.receive('director:a')], ['selected'])

    def test_group_ready_replay_uses_group_scope_and_never_child_ownership(self):
        group = self.service.create_group('G-replay', 'director:a')
        child = self.task(group_id='G-replay')
        self.accept(child)
        for sub in self.service.health('director:a')['subscriptions']:
            if sub['selectors'] == {'groups': ['G-replay']}:
                self.service.tune(sub['id'], sub['revision'], enabled=False)
        group = self.service.get_group('G-replay')
        ready = self.service.seal('G-replay', group['revision'], 'seal')
        self.project()
        self.assertEqual(self.service.receive('director:a'), [])
        replay = self.service.follow('director:a', {'groups': ['G-replay'], 'events': ['group-ready']},
                                     mode='immediate', replay_after=ready['completion_event'] - 1)
        self.assertEqual(replay['preview'], [dict(event_id=ready['completion_event'], event_class='group-ready')])
        self.project()
        message, = self.service.receive('director:a')
        self.assertEqual(message['group_id'], 'G-replay')
        self.assertIsNone(message['task_id'])

    def test_concurrent_acceptances_commit_one_completion_across_restart(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        group = self.service.create_group('G-concurrent', 'director:a')
        children = [self.task(group_id='G-concurrent') for _ in range(2)]
        group = self.service.get_group('G-concurrent')
        self.service.seal('G-concurrent', group['revision'], 'seal')
        for child in children:
            ops.approve(self.conn, child['id'])
            fixture_claim(self.conn, child['id'])
            run = ops.start_run(self.conn, child['id'], 'worker', 'local')
            ops.finish_run(self.conn, run, 'read-only-done', note='Findings')
            ops.set_status(self.conn, child['id'], 'needs-owner')
        barrier = Barrier(2)
        def accept_child(child):
            conn = connect(self.path)
            try:
                barrier.wait(timeout=5)
                ops.accept_read_only(conn, child['id'])
            finally:
                conn.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(accept_child, children))
        ready = self.service.get_group('G-concurrent')
        self.assertEqual(len(ready['manifest']), 2)
        self.assertEqual(len(ops.events(self.conn, kind='group.ready')), 1)
        self.conn.close()
        self.conn = connect(self.path)
        self.service = Notifications(self.conn, now=lambda: self.now)
        self.project()
        self.assertEqual(len([m for m in self.service.receive('director:a') if m['event_class'] == 'group-ready']), 1)
        self.assertEqual(self.service.get_group('G-concurrent')['manifest'], ready['manifest'])

    def test_synthesis_parent_becomes_dispatch_eligible_only_after_join_without_completion(self):
        parent = self.task()
        ops.approve(self.conn, parent['id'])
        group = self.service.create_group('G-parent', 'director:a', parent_id=parent['id'])
        child = self.task(group_id='G-parent')
        group = self.service.get_group('G-parent')
        self.service.seal('G-parent', group['revision'], 'seal')
        self.assertNotIn(parent['id'], [t['id'] for t in ops.ready_tasks(self.conn)])
        self.assertIsNone(ops.claim_next(self.conn))
        self.assertEqual(self.service.get_group('G-parent')['readiness_reason'], 'awaiting-accepted-results')
        self.accept(child)
        self.assertIn(parent['id'], [t['id'] for t in ops.ready_tasks(self.conn)])
        self.assertEqual(ops.get_task(self.conn, parent['id'])['status'], 'ready')
        self.assertIsNotNone(ops.get_task(self.conn, parent['id'])['approved_at'])
        self.assertEqual(ops.claim_next(self.conn)['id'], parent['id'])

    def test_foreign_group_does_not_suppress_child_directors_ordinary_completion(self):
        self.service.register('director:b')
        group = self.service.create_group('G-foreign', 'director:a')
        child = ops.create_task(self.conn, 'Findings', 'repo', kind='research', initiating_role='director:b', scope_files=['fixture-235'])
        self.service.enroll('G-foreign', child['id'], group['revision'], 'enroll')
        self.accept(child)
        self.project()
        self.assertEqual([(m['task_id'], m['event_class']) for m in self.service.receive('director:b')], [(child['id'], 'landed')])
        self.assertEqual(self.service.receive('director:a'), [])

    def test_group_issue_updates_fence_ack_and_can_be_held_without_a_task_identity(self):
        group = self.service.create_group('G-issue', 'director:a')
        child = self.task(group_id='G-issue')
        group = self.service.get_group('G-issue')
        self.service.seal('G-issue', group['revision'], 'seal')
        ops.approve(self.conn, child['id'])
        fixture_claim(self.conn, child['id'])
        ops.set_status(self.conn, child['id'], 'needs-owner')
        self.project()
        issue, = [m for m in self.service.receive('director:a') if m['group_id'] == 'G-issue']
        self.service.issue_session('director:a', 's')
        token = self.service.acquire('director:a', 's')['lease_token']
        self.service.claim_issue('director:a', issue['delivery_id'], token)
        held = self.service.hold_issue('director:a', issue['delivery_id'], token, 'Owner must replace cancelled membership')
        self.assertIsNotNone(held['question_id'])
        self.assertEqual([q for q in ops.open_questions(self.conn) if q['id'] == held['question_id']][0]['target_kind'], 'group')
        ops.set_status(self.conn, child['id'], 'cancelled')
        with self.assertRaises(Conflict):
            self.service.commit('director:a', token, {}, [issue['delivery_id']], [], 'stale',
                                delivery_versions={issue['delivery_id']: issue['message_version']})
        self.project()
        updated, = [m for m in self.service.receive('director:a') if m['group_id'] == 'G-issue']
        self.assertEqual(updated['delivery_id'], issue['delivery_id'])
        self.assertIn('child-cancelled', updated['reason'])

    def test_group_submission_is_atomic_and_request_keys_and_revisions_are_fenced(self):
        group = self.service.create_group('G-atomic', 'director:a')
        child = self.task()
        enrolled = self.service.enroll('G-atomic', child['id'], 1, 'enroll')
        self.assertEqual(self.service.enroll('G-atomic', child['id'], 1, 'enroll'), enrolled)
        with self.assertRaises(Conflict):
            self.service.enroll('G-atomic', child['id'], 2, 'enroll')
        with self.assertRaises(Conflict):
            self.service.seal('G-atomic', 1, 'stale')
        self.service.seal('G-atomic', enrolled['revision'], 'seal')
        before = (ops.list_tasks(self.conn), ops.events(self.conn))
        with self.assertRaises(Conflict):
            self.task(group_id='G-atomic')
        self.assertEqual((ops.list_tasks(self.conn), ops.events(self.conn)), before)
        ops.delete_task(self.conn, child['id'], confirm=True)
        self.assertIn('child-deleted', self.service.get_group('G-atomic')['failure_reason'])
        self.service.health('director:a')  # Historical deleted-task previews remain readable.

    def test_group_stop_condition_waits_for_group_completion_instead_of_the_first_child(self):
        group = self.service.create_group('G-stop-follow', 'director:a')
        child = self.task(group_id='G-stop-follow')
        for sub in self.service.health('director:a')['subscriptions']:
            if sub['selectors'] == {'tasks': [child['id']]}:
                self.service.tune(sub['id'], sub['revision'], enabled=False)
            if sub['selectors'] == {'groups': ['G-stop-follow']}:
                self.service.tune(sub['id'], sub['revision'], stop_on_completion=True)
        group = self.service.get_group('G-stop-follow')
        self.service.seal('G-stop-follow', group['revision'], 'seal')
        self.accept(child)
        self.project()
        self.assertEqual([m['event_class'] for m in self.service.receive('director:a')], ['group-ready'])
        group_follow, = [s for s in self.service.health('director:a')['subscriptions'] if s['selectors'] == {'groups': ['G-stop-follow']}]
        self.assertFalse(group_follow['preferences']['enabled'])

    def test_retryable_failure_waits_and_explicit_refusal_issue_clears_on_recovery(self):
        group = self.service.create_group('G-retry', 'director:a')
        child = self.task(group_id='G-retry')
        group = self.service.get_group('G-retry')
        self.service.seal('G-retry', group['revision'], 'seal')
        ops.approve(self.conn, child['id'])
        fixture_claim(self.conn, child['id'])
        ops.set_status(self.conn, child['id'], 'failed')
        self.project()
        self.assertEqual(self.service.receive('director:a'), [])
        self.assertIn('child-failed', self.service.get_group('G-retry')['failure_reason'])
        ops.event(self.conn, 'recovery.exhausted', 'system', {'cause': 'exhausted:1'}, task_id=child['id'])
        self.project()
        self.assertEqual(len([m for m in self.service.receive('director:a') if m['group_id'] == 'G-retry']), 1)
        ops.event(self.conn, 'recovery.resolved', 'system', {'cause': 'exhausted:1'}, task_id=child['id'])
        self.project()
        self.assertEqual(self.service.receive('director:a'), [])
        self.assertFalse(self.service.get_group('G-retry')['ready'])
        self.assertIn('child-failed', self.service.get_group('G-retry')['failure_reason'])
        ops.set_status(self.conn, child['id'], 'ready')
        self.assertIsNone(self.service.get_group('G-retry')['failure_reason'])
        self.assertEqual(self.service.get_group('G-retry')['readiness_reason'], 'awaiting-accepted-results')

    def test_read_only_acceptance_freezes_evidence_across_later_seals_and_enrollment(self):
        child = self.task()
        for name in ('G-proof-a', 'G-proof-b'):
            self.service.create_group(name, 'director:a')
            self.service.enroll(name, child['id'], 1, 'enroll')
        self.accept(child)
        first = self.service.seal('G-proof-a', 2, 'seal')['manifest'][0]
        run = ops.list_runs(self.conn, child['id'])[-1]
        ops.amend_run(self.conn, run['id'], note='Findings changed after owner acceptance')
        second = self.service.seal('G-proof-b', 2, 'seal')['manifest'][0]
        self.assertEqual(second, first)
        self.service.create_group('G-proof-late', 'director:a')
        self.service.enroll('G-proof-late', child['id'], 1, 'enroll')
        late = self.service.seal('G-proof-late', 2, 'seal')['manifest'][0]
        self.assertEqual(late, first)
        accepted = [e for e in ops.events(self.conn, task_id=child['id'], kind='status') if e['data']['to'] == 'landed'][0]
        self.assertEqual(accepted['data']['read_only_receipt']['evidence']['note'], 'Accepted findings')
        self.assertEqual(accepted['data']['read_only_receipt']['result_hash'], first['result_hash'])

    def test_amended_historical_read_only_evidence_fails_closed_with_retained_reason(self):
        child = self.task()
        ops.approve(self.conn, child['id'])
        fixture_claim(self.conn, child['id'])
        run = ops.start_run(self.conn, child['id'], 'worker', 'local')
        ops.finish_run(self.conn, run, 'read-only-done', note='Historical accepted findings')
        ops.set_status(self.conn, child['id'], 'needs-owner')
        # A pre-receipt acceptance fixture: its historical event/state pair has
        # no immutable evidence snapshot, so later amendments are ambiguous.
        self.accept_historical_read_only(child, run)
        self.service.create_group('G-historical-before', 'director:a')
        self.service.enroll('G-historical-before', child['id'], 1, 'enroll')
        self.assertTrue(self.service.seal('G-historical-before', 2, 'seal')['ready'])
        ops.amend_run(self.conn, run, note='Unaccepted amendment')
        self.service.create_group('G-historical-after', 'director:a')
        self.service.enroll('G-historical-after', child['id'], 1, 'enroll')
        sealed = self.service.seal('G-historical-after', 2, 'seal')
        self.assertFalse(sealed['ready'])
        self.assertIn('accepted-evidence-ambiguous', sealed['failure_reason'])
        self.assertIsNone(sealed['manifest'])

    def accept_historical_read_only(self, child, run):
        # Historical fixture construction only: existing V11 databases contain
        # this state/event pair, whose public join behavior must remain safe.
        self.conn.execute("UPDATE tasks SET status='landed' WHERE id=?", (child['id'],))
        ops.event(self.conn, 'status', 'owner', {'from': 'needs-owner', 'to': 'landed', 'read_only': True, 'run': run}, task_id=child['id'])
