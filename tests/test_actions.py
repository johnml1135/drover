"""The shared outbox and task destination transaction are public ledger seams."""
import tempfile
import unittest
from tests.ledger_fixture import claim as fixture_claim, stage as fixture_stage
from pathlib import Path

from drover.ledger import actions, ops
from drover.ledger.db import connect
from drover.notifications import Conflict, Notifications

class ActionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.conn = connect(Path(self.tmp.name) / 'ledger.db')
        self.addCleanup(self.conn.close)
        self.time = 1000.0
        self.service = Notifications(self.conn, now=lambda: self.time)
        ops.set_setting(self.conn, 'dispatch_enabled', 'true')
        self.service.register('director:a', action_allowlist=['task.comment', 'task.retry', 'issue.resolve'])
        self.task = ops.create_task(self.conn, 'Work', 'repo', initiating_role='director:a', scope_files=['fixture-21'])
        ops.approve(self.conn, self.task['id'])
        self.service.issue_session('director:a', 'session:1')
        self.token = self.service.acquire('director:a', 'session:1')['lease_token']

    def comment(self, text='A recorded observation'):
        return dict(kind='task.comment', payload=dict(task_id=self.task['id'], event_class='progress',
                    expected_revision=ops.events(self.conn, task_id=self.task['id'])[-1]['id'],
                    expected_status='ready', text=text))

    def test_shared_identity_conflicts_and_completion_is_idempotent(self):
        source = ops.event(self.conn, 'test.action-source', 'system')
        first = actions.enqueue(self.conn, source, 0, 'task.comment', {'text': 'one'})
        self.assertEqual(actions.enqueue(self.conn, source, 0, 'task.comment', {'text': 'one'}), first)
        with self.assertRaises(Conflict):
            actions.enqueue(self.conn, source, 0, 'task.comment', {'text': 'two'})
        with self.assertRaises(ValueError):
            actions.enqueue(self.conn, source, 1, 'shell', {'command': 'anything'})
        self.assertEqual(actions.pending(self.conn), [first])
        receipt = actions.complete(self.conn, first['action_id'], {'done': True})
        self.assertEqual(actions.complete(self.conn, first['action_id'], {'done': True}), receipt)
        self.assertEqual(actions.pending(self.conn), [])

    def test_processing_rolls_back_checkpoint_ack_and_ordered_actions_then_replays(self):
        ops.event(self.conn, 'task.result-accepted', 'owner', {'report': 'findings'}, task_id=self.task['id'])
        # Make this explicit accepted result immediate for this implementation task.
        sub, = self.service.health('director:a')['subscriptions']
        self.service.tune(sub['id'], sub['revision'], mode='immediate')
        ops.event(self.conn, 'progress', 'worker', {'step': 'ready'}, task_id=self.task['id'])
        self.service.project()
        message, = self.service.receive('director:a')
        action = self.comment()
        with self.assertRaises(ValueError):
            self.service.commit('director:a', self.token, {'next': 'wait'}, [message['delivery_id']],
                                [action, {'kind': 'shell', 'payload': {}}], 'attempt')
        self.assertEqual(self.service.role_state('director:a')['checkpoint_revision'], 0)
        self.assertEqual(actions.pending(self.conn), [])
        self.assertEqual(self.service.health('director:a')['acknowledged'], 0)
        self.assertEqual(ops.events(self.conn, kind='director.checkpoint'), [])
        receipt = self.service.commit('director:a', self.token, {'next': 'wait'}, [message['delivery_id']], [action], 'attempt')
        self.assertEqual(receipt['actions'][0]['action_id'], f"{receipt['event_id']}:0")
        self.assertEqual(self.service.commit('director:a', self.token, {'next': 'wait'}, [message['delivery_id']], [action], 'attempt'), receipt)
        done = actions.execute(self.conn, receipt['actions'][0]['action_id'], now=lambda: self.time)
        self.assertEqual(done['state'], 'complete')
        self.assertEqual(len(ops.list_comments(self.conn, 'task', self.task['id'])), 1)
        self.assertEqual(actions.execute(self.conn, done['action_id'], now=lambda: self.time), done)

    def test_acceptance_refuses_unapproved_and_unlisted_work(self):
        self.service.register('director:b')
        task = ops.create_task(self.conn, 'Unapproved', 'repo', initiating_role='director:a', scope_files=['fixture-70'])
        request = self.comment()
        request['payload'].update(task_id=task['id'], expected_status='proposed',
                                  expected_revision=ops.events(self.conn, task_id=task['id'])[-1]['id'])
        with self.assertRaises(ops.NotApproved):
            self.service.commit('director:a', self.token, {}, [], [request], 'unapproved')
        self.service.assign(self.task['id'], 'director:b', expected_revision=1)
        self.service.issue_session('director:b', 'session:b')
        token = self.service.acquire('director:b', 'session:b')['lease_token']
        with self.assertRaises(PermissionError):
            self.service.commit('director:b', token, {}, [], [self.comment()], 'unlisted')

    def test_destination_rechecks_generation_transfer_revision_and_pause_after_commit(self):
        for fence in ('generation', 'ownership', 'revision', 'pause', 'cancelled'):
            with self.subTest(fence=fence):
                # Each fence gets its own role and scope, leaving pending receipts independent.
                role = f'director:{fence}'
                self.service.register(role, action_allowlist=['task.comment'])
                task = ops.create_task(self.conn, 'Work', 'repo', initiating_role=role, scope_files=['fixture-88'])
                ops.approve(self.conn, task['id'])
                self.service.issue_session(role, f'session:{fence}')
                token = self.service.acquire(role, f'session:{fence}')['lease_token']
                request = self.comment()
                request['payload'].update(task_id=task['id'],
                                          expected_revision=ops.events(self.conn, task_id=task['id'])[-1]['id'])
                receipt = self.service.commit(role, token, {}, [], [request], 'work', release=True)
                if fence == 'generation':
                    self.service.issue_session(role, 'replacement')
                elif fence == 'ownership':
                    self.service.assign(task['id'], 'director:a', expected_revision=1)
                elif fence == 'revision':
                    ops.event(self.conn, 'progress', 'worker', {}, task_id=task['id'])
                elif fence == 'pause':
                    ops.set_setting(self.conn, 'dispatch_enabled', 'false')
                else:
                    ops.set_status(self.conn, task['id'], 'cancelled', actor='owner')
                result = actions.execute(self.conn, receipt['actions'][0]['action_id'], now=lambda: self.time)
                self.assertEqual(result['state'], 'held')
                self.assertTrue(result['result']['reason'])
                self.assertEqual(ops.list_comments(self.conn, 'task', task['id']), [])
                ops.set_setting(self.conn, 'dispatch_enabled', 'true')

    def test_delegated_recovery_has_two_durable_attempts_and_replay_spends_none(self):
        self.service.register('director:recovery', action_allowlist=['task.retry'])
        self.service.assign(self.task['id'], 'director:recovery', event_class='issue')
        self.service.issue_session('director:recovery', 'session:recovery')
        token = self.service.acquire('director:recovery', 'session:recovery')['lease_token']
        claim = None
        for attempt in range(3):
            ops.set_status(self.conn, self.task['id'], 'needs-owner', actor='system')
            ops.set_status(self.conn, self.task['id'], 'failed', actor='system')
            ops.event(self.conn, 'recovery.exhausted', 'system', {'cause': 'incident', 'attempt': attempt}, task_id=self.task['id'])
            self.service.project()
            issue = next(message for message in self.service.receive('director:recovery') if message['cause_id'] == 'incident')
            if claim is None:
                claim = self.service.claim_issue('director:recovery', issue['delivery_id'], token)
            request = dict(kind='task.retry', payload=dict(task_id=self.task['id'], event_class='issue',
                           expected_revision=ops.events(self.conn, task_id=self.task['id'])[-1]['id'], expected_status='failed',
                           issue_id=issue['delivery_id'], issue_token=claim['issue_token']))
            if attempt == 2:
                with self.assertRaises(Conflict):
                    self.service.commit('director:recovery', token, {}, [], [request], 'attempt:2')
                break
            receipt = self.service.commit('director:recovery', token, {}, [], [request], f'attempt:{attempt}')
            done = actions.execute(self.conn, receipt['actions'][0]['action_id'], now=lambda: self.time)
            self.assertEqual(done['state'], 'complete')
            self.assertEqual(ops.get_task(self.conn, self.task['id'])['status'], 'ready')
            self.assertEqual(self.service.commit('director:recovery', token, {}, [], [request], f'attempt:{attempt}'), receipt)
            self.assertEqual(actions.execute(self.conn, done['action_id'], now=lambda: self.time), done)
            self.service = Notifications(self.conn, now=lambda: self.time)
        held = self.service.hold_issue('director:recovery', issue['delivery_id'], token, 'recovery-budget-exhausted')
        self.assertTrue(held['held'])

    def test_unaccepted_intent_cannot_execute_as_a_director_action(self):
        payload = self.comment()['payload']
        payload['_authority'] = dict(role_id='director:a', generation=1, ownership_revision=1)
        source = ops.event(self.conn, 'test.action-source', 'system')
        queued = actions.enqueue(self.conn, source, 0, 'task.comment', payload)
        result = actions.execute(self.conn, queued['action_id'], now=lambda: self.time)
        self.assertEqual(result['state'], 'held')
        self.assertEqual(ops.list_comments(self.conn, 'task', self.task['id']), [])

    def test_resolution_after_release_redacts_tokens_and_replay_cannot_resolve_new_episode(self):
        ops.event(self.conn, 'recovery.exhausted', 'system', {'cause': 'incident'}, task_id=self.task['id'])
        self.service.project()
        issue, = self.service.receive('director:a')
        claim = self.service.claim_issue('director:a', issue['delivery_id'], self.token)
        request = dict(kind='issue.resolve', payload=dict(task_id=self.task['id'], event_class='issue',
                       expected_revision=ops.events(self.conn, task_id=self.task['id'])[-1]['id'], expected_status='ready',
                       issue_id=issue['delivery_id'], issue_token=claim['issue_token']))
        versions = {issue['delivery_id']: issue['message_version']}
        receipt = self.service.commit('director:a', self.token, {}, [issue['delivery_id']], [request], 'resolve', release=True,
                                      delivery_versions=versions)
        public = str([receipt, actions.pending(self.conn), ops.events(self.conn), self.service.health('director:a'),
                      self.service.role_state('director:a')])
        self.assertNotIn(self.token, public)
        self.assertNotIn(claim['issue_token'], public)
        done = actions.execute(self.conn, receipt['actions'][0]['action_id'], now=lambda: self.time)
        self.assertEqual(done['state'], 'complete')
        ops.event(self.conn, 'recovery.exhausted', 'system', {'cause': 'incident', 'episode': 2}, task_id=self.task['id'])
        self.service.project()
        current, = self.service.receive('director:a')
        self.assertNotEqual(current['delivery_id'], issue['delivery_id'])
        self.assertEqual(self.service.commit('director:a', self.token, {}, [issue['delivery_id']], [request], 'resolve', release=True,
                                            delivery_versions=versions), receipt)
        self.assertEqual(actions.execute(self.conn, done['action_id'], now=lambda: self.time), done)
        self.assertEqual(self.service.receive('director:a')[0]['delivery_id'], current['delivery_id'])

    def test_reserved_director_comment_actor_cannot_requeue_owner_work(self):
        for role in ('owner', 'service'):
            with self.subTest(role=role):
                self.service.register(role, action_allowlist=['task.comment'])
                task = ops.create_task(self.conn, 'Findings', 'repo', kind='research', initiating_role=role, scope_files=['fixture-182'])
                ops.approve(self.conn, task['id'])
                fixture_claim(self.conn, task['id'])
                run = ops.start_run(self.conn, task['id'], 'worker', 'local')
                ops.finish_run(self.conn, run, 'read-only-done', note='Findings for owner')
                ops.set_status(self.conn, task['id'], 'needs-owner')
                self.service.issue_session(role, 'session')
                token = self.service.acquire(role, 'session')['lease_token']
                request = dict(kind='task.comment', payload=dict(task_id=task['id'], event_class='issue',
                               expected_revision=ops.events(self.conn, task_id=task['id'])[-1]['id'],
                               expected_status='needs-owner', text='Director observation only'))
                receipt = self.service.commit(role, token, {}, [], [request], 'comment')['actions'][0]
                self.assertEqual(actions.execute(self.conn, receipt['action_id'], now=lambda: self.time)['state'], 'complete')
                self.assertEqual(ops.get_task(self.conn, task['id'])['status'], 'needs-owner')
                comments = ops.list_comments(self.conn, 'task', task['id'])
                self.assertEqual([c['author'] for c in comments], [f'director:{role}'])
                self.assertEqual(len(ops.list_runs(self.conn, task['id'])), 1)
                self.assertEqual(self.service.role_state(role)['issues'], [])


if __name__ == '__main__':
    unittest.main()
