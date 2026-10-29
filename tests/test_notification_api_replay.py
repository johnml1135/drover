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


from tests.test_notification_api import BASE, SAME, WORKER, json_text, _NotificationBase


class NotificationReplayTests(_NotificationBase):
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
