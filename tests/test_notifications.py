"""Durable notification behavior through the trusted public service seam."""
import tempfile
import unittest
from pathlib import Path

from drover.ledger import ops
from drover.ledger.db import connect
from drover.notifications import Conflict, Notifications


class NotificationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'ledger.db'
        self.conn = connect(self.path)
        self.addCleanup(lambda: self.conn.close())
        self.time = 1000.0
        self.service = Notifications(self.conn, now=lambda: self.time)

    def task(self, role='director:a', **kw):
        kw.setdefault('scope_files', [f"fixture-{ops.get_setting(self.conn, 'seq_T', '0')}"])
        return ops.create_task(self.conn, 'Work', 'repo', initiating_role=role, **kw)

    def test_immediate_completion_survives_restart_for_ten_disjoint_owners(self):
        tasks = {}
        for n in range(10):
            role = f'director:{n}'
            self.service.register(role)
            task = self.task(role, kind='research')
            tasks[role] = task['id']
            ops.event(self.conn, 'task.result-accepted', 'owner', {'result': f'report:{n}'}, task_id=task['id'])
        self.conn.close()
        self.conn = connect(self.path)
        self.service = Notifications(self.conn, now=lambda: self.time)
        self.assertGreater(self.service.project(), 0)
        for role, task_id in tasks.items():
            messages = self.service.receive(role)
            self.assertEqual([(m['task_id'], m['event_class']) for m in messages], [(task_id, 'result-accepted')])
            self.assertEqual(messages, self.service.receive(role))
        self.assertEqual(self.service.project(), 0)

    def test_selectors_union_scope_intersect_filters_and_exclusions_win(self):
        self.service.register('director:a')
        first = self.task(kind='research')
        second = self.task(kind='research')
        excluded = self.task(kind='research')
        implementation = self.task()
        sub = self.service.follow('director:a', {
            'tasks': [first['id']], 'repos': ['repo'], 'types': ['research'],
            'events': ['result-accepted'], 'reasons': ['accepted'],
            'exclude': {'tasks': [excluded['id']]},
        }, mode='immediate')
        # Disable the automatic task subscriptions, so only this selection applies.
        for auto in self.service.health('director:a')['subscriptions']:
            if auto['id'] != sub['id']:
                self.service.tune(auto['id'], auto['revision'], enabled=False)
        for task in (first, second, excluded, implementation):
            ops.event(self.conn, 'task.result-accepted', 'owner', {'reason': 'accepted'}, task_id=task['id'])
        ops.event(self.conn, 'task.result-accepted', 'owner', {'reason': 'ignored'}, task_id=first['id'])
        self.service.project()
        self.assertEqual([m['task_id'] for m in self.service.receive('director:a')], [first['id'], second['id']])
        for selectors in ({'typo': []}, {'events': ['unrecognized']}, {'exclude': {'typo': []}}):
            with self.assertRaises(ValueError):
                self.service.follow('director:a', selectors)
        with self.assertRaises(ValueError):
            self.service.follow('director:a', {'tasks': [first['id']]}, capabilities=['approve'])

    def test_tuning_is_future_only_even_when_projection_is_behind(self):
        from drover.notifications import Conflict
        self.service.register('director:a')
        task = self.task(kind='research')
        sub, = self.service.health('director:a')['subscriptions']
        ops.event(self.conn, 'task.result-accepted', 'owner', {'result': 'before'}, task_id=task['id'])
        tuned = self.service.tune(sub['id'], sub['revision'], preset='quiet')
        self.assertEqual(tuned['revision'], 2)
        self.assertEqual(self.service.tune(sub['id'], sub['revision'], preset='quiet'), tuned)
        with self.assertRaises(Conflict):
            self.service.tune(sub['id'], 1, preset='verbose')
        ops.event(self.conn, 'task.result-accepted', 'owner', {'result': 'after'}, task_id=task['id'])
        self.service.project()
        messages = self.service.receive('director:a')
        self.assertEqual([m['evidence']['result'] for m in messages], ['before'])
        self.assertEqual(messages[0]['subscription_revision'], 1)

    def test_transfer_moves_unprojected_and_offered_work_and_delegation_stays_exclusive(self):
        from drover.notifications import Conflict
        for role in ('director:a', 'director:b', 'director:recovery'):
            self.service.register(role)
        task = self.task(kind='research')
        ops.event(self.conn, 'task.result-accepted', 'owner', {'result': 'unprojected'}, task_id=task['id'])
        ownership = self.service.assign(task['id'], 'director:b', expected_revision=1)
        with self.assertRaises(Conflict):
            self.service.assign(task['id'], 'director:a', expected_revision=1)
        self.service.project()
        self.assertEqual(self.service.receive('director:a'), [])
        before = self.service.receive('director:b')
        self.assertEqual([m['evidence']['result'] for m in before], ['unprojected'])
        self.service.assign(task['id'], 'director:a', expected_revision=ownership['revision'])
        self.assertEqual(self.service.receive('director:b'), [])
        after = self.service.receive('director:a')
        self.assertEqual(after[0]['delivery_id'], before[0]['delivery_id'])
        self.service.assign(task['id'], 'director:recovery', event_class='issue')
        ops.event(self.conn, 'recovery.exhausted', 'system', {'cause': 'incident:1'}, task_id=task['id'])
        self.service.project()
        self.assertEqual([m['event_class'] for m in self.service.receive('director:a')], ['result-accepted'])
        self.assertEqual([m['event_class'] for m in self.service.receive('director:recovery')], ['issue'])
        with self.assertRaises(Conflict):
            self.service.follow('director:b', {'tasks': [task['id']]})
        with self.assertRaises(ValueError):
            self.service.assign(task['id'], 'director:a', event_class='typo')

    def test_personal_digests_are_timed_bounded_and_have_independent_coverage(self):
        for role in ('director:a', 'director:b'):
            self.service.register(role)
            self.task(role)
        a, = self.service.health('director:a')['subscriptions']
        self.service.tune(a['id'], 1, digest_interval=60, detail_limit=2)
        task_id = a['selectors']['tasks'][0]
        for n in range(4):
            ops.event(self.conn, 'decision.recorded', 'worker', {'text': f'decision {n}', 'state': 'committed'}, task_id=task_id)
        self.service.project()
        self.assertIsNone(self.service.digest('director:a'))
        self.time += 60
        digest = self.service.digest('director:a')
        self.assertEqual(digest['counts'], {'progress': 1, 'decision-recorded': 4})
        self.assertEqual(digest['omitted'], 3)
        self.assertLessEqual(len(digest['entries']), 2)
        self.assertEqual(digest['entries'][-1]['evidence']['text'], 'decision 3')
        self.assertFalse(digest['wake'])
        self.assertIsNone(self.service.digest('director:a'))
        self.assertIsNone(self.service.digest('director:b'))
        self.time += 840
        self.assertEqual(self.service.digest('director:b')['counts'], {'progress': 1})
        self.assertIsNone(self.service.digest('director:a'))
        self.assertEqual(self.service.health('director:a')['coverage']['digest']['count'], 5)
        self.assertEqual(self.service.health('director:b')['coverage']['digest']['count'], 1)

    def test_job_profiles_keep_worker_completion_quiet_and_only_accept_final_facts(self):
        self.service.register('director:a')
        research = self.task(kind='research')
        implementation = self.task()
        mechanical = self.task(kind='mechanical')
        for task in (research, implementation, mechanical):
            ops.event(self.conn, 'run-finished', 'worker', {'outcome': 'done'}, task_id=task['id'])
            ops.event(self.conn, 'task.result-accepted', 'owner', {'result': 'report'}, task_id=task['id'])
        from drover.ledger import landing
        for task in (implementation, mechanical):
            ops.approve(self.conn, task['id'])
            from tests.ledger_fixture import stage
            stage(self.conn, task['id'], 'landed')
        self.service.project()
        self.assertEqual([(m['task_id'], m['event_class']) for m in self.service.receive('director:a')],
                         [(research['id'], 'result-accepted'), (implementation['id'], 'landed')])

    def test_offline_routine_queue_compacts_without_acknowledging_or_losing_issues(self):
        self.service.register('director:a', max_pending_routine=3, max_pending_actionable=2)
        task = self.task()
        for n in range(12):
            ops.event(self.conn, 'progress', 'worker', {'step': n}, task_id=task['id'])
        for n in range(5):
            ops.event(self.conn, 'recovery.exhausted', 'system', {'cause': 'same-issue', 'observation': n}, task_id=task['id'])
        self.service.project()
        health = self.service.health('director:a')
        self.assertEqual(health['pending_routine'], 3)
        self.assertEqual(health['pending_actionable'], 1)
        self.assertEqual(health['coverage']['compacted']['count'], 10)
        self.assertEqual(health['acknowledged'], 0)
        issues = self.service.receive('director:a')
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['evidence']['observation'], 4)
        self.assertTrue(health['resync_required'])
        self.time += 900
        digest = self.service.digest('director:a')
        self.assertEqual(digest['compacted']['count'], 10)
        self.assertIsNone(self.service.digest('director:a'))
        ops.event(self.conn, 'recovery.exhausted', 'system', {'cause': 'another-issue'}, task_id=task['id'])
        self.service.project()
        with self.assertRaises(Conflict):
            self.task()
        self.assertTrue(self.service.health('director:a')['admission_held'])
        self.assertTrue(any('Notification storage' in q['title'] for q in ops.open_questions(self.conn)))
        self.service.register('director:b')
        self.assertIsNotNone(self.task('director:b'))

    def test_payload_and_batch_bounds_and_digest_content_remain_readable(self):
        import json
        self.service.register('director:a')
        task = self.task(kind='research')
        sub, = self.service.health('director:a')['subscriptions']
        self.service.tune(sub['id'], 1, preset='verbose')
        for n in range(20):
            ops.event(self.conn, 'progress', 'worker', {'text': 'x' * 100000, 'result': f'report:{n}'}, task_id=task['id'])
        while self.service.project(limit=7):
            pass
        messages = self.service.receive('director:a', limit=2)
        self.assertEqual(len(messages), 2)
        self.assertTrue(messages[-1]['evidence']['truncated'])
        self.assertLessEqual(len(json.dumps(messages[-1]).encode()), 16384)
        for action in (lambda: self.service.receive('director:a', limit=-1),
                       lambda: self.service.receive('director:a', limit=101),
                       lambda: self.service.project(limit=1001)):
            with self.assertRaises(ValueError):
                action()
        self.time += 900
        digest = self.service.digest('director:a')
        self.assertLessEqual(len(json.dumps(digest).encode()), 32768)
        self.assertTrue(any(m['delivery_id'] == digest['digest_id'] for m in self.service.receive('director:a')))

    def test_role_defaults_and_stop_conditions_apply_prospectively(self):
        self.service.register('director:a', notifications={'preset': 'quiet'})
        task = self.task(kind='research')
        ops.event(self.conn, 'task.result-accepted', 'owner', {'result': 'quiet'}, task_id=task['id'])
        sub, = self.service.health('director:a')['subscriptions']
        tuned = self.service.tune(sub['id'], 1, preset='normal', stop_on_completion=True)
        ops.event(self.conn, 'task.result-accepted', 'owner', {'result': 'normal'}, task_id=task['id'])
        ops.event(self.conn, 'progress', 'worker', {'text': 'late'}, task_id=task['id'])
        self.service.project()
        self.assertEqual([m['evidence']['result'] for m in self.service.receive('director:a')], ['normal'])
        self.time += 900
        digest = self.service.digest('director:a')
        self.assertEqual(digest['counts'], {'progress': 1, 'result-accepted': 2})
        self.assertFalse(self.service.health('director:a')['subscriptions'][0]['preferences']['enabled'])

    def test_compacted_pending_work_moves_to_successor_digest(self):
        self.service.register('director:a', max_pending_routine=2)
        self.service.register('director:b')
        task = self.task()
        for n in range(5):
            ops.event(self.conn, 'progress', 'worker', {'step': n}, task_id=task['id'])
        self.service.project()
        self.service.assign(task['id'], 'director:b', expected_revision=1)
        self.time += 900
        self.assertIsNone(self.service.digest('director:a'))
        successor = self.service.digest('director:b')
        self.assertEqual(successor['compacted']['count'], 4)
        self.assertEqual(successor['counts'], {'progress': 2})

    def test_unfollow_preview_and_explicit_history_replay_are_bounded_and_scoped(self):
        self.service.register('director:a')
        task = self.task(kind='research')
        sub, = self.service.health('director:a')['subscriptions']
        self.service.tune(sub['id'], 1, enabled=False)
        source = ops.event(self.conn, 'task.result-accepted', 'owner', {'result': 'retained'}, task_id=task['id'])
        followed = self.service.follow('director:a', {'tasks': [task['id']], 'events': ['result-accepted']}, replay_after=source - 1)
        self.assertEqual([event['event_id'] for event in followed['preview']], [source])
        self.service.project()
        self.assertEqual([m['evidence']['result'] for m in self.service.receive('director:a')], ['retained'])
        again = self.service.follow('director:a', {'events': ['result-accepted'], 'tasks': [task['id']]})
        self.assertEqual(again['id'], followed['id'])

    def test_resolved_issue_stops_offers_and_new_episode_has_a_new_identity(self):
        self.service.register('director:a')
        task = self.task()
        ops.event(self.conn, 'progress.stalled', 'system', {'cause': 'stall:1'}, task_id=task['id'])
        self.service.project()
        old, = self.service.receive('director:a')
        ops.event(self.conn, 'progress.recovered', 'system', {'cause': 'stall:1'}, task_id=task['id'])
        self.service.project()
        self.assertEqual(self.service.receive('director:a'), [])
        self.assertEqual(self.service.health('director:a')['pending_actionable'], 0)
        ops.event(self.conn, 'progress.stalled', 'system', {'cause': 'stall:2'}, task_id=task['id'])
        self.service.project()
        new, = self.service.receive('director:a')
        self.assertNotEqual(new['delivery_id'], old['delivery_id'])

    def test_projection_failure_rolls_back_delivery_and_cursor_together(self):
        self.service.register('director:a')
        task = self.task(kind='research')
        for n in range(2):
            ops.event(self.conn, 'task.result-accepted', 'owner', {'result': f'report:{n}'}, task_id=task['id'])
        calls = 0
        def crashing_clock():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError('simulated process failure')
            return self.time
        self.service.now = crashing_clock
        with self.assertRaises(RuntimeError):
            self.service.project()
        self.service.now = lambda: self.time
        self.assertEqual(self.service.receive('director:a'), [])
        self.assertGreater(self.service.project(), 0)
        self.assertEqual([m['evidence']['result'] for m in self.service.receive('director:a')], ['report:0', 'report:1'])

    def test_failed_initial_enrollment_creates_no_task_or_first_event(self):
        before = ops.events(self.conn)
        with self.assertRaises(ops.NotFound):
            self.task('missing-role')
        self.assertEqual(ops.list_tasks(self.conn), [])
        self.assertEqual(ops.events(self.conn), before)
        with self.assertRaises(ValueError):
            self.task('missing-role', group_id='unsupported-group')
        self.assertEqual(ops.list_tasks(self.conn), [])

    def test_deleting_owned_task_preserves_audit_without_leaving_offers(self):
        self.service.register('director:a', max_pending_actionable=1)
        task = self.task(kind='research')
        ops.event(self.conn, 'task.result-accepted', 'owner', {'result': 'report'}, task_id=task['id'])
        self.service.project()
        ops.event(self.conn, 'progress.stalled', 'system', {'cause': 'stall'}, task_id=task['id'])
        self.service.project()
        self.assertTrue(self.service.health('director:a')['admission_held'])
        ops.delete_task(self.conn, task['id'], confirm=True)
        self.assertFalse(self.service.health('director:a')['admission_held'])
        self.assertEqual(self.service.receive('director:a'), [])
        self.assertEqual(self.service.scoped_snapshot('director:a'), [])

    def test_resolving_storage_pressure_releases_only_affected_role_admission(self):
        self.service.register('director:a', max_pending_actionable=1)
        task = self.task()
        ops.event(self.conn, 'progress.stalled', 'system', {'cause': 'stall'}, task_id=task['id'])
        while self.service.project():
            pass
        self.assertTrue(self.service.health('director:a')['admission_held'])
        self.assertEqual(self.service.health('director:a')['pending_actionable'], 1)
        ops.event(self.conn, 'progress.recovered', 'system', {'cause': 'stall'}, task_id=task['id'])
        self.service.project()
        self.assertFalse(self.service.health('director:a')['admission_held'])
        self.assertIsNotNone(self.task())

    def test_ownership_changes_have_durable_policy_provenance(self):
        self.service.register('director:a')
        self.service.register('director:b')
        task = self.task()
        self.service.assign(task['id'], 'director:b', expected_revision=1)
        changes = ops.events(self.conn, task_id=task['id'], kind='notification.ownership-assigned')
        self.assertEqual([(event['data']['from'], event['data']['to'], event['data']['revision']) for event in changes],
                         [(None, 'director:a', 1), ('director:a', 'director:b', 2)])
        self.assertTrue(all(event['event_class'] is not None for event in
                            self.service.health('director:b')['subscriptions'][0]['preview']))

    def test_replaying_old_recovery_cannot_resolve_a_newer_issue_episode(self):
        self.service.register('director:a')
        task = self.task()
        sub, = self.service.health('director:a')['subscriptions']
        self.service.tune(sub['id'], sub['revision'], mode='muted')
        ops.event(self.conn, 'progress.stalled', 'system', {'cause': 'same-cause'}, task_id=task['id'])
        recovered = ops.event(self.conn, 'progress.recovered', 'system', {'cause': 'same-cause'}, task_id=task['id'])
        self.service.project()
        ops.event(self.conn, 'progress.stalled', 'system', {'cause': 'same-cause'}, task_id=task['id'])
        self.service.project()
        current, = self.service.receive('director:a')
        self.service.follow('director:a', {'tasks': [task['id']], 'events': ['recovered']},
                            mode='digest', replay_after=recovered - 1)
        self.service.project()
        self.assertEqual(self.service.receive('director:a'), [current])
        self.assertEqual(self.service.health('director:a')['pending_actionable'], 1)

    def test_transferring_issues_releases_previous_owner_storage_hold(self):
        self.service.register('director:a', max_pending_actionable=1)
        self.service.register('director:b', max_pending_actionable=10)
        task = self.task()
        ops.event(self.conn, 'progress.stalled', 'system', {'cause': 'stall'}, task_id=task['id'])
        self.service.project()
        self.assertTrue(self.service.health('director:a')['admission_held'])
        self.service.assign(task['id'], 'director:b', expected_revision=1)
        self.assertFalse(self.service.health('director:a')['admission_held'])
        self.assertFalse(any('Notification storage' in q['title'] for q in ops.open_questions(self.conn)))
        self.assertEqual(self.service.health('director:a')['pending_actionable'], 0)
        self.assertEqual(self.service.health('director:b')['pending_actionable'], 1)
        self.assertIsNotNone(self.task('director:a'))

    def test_offline_digest_backlog_shares_routine_cap_and_preserves_resync_audit(self):
        self.service.register('director:a', max_pending_routine=2)
        task = self.task()
        generated = []
        for n in range(10):
            ops.event(self.conn, 'progress', 'worker', {'step': n}, task_id=task['id'])
            self.service.project()
            self.time += 900
            generated.append(self.service.digest('director:a'))
        messages = self.service.receive('director:a')
        self.assertLessEqual(len(messages), 2)
        health = self.service.health('director:a')
        self.assertLessEqual(health['pending_routine'] + health['pending_digests'], 2)
        self.assertEqual(health['acknowledged'], 0)
        self.assertEqual(health['coverage']['digest-compacted']['count'], 8)
        self.assertTrue(messages[-1]['resync_required'])
        archived = ops.events(self.conn, kind='notification.digest-compacted')
        self.assertEqual([e['data']['digest_id'] for e in archived], [d['digest_id'] for d in generated[:8]])
        self.assertEqual(archived[0]['data']['body']['entries'], generated[0]['entries'])

    def test_overlapping_follows_use_specific_preferences_and_union_event_filters(self):
        self.service.register('director:a')
        immediate = self.task()
        muted = self.task()
        self.service.follow('director:a', {'tasks': [immediate['id']], 'events': ['progress']}, mode='immediate')
        self.service.follow('director:a', {'tasks': [muted['id']], 'events': ['progress']}, mode='muted')
        self.service.follow('director:a', {'repos': ['repo']}, preset='quiet')
        ops.event(self.conn, 'progress', 'worker', {'selected': 'explicit-immediate'}, task_id=immediate['id'])
        self.service.follow('director:a', {'repos': ['repo'], 'events': ['progress']}, mode='immediate')
        ops.event(self.conn, 'progress', 'worker', {'selected': 'explicit-mute'}, task_id=muted['id'])
        # Two equally scoped, disjoint filters select a union of facts.
        self.service.follow('director:a', {'tasks': [immediate['id']], 'events': ['decision-recorded']}, mode='immediate')
        ops.event(self.conn, 'decision.recorded', 'worker', {'selected': 'second-filter'}, task_id=immediate['id'])
        ops.event(self.conn, 'progress', 'worker', {'selected': 'first-filter'}, task_id=immediate['id'])
        self.service.project()
        self.assertEqual([m['evidence'].get('selected') for m in self.service.receive('director:a')],
                         ['explicit-immediate', 'second-filter', 'first-filter'])
        # Equally specific explicit modes merge deterministically: mute wins.
        self.service.follow('director:a', {'tasks': [immediate['id']], 'events': ['progress']}, mode='muted')
        ops.event(self.conn, 'progress', 'worker', {'selected': 'equal-scope-mute'}, task_id=immediate['id'])
        self.service.project()
        self.assertEqual([m['evidence'].get('selected') for m in self.service.receive('director:a')],
                         ['explicit-immediate', 'second-filter', 'first-filter'])

    def test_unicode_payloads_obey_total_serialized_envelope_and_digest_byte_limits(self):
        import json
        role = '😀' * 256
        self.service.register(role)
        task = self.task(role, kind='research')
        data = {key: '😀' * 512 for key in
                ('text', 'decision', 'state', 'result', 'reason', 'cause', 'question', 'commit', 'sha', 'report')}
        event_id = ops.event(self.conn, 'task.result-accepted', '😀' * 256, data, task_id=task['id'])
        self.service.project()
        message, = self.service.receive(role)
        self.assertLessEqual(len(json.dumps(message).encode('utf-8')), 16384)
        self.assertTrue(message['evidence']['truncated'])
        self.assertEqual(message['evidence']['source_event_id'], event_id)
        self.assertEqual(ops.events(self.conn, after=event_id - 1, limit=1)[0]['data']['report'], '😀' * 512)
        self.time += 900
        self.assertLessEqual(len(json.dumps(self.service.digest(role)).encode('utf-8')), 32768)


if __name__ == '__main__':
    unittest.main()
