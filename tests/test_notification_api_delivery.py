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


from tests.test_notification_api import ASGIClient, BASE, SAME, WORKER, json_text, _NotificationBase


class NotificationDeliveryTests(_NotificationBase):
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
