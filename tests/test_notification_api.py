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


class NotificationApiTests(unittest.TestCase):
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

    def test_owner_ui_alone_can_register_issue_and_transfer(self):
        self.assertEqual(self.c.post(BASE + '/roles', json={'role_id': 'alpha'}).status_code, 403)
        self.assertEqual(self.c.post(BASE + '/roles', json={'role_id': 'alpha'}, headers=WORKER).status_code, 403)
        a = self.role()
        self.assertEqual(self.c.post(BASE + '/roles', json={'role_id': 'forged'}, headers={**a, **SAME}).status_code, 403)
        self.assertEqual(self.c.post(BASE + '/roles/alpha/sessions', json={'session_id': 'forged'}, headers={**a, **SAME}).status_code, 403)
        self.assertEqual(self.c.post(BASE + '/controls/stop', json={'request_key': 'stop'}, headers=WORKER).status_code, 403)
        self.assertEqual(self.c.post(BASE + '/controls/stop', json={'request_key': 'stop'}, headers={**a, **SAME}).status_code, 403)
        self.assertEqual(self.owner('/controls/stop', {'request_key': 'stop'}).status_code, 200)

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

    def test_replay_pending_transfer_late_projection_restart_and_processed_history(self):
        a, b = self.role(), self.role('beta')
        task = self.submit(a)
        self.post('/follow', {'selectors': {'tasks': [task]}, 'preferences': {'mode': 'immediate'}}, a)
        self.app.state.store.add_event('progress', 'dispatcher', {'message': 'first'}, task_id=task)
        first = self.get('/replay', a).json()
        self.assertTrue(first['items'])
        seen = first['items'][-1]['message']
        lease = self.lease(a)
        ack = {'lease_token': lease, 'checkpoint': {}, 'delivery_ids': [i['message']['delivery_id'] for i in first['items']],
               'delivery_versions': {i['message']['delivery_id']: i['message']['message_version'] for i in first['items']}, 'request_key': 'ack', 'release': True}
        self.assertEqual(self.post('/commit', ack, a).status_code, 200)
        self.app.state.store.add_event('progress', 'dispatcher', {'message': 'late'}, task_id=task)
        # A saved high cursor cannot conceal a pending source fact projected later.
        late = self.get('/replay?after=v1:999999:0', a).json()
        self.assertTrue(any(i['message'].get('evidence', {}).get('message') == 'late' for i in late['items']), late)
        self.assertEqual(self.owner('/ownership', {'task_id': task, 'role_id': 'beta', 'expected_revision': 1}).status_code, 200)
        successor = self.get('/replay?after=v1:999999:0', b).json()
        self.assertTrue(successor['items'])
        self.assertNotIn(seen['delivery_id'], [i['message']['delivery_id'] for i in successor['items']])
        self.assertFalse(self.get('/replay?after=v1:999999:0', a).json()['items'])
        settings = self.app.state.settings
        restarted = create_app(self.path, settings)
        self.addCleanup(restarted.state.store.close)
        response = ASGIClient(restarted).get(BASE + '/replay?after=v1:999999:0', headers=b)
        self.assertEqual([i['message']['delivery_id'] for i in response.json()['items']], [i['message']['delivery_id'] for i in successor['items']])
        sse = self.c.get(BASE + '/events?after=v1:999999:0&limit=1', headers=b)
        self.assertEqual(sse.status_code, 200)
        self.assertIn('event: notification', sse.text)
        self.assertIn('id: v1:', sse.text)
        self.assertEqual(self.get('/replay?after=bad', b).status_code, 400)


    def test_unassigned_and_taskless_owner_questions_are_visible_and_answerable(self):
        from drover.notifications import Notifications
        from drover.ledger import ops
        a = self.role()
        service = Notifications(self.app.state.store.conn)
        held = service.reserve_wake('alpha', 'hold')
        self.assertFalse(held['accepted'])
        qid = next(q['id'] for q in ops.open_questions(service.conn) if q['target_kind'] == 'director')
        question = self.c.get('/inbox')
        self.assertEqual(question.status_code, 200, question.text)
        self.assertIn('director: alpha', question.text)
        comment = self.c.post('/api/v1/comments', headers=SAME,
            json={'target_kind': 'director', 'target_id': 'alpha', 'body': 'Investigating'})
        self.assertEqual(comment.status_code, 201, comment.text)
        answer = self.c.post(f'/api/v1/questions/{qid}/answer', headers=SAME, json={'text': 'Noted'})
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertEqual(service.role_state('alpha')['held_reason'], held['reason'])
        group = service.create_group('join', 'alpha')
        group_question = ops.ask_question(service.conn, 'group', 'join', 'Investigate join')
        answer = self.c.post(f'/api/v1/questions/{group_question}/answer', headers=SAME, json={'text': 'Noted'})
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertFalse(service.get_group('join')['ready'])
        task = self.app.state.store.create_task('Unassigned')['id']
        for _ in range(2):
            self.app.state.store.add_event('recovery.exhausted', 'system', {'cause': 'incident:1'}, task_id=task)
        page = self.c.get('/inbox')
        self.assertEqual(page.text.count('Unassigned notification: issue'), 1)
        health = self.c.get('/health')
        self.assertEqual(health.status_code, 200, health.text)
        self.assertIn('Director notifications', health.text)
        self.assertIn('Held:', health.text)
        self.assertEqual(self.get('/owner/health', a).status_code, 403)

    def test_stale_coalesced_version_rejects_unprojected_evidence_then_echoes_new_version(self):
        a = self.role()
        task = self.submit(a)
        self.app.state.store.add_event('recovery.exhausted', 'system', {'cause': 'incident:1', 'evidence': 'old'}, task_id=task)
        item = next(i for i in self.get('/inbox', a).json() if i['event_class'] == 'issue')
        lease = self.lease(a)
        body = {'lease_token': lease, 'checkpoint': {}, 'delivery_ids': [item['delivery_id']], 'delivery_versions': {item['delivery_id']: item['message_version']}, 'request_key': 'observe'}
        self.app.state.store.add_event('recovery.exhausted', 'system', {'cause': 'incident:1', 'evidence': 'new'}, task_id=task)
        self.assertEqual(self.post('/commit', body, a).status_code, 409)
        current = next(i for i in self.get('/inbox', a).json() if i['event_class'] == 'issue')
        self.assertEqual(current['delivery_id'], item['delivery_id'])
        self.assertGreater(current['message_version'], item['message_version'])
        body['delivery_versions'][item['delivery_id']] = current['message_version']
        self.assertEqual(self.post('/commit', body, a).status_code, 200)

    def test_expired_narrow_capability_and_payload_validation(self):
        from unittest.mock import patch
        a = self.role()
        r = self.owner('/roles/alpha/sessions', {'session_id': 'read-only', 'capabilities': ['notifications.read']})
        read = {'Authorization': 'Bearer ' + r.json()['credential']}
        self.assertEqual(self.get('/health', read).status_code, 200)
        self.assertEqual(self.post('/follow', {'selectors': {}}, read).status_code, 403)
        self.assertEqual(self.post('/lease', {}, read).status_code, 403)
        self.assertEqual(self.post('/commit', {'lease_token': 'x', 'checkpoint': {}, 'delivery_ids': ['notification:1'], 'request_key': 'missing-versions'}, read).status_code, 422)
        self.assertEqual(self.post('/follow', {'selectors': {}, 'role_id': 'beta'}, read).status_code, 422)
        self.assertEqual(self.owner('/roles', {'role_id': 'oversized', 'policy': {'text': 'x' * 70000}}).status_code, 422)
        with patch('drover.notifications.core.time.time', return_value=r.json()['expires_at'] + 1):
            self.assertEqual(self.get('/health', read).status_code, 401)
            self.assertEqual(self.c.get('/api/v1/tasks', headers={**read, **SAME}).status_code, 401)

    def test_bounded_compaction_resync_and_owner_group_readiness(self):
        a = self.role(max_pending_routine=1)
        parent = self.submit(a, 'Synthesis')
        group = self.post('/groups', {'group_id': 'pending', 'parent_id': parent}, a)
        self.assertEqual(group.status_code, 201, group.text)
        view = self.c.get('/tasks/' + parent)
        self.assertEqual(view.status_code, 200, view.text)
        self.assertIn('membership-open', view.text)
        self.post('/follow', {'selectors': {'tasks': [parent]}, 'preferences': {'mode': 'immediate'}}, a)
        for i in range(4):
            self.app.state.store.add_event('progress', 'dispatcher', {'step': i}, task_id=parent)
        replay = self.get('/replay?after=v1:0:0&limit=1', a)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertTrue(replay.json()['resync_required'])
        self.assertIn(parent, json_text(replay.json()['snapshot']))
        sse = self.c.get(BASE + '/events', headers={**a, 'Last-Event-ID': 'v1:0:0'})
        self.assertIn('event: resync', sse.text)


    def test_foreign_action_and_issue_claim_cannot_use_another_roles_work(self):
        a = self.role(action_allowlist=['task.comment'])
        b = self.role('beta', action_allowlist=['task.comment'])
        task = self.submit(a)
        self.app.state.store.approve(task)
        from drover.ledger import ops
        ops.set_setting(self.app.state.store.conn, 'dispatch_enabled', 'true')
        self.app.state.store.add_event('recovery.exhausted', 'system', {'cause': 'incident:1'}, task_id=task)
        issue = next(i for i in self.get('/inbox', a).json() if i['event_class'] == 'issue')
        lease = self.lease(b)
        self.assertIn(self.post('/issues/' + issue['delivery_id'] + '/claim', {'lease_token': lease}, b).status_code, (403, 409))
        events = self.app.state.store.list_events(task_id=task)
        body = {'lease_token': lease, 'checkpoint': {}, 'request_key': 'foreign-action',
            'actions': [{'kind': 'task.comment', 'payload': {'task_id': task, 'event_class': 'issue', 'expected_revision': events[-1]['id'], 'expected_status': 'ready', 'text': 'forged'}}]}
        self.assertEqual(self.post('/commit', body, b).status_code, 403)
        self.assertEqual(self.get('/health', b).json()['runtime']['checkpoint_revision'], 0)

    def test_long_poll_releases_write_transaction_and_revalidates_replaced_session(self):
        a = self.role()

        async def scenario():
            async def tick():
                while True:
                    await asyncio.sleep(.01)
            ticker = asyncio.create_task(tick())
            transport = httpx.ASGITransport(app=self.app, client=('testclient', 123))
            try:
                async with httpx.AsyncClient(transport=transport, base_url='http://testserver') as client:
                    polling = asyncio.create_task(client.get(BASE + '/inbox?wait=5', headers=a))
                    await asyncio.sleep(.05)
                    replaced = await asyncio.wait_for(client.post(BASE + '/roles/alpha/sessions', headers=SAME,
                        json={'session_id': 'new-session'}), timeout=2)
                    self.assertEqual(replaced.status_code, 201, replaced.text)
                    response = await asyncio.wait_for(polling, timeout=2)
                    self.assertEqual(response.status_code, 401, response.text)
            finally:
                ticker.cancel()
        asyncio.run(scenario())

    def test_http_submit_auto_follows_immediate_accepted_result_across_restart(self):
        a = self.role()
        task = self.submit(a, kind='research')
        def legacy(path, body, headers=WORKER):
            result = self.c.post('/api/v1' + path, headers=headers, json=body)
            self.assertLess(result.status_code, 300, result.text)
            return result.json()
        legacy('/tasks/' + task + '/approve', {}, SAME)
        legacy('/tasks/' + task + '/status', {'status': 'claimed'})
        run = legacy('/runs', {'task_id': task, 'role': 'worker', 'backend': 'local'})['id']
        legacy('/runs/' + str(run) + '/finish', {'outcome': 'read-only-done', 'note': 'Accepted evidence'})
        legacy('/tasks/' + task + '/status', {'status': 'needs-owner'})
        legacy('/tasks/' + task + '/status', {'status': 'landed'}, SAME)
        restarted = create_app(self.path, self.app.state.settings)
        self.addCleanup(restarted.state.store.close)
        messages = ASGIClient(restarted).get(BASE + '/inbox', headers=a).json()
        self.assertTrue(any(i['task_id'] == task and i['event_class'] == 'landed' for i in messages), messages)


    def test_empty_explicit_authorization_never_falls_back_to_owner(self):
        headers = {**SAME, 'Authorization': ''}
        self.assertEqual(self.c.get('/api/v1/tasks', headers=headers).status_code, 401)
        self.assertEqual(self.c.post(BASE + '/roles', headers=headers, json={'role_id': 'forged'}).status_code, 403)
        self.assertEqual(self.c.get('/inbox', headers=headers).status_code, 403)


    def test_sse_returns_normal_http_errors_before_streaming(self):
        from unittest.mock import patch
        a = self.role()
        self.assertEqual(self.get('/events', {'Authorization': 'Bearer invalid'}).status_code, 401)
        self.assertEqual(self.get('/events?after=malformed', a).status_code, 400)
        self.assertEqual(self.get('/events?after=', a).status_code, 400)
        self.assertEqual(self.c.get(BASE + '/events', headers={**a, 'Last-Event-ID': 'malformed'}).status_code, 400)
        self.owner('/roles/alpha/sessions', {'session_id': 'replacement'})
        self.assertEqual(self.get('/events', a).status_code, 401)
        issued = self.owner('/roles/alpha/sessions', {'session_id': 'expires'}).json()
        current = {'Authorization': 'Bearer ' + issued['credential']}
        with patch('drover.notifications.core.time.time', return_value=issued['expires_at'] + 1):
            self.assertEqual(self.get('/events', current).status_code, 401)
        self.assertEqual(self.get('/events', WORKER).status_code, 401)
        issued = self.owner('/roles/alpha/sessions', {'session_id': 'no-read', 'capabilities': ['notifications.tune']}).json()
        self.assertEqual(self.get('/events', {'Authorization': 'Bearer ' + issued['credential']}).status_code, 403)

    def test_nested_policy_and_preferences_reject_unknown_keys_and_invalid_types(self):
        a = self.role()
        sub = self.post('/follow', {'selectors': {}}, a).json()
        for key in ('role_id', 'selectors', 'self', 'unexpected'):
            with self.subTest(follow_key=key):
                result = self.post('/follow', {'selectors': {}, 'preferences': {key: 'beta'}}, a)
                self.assertEqual(result.status_code, 422, result.text)
        for key in ('subscription_id', 'expected_revision', 'self', 'unexpected'):
            with self.subTest(tune_key=key):
                result = self.c.patch(BASE + '/subscriptions/' + sub['id'], headers=a,
                    json={'expected_revision': 1, 'preferences': {key: 1}})
                self.assertEqual(result.status_code, 422, result.text)
        for policy in ({'role_id': 'beta'}, {'self': 'beta'}, {'unexpected': True},
                {'notifications': []}, {'notifications': None},
                {'notifications': {'digest_interval': True}}, {'notifications': {'detail_limit': True}},
                {'notifications': {'enabled': 'yes'}}, {'notifications': {'class_modes': []}},
                {'max_pending_routine': True}, {'action_allowlist': 'task.comment'}):
            with self.subTest(policy=policy):
                result = self.owner('/roles', {'role_id': 'invalid', 'policy': policy})
                self.assertEqual(result.status_code, 422, result.text)
        before = self.get('/health', a).json()['subscriptions']
        result = self.post('/follow', {'selectors': {}, 'preferences': {'mode': 'immediate', 'digest_interval': 30}}, a)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertGreaterEqual(len(self.get('/health', a).json()['subscriptions']), len(before))

    def test_replay_pages_retained_acknowledged_and_pending_notification_and_digest_history(self):
        import time
        from drover.notifications import Notifications
        for name, digests in (('alpha', False), ('digest', True)):
            with self.subTest(digests=digests):
                a = self.role(name, notifications={'digest_interval': 1})
                task = self.submit(a)
                if not digests:
                    self.post('/follow', {'selectors': {'tasks': [task]}, 'preferences': {'mode': 'immediate'}}, a)
                lease = self.lease(a)
                expected = []
                now = [time.time() + 2]
                service = Notifications(self.app.state.store.conn, now=lambda: now[0])
                def ack(messages, key):
                    body = {'lease_token': lease, 'checkpoint': {}, 'delivery_ids': [i['delivery_id'] for i in messages],
                        'delivery_versions': {i['delivery_id']: i['message_version'] for i in messages}, 'request_key': key}
                    result = self.post('/commit', body, a)
                    self.assertEqual(result.status_code, 200, result.text)
                for index in range(3):
                    self.app.state.store.add_event('progress', 'dispatcher', {'message': str(index)}, task_id=task)
                    if digests:
                        service.project()
                        service.digest(name)
                        now[0] += 2
                    pending = self.get('/inbox', a).json()
                    self.assertEqual(len(pending), 1, pending)
                    expected.append(pending[0]['delivery_id'])
                    if index < 2:
                        ack(pending, 'old-' + str(index))
                after, seen = 'v1:0:0', []
                for index in range(3):
                    batch = self.get('/replay?after=' + after + '&limit=1', a).json()
                    self.assertEqual(len(batch['items']), 1, batch)
                    message = batch['items'][0]['message']
                    self.assertEqual(message['delivery_id'], expected[index])
                    seen.append(message['delivery_id'])
                    ack([message], 'page-' + str(index))
                    after = batch['cursor']
                self.assertEqual(seen, expected)
                self.assertEqual(self.get('/replay?after=' + after + '&limit=1', a).json()['items'], [])

    def test_large_legal_digest_replays_over_http_and_sse_with_cursor_continuation(self):
        import json
        import time
        from drover.notifications import Notifications
        a = self.role(notifications={'digest_interval': 1})
        task = self.submit(a)
        service = Notifications(self.app.state.store.conn, now=lambda: time.time() + 2)
        for index in range(51):
            self.app.state.store.add_event('decision.recorded', 'dispatcher',
                {'decision': f'Decision {index}: ' + 'detail ' * 20}, task_id=task)
        service.project()
        digest = service.digest('alpha')
        self.assertEqual(len(digest['entries']), 50)
        expected, = self.get('/inbox', a).json()
        self.assertGreater(len(json.dumps(expected).encode()), 16384)
        self.assertLessEqual(len(json.dumps(expected).encode()), 32768)
        response = self.get('/replay?limit=1', a)
        self.assertEqual(response.status_code, 200, response.text)
        batch = response.json()
        self.assertEqual(batch['items'][0]['message'], expected)
        sse = self.get('/events?limit=1', a)
        self.assertEqual(sse.status_code, 200, sse.text)
        messages = [json.loads(line.removeprefix('data: ')) for line in sse.text.splitlines()
                    if line.startswith('data: ')]
        self.assertEqual(messages[0], expected)
        self.assertIn('id: ' + batch['cursor'], sse.text)
        lease = self.lease(a)
        ack = self.post('/commit', {'lease_token': lease, 'checkpoint': {},
            'delivery_ids': [expected['delivery_id']],
            'delivery_versions': {expected['delivery_id']: expected['message_version']},
            'request_key': 'large-digest'}, a)
        self.assertEqual(ack.status_code, 200, ack.text)
        self.assertEqual(self.get('/replay?after=' + batch['cursor'], a).json()['items'], [])

    def test_replay_authority_filtered_predecessor_rows_do_not_hide_later_owned_work(self):
        a, b = self.role(), self.role('beta')
        task = self.submit(a)
        self.post('/follow', {'selectors': {'tasks': [task]}, 'preferences': {'mode': 'immediate'}}, a)
        lease = self.lease(a)
        for index in range(3):
            self.app.state.store.add_event('progress', 'dispatcher', {'message': str(index)}, task_id=task)
        pending = self.get('/inbox', a).json()
        body = {'lease_token': lease, 'checkpoint': {}, 'delivery_ids': [i['delivery_id'] for i in pending],
                'delivery_versions': {i['delivery_id']: i['message_version'] for i in pending}, 'request_key': 'old', 'release': True}
        self.assertEqual(self.post('/commit', body, a).status_code, 200)
        self.assertEqual(self.owner('/ownership', {'task_id': task, 'role_id': 'beta', 'expected_revision': 1}).status_code, 200)
        newer = self.submit(a)
        self.post('/follow', {'selectors': {'tasks': [newer]}, 'preferences': {'mode': 'immediate'}}, a)
        self.app.state.store.add_event('progress', 'dispatcher', {'message': 'current-owned'}, task_id=newer)
        current = self.get('/replay?after=v1:0:0&limit=1', a).json()
        self.assertEqual(len(current['items']), 1, current)
        self.assertEqual(current['items'][0]['message']['task_id'], newer)
        self.assertFalse(current['more'])
        item = current['items'][0]['message']
        lease = self.lease(a)
        body = {'lease_token': lease, 'checkpoint': {}, 'delivery_ids': [item['delivery_id']],
                'delivery_versions': {item['delivery_id']: item['message_version']}, 'request_key': 'current'}
        self.assertEqual(self.post('/commit', body, a).status_code, 200)
        retained = self.get('/replay?after=v1:0:0&limit=1', a).json()
        self.assertEqual([i['message']['delivery_id'] for i in retained['items']], [item['delivery_id']])
        self.assertFalse(retained['more'])

    def test_replay_superseded_control_rows_cannot_hide_current_control(self):
        a = self.role()
        controls = []
        for version in range(3):
            control = self.owner('/controls/skill', {'skill': 'review', 'digest': str(version),
                'targets': ['alpha'], 'request_key': 'skill-' + str(version)})
            self.assertEqual(control.status_code, 200, control.text)
            controls.append(control.json()['control_id'])
            self.get('/inbox', a)
        batch = self.get('/replay?after=v1:0:0&limit=1', a).json()
        self.assertEqual([i['message']['control_id'] for i in batch['items']], [controls[-1]])
        self.assertFalse(batch['more'])
        # Old source cursors still reoffer the unacknowledged current requirement.
        again = self.get('/replay?after=' + batch['cursor'] + '&limit=1', a).json()
        self.assertEqual([i['message']['control_id'] for i in again['items']], [controls[-1]])


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
