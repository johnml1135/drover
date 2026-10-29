"""Public HTTP notification authority and durable delivery behavior."""
import asyncio
import tempfile
import unittest
from pathlib import Path

try:
    import httpx2 as httpx
    from hub.app import create_app
    from hub.settings import Settings
except ImportError as exc:
    raise unittest.SkipTest(f'hub deps missing: {exc}')

class ASGIClient:
    def __init__(self, app):
        self.app = app

    def request(self, method, path, **kwargs):
        async def send():
            # Sandbox restrictions can suppress thread-to-loop socket wakeups.
            async def tick():
                while True:
                    await asyncio.sleep(.01)
            ticker = asyncio.create_task(tick())
            transport = httpx.ASGITransport(app=self.app, client=('testclient', 123))
            async with httpx.AsyncClient(transport=transport, base_url='http://testserver') as client:
                try:
                    return await client.request(method, path, **kwargs)
                finally:
                    ticker.cancel()
        return asyncio.run(send())

    def post(self, path, **kwargs):
        return self.request('POST', path, **kwargs)

    def get(self, path, **kwargs):
        return self.request('GET', path, **kwargs)

    def patch(self, path, **kwargs):
        return self.request('PATCH', path, **kwargs)


BASE = '/api/v1/notifications'
SAME = {'Sec-Fetch-Site': 'same-origin'}
WORKER = {'Authorization': 'Bearer worker'}


class _NotificationBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'ledger.db'
        self.app = create_app(self.path, Settings(token='worker', run_watchdog=False,
            trusted_ui_hosts=('testclient',), ui_allowed_hosts=('testserver',),
            use_tailscale=False))
        self.addCleanup(self.app.state.store.close)
        self.c = ASGIClient(self.app)


    def owner(self, path, body):
        return self.c.post(BASE + path, json=body, headers=SAME)


    def role(self, name='alpha', **policy):
        r = self.owner('/roles', {'role_id': name, 'policy': policy})
        self.assertEqual(r.status_code, 201, r.text)
        r = self.owner('/roles/' + name + '/sessions', {'session_id': 'session-' + name})
        self.assertEqual(r.status_code, 201, r.text)
        return {'Authorization': 'Bearer ' + r.json()['credential']}


    def post(self, path, body, headers):
        return self.c.post(BASE + path, json=body, headers=headers)


    def get(self, path, headers):
        return self.c.get(BASE + path, headers=headers)


    def submit(self, headers, title='Task', **fields):
        r = self.post('/tasks', {'title': title, **fields}, headers)
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()['id']


    def lease(self, headers):
        r = self.post('/lease', {}, headers)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()['lease_token']


    def landing_follow(self):
        from drover.notifications import Notifications
        a = self.role()
        self.role('collector')
        task = self.submit(a)
        service = Notifications(self.app.state.store.conn)
        group = service.create_group('merged', 'collector')
        group = service.enroll('merged', task, group['revision'], 'enroll')
        service.seal('merged', group['revision'], 'seal')
        follow, = service.health('alpha')['subscriptions']
        follow = self.c.patch(BASE + '/subscriptions/' + follow['id'], headers=a,
            json={'expected_revision': follow['revision'], 'preferences': {'stop_on_completion': True}}).json()
        self.app.state.store.approve(task)
        self.app.state.store.set_status(task, 'needs-owner', 'dispatcher', 'awaiting acceptance')
        issue, = self.get('/inbox', a).json()
        return a, task, service, follow, issue


class NotificationApiTests(_NotificationBase):
    def test_owner_ui_alone_can_register_issue_and_transfer(self):
        self.assertEqual(self.c.post(BASE + '/roles', json={'role_id': 'alpha'}).status_code, 403)
        self.assertEqual(self.c.post(BASE + '/roles', json={'role_id': 'alpha'}, headers=WORKER).status_code, 403)
        a = self.role()
        self.assertEqual(self.c.post(BASE + '/roles', json={'role_id': 'forged'}, headers={**a, **SAME}).status_code, 403)
        self.assertEqual(self.c.post(BASE + '/roles/alpha/sessions', json={'session_id': 'forged'}, headers={**a, **SAME}).status_code, 403)
        self.assertEqual(self.c.post(BASE + '/controls/stop', json={'request_key': 'stop'}, headers=WORKER).status_code, 403)
        self.assertEqual(self.c.post(BASE + '/controls/stop', json={'request_key': 'stop'}, headers={**a, **SAME}).status_code, 403)
        self.assertEqual(self.owner('/controls/stop', {'request_key': 'stop'}).status_code, 200)


    def test_scoped_reads_tuning_processing_and_replaced_credentials(self):
        a, b = self.role(), self.role('beta')
        ta, tb = self.submit(a, 'Alpha'), self.submit(b, 'Beta')
        fa = self.post('/follow', {'selectors': {'tasks': [ta]}, 'preferences': {'mode': 'immediate'}}, a).json()
        self.assertEqual(self.post('/follow', {'selectors': {'tasks': [ta]}}, b).status_code, 409)
        self.assertEqual(self.c.patch(BASE + '/subscriptions/' + fa['id'], headers=b,
            json={'expected_revision': fa['revision'], 'preferences': {'mode': 'muted'}}).status_code, 403)
        self.assertEqual(self.get('/health', a).json()['role_id'], 'alpha')
        self.assertNotIn(tb, json_text(self.get('/health', a).json()))
        self.app.state.store.add_event('progress', 'dispatcher', {'message': 'alpha progress'}, task_id=ta)
        offers = self.get('/inbox', a).json()
        self.assertTrue(offers)
        self.assertEqual(self.get('/inbox', b).json(), [])
        item = offers[0]
        la, lb = self.lease(a), self.lease(b)
        commit = {'lease_token': lb, 'checkpoint': {}, 'delivery_ids': [item['delivery_id']],
                  'delivery_versions': {item['delivery_id']: item['message_version']}, 'request_key': 'foreign'}
        self.assertIn(self.post('/commit', commit, b).status_code, (403, 409))
        commit.update(lease_token=la, request_key='own', release=True)
        first = self.post('/commit', commit, a)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(self.post('/commit', commit, a).json(), first.json())
        commit['checkpoint'] = {'different': True}
        self.assertEqual(self.post('/commit', commit, a).status_code, 409)
        new = self.owner('/roles/alpha/sessions', {'session_id': 'replacement'})
        self.assertEqual(new.status_code, 201)
        for path in ('/health', '/inbox', '/controls'):
            self.assertEqual(self.get(path, a).status_code, 401)
        for path in ('/api/v1/tasks', '/inbox', '/events'):
            self.assertIn(self.c.get(path, headers={**a, **SAME}).status_code, (401, 403))
        self.assertEqual(self.c.get('/api/v1/tasks', headers={'Authorization': 'Bearer invalid'}).status_code, 401)


    def test_group_authority_does_not_grant_foreign_task_access(self):
        a, b = self.role(), self.role('beta')
        child = self.submit(b, 'Foreign child')
        group = self.post('/groups', {'group_id': 'join'}, a).json()
        body = {'child_id': child, 'expected_revision': group['revision'], 'request_key': 'enroll'}
        self.assertEqual(self.post('/groups/join/enroll', body, a).status_code, 403)
        self.assertEqual(self.owner('/owner/groups/join/enroll', body).status_code, 200)
        self.assertEqual(self.get('/groups/join', b).status_code, 403)
        self.assertEqual(self.post('/tasks', {'title': 'forged lineage', 'parent_id': child}, a).status_code, 403)
        self.assertEqual(self.post('/tasks', {'title': 'forged group', 'group_id': 'join'}, b).status_code, 403)
        self.assertEqual(self.post('/follow', {'selectors': {'tasks': [child]}}, a).status_code, 409)


    def test_reserved_role_text_never_becomes_owner_and_control_receipt_differs_from_apply(self):
        role = self.role('owner', broadcast_summary=True)
        r = self.post('/broadcast', {'targets': 'all', 'body': {'message': 'summary'}, 'request_key': 'summary'}, role)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.post('/broadcast', {'targets': 'all', 'body': {}, 'urgency': 'urgent', 'request_key': 'urgent'}, role).status_code, 403)
        stop = self.owner('/controls/stop', {'request_key': 'stop'}).json()
        skill = self.owner('/controls/skill', {'skill': 'review', 'digest': 'sha', 'targets': ['owner'], 'request_key': 'skill'}).json()
        state = self.get('/controls', role).json()
        self.assertEqual(state['global_epoch'], stop['global_epoch'])
        self.assertGreater(state['sequence'], state['global_epoch'])
        lease = self.lease(role)
        offers = self.get('/inbox', role).json()
        control_offer = next(i for i in offers if i.get('control_id') == skill['control_id'])
        body = {'lease_token': lease, 'checkpoint': {}, 'delivery_ids': [control_offer['delivery_id']], 'delivery_versions': {control_offer['delivery_id']: control_offer['message_version']}, 'request_key': 'receipt'}
        self.assertEqual(self.post('/commit', body, role).status_code, 200)
        self.assertEqual(self.owner('/controls/resume', {'expected_epoch': state['sequence'], 'request_key': 'wrong'}).status_code, 409)
        self.assertEqual(self.owner('/controls/resume', {'expected_epoch': state['global_epoch'], 'request_key': 'resume'}).status_code, 200)
        self.assertFalse(self.get('/health', role).json()['admission']['allowed'])
        applied = self.post('/controls/' + skill['control_id'] + '/apply', {'lease_token': lease, 'digest': 'sha'}, role)
        self.assertEqual(applied.status_code, 200, applied.text)
        self.assertTrue(self.get('/health', role).json()['admission']['allowed'])


    def test_legacy_worker_cannot_forge_semantic_events_or_director_lifecycle(self):
        for kind in ('director.finished', 'notification.broadcast', 'group.ready', 'task.result-accepted', 'result.accepted'):
            r = self.c.post('/api/v1/events', headers=WORKER, json={'kind': kind, 'data': {}})
            self.assertEqual(r.status_code, 403, r.text)
        task = self.app.state.store.create_task('Accounting')['id']
        self.assertEqual(self.c.post('/api/v1/runs', headers=WORKER, json={'task_id': task, 'role': 'director', 'backend': 'fake'}).status_code, 403)
        from drover.directors import DirectorRuntime
        from drover.port import DirectPort
        from drover.notifications import Notifications
        from drover.ledger import ops
        from tests.test_directors import ControlledAdapter
        conn = self.app.state.store.conn
        service = Notifications(conn)
        service.register('accounting', allowed_models=['fixture'], token_budget=10)
        service.broadcast('owner', ['accounting'], 'work', urgency='urgent')
        ops.set_setting(conn, 'dispatch_enabled', 'true')
        ops.set_setting(conn, 'director_execution_enabled', 'true')
        ops.set_setting(conn, 'director_execution', json_text({'accounting': dict(backend='local',
            model='fixture', seat='fixture', max_tokens=10, placement='windows', cwd=self.tmp.name)}))
        runtime = DirectorRuntime(DirectPort(conn), ControlledAdapter(service))
        rid, = runtime.tick(service.admission(), {'slots': 1, 'admit': lambda spec: None})['launched']
        self.assertEqual(self.c.post(f'/api/v1/runs/{rid}/finish', headers=WORKER, json={'outcome': 'done'}).status_code, 403)


    def test_worker_generic_lifecycle_injection_keeps_issue_follow_and_join(self):
        a, task, service, follow, issue = self.landing_follow()
        for kind, data in (('status', {'from': 'reviewed', 'to': 'landed'}),
                           ('status', {'to': 'cancelled'}),
                           ('task.landed', {'land_sha': 'forged', 'tip': 'forged'})):
            with self.subTest(kind=kind, data=data):
                result = self.c.post('/api/v1/events', headers=WORKER,
                    json={'kind': kind, 'task_id': task, 'data': data})
                self.assertEqual(result.status_code, 403, result.text)
                self.assertEqual(self.get('/inbox', a).json(), [issue])
                self.assertTrue(next(s for s in service.health('alpha')['subscriptions']
                    if s['id'] == follow['id'])['preferences']['enabled'])
                self.assertFalse(service.get_group('merged')['ready'])
        observation = self.c.post('/api/v1/events', headers=WORKER,
            json={'kind': 'worker-observation', 'task_id': task, 'data': {'message': 'still checking'}})
        self.assertEqual(observation.status_code, 201, observation.text)


    def test_worker_status_chain_needs_real_merge_receipt_for_semantic_completion(self):
        from drover.ledger import landing
        a, task, service, follow, issue = self.landing_follow()
        lease = self.lease(a)
        ack = self.post('/commit', {'lease_token': lease, 'checkpoint': {},
            'delivery_ids': [issue['delivery_id']],
            'delivery_versions': {issue['delivery_id']: issue['message_version']},
            'request_key': 'observed-issue', 'release': True}, a)
        self.assertEqual(ack.status_code, 200, ack.text)
        for status in ('ready', 'claimed', 'running', 'gated', 'reviewed', 'landed'):
            result = self.c.post(f'/api/v1/tasks/{task}/status', headers=WORKER, json={'status': status})
            self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.get('/inbox', a).json(), [])
        health = service.health('alpha')
        self.assertEqual(health['pending_actionable'], 1)
        self.assertFalse(health['runtime']['pending_wake'])
        self.assertTrue(next(s for s in health['subscriptions'] if s['id'] == follow['id'])['preferences']['enabled'])
        self.assertFalse(service.get_group('merged')['ready'])
        landing.record_merged(service.conn, {'id': task, 'last_sha': 'a' * 40}, 'b' * 40, 'fixture merge receipt')
        landed, = self.get('/inbox', a).json()
        self.assertEqual((landed['task_id'], landed['event_class']), (task, 'landed'))
        self.assertEqual(landed['evidence']['land_sha'], 'a' * 40)
        health = service.health('alpha')
        self.assertEqual(health['pending_actionable'], 0)
        self.assertTrue(health['runtime']['pending_wake'])
        self.assertFalse(next(s for s in health['subscriptions'] if s['id'] == follow['id'])['preferences']['enabled'])
        self.assertTrue(service.get_group('merged')['ready'])
        landing.record_merged(service.conn, {'id': task, 'last_sha': 'a' * 40}, 'b' * 40, 'fixture merge receipt')
        self.assertEqual(self.get('/inbox', a).json(), [landed])


    def test_committed_action_executes_once_on_deterministic_tick_with_paid_execution_disabled(self):
        from drover.directors import service_tick
        from drover.port import DirectPort
        from drover.ledger import ops
        who = self.role(action_allowlist=['task.comment'])
        task = self.submit(who)
        self.app.state.store.approve(task)
        conn = self.app.state.store.conn
        ops.set_setting(conn, 'dispatch_enabled', 'true')
        lease = self.lease(who)
        action = {'kind': 'task.comment', 'payload': {'task_id': task, 'event_class': 'progress',
                  'expected_status': 'ready', 'expected_revision': ops.events(conn, task_id=task)[-1]['id'], 'text': 'bounded effect'}}
        response = self.post('/commit', {'lease_token': lease, 'checkpoint': {'saved': True},
            'delivery_ids': [], 'actions': [action], 'request_key': 'effect', 'release': True}, who)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(ops.list_comments(conn, 'task', task), [])
        service_tick(DirectPort(conn))
        service_tick(DirectPort(conn))
        self.assertEqual([row['body'] for row in ops.list_comments(conn, 'task', task)], ['bounded effect'])
        self.assertEqual(conn.execute('SELECT state FROM action_outbox').fetchone()[0], 'complete')
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM director_runs').fetchone()[0], 0)


class NotificationCommentMigrationTests(unittest.TestCase):
    def test_upgrade_preserves_comment_history_and_deleted_maximum_identity(self):
        from drover.ledger import connect, migrate, ops
        from drover.ledger.schema import MIGRATIONS
        from drover.notifications import Notifications
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            conn = connect(Path(directory) / 'upgrade.db', migrate_db=False)
            self.addCleanup(conn.close)
            migrate(conn, MIGRATIONS[:11])
            task = ops.create_task(conn, 'Historical task', 'repo')['id']
            old = ops.add_comment(conn, 'task', task, 'owner', 'retained comment')['id']
            conn.execute("INSERT INTO comments(id,target_kind,target_id,author,body,at) VALUES(41,'task',?,'owner','deleted comment','now')", (task,))
            conn.execute('DELETE FROM comments WHERE id=41')
            migrate(conn)
            unassigned = Notifications(conn).health()['unassigned']
            Notifications(conn).register('alpha')
            new = ops.add_comment(conn, 'director', 'alpha', 'owner', 'new target comment')
            self.assertGreater(new['id'], 41)
            self.assertEqual(ops.list_comments(conn, 'task', task)[0]['id'], old)
            self.assertEqual(ops.list_comments(conn, 'director', 'alpha')[0]['body'], 'new target comment')
            self.assertEqual(Notifications(conn).health()['unassigned'], unassigned)


def json_text(value):
    import json
    return json.dumps(value)
