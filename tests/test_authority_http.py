"""The authenticated status route cannot assert engine success."""
import unittest

from fastapi.testclient import TestClient
from tests.test_hub_api import AUTH, SAME, make
from drover.ledger import ops


class AuthorityHTTP(unittest.TestCase):
    def test_owner_and_shared_token_cannot_assert_engine_stages(self):
        tmp, app = make(trusted_ui_hosts=('testclient',))
        self.addCleanup(tmp.cleanup)
        self.addCleanup(app.state.store.close)
        client = TestClient(app)
        store = app.state.store
        tid = store.create_task('Scoped work', scope_files=['src'])['id']
        store.approve(tid)
        for old, stage in [('running', 'gated'), ('gated', 'reviewed'), ('reviewed', 'landed')]:
            with ops.tx(store.conn):
                store.conn.execute('UPDATE tasks SET status=? WHERE id=?', (old, tid))
            before = ops.events(store.conn, task_id=tid)
            for headers in (AUTH, SAME):
                response = client.post(f'/api/v1/tasks/{tid}/status', json={'status': stage}, headers=headers)
                self.assertGreaterEqual(response.status_code, 400, response.text)
                self.assertEqual(store.get_task(tid)['status'], old)
                self.assertEqual(ops.events(store.conn, task_id=tid), before)

    def test_approving_an_unscoped_writable_task_is_a_400_that_says_how_to_fix_it(self):
        tmp, app = make(trusted_ui_hosts=('testclient',))
        self.addCleanup(tmp.cleanup)
        self.addCleanup(app.state.store.close)
        client = TestClient(app)
        store = app.state.store
        tid = store.create_task('Unscoped work')['id']
        response = client.post(f'/api/v1/tasks/{tid}/approve', headers=SAME)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn('add scope_files or mark the task read-only', response.text)
        self.assertEqual(store.get_task(tid)['status'], 'proposed')
        skipped = store.approve_tasks([tid])['results'][0]
        self.assertEqual(skipped['result'], 'skipped')
        self.assertIn('add scope_files or mark the task read-only', skipped['reason'])
        response = client.post(f'/items/task/{tid}/approve', headers=SAME)
        self.assertEqual(response.status_code, 400, response.text)
