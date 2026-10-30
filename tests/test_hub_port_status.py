import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import ledger_fixture
from hub.dispatch import HubPort
from hub.events import Bus
from hub.store import Store


class HubPortStatusEvents(unittest.TestCase):
    def test_every_engine_stage_publishes_one_task_status(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bus = Bus()
        store = Store(Path(tmp.name) / 'l.db', publish=bus.publish)
        self.addCleanup(store.close)
        tid = store.create_task('t', 'p', scope_files=['fixture-1'])['id']
        store.approve(tid)
        port = HubPort(store)
        def count(status):
            return [e for e in bus.recent() if e['kind'] == 'task.status' and e['data'].get('status') == status]
        with patch.object(ledger_fixture, 'advance_stage', lambda conn, *a, **k: port.advance_stage(*a, **k)), \
                patch.object(ledger_fixture, 'land_stage', lambda conn, *a: port.land_stage(*a)):
            ledger_fixture.stage(store.conn, tid, 'reviewed')
            ledger_fixture.stage(store.conn, tid, 'landed')
        for status in ('gated', 'reviewed', 'landed'):
            events = count(status)
            self.assertEqual(len(events), 1, status)
            self.assertEqual(events[0]['data'], {'id': tid, 'status': status})

    def test_landing_batch_publishes_one_landed_status_through_the_hub_port(self):
        from drover import ci, landing
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bus = Bus()
        store = Store(Path(tmp.name) / 'l.db', publish=bus.publish)
        self.addCleanup(store.close)
        tid = store.create_task('t', 'p', scope_files=['fixture-1'])['id']
        store.approve(tid)
        ledger_fixture.stage(store.conn, tid, 'reviewed')
        tip = 'b' * 40
        task = {'id': tid, 'branch': 'drover/t', 'head': ledger_fixture.SHA, 'last_sha': tip}
        receipt = {'version': ci.VERSION, 'passed': True, 'sha': tip, 'log': 'fixture'}
        result = {'outcome': None, 'landed': [], 'required_platforms': ['wsl']}
        with patch.object(landing, 'branch_head', return_value=task['head']), \
                patch.object(landing, '_control_guard'), patch.object(landing, 'sync_main', return_value=None), \
                patch.object(landing, 'local_fast_forward'):
            landing._land(store.conn, 'repo', 'work', [task], tip, receipt, result, 'origin', 'main',
                          land_stage=HubPort(store, actor='landing').land_stage)
        self.assertEqual(result['landed'], [tid])
        landed = [e for e in bus.recent() if e['kind'] == 'task.status' and e['data']['status'] == 'landed']
        self.assertEqual([e['data'] for e in landed], [{'id': tid, 'status': 'landed'}])
