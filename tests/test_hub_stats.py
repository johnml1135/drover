"""Stats, seats and task lifecycle API/website integration."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from fastapi.testclient import TestClient
    from hub.app import create_app
    from hub.settings import Settings
except ImportError as exc:
    raise unittest.SkipTest(f'hub deps missing: {exc}')

from drover import config, stats, usage
from drover.ledger import ops


class StatsApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        settings = Settings(token='fictional-token', run_watchdog=False, run_dispatcher=False, run_services=False,
                            trusted_ui_hosts=('testclient',), tailscale=lambda a: '', ui_allowed_hosts=('testserver',))
        self.app = create_app(Path(self.tmp.name) / 'ledger.db', settings)
        self.addCleanup(self.app.state.store.close)
        self.c = TestClient(self.app, follow_redirects=False)
        self.owner = {'Sec-Fetch-Site': 'same-origin'}
        self.token = {'Authorization': 'Bearer fictional-token'}
        self.store = self.app.state.store
        self.tid = self.store.create_task('Stats for acme-app', 'acme-app')['id']

    def test_stats_endpoint_auth_filters_and_tab(self):
        rid = ops.start_run(self.store.conn, self.tid, 'worker', 'claude', 'sonnet-5.5')
        ops.finish_run(self.store.conn, rid, 'done', tokens_in=12, tokens_out=8)
        stats.sample(self.store.conn, {'facts': {'ram_available_gb': 8}})
        result = self.c.get('/api/v1/stats', headers=self.token)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['tasks'][self.tid]['tokens_by_model']['sonnet-5.5']['tokens_in'], 12)
        self.assertEqual(self.c.get('/api/v1/stats?task=absent', headers=self.token).json()['tasks'], {})
        self.assertEqual(self.c.get('/api/v1/stats?since=bad', headers=self.token).status_code, 400)
        page = self.c.get('/stats')
        self.assertEqual(page.status_code, 200)
        for text in ('plotly.js@2.35.2', 'chart-allowance', 'chart-tokens', 'chart-resources', 'chart-throughput', 'Seats'):
            self.assertIn(text, page.text)

    def test_manual_reading_alert_and_order(self):
        seat = config.get()['seats'][0]['name']
        usage.store(self.store.conn, seat, [], 'format changed', ok=False)
        self.assertIn('Allowance reading failed', self.c.get('/inbox').text)
        body = {'seat': seat, 'window': 'seven_day', 'pct': 23,
                'resets': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}
        response = self.c.post('/api/v1/usage', headers=self.token, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        response = self.c.get('/api/v1/usage/order', headers=self.token)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('raw_text', response.text)
        self.assertIn('23.0', self.c.get('/stats').text)
        self.assertEqual(self.store.usage_alerts(), [])
        body['pct'] = 101
        self.assertEqual(self.c.post('/api/v1/usage', headers=self.token, json=body).status_code, 400)
        response = self.c.post('/usage', headers=self.owner, data={**body, 'pct': 20})
        self.assertEqual(response.status_code, 303)

    def test_owner_archive_restore_delete_confirm_and_token_denial(self):
        url = '/api/v1/tasks/' + self.tid
        self.assertEqual(self.c.post(url + '/archive', headers=self.token, json={}).status_code, 403)
        response = self.c.post(url + '/archive', headers=self.owner, json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.c.get('/api/v1/tasks', headers=self.token).json(), [])
        self.assertEqual(len(self.c.get('/api/v1/tasks?include_archived=true', headers=self.token).json()), 1)
        self.assertIn('Restore', self.c.get('/stats').text)
        response = self.c.post('/tasks/' + self.tid + '/archive', data={'archived': '0'}, headers=self.owner)
        self.assertEqual(response.status_code, 303)
        page = self.c.get('/tasks/' + self.tid + '/delete')
        self.assertIn('Permanently delete', page.text)
        for headers, body, code in ((self.token, {'confirm': True}, 403), (self.owner, {'confirm': False}, 400)):
            self.assertEqual(self.c.request('DELETE', url, headers=headers, json=body).status_code, code)
        response = self.c.post('/tasks/' + self.tid + '/delete', headers=self.owner, data={'confirm': '1'})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.c.get(url, headers=self.token).status_code, 404)

    def test_maintenance_rolls_samples_and_keeps_aggregates_on_delete(self):
        from drover.dispatcher import Dispatcher
        from drover.port import DirectPort
        from tests.test_dispatcher import FakeLauncher
        d = Dispatcher(DirectPort(self.store.conn), launcher=FakeLauncher(),
                       resources_fn=lambda **kw: {'facts': {'ram_available_gb': 12}})
        d._seat_tick()
        self.assertEqual(len(stats.query(self.store.conn)['hourly']), 1)
        rid = ops.start_run(self.store.conn, self.tid, 'worker', 'codex', 'gpt-6-luna')
        ops.finish_run(self.store.conn, rid, 'done', tokens_in=30)
        expected = stats.query(self.store.conn)['hourly']
        self.store.delete_task(self.tid, confirm=True)
        self.assertEqual(stats.query(self.store.conn)['hourly'], expected)

    def test_archive_hides_open_question_and_unread_count_until_restored(self):
        from hub.store import UiView
        from hub.web import inbox_count
        view = UiView(self.store)
        # Approval removes the separate import proposal from the Inbox count.
        self.store.approve(self.tid)
        q = self.store.ask_question('task', self.tid, 'Choose acme-app option?')
        self.assertEqual(inbox_count(view), 1)
        url = '/api/v1/tasks/' + self.tid + '/archive'
        self.assertEqual(self.c.post(url, headers=self.owner, json={}).status_code, 200)
        self.assertNotIn(q['title'], self.c.get('/inbox').text)
        self.assertEqual(inbox_count(view), 0)
        self.assertEqual(ops.get_question(self.store.conn, q['id'])['status'], 'open')
        self.assertEqual(self.c.post(url, headers=self.owner, json={'archived': False}).status_code, 200)
        self.assertIn(q['title'], self.c.get('/inbox').text)
        self.assertEqual(inbox_count(view), 1)
