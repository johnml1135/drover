"""Director identity and processing through the trusted notification service."""
import tempfile
import unittest
from pathlib import Path

from drover.ledger import actions, ops
from drover.ledger.db import connect
from drover.notifications import Conflict, Notifications


class DirectorRolesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'ledger.db'
        self.conn = connect(self.path)
        self.addCleanup(lambda: self.conn.close())
        self.time = 1000.0
        self.service = Notifications(self.conn, now=lambda: self.time)
        ops.set_setting(self.conn, 'dispatch_enabled', 'true')
        self.service.register('director:a')

    def test_session_credentials_expire_and_replacement_revokes_old_authority(self):
        session = self.service.issue_session('director:a', 'session:1', ttl=30)
        identity = self.service.authenticate(session['credential'])
        self.assertEqual((identity['role_id'], identity['session_id'], identity['generation']),
                         ('director:a', 'session:1', 1))
        self.assertNotIn('credential', identity)
        self.assertNotIn(session['credential'], str(self.service.health('director:a')))
        self.time += 30
        with self.assertRaises(PermissionError):
            self.service.authenticate(session['credential'])
        fresh = self.service.issue_session('director:a', 'session:2')
        self.assertEqual(fresh['generation'], 2)
        with self.assertRaises(PermissionError):
            self.service.authenticate(session['credential'])
        self.assertEqual(self.service.authenticate(fresh['credential'])['session_id'], 'session:2')

    def lease(self, role='director:a', session='session:1'):
        self.service.issue_session(role, session)
        return self.service.acquire(role, session)['lease_token']

    def test_one_execution_lease_and_atomic_processing_replay_including_empty_inbox(self):
        token = self.lease()
        with self.assertRaises(Conflict):
            self.service.acquire('director:a', 'session:1')
        receipt = self.service.commit('director:a', token, {'next': 'collect'}, [], [], 'process:1')
        self.assertEqual(receipt['checkpoint_revision'], 1)
        self.assertEqual(self.service.commit('director:a', token, {'next': 'collect'}, [], [], 'process:1'), receipt)
        self.assertEqual(len(ops.events(self.conn, kind='director.checkpoint')), 1)
        with self.assertRaises(Conflict):
            self.service.commit('director:a', token, {'next': 'changed'}, [], [], 'process:1')
        self.assertEqual(self.service.role_state('director:a')['checkpoint'], {'next': 'collect'})
        self.time += 61
        with self.assertRaises(PermissionError):
            self.service.commit('director:a', token, {}, [], [], 'expired')

    def test_processing_acknowledges_only_current_owned_deliveries(self):
        self.service.register('director:b')
        task = ops.create_task(self.conn, 'Research', 'repo', kind='research', initiating_role='director:a')
        ops.event(self.conn, 'task.result-accepted', 'owner', {'result': 'report'}, task_id=task['id'])
        self.service.project()
        message, = self.service.receive('director:a')
        token = self.lease()
        self.service.assign(task['id'], 'director:b', expected_revision=1)
        with self.assertRaises(PermissionError):
            self.service.commit('director:a', token, {}, [message['delivery_id']], [], 'stale-owner')
        token_b = self.lease('director:b', 'session:2')
        receipt = self.service.commit('director:b', token_b, {'done': True}, [message['delivery_id']], [], 'handled')
        self.assertEqual(receipt['delivery_ids'], [message['delivery_id']])
        self.assertEqual(self.service.receive('director:b'), [])
        self.assertEqual(self.service.health('director:b')['acknowledged'], 1)

    def issue(self, cause='incident:1'):
        task = ops.create_task(self.conn, 'Work', 'repo', initiating_role='director:a')
        ops.approve(self.conn, task['id'])
        ops.event(self.conn, 'recovery.exhausted', 'system', {'cause': cause}, task_id=task['id'])
        self.service.project()
        message, = self.service.receive('director:a')
        return task, message

    def test_issue_claim_is_global_and_transfer_revokes_live_old_handler(self):
        task, message = self.issue()
        token = self.lease()
        claim = self.service.claim_issue('director:a', message['delivery_id'], token)
        with self.assertRaises(Conflict):
            self.service.claim_issue('director:a', message['delivery_id'], token)
        self.service.register('director:b')
        self.service.assign(task['id'], 'director:b', expected_revision=1)
        new_token = self.lease('director:b', 'session:2')
        successor = self.service.claim_issue('director:b', message['delivery_id'], new_token)
        self.assertNotEqual(successor['issue_token'], claim['issue_token'])
        with self.assertRaises(PermissionError):
            self.service.hold_issue('director:a', message['delivery_id'], token, 'unavailable')

    def test_muted_unresolved_issue_is_retained_and_fallback_is_deduplicated(self):
        task = ops.create_task(self.conn, 'Work', 'repo', initiating_role='director:a')
        sub, = self.service.health('director:a')['subscriptions']
        self.service.tune(sub['id'], 1, mode='muted')
        ops.event(self.conn, 'recovery.exhausted', 'system', {'cause': 'incident'}, task_id=task['id'])
        self.service.project()
        issue, = self.service.receive('director:a')
        self.assertFalse(self.service.role_state('director:a')['pending_wake'])
        token = self.lease()
        first = self.service.hold_issue('director:a', issue['delivery_id'], token, 'unsupported-resume')
        again = self.service.hold_issue('director:a', issue['delivery_id'], token, 'unsupported-resume')
        self.assertEqual(first['question_id'], again['question_id'])
        self.assertEqual(len(ops.open_questions(self.conn)), 1)
        self.assertEqual(self.service.health('director:a')['pending_actionable'], 1)

    def test_arrival_during_live_run_and_atomic_release_preserve_pending_wake(self):
        task = ops.create_task(self.conn, 'Research', 'repo', kind='research', initiating_role='director:a')
        token = self.lease()
        self.assertFalse(self.service.role_state('director:a')['pending_wake'])
        event = ops.event(self.conn, 'task.result-accepted', 'owner', {'result': 'arrived-during-run'}, task_id=task['id'])
        live = self.service.role_state('director:a')
        self.assertTrue(live['pending_wake'])
        self.assertEqual(live['wake_position'], event)
        self.service.commit('director:a', token, {}, [], [], 'release', release=True)
        self.assertTrue(self.service.role_state('director:a')['pending_wake'])
        token = self.service.acquire('director:a', 'session:1')['lease_token']
        self.service.project()
        message, = self.service.receive('director:a')
        self.service.commit('director:a', token, {}, [message['delivery_id']], [], 'handled', release=True)
        self.assertFalse(self.service.role_state('director:a')['pending_wake'])
        self.assertEqual(self.service.role_state('director:a')['wake_position'], event)
        later = ops.event(self.conn, 'task.result-accepted', 'owner', {'result': 'after-release'}, task_id=task['id'])
        self.assertTrue(self.service.role_state('director:a')['pending_wake'])
        self.assertEqual(self.service.role_state('director:a')['wake_position'], later)

    def test_checkpoint_continuation_survives_actionless_release(self):
        token = self.lease()
        self.service.commit('director:a', token, {'continuation': True}, [], [], 'continue', release=True)
        self.assertTrue(self.service.role_state('director:a')['pending_wake'])

    def test_lost_release_response_replays_original_receipt_after_lease_is_gone(self):
        token = self.lease()
        receipt = self.service.commit('director:a', token, {'saved': True}, [], [], 'release-once', release=True)
        self.assertIsNone(self.service.role_state('director:a')['lease'])
        self.assertEqual(self.service.commit('director:a', token, {'saved': True}, [], [], 'release-once', release=True), receipt)
        with self.assertRaises(Conflict):
            self.service.commit('director:a', token, {'saved': False}, [], [], 'release-once', release=True)
        with self.assertRaises(PermissionError):
            self.service.commit('director:a', token, {}, [], [], 'new-work')
        self.service.issue_session('director:a', 'replacement')
        with self.assertRaises(PermissionError):
            self.service.commit('director:a', token, {'saved': True}, [], [], 'release-once', release=True)

    def test_changed_issue_evidence_is_reoffered_after_ack_and_old_replay_cannot_regress_it(self):
        task, issue = self.issue()
        token = self.lease()
        self.service.commit('director:a', token, {}, [issue['delivery_id']], [], 'first-observation',
                            delivery_versions={issue['delivery_id']: issue['message_version']})
        ops.event(self.conn, 'recovery.exhausted', 'system', {'cause': 'incident:1', 'evidence': 'changed'}, task_id=task['id'])
        self.service.project()
        changed, = self.service.receive('director:a')
        self.assertEqual(changed['delivery_id'], issue['delivery_id'])
        self.assertEqual(changed['evidence']['evidence'], 'changed')
        sub, = self.service.health('director:a')['subscriptions']
        self.service.follow('director:a', sub['selectors'], replay_after=0)
        self.service.project()
        replayed, = self.service.receive('director:a')
        self.assertEqual(replayed['evidence']['evidence'], 'changed')

    def test_ack_rejects_changed_issue_evidence_even_before_projection(self):
        task, issue = self.issue()
        token = self.lease()
        ops.event(self.conn, 'recovery.exhausted', 'system', {'cause': 'incident:1', 'evidence': 'new'}, task_id=task['id'])
        with self.assertRaises(Conflict):
            self.service.commit('director:a', token, {}, [issue['delivery_id']], [], 'unseen',
                                delivery_versions={issue['delivery_id']: issue['message_version']})
        self.assertEqual(self.service.health('director:a')['acknowledged'], 0)
        self.service.project()
        changed, = self.service.receive('director:a')
        self.assertGreater(changed['message_version'], issue['message_version'])
        with self.assertRaises(Conflict):
            self.service.commit('director:a', token, {}, [issue['delivery_id']], [], 'missing-version')
        self.service.commit('director:a', token, {}, [changed['delivery_id']], [], 'seen',
                            delivery_versions={changed['delivery_id']: changed['message_version']})
        self.assertEqual(self.service.receive('director:a'), [])

    def test_compacted_digest_commit_requires_resync_and_never_acks_successor(self):
        self.service.register('director:offline', max_pending_routine=1)
        task = ops.create_task(self.conn, 'Work', 'repo', initiating_role='director:offline')
        self.service.project()
        self.time += 900
        first = self.service.digest('director:offline')
        ops.event(self.conn, 'progress', 'worker', {'step': 'later'}, task_id=task['id'])
        self.service.project()
        self.time += 900
        second = self.service.digest('director:offline')
        token = self.lease('director:offline', 'session:offline')
        with self.assertRaisesRegex(Conflict, 'resync-required'):
            self.service.commit('director:offline', token, {}, [first['digest_id']], [], 'stale-digest')
        offered = self.service.receive('director:offline')
        self.assertEqual([item['delivery_id'] for item in offered], [second['digest_id']])

    def test_wake_and_model_token_budgets_survive_restart_and_fail_closed(self):
        self.service.register('director:budget', wake_limit_per_hour=1, token_budget=60, allowed_models=['sol'])
        reserved = self.service.reserve_wake('director:budget', 'wake:1', tokens=40, model='sol')
        self.assertTrue(reserved['accepted'])
        self.assertEqual(self.service.reserve_wake('director:budget', 'wake:1', tokens=40, model='sol'), reserved)
        with self.assertRaises(Conflict):
            self.service.reserve_wake('director:budget', 'wake:1', tokens=10, model='sol')
        self.conn.close()
        self.conn = connect(self.path)
        self.service = Notifications(self.conn, now=lambda: self.time)
        self.assertFalse(self.service.reserve_wake('director:budget', 'wake:2', tokens=10, model='sol')['accepted'])
        self.time += 3601
        held = self.service.reserve_wake('director:budget', 'wake:3', tokens=30, model='sol')
        self.assertEqual(held['reason'], 'token-budget-exhausted')
        self.assertEqual(self.service.role_state('director:budget')['tokens_reserved'], 40)
        self.assertEqual(self.service.reserve_wake('director:budget', 'wake:4', tokens=10, model='other')['reason'], 'model-not-authorized')

    def test_replacement_requires_confirmed_death_is_once_and_preserves_child_state(self):
        task = ops.create_task(self.conn, 'Child', 'repo', initiating_role='director:a')
        token = self.lease()
        self.service.commit('director:a', token, {'result': 'saved'}, [], [], 'checkpoint')
        held = self.service.replace('director:a', 1, confirmed_dead=False)
        self.assertEqual(held['reason'], 'consumer-liveness-uncertain')
        self.assertEqual(self.service.role_state('director:a')['replacements_used'], 0)
        replacement = self.service.replace('director:a', 1, confirmed_dead=True)
        self.assertEqual(replacement['generation'], 2)
        with self.assertRaises(PermissionError):
            self.service.commit('director:a', token, {}, [], [], 'stale')
        issued = self.service.issue_session('director:a', 'replacement:2')
        self.assertEqual(issued['generation'], 2)
        self.assertEqual(self.service.authenticate(issued['credential'])['session_id'], 'replacement:2')
        self.assertEqual(self.service.role_state('director:a')['checkpoint'], {'result': 'saved'})
        self.service = Notifications(self.conn, now=lambda: self.time)
        exhausted = self.service.replace('director:a', 2, confirmed_dead=True)
        self.assertEqual(exhausted['reason'], 'replacement-budget-exhausted')
        self.assertEqual(ops.get_task(self.conn, task['id'])['status'], 'proposed')
        self.assertEqual(ops.list_runs(self.conn, task['id']), [])

    def test_bad_boundaries_and_secret_artifacts_are_refused_without_processing(self):
        token = self.lease()
        for ttl in (0, -1, True, float('nan'), float('inf')):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                self.service.issue_session('director:a', 'bad', ttl=ttl)
        for tokens in (-1, True, float('nan'), 0.5):
            with self.subTest(tokens=tokens), self.assertRaises(ValueError):
                self.service.reserve_wake('director:a', 'bad', tokens=tokens)
        with self.assertRaises(ValueError):
            self.service.reserve_wake('director:a', 'secret-model', model=token)
        for checkpoint in ({'credential': 'hidden'}, {'copied': token}, {'large': 'x' * 40000}, {'nan': float('nan')}):
            with self.subTest(checkpoint=list(checkpoint)), self.assertRaises(ValueError):
                self.service.commit('director:a', token, checkpoint, [], [], 'bad')
        for deliveries in ([{}], ['notification:1'] * 101):
            with self.subTest(deliveries=len(deliveries)), self.assertRaises(ValueError):
                self.service.commit('director:a', token, {}, deliveries, [], 'bad')
        with self.assertRaises(ValueError):
            self.service.register('director:unsafe', action_allowlist=['task.approve'])
        with self.assertRaises(ValueError):
            self.service.register('director:secret', credential='never-persist')
        self.assertEqual(ops.events(self.conn, kind='director.checkpoint'), [])

    def test_checkpoint_history_is_immutable_and_active_wakes_coalesce_without_spending(self):
        token = self.lease()
        first = self.service.commit('director:a', token, {'step': 1}, [], [], 'one')
        self.service.commit('director:a', token, {'step': 2}, [], [], 'two')
        self.assertEqual(self.service.get_checkpoint('director:a', 1),
                         dict(checkpoint={'step': 1}, checkpoint_revision=1, event_id=first['event_id']))
        self.assertEqual(self.service.get_checkpoint('director:a')['checkpoint'], {'step': 2})
        coalesced = self.service.reserve_wake('director:a', 'live-arrival')
        self.assertFalse(coalesced['accepted'])
        self.assertTrue(coalesced['coalesced'])
        self.assertEqual(self.service.role_state('director:a')['wakes_reserved'], 0)
        self.assertEqual(ops.open_questions(self.conn), [])

    def test_paused_role_can_save_and_release_existing_work_without_recovery_attempts(self):
        token = self.lease()
        ops.set_setting(self.conn, 'dispatch_enabled', 'false')
        held = self.service.reserve_wake('director:a', 'paused')
        self.assertEqual(held['reason'], 'dispatch is paused')
        self.assertEqual(self.service.replace('director:a', 1, confirmed_dead=True)['reason'], 'dispatch is paused')
        self.service.commit('director:a', token, {'saved': True}, [], [], 'paused-release', release=True)
        self.assertEqual(self.service.role_state('director:a')['checkpoint'], {'saved': True})
        self.assertEqual(self.service.role_state('director:a')['replacements_used'], 0)
        self.service.acquire('director:a', 'session:1')

    def test_issue_identity_aliases_cannot_create_competing_handlers(self):
        _, issue = self.issue()
        token = self.lease()
        alias = issue['delivery_id'].replace(':', ':0')
        self.service.claim_issue('director:a', issue['delivery_id'], token)
        with self.assertRaises(ValueError):
            self.service.claim_issue('director:a', alias, token)

    def test_resolution_before_invocation_clears_obsolete_wake(self):
        task, _ = self.issue()
        self.assertTrue(self.service.role_state('director:a')['pending_wake'])
        ops.event(self.conn, 'recovery.resolved', 'owner', {'cause': 'incident:1'}, task_id=task['id'])
        self.assertFalse(self.service.role_state('director:a')['pending_wake'])

    def test_capabilities_are_explicit_and_read_only_identity_cannot_process(self):
        issued = self.service.issue_session('director:a', 'reader', capabilities=['notifications.read'])
        self.assertEqual(self.service.authenticate(issued['credential'])['capabilities'], ['notifications.read'])
        with self.assertRaises(PermissionError):
            self.service.acquire('director:a', 'reader')
        for capabilities in ({'notifications.process': False}, True, ['owner'], ['notifications.process', {}]):
            with self.subTest(capabilities=capabilities), self.assertRaises(ValueError):
                self.service.issue_session('director:a', 'bad', capabilities=capabilities)

    def test_secret_keys_are_refused_in_checkpoints_nested_config_and_action_artifacts(self):
        issued = self.service.issue_session('director:a', 'session:keys')
        token = self.service.acquire('director:a', 'session:keys')['lease_token']
        secret = issued['credential']
        for checkpoint in ({secret: 'copied'}, {'nested': [{f'Bearer {secret}': 'copied'}]}):
            with self.assertRaises(ValueError):
                self.service.commit('director:a', token, checkpoint, [], [], 'secret-key')
        with self.assertRaises(ops.NotFound):
            self.service.get_checkpoint('director:a')
        with self.assertRaises(ValueError):
            self.service.register('director:bad-keys', annotations={'nested': {secret: 'copied'}})
        source = ops.event(self.conn, 'test.artifact-source', 'system')
        with self.assertRaises(ValueError):
            actions.enqueue(self.conn, source, 0, 'task.comment', {'nested': {secret: 'copied'}})
        self.assertEqual(actions.pending(self.conn), [])
        self.assertNotIn(secret, str(self.service.health()))

    def test_release_preserves_changed_immediate_issue_after_initial_muted_observation(self):
        task = ops.create_task(self.conn, 'Work', 'repo', initiating_role='director:a')
        sub, = self.service.health('director:a')['subscriptions']
        self.service.tune(sub['id'], 1, mode='muted')
        ops.event(self.conn, 'recovery.exhausted', 'system', {'cause': 'incident', 'evidence': 'first'}, task_id=task['id'])
        self.service.project()
        token = self.lease()
        self.service.tune(sub['id'], 2, mode='immediate')
        later = ops.event(self.conn, 'recovery.exhausted', 'system', {'cause': 'incident', 'evidence': 'changed'}, task_id=task['id'])
        self.service.project()
        self.assertTrue(self.service.role_state('director:a')['pending_wake'])
        self.service.commit('director:a', token, {}, [], [], 'release-unread', release=True)
        self.assertTrue(self.service.role_state('director:a')['pending_wake'])
        self.assertEqual(self.service.role_state('director:a')['wake_position'], later)
        unread, = self.service.receive('director:a')
        self.assertEqual((unread['mode'], unread['evidence']['evidence']), ('immediate', 'changed'))

    def test_release_suppresses_identical_unprojected_observation_of_acknowledged_issue(self):
        task, issue = self.issue()
        token = self.lease()
        self.service.commit('director:a', token, {}, [issue['delivery_id']], [], 'first', release=True,
                            delivery_versions={issue['delivery_id']: issue['message_version']})
        token = self.service.acquire('director:a', 'session:1')['lease_token']
        ops.event(self.conn, 'recovery.exhausted', 'system', {'cause': 'incident:1'}, task_id=task['id'])
        self.assertFalse(self.service.role_state('director:a')['pending_wake'])
        self.service.commit('director:a', token, {}, [], [], 'identical-release', release=True)
        self.assertFalse(self.service.role_state('director:a')['pending_wake'])
        self.service.project()
        self.assertEqual(self.service.receive('director:a'), [])


if __name__ == '__main__':
    unittest.main()
