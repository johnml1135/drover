"""Cross-component restart and real zero-model consumer disconnect acceptance.

The process fixture uses private pipes to proxy authenticated requests into the
real ASGI app. It proves this controlled adapter contract, not model CLI support
or Windows/WSL network reachability.
"""
import json
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_notification_api import ASGIClient, BASE, SAME, WORKER
from hub.app import create_app
from hub.settings import Settings
from drover.directors import DirectorRuntime
from drover.ledger import ops
from drover.notifications import Notifications
from drover.port import DirectPort


# Credentials and lease tokens travel only on the fixture-owned anonymous pipes.
# The consumer deliberately exits after its first checkpoint without acking.
CONSUMER = r'''
import json, sys
initial = json.loads(sys.stdin.readline())
def request(method, path, body=None, status=200):
    print(json.dumps(dict(method=method, path=path, body=body,
                         authorization='Bearer ' + initial['credential'])), flush=True)
    response = json.loads(sys.stdin.readline())
    assert response['status'] == status
    return response['body']
if initial['first']:
    request('GET', '/checkpoint', status=404)
    checkpoint = {}
else:
    checkpoint = request('GET', '/checkpoint')['checkpoint']
lease = request('POST', '/lease', {})['lease_token']
messages = request('GET', '/inbox')
assert messages
if initial['first']:
    assert checkpoint == {}
    request('POST', '/commit', dict(lease_token=lease, checkpoint={'phase':'saved'},
        delivery_ids=[], actions=[], request_key='before-disconnect'))
    print(json.dumps({'summary': {'ids':[m['delivery_id'] for m in messages]}}), flush=True)
    sys.exit(23)
assert checkpoint == {'phase':'saved'}
body = dict(lease_token=lease, checkpoint={'phase':'complete'},
    delivery_ids=[m['delivery_id'] for m in messages],
    delivery_versions={m['delivery_id']:m['message_version'] for m in messages},
    actions=[], request_key='after-replacement', release=True)
receipt = request('POST', '/commit', body)
assert request('POST', '/commit', body) == receipt
print(json.dumps({'summary': {'ids':body['delivery_ids'], 'replayed':True}}), flush=True)
'''


class ProcessHandle:
    def __init__(self, process):
        self.process = process

    def poll(self):
        code = self.process.poll()
        if code is None:
            return None
        return dict(status='done' if code == 0 else 'error',
                    usage=dict(tokens_in=0, tokens_out=0))


class PipeConsumerAdapter:
    capabilities = dict(live_offer=False, verified_resume=False, fresh_start=True,
                        enforced_token_cap=True, authenticated_processing=True, secure_handoff=True)

    def __init__(self, client):
        self.client = client
        self.invocations = []
        self.handles = {}

    def launch(self, spec, credential, guard):
        guard()
        process = subprocess.Popen([sys.executable, '-u', '-c', CONSUMER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        handle = ProcessHandle(process)
        self.handles[spec['nonce']] = handle
        invocation = dict(spec=spec, summary=None, returncode=None, requests=[])
        self.invocations.append(invocation)
        lines = queue.Queue()
        def read():
            for line in process.stdout:
                lines.put(line)
            lines.put(None)
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        def send(value):
            process.stdin.write(json.dumps(value) + '\n')
            process.stdin.flush()
        try:
            send(dict(credential=credential, first=len(self.invocations) == 1))
            for _ in range(12):
                line = lines.get(timeout=5)
                if line is None:
                    break
                message = json.loads(line)
                if 'summary' in message:
                    invocation['summary'] = message['summary']
                    continue
                response = self.client.request(message['method'], BASE + message['path'],
                    headers={'Authorization': message['authorization']},
                    **({'json': message['body']} if message['body'] is not None else {}))
                invocation['requests'].append((message['method'], message['path'], response.status_code))
                send(dict(status=response.status_code, body=response.json()))
            invocation['returncode'] = process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            reader.join(timeout=5)
            process.stdin.close()
            process.stdout.close()
        return handle

    def probe(self, spec):
        handle = self.handles.get(spec['nonce'])
        return 'unknown' if handle is None else 'gone' if handle.poll() else 'alive'

    def adopt(self, spec):
        return self.handles[spec['nonce']]

    def stop(self, spec):
        handle = self.handles[spec['nonce']]
        if handle.process.poll() is None:
            handle.process.terminate()
        return True


class NotificationAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'ledger.db'
        self.clock = patch('drover.notifications.core.time.time', return_value=1000.)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.settings = Settings(token='worker', run_watchdog=False, run_dispatcher=False,
            run_services=False, trusted_ui_hosts=('testclient',), ui_allowed_hosts=('testserver',),
            use_tailscale=False)
        self.open_hub()

    def open_hub(self):
        self.app = create_app(self.path, self.settings)
        self.addCleanup(self.app.state.store.close)
        self.client = ASGIClient(self.app)

    def request(self, method, path, headers, body=None, status=200):
        response = self.client.request(method, path, headers=headers,
            **({'json': body} if body is not None else {}))
        self.assertEqual(response.status_code, status)
        return response.json()

    def role(self, name='research', **policy):
        self.request('POST', BASE + '/roles', SAME, {'role_id':name,
            'policy': {'allowed_models':['zero-model'], 'token_budget':100, 'wake_limit_per_hour':4, **policy}}, 201)
        session = self.request('POST', BASE + '/roles/' + name + '/sessions', SAME,
                               {'session_id':'initial'}, 201)
        return {'Authorization':'Bearer ' + session['credential']}

    def accepted_result(self, headers):
        task = self.request('POST', BASE + '/tasks', headers, {'title':'Findings', 'kind':'research'}, 201)['id']
        self.accept(task)
        return task

    def accept(self, task):
        self.request('POST', '/api/v1/tasks/' + task + '/approve', SAME, {})
        self.request('POST', '/api/v1/tasks/' + task + '/status', WORKER, {'status':'claimed'})
        run = self.request('POST', '/api/v1/runs', WORKER,
            {'task_id':task, 'role':'worker', 'backend':'local'}, 201)['id']
        self.request('POST', f'/api/v1/runs/{run}/finish', WORKER,
                     {'outcome':'read-only-done', 'note':'Accepted findings'})
        self.request('POST', '/api/v1/tasks/' + task + '/status', WORKER, {'status':'needs-owner'})
        self.request('POST', '/api/v1/tasks/' + task + '/status', SAME, {'status':'landed'})

    def test_completion_offered_before_hub_restart_commits_and_retries_after_restart(self):
        headers = self.role()
        task = self.accepted_result(headers)
        messages = self.request('GET', BASE + '/inbox', headers)
        self.assertTrue(any(m['task_id'] == task and m['event_class'] == 'landed' for m in messages))
        self.app.state.store.close()
        self.open_hub()
        self.assertEqual(self.request('GET', BASE + '/inbox', headers), messages)
        lease = self.request('POST', BASE + '/lease', headers, {})
        body = dict(lease_token=lease['lease_token'], checkpoint={'phase':'complete'},
            delivery_ids=[m['delivery_id'] for m in messages],
            delivery_versions={m['delivery_id']:m['message_version'] for m in messages},
            actions=[], request_key='after-restart', release=True)
        receipt = self.request('POST', BASE + '/commit', headers, body)
        self.assertEqual(self.request('POST', BASE + '/commit', headers, body), receipt)
        self.assertEqual(self.request('GET', BASE + '/inbox', headers), [])
        self.assertEqual(self.request('GET', BASE + '/checkpoint', headers)['checkpoint'], {'phase':'complete'})

    def test_foreign_accepted_join_wakes_dependent_owner_without_granting_child_scope(self):
        parent_owner, child_owner = self.role(), self.role('child-owner')
        parent = self.request('POST', BASE + '/tasks', parent_owner, {'title':'Synthesis', 'kind':'research'}, 201)['id']
        self.request('POST', '/api/v1/tasks/' + parent + '/approve', SAME, {})
        child = self.request('POST', BASE + '/tasks', child_owner, {'title':'Finding', 'kind':'research'}, 201)['id']
        unrelated = self.accepted_result(child_owner)
        group = self.request('POST', BASE + '/owner/groups', SAME,
            {'group_id':'join', 'role_id':'research', 'parent_id':parent}, 201)
        group = self.request('POST', BASE + '/owner/groups/join/enroll', SAME,
            {'child_id':child, 'expected_revision':group['revision'], 'request_key':'enroll'})
        self.request('POST', BASE + '/groups/join/seal', parent_owner,
            {'expected_revision':group['revision'], 'request_key':'seal'})
        self.assertFalse(self.request('GET', BASE + '/groups/join', parent_owner)['ready'])
        self.accept(child)
        messages = self.request('GET', BASE + '/inbox', parent_owner)
        ready, = [m for m in messages if m['event_class'] == 'group-ready']
        self.assertEqual(ready['group_id'], 'join')
        self.assertIsNone(ready['task_id'])
        self.assertEqual([m['task_id'] for m in ready['evidence']['manifest']], [child])
        self.assertNotIn(unrelated, json.dumps(messages))
        self.assertFalse(any(m.get('task_id') == child for m in messages))
        self.request('POST', BASE + '/follow', parent_owner,
                     {'selectors':{'tasks':[child]}}, status=409)
        self.assertEqual(ops.get_task(self.app.state.store.conn, parent)['status'], 'ready')
        self.assertFalse(ops.events(self.app.state.store.conn, task_id=parent, kind='task.result-accepted'))

    def test_fifteen_minute_service_digests_are_bounded_and_wakes_obey_separate_budget(self):
        ordinary = self.role()
        waking = self.role('waking', notifications={'digest_wake':True}, token_budget=0)
        for headers in (ordinary, waking):
            task = self.request('POST', BASE + '/tasks', headers, {'title':'Routine work'}, 201)['id']
            for n in range(51):
                self.app.state.store.add_event('decision.recorded', 'worker',
                    {'text':f'decision {n}', 'state':'committed'}, task_id=task)
        conn = self.app.state.store.conn
        ops.set_setting(conn, 'dispatch_enabled', 'true')
        ops.set_setting(conn, 'director_execution_enabled', 'true')
        config = dict(backend='local', model='zero-model', seat='fixture', max_tokens=20,
                      placement='windows', cwd=self.tmp.name)
        ops.set_setting(conn, 'director_execution', json.dumps({'research':config, 'waking':config}))
        adapter = PipeConsumerAdapter(self.client)
        runtime = DirectorRuntime(DirectPort(conn), adapter, now=lambda:self.now.return_value)
        service = Notifications(conn)
        capacity = dict(slots=2, admit=lambda spec:None)
        for _ in range(2):
            self.assertEqual(runtime.tick(service.admission(), capacity)['service']['digests'], [])
        self.now.return_value = 1900.
        result = runtime.tick(service.admission(), capacity)
        self.assertEqual(len(result['service']['digests']), 2)
        for digest in result['service']['digests']:
            self.assertEqual(digest['counts']['decision-recorded'], 51)
            self.assertEqual(len(digest['entries']), 50)
            self.assertEqual(digest['entries'][-1]['evidence']['text'], 'decision 50')
            self.assertLessEqual(len(json.dumps(digest).encode()), 32768)
        self.assertFalse(service.role_state('research')['pending_wake'])
        self.assertEqual(service.role_state('waking')['held_reason'], 'token-budget-exhausted')
        self.assertEqual(adapter.invocations, [])
        self.now.return_value = 2800.
        self.assertEqual(runtime.tick(service.admission(), capacity)['service']['digests'], [])
        self.assertEqual(adapter.invocations, [])

    def test_process_disconnect_replacement_restores_checkpoint_and_unread_without_child_restart(self):
        headers = self.role()
        child = self.request('POST', BASE + '/tasks', headers, {'title':'Running child'}, 201)['id']
        conn = self.app.state.store.conn
        ops.approve(conn, child)
        ops.claim_next(conn)
        worker = ops.start_run(conn, child, 'worker', 'local')
        before = ops.get_task(conn, child)
        self.accepted_result(headers)
        ops.set_setting(conn, 'dispatch_enabled', 'true')
        ops.set_setting(conn, 'director_execution_enabled', 'true')
        ops.set_setting(conn, 'director_execution', json.dumps({'research': dict(
            backend='local', model='zero-model', seat='fixture', max_tokens=20,
            placement='windows', cwd=self.tmp.name)}))
        adapter = PipeConsumerAdapter(self.client)
        service = Notifications(conn, now=lambda:1000.)
        runtime = DirectorRuntime(DirectPort(conn), adapter, now=lambda:1000.)
        capacity = dict(slots=1, admit=lambda spec:None)
        self.assertEqual(len(runtime.tick(service.admission(), capacity)['launched']), 1)
        first, = adapter.invocations
        self.assertEqual(first['returncode'], 23, first['requests'])
        self.assertIsNotNone(first['summary'])
        self.assertEqual(service.get_checkpoint('research')['checkpoint'], {'phase':'saved'})
        self.assertEqual([m['delivery_id'] for m in service.receive('research')], first['summary']['ids'])
        runtime = DirectorRuntime(DirectPort(conn), adapter, now=lambda:1000.)
        self.assertEqual(len(runtime.tick(service.admission(), capacity)['launched']), 1)
        second = adapter.invocations[1]
        self.assertEqual(second['returncode'], 0, second['requests'])
        self.assertEqual(second['summary'], dict(ids=first['summary']['ids'], replayed=True))
        self.assertEqual(second['spec']['generation'], first['spec']['generation'] + 1)
        self.assertEqual(second['spec']['checkpoint'], {'phase':'saved'})
        runtime.tick(service.admission(), capacity)
        self.assertEqual(runtime.active_runs(), [])
        self.assertEqual(len(adapter.invocations), 2)
        self.assertEqual(service.receive('research'), [])
        self.assertEqual(service.get_checkpoint('research')['checkpoint'], {'phase':'complete'})
        self.assertEqual(service.role_state('research')['replacements_used'], 1)
        self.assertEqual(ops.get_task(conn, child), before)
        self.assertEqual([(r['id'], r['ended']) for r in ops.list_runs(conn, child)], [(worker, None)])
        for invocation in adapter.invocations:
            spec = invocation['spec']
            run = next(r for r in ops.list_runs(conn, spec['task']['id']) if r['id'] == spec['run_id'])
            self.assertEqual((run['tokens_in'], run['tokens_out']), (0, 0))

    def test_dispatcher_retains_missing_seat_and_exhausted_allowance_without_launching(self):
        from drover import config
        from tests.dispatch_fixture import FakePipeline, Observations, make_dispatcher
        from tests.test_dispatcher import FakeLauncher
        headers = self.role()
        self.accepted_result(headers)
        conn = self.app.state.store.conn
        port = DirectPort(conn)
        ops.set_setting(conn, 'dispatch_enabled', 'true')
        ops.set_setting(conn, 'director_execution_enabled', 'true')
        adapter = PipeConsumerAdapter(self.client)
        fixture_now = 1_700_000_000.0
        dispatcher = make_dispatcher(port, launcher=FakeLauncher(),
            pipeline=FakePipeline(port, Path(self.tmp.name) / 'runs'), now=lambda:fixture_now,
            usage_poller=Observations(conn, lambda:fixture_now, lambda backend:{'remaining_pct':90}, lambda backend:False))
        dispatcher.directors = DirectorRuntime(port, adapter, now=lambda:fixture_now)
        seat = config.director_seats()[0]
        configuration = dict(backend=seat['backend'], model='zero-model', seat='absent-fixture-seat',
                             max_tokens=20, placement=seat['placement'], cwd=self.tmp.name)
        service = Notifications(conn)
        for seat_name, reason in (('absent-fixture-seat', 'seat-not-configured'),
                                  (seat['name'], 'allowance-unavailable')):
            configuration['seat'] = seat_name
            ops.set_setting(conn, 'director_execution', json.dumps({'research':configuration}))
            for _ in range(2):
                dispatcher.tick()
                state = service.role_state('research')
                self.assertEqual(state['held_reason'], reason)
                self.assertEqual(state['tokens_reserved'], 0)
                self.assertTrue(state['pending_wake'])
                self.assertEqual(adapter.invocations, [])


if __name__ == '__main__':
    unittest.main()
