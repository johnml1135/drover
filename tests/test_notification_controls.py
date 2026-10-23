"""Broadcast snapshots and deterministic controls through the public service."""
import tempfile
import unittest
from pathlib import Path

from drover.ledger import ops
from drover.ledger.db import connect
from drover.notifications import Conflict, Notifications
from drover.notifications.controls import admission


class NotificationControlsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'ledger.db'
        self.conn = connect(self.path)
        self.addCleanup(lambda: self.conn.close())
        self.now = 1000
        self.service = Notifications(self.conn, now=lambda: self.now)
        for role in ('director:a', 'director:b'):
            self.service.register(role, notifications={'mode': 'muted'})

    def project(self):
        while self.service.project():
            pass

    def lease(self, role, session='s'):
        self.service.issue_session(role, session)
        return self.service.acquire(role, session)['lease_token']

    def test_summary_snapshot_and_urgent_bypass_have_independent_acknowledgements(self):
        summary = self.service.broadcast('owner', 'all', 'Routine notice', request_key='summary')
        urgent = self.service.broadcast('owner', 'all', 'Urgent notice', urgency='urgent', request_key='urgent')
        self.service.register('director:late')
        self.project()
        a, = self.service.receive('director:a')
        b, = self.service.receive('director:b')
        self.assertEqual(a['notice_id'], urgent['notice_id'])
        self.assertEqual(b['notice_id'], urgent['notice_id'])
        self.assertNotEqual(a['delivery_id'], b['delivery_id'])
        self.assertTrue(self.service.role_state('director:a')['pending_wake'])
        token = self.lease('director:a')
        self.service.commit('director:a', token, {}, [a['delivery_id']], [], 'ack', release=True)
        self.assertFalse(self.service.role_state('director:a')['pending_wake'])
        self.assertEqual(self.service.receive('director:a'), [])
        self.assertEqual(self.service.receive('director:b'), [b])
        self.assertEqual(self.service.receive('director:late'), [])
        self.now += 900
        for role in ('director:a', 'director:b'):
            digest = self.service.digest(role)
            self.assertEqual(len([e for e in digest['entries'] if e['notice_id'] == summary['notice_id']]), 1)
            self.assertIsNone(self.service.digest(role))
            self.assertEqual(len(self.service.receive(role)[-1]['entries']), 2)
        self.assertIsNone(self.service.digest('director:late'))
        self.assertEqual(self.service.broadcast('owner', 'all', 'Routine notice', request_key='summary'), summary)
        with self.assertRaises(Conflict):
            self.service.broadcast('owner', 'all', 'Changed notice', request_key='summary')

    def test_stop_is_durable_global_and_acknowledgement_never_resumes(self):
        stop = self.service.stop('owner', 'stop')
        self.assertFalse(admission(self.conn)['allowed'])
        self.assertEqual(admission(self.conn)['epoch'], stop['epoch'])
        token = self.lease('director:a')
        applied = self.service.ack_control('director:a', token, stop['control_id'])
        self.assertEqual(applied['status'], 'applied')
        self.assertFalse(admission(self.conn, role_id='director:a')['allowed'])
        # Control acknowledgement/checkpoint must stay possible under STOP.
        self.service.commit('director:a', token, {'saved': True}, [], [], 'save', release=True)
        self.conn.close()
        self.conn = connect(self.path)
        self.service = Notifications(self.conn, now=lambda: self.now)
        self.service.register('director:new')
        self.assertEqual(admission(self.conn, role_id='director:new')['reason'], 'stop-work')
        self.project()
        self.assertTrue(any(m['control_id'] == stop['control_id'] for m in self.service.receive('director:new')))
        self.assertEqual(self.service.stop('owner', 'stop'), stop)
        with self.assertRaises(Conflict):
            self.service.resume('owner', stop['epoch'] - 1, 'bad-resume')
        resumed = self.service.resume('owner', stop['epoch'], 'resume')
        self.assertTrue(admission(self.conn)['allowed'])
        self.assertEqual(admission(self.conn, epoch=stop['epoch'])['reason'], 'control-epoch-changed')
        self.assertGreater(resumed['epoch'], stop['epoch'])

    def test_skill_requirements_are_session_specific_and_cannot_be_forged_by_delivery_ack(self):
        token = self.lease('director:a')
        requirement = self.service.require_skill('owner', 'careful-work', 'sha256:v2', 'all', 'require')
        self.assertIn('skill-required', admission(self.conn, role_id='director:a')['reason'])
        self.assertTrue(admission(self.conn)['allowed'])
        self.project()
        notice, = self.service.receive('director:a')
        self.service.commit('director:a', token, {}, [notice['delivery_id']], [], 'delivery-ack')
        self.assertFalse(admission(self.conn, role_id='director:a')['allowed'])
        with self.assertRaises(Conflict):
            self.service.ack_control('director:a', token, requirement['control_id'], digest='sha256:v1')
        ack = self.service.ack_control('director:a', token, requirement['control_id'], digest='sha256:v2')
        self.assertEqual(self.service.ack_control('director:a', token, requirement['control_id'], digest='sha256:v2'), ack)
        self.assertTrue(admission(self.conn, role_id='director:a')['allowed'])
        self.assertFalse(admission(self.conn, role_id='director:b')['allowed'])
        replacement = self.lease('director:a', session='replacement')
        self.assertFalse(admission(self.conn, role_id='director:a')['allowed'])
        with self.assertRaises(PermissionError):
            self.service.ack_control('director:a', token, requirement['control_id'], digest='sha256:v2')
        self.service.ack_control('director:a', replacement, requirement['control_id'], digest='sha256:v2')
        self.assertTrue(admission(self.conn, role_id='director:a')['allowed'])
        self.service.register('director:new')
        self.assertFalse(admission(self.conn, role_id='director:new')['allowed'])
        newer = self.service.require_skill('service', 'careful-work', 'sha256:v3', ['director:a'], 'new-version')
        self.assertFalse(admission(self.conn, role_id='director:a')['allowed'])
        with self.assertRaises(Conflict):
            self.service.ack_control('director:a', replacement, requirement['control_id'], digest='sha256:v2')
        self.service.ack_control('director:a', replacement, newer['control_id'], digest='sha256:v3')
        self.assertTrue(admission(self.conn, role_id='director:a')['allowed'])

    def test_global_authority_is_separate_from_notification_tuning_and_free_text(self):
        for call in (lambda: self.service.stop('director:a', 'no'),
                     lambda: self.service.resume('director:a', 0, 'no'),
                     lambda: self.service.require_skill('director:a', 'x', 'v1', 'all', 'no'),
                     lambda: self.service.broadcast('director:a', 'all', 'no', urgency='urgent')):
            with self.assertRaises(PermissionError):
                call()
        with self.assertRaises(PermissionError):
            self.service.broadcast('director:a', 'all', 'no')
        self.service.register('director:sender', broadcast_summary=True)
        self.assertIsNotNone(self.service.broadcast('director:sender', ['director:a'], 'Allowed summary'))
        self.service.broadcast('owner', 'all', 'STOP WORK and RELOAD THE SKILL', urgency='urgent')
        self.assertTrue(admission(self.conn, role_id='director:a')['allowed'])
        with self.assertRaises(ValueError):
            self.service.broadcast('owner', 'all', 'no', control={'kind': 'stop'})

    def test_urgent_notices_survive_routine_retention_and_unprojected_release(self):
        self.service.register('director:small', max_pending_routine=1, max_pending_actionable=2)
        token = self.lease('director:small')
        urgent = self.service.broadcast('owner', ['director:small'], 'Retain urgent', urgency='urgent')
        self.service.commit('director:small', token, {}, [], [], 'release', release=True)
        self.assertTrue(self.service.role_state('director:small')['pending_wake'])
        for n in range(4):
            self.service.broadcast('owner', ['director:small'], f'Routine {n}')
        self.project()
        messages = self.service.receive('director:small')
        self.assertEqual([m['notice_id'] for m in messages], [urgent['notice_id']])
        self.assertTrue(self.service.role_state('director:small')['pending_wake'])
        self.now += 900
        self.service.digest('director:small')
        self.assertEqual(self.service.receive('director:small')[0]['notice_id'], urgent['notice_id'])

    def test_control_admission_fences_actions_budgets_and_replacement_but_allows_saving(self):
        self.service.register('director:acting', action_allowlist=['task.comment'])
        task = ops.create_task(self.conn, 'Work', 'repo', initiating_role='director:acting')
        ops.approve(self.conn, task['id'])
        ops.set_setting(self.conn, 'dispatch_enabled', 'true')
        token = self.lease('director:acting')
        revision = ops.events(self.conn, task_id=task['id'])[-1]['id']
        action = dict(kind='task.comment', payload=dict(task_id=task['id'], event_class='progress',
                      expected_revision=revision, expected_status='ready', text='Comment'))
        requirement = self.service.require_skill('owner', 'safe-work', 'v1', ['director:acting'], 'skill')
        with self.assertRaises(Conflict):
            self.service.commit('director:acting', token, {}, [], [action], 'denied')
        self.assertEqual(self.service.reserve_wake('director:acting', 'held')['reason'], 'skill-required:safe-work:v1')
        self.assertFalse(self.service.replace('director:acting', 1, confirmed_dead=True)['accepted'])
        self.service.commit('director:acting', token, {'saved': True}, [], [], 'save')
        self.service.ack_control('director:acting', token, requirement['control_id'], digest='v1')
        self.service.commit('director:acting', token, {}, [], [], 'observe-skill', release=True)
        token = self.service.acquire('director:acting', 's')['lease_token']
        receipt = self.service.commit('director:acting', token, {}, [], [action], 'accepted')
        self.service.stop('service', 'stop')
        from drover.ledger import actions
        result = actions.execute(self.conn, receipt['actions'][0]['action_id'], now=lambda: self.now)
        self.assertEqual(result['state'], 'held')
        self.assertIn('stop-work', result['result']['reason'])

    def test_explicit_skill_targets_stay_snapshots_and_control_health_distinguishes_application(self):
        requirement = self.service.require_skill('owner', 'skill', 'v1', ['director:a'], 'targeted')
        self.service.register('director:late')
        self.assertTrue(admission(self.conn, role_id='director:late')['allowed'])
        self.assertTrue(admission(self.conn, role_id='director:b')['allowed'])
        before = self.service.control_state('director:a')['controls']
        self.assertEqual(before[0]['status'], 'pending')
        self.project()
        self.service.receive('director:a')
        self.assertEqual(self.service.control_state('director:a')['controls'][0]['status'], 'received')
        token = self.lease('director:a')
        self.service.ack_control('director:a', token, requirement['control_id'], digest='v1')
        self.assertEqual(self.service.control_state('director:a')['controls'][0]['status'], 'applied')
        self.assertEqual(self.service.health('director:a')['controls']['controls'][0]['status'], 'applied')
        self.lease('director:a', 'next')
        self.assertEqual(self.service.control_state('director:a')['controls'][0]['status'], 'received')

    def test_targeted_notice_replay_preview_and_processing_do_not_leak_other_targets(self):
        own = self.service.broadcast('owner', ['director:a'], 'Only A', urgency='urgent')
        other = self.service.broadcast('owner', ['director:b'], 'Only B', urgency='urgent')
        a_follow = self.service.follow('director:a', {}, replay_after=0)
        b_follow = self.service.follow('director:b', {}, replay_after=0)
        self.assertEqual([e['event_id'] for e in a_follow['preview']], [own['event_id']])
        self.assertEqual([e['event_id'] for e in b_follow['preview']], [other['event_id']])
        self.project()
        a, = self.service.receive('director:a')
        b, = self.service.receive('director:b')
        token = self.lease('director:a')
        with self.assertRaises(PermissionError):
            self.service.commit('director:a', token, {}, [b['delivery_id']], [], 'cross-ack')
        self.service.commit('director:a', token, {}, [a['delivery_id']], [], 'own-ack', release=True)
        self.assertEqual(self.service.receive('director:b'), [b])

    def test_superseded_controls_stop_offers_without_losing_target_audit(self):
        old = self.service.require_skill('owner', 'skill', 'v1', 'all', 'v1')
        new = self.service.require_skill('owner', 'skill', 'v2', ['director:a'], 'v2')
        self.project()
        self.assertEqual([m['control_id'] for m in self.service.receive('director:a')], [new['control_id']])
        self.assertEqual([m['control_id'] for m in self.service.receive('director:b')], [old['control_id']])
        history = self.service.control_state('director:a')['controls']
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]['superseded'], 1)

    def test_invalid_controls_and_secret_or_oversized_bodies_roll_back_all_fences(self):
        token = self.lease('director:a')
        before = ops.events(self.conn)
        for operation in (lambda: self.service.require_skill('owner', 'skill', 'v1', ['missing'], 'missing'),
                          lambda: self.service.broadcast('owner', 'all', 'x' * 8193, request_key='oversized'),
                          lambda: self.service.broadcast('owner', 'all', {'text': token}, request_key='secret')):
            with self.assertRaises((ValueError, ops.NotFound)):
                operation()
        self.assertEqual(ops.events(self.conn), before)
        self.assertEqual(self.service.admission()['epoch'], 0)
        self.assertTrue(self.service.admission()['allowed'])

    def test_stop_resume_never_revives_an_action_accepted_under_the_old_epoch(self):
        from drover.ledger import actions
        self.service.register('director:acting', action_allowlist=['task.comment'])
        task = ops.create_task(self.conn, 'Work', 'repo', initiating_role='director:acting')
        ops.approve(self.conn, task['id'])
        ops.set_setting(self.conn, 'dispatch_enabled', 'true')
        token = self.lease('director:acting')
        revision = ops.events(self.conn, task_id=task['id'])[-1]['id']
        action = dict(kind='task.comment', payload=dict(task_id=task['id'], event_class='progress',
                      expected_revision=revision, expected_status='ready', text='Authorized comment'))
        old = self.service.commit('director:acting', token, {}, [], [action], 'before-stop')['actions'][0]
        stopped = self.service.stop('owner', 'stop')
        self.service.resume('owner', stopped['epoch'], 'resume')
        stale = actions.execute(self.conn, old['action_id'], now=lambda: self.now)
        self.assertEqual(stale['state'], 'held')
        self.assertEqual(stale['result']['reason'], 'control-epoch-changed')
        with self.assertRaisesRegex(Conflict, 'control-epoch-changed'):
            self.service.commit('director:acting', token, {}, [], [action], 'stale-lease')
        self.service.commit('director:acting', token, {'saved': True}, [], [], 'release-stale', release=True)
        fresh = self.service.acquire('director:acting', 's')
        self.assertGreater(fresh['control_epoch'], 0)
        new = self.service.commit('director:acting', fresh['lease_token'], {}, [], [action], 'after-resume')['actions'][0]
        self.assertEqual(actions.execute(self.conn, new['action_id'], now=lambda: self.now)['state'], 'complete')

    def test_v10_migration_never_reuses_a_compacted_delivery_identity(self):
        from drover.ledger.db import migrate, schema_version
        from drover.ledger.schema import MIGRATIONS
        legacy = connect(Path(self.tmp.name) / 'legacy.db', migrate_db=False)
        self.addCleanup(legacy.close)
        migrate(legacy, [(version, sql) for version, sql in MIGRATIONS if version <= 10])
        self.assertEqual(schema_version(legacy), 10)
        # An isolated historical V10 fixture: its last inbox identity was compacted.
        legacy.execute("INSERT INTO director_roles(role_id,policy,created,last_digest) VALUES('director:a','{}',1000,1000)")
        legacy.execute("INSERT INTO task_events(id,at,kind,actor,data) VALUES(1,'2026-10-23T00:00:00Z','progress','legacy','{}')")
        legacy.execute("INSERT INTO notification_inbox(id,event_id,role_id,event_class,ownership_revision,mode,actionable,envelope,created) "
                       "VALUES(41,1,'director:a','progress',1,'digest',0,'{}',1000)")
        legacy.execute('DELETE FROM notification_inbox WHERE id=41')
        migrate(legacy)
        self.assertEqual(schema_version(legacy), MIGRATIONS[-1][0])
        service = Notifications(legacy, now=lambda: self.now)
        service.broadcast('owner', ['director:a'], 'New notice', urgency='urgent')
        service.project()
        message, = service.receive('director:a')
        self.assertGreater(int(message['delivery_id'].split(':')[1]), 41)

    def test_director_principal_named_owner_or_service_never_gets_global_authority(self):
        for name in ('owner', 'service'):
            self.service.register(name, broadcast_summary=True)
            principal = {'kind': 'director', 'role_id': name}
            for call in (lambda: self.service.stop(principal, 'forged-stop'),
                         lambda: self.service.resume(principal, 0, 'forged-resume'),
                         lambda: self.service.require_skill(principal, 'skill', 'v1', 'all', 'forged-skill'),
                         lambda: self.service.broadcast(principal, 'all', 'Urgent', urgency='urgent')):
                with self.assertRaises(PermissionError):
                    call()
            notice = self.service.broadcast(principal, ['director:a'], 'Allowed summary', request_key='summary')
            self.assertEqual(notice['targets'], ['director:a'])
        self.assertTrue(self.service.admission()['allowed'])

    def test_targeted_skill_epoch_preserves_unrelated_intents_and_leases(self):
        from drover.ledger import actions
        ops.set_setting(self.conn, 'dispatch_enabled', 'true')
        tasks, leases, accepted = {}, {}, {}
        def comment(role):
            task = tasks[role]
            return dict(kind='task.comment', payload=dict(task_id=task['id'], event_class='progress',
                        expected_revision=ops.events(self.conn, task_id=task['id'])[-1]['id'],
                        expected_status='ready', text='Scoped work'))
        for role in ('director:affected', 'director:unrelated'):
            self.service.register(role, action_allowlist=['task.comment'])
            tasks[role] = ops.create_task(self.conn, 'Work', 'repo', initiating_role=role)
            ops.approve(self.conn, tasks[role]['id'])
            leases[role] = self.lease(role)
            accepted[role] = self.service.commit(role, leases[role], {}, [], [comment(role)], 'before-skill')['actions'][0]
        affected, unrelated = 'director:affected', 'director:unrelated'
        old_epoch = self.service.admission(role_id=unrelated)['epoch']
        required = self.service.require_skill('owner', 'skill', 'v2', [affected], 'targeted-skill')
        self.assertEqual(self.service.admission(role_id=unrelated)['epoch'], old_epoch)
        self.assertEqual(self.service.admission()['epoch'], old_epoch)
        self.assertEqual(actions.execute(self.conn, accepted[unrelated]['action_id'], now=lambda: self.now)['state'], 'complete')
        second = self.service.commit(unrelated, leases[unrelated], {}, [], [comment(unrelated)], 'after-skill')['actions'][0]
        self.assertEqual(actions.execute(self.conn, second['action_id'], now=lambda: self.now)['state'], 'complete')
        self.service.ack_control(affected, leases[affected], required['control_id'], digest='v2')
        self.assertEqual(actions.execute(self.conn, accepted[affected]['action_id'], now=lambda: self.now)['result']['reason'], 'control-epoch-changed')
        with self.assertRaisesRegex(Conflict, 'control-epoch-changed'):
            self.service.commit(affected, leases[affected], {}, [], [comment(affected)], 'stale-lease')
        self.service.commit(affected, leases[affected], {}, [], [], 'release', release=True)
        fresh = self.service.acquire(affected, 's')
        self.assertEqual(fresh['control_epoch'], self.service.admission(role_id=affected)['epoch'])
        next_action = self.service.commit(affected, fresh['lease_token'], {}, [], [comment(affected)], 'fresh')['actions'][0]
        self.assertEqual(actions.execute(self.conn, next_action['action_id'], now=lambda: self.now)['state'], 'complete')

    def test_owner_resume_epoch_is_global_boundary_not_targeted_control_sequence(self):
        stopped = self.service.stop('owner', 'stop-boundary')
        requirement = self.service.require_skill('owner', 'skill', 'v1', ['director:a'], 'skill-while-stopped')
        state = self.service.control_state()
        self.assertEqual(state['epoch'], stopped['epoch'])
        self.assertEqual(state['global_epoch'], stopped['epoch'])
        self.assertGreater(state['sequence'], state['epoch'])
        self.assertEqual(self.service.control_state('director:a')['role_epoch'], requirement['epoch'])
        self.assertEqual(self.service.control_state('director:b')['role_epoch'], stopped['epoch'])
        with self.assertRaises(Conflict):
            self.service.resume('owner', state['sequence'], 'sequence-is-not-resume-authority')
        resumed = self.service.resume('owner', state['global_epoch'], 'resume-global')
        self.assertEqual(self.service.admission()['epoch'], resumed['epoch'])
        self.assertFalse(self.service.admission(role_id='director:a')['allowed'])
        self.assertTrue(self.service.admission(role_id='director:b')['allowed'])
