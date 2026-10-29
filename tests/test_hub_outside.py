"""The owner API exposes external landing without granting workers the operation."""
from types import SimpleNamespace
from unittest import TestCase, mock

from fastapi.testclient import TestClient
from drover.ledger import ops
from tests.test_hub_api import AUTH, SAME, make
from tests.test_ledger_outside import BASE, PROG, SHA


class OutsideLandingApiTests(TestCase):
    def test_owner_route_and_token_denial(self):
        tmp, app = make(trusted_ui_hosts=('testclient',))
        self.addCleanup(tmp.cleanup)
        self.addCleanup(app.state.store.close)
        client = TestClient(app)
        store = app.state.store
        tid = store.create_task('outside', repo='p')['id']
        store.approve(tid)
        store.set_status(tid, 'needs-owner', 'worker', 'landed elsewhere')
        body = {'sha': SHA, 'base_ref': 'origin/main', 'gate': 'passed', 'review': 'approved'}
        path = f'/api/v1/tasks/{tid}/external-landing'
        self.assertEqual(client.post(path, json=body, headers=AUTH).status_code, 403)
        self.assertEqual(store.get_task(tid)['status'], 'needs-owner')
        def git(repo, *args, **kw):
            return SimpleNamespace(returncode=0, stdout='commit' if args[0] == 'cat-file' else BASE)
        with mock.patch('drover.repos.load_all', return_value={'p': PROG}), mock.patch('drover.placement.git', side_effect=git):
            response = client.post(path, json=body, headers=SAME)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['status'], 'landed')
        before = list(app.state.bus.recent())
        with mock.patch('drover.repos.load_all', return_value={'p': PROG}), mock.patch('drover.placement.git', side_effect=git):
            repeated = client.post(path, json=body, headers=SAME)
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(app.state.bus.recent(), before)
        events = ops.events(store.conn, task_id=tid, kind='status')
        self.assertEqual(events[-1]['data']['external_landing']['sha'], SHA)
