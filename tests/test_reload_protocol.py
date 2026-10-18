"""Standalone protocol records, admission fencing and durable replay."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from hub.reload_protocol import (
    PROTOCOL, RUN_FORMAT, ActivityBarrier, Journal, ProtocolError,
    atomic_json, validate_record,
)

A = 'a' * 40
B = 'b' * 40
DIGEST = 'c' * 64
OP = '20000000-0000-4000-8000-000000000001'
INSTANCE = '10000000-0000-4000-8000-000000000001'
CHALLENGE = '30000000-0000-4000-8000-000000000001'


def event_data(generation=12):
    return dict(operation_id=OP, request_id=STATE['request']['id'],
                generation=generation, from_sha=A, target_sha=B, active_sha=A,
                phase='validating', reason=None, actor='parent')


def enrollment():
    return dict(protocol=1, source_root='C:/src/drover', common_dir='C:/src/drover/.git',
                default_branch='main', interpreter=dict(path='C:/venv/python.exe', identity=DIGEST),
                paths={k: 'C:/state/' + k for k in
                       ('ledger', 'runs', 'repo_config', 'config_home', 'token', 'overlay')},
                ledger_schema=4, required_schema=4, bootstrap_digest=DIGEST)


def release():
    return dict(protocol=1, run_format=1, sha=A, tree_digest=DIGEST,
                content_digest=DIGEST, ledger_schema=4, required_schema=4,
                dependency_digests={'hub/uv.lock': DIGEST, 'hub/pyproject.toml': DIGEST},
                schema_digest=DIGEST, bootstrap_digest=DIGEST,
                files={'hub/__init__.py': DIGEST}, release_path='C:/state/releases/' + A)


STATE = {'protocol': 1,
 'operation_id': '20000000-0000-4000-8000-000000000001',
 'generation': 12,
 'phase': 'validating',
 'request': {'protocol': 1,
             'id': '00000000-0000-4000-8000-000000000042',
             'key': 'landing:drover:42:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
             'action': 'request',
             'source': 'landing',
             'operation_id': None,
             'generation': 10,
             'instance': '10000000-0000-4000-8000-000000000001',
             'from_sha': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
             'target_sha': 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
             'batch_id': 42,
             'auto': True,
             'enabled': None,
             'created_at': '2026-10-17T18:00:00Z'},
 'active': {'sha': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            'manifest_digest': 'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
            'instance': '10000000-0000-4000-8000-000000000001',
            'healthy_at': '2026-10-17T17:00:00Z'},
 'previous': None,
 'child': {'sha': 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
           'instance': '10000000-0000-4000-8000-000000000002',
           'pid': 7200,
           'creation_identity': 'spawn-12'},
 'handoff_schema': {'operation_id': '20000000-0000-4000-8000-000000000001',
                    'generation': 11,
                    'instance': '10000000-0000-4000-8000-000000000001',
                    'creation_identity': 'spawn-10',
                    'ledger_schema': 4,
                    'challenge': '30000000-0000-4000-8000-000000000001',
                    'heartbeat_seq': 81},
 'auto_enabled': True,
 'owner_dispatch_enabled': True,
 'blockers': [],
 'drained': {'generation': 11,
             'open_gate_ids': [],
             'active_sections': 0,
             'landing_idle': True,
             'services_idle': True,
             'recovery_complete': True,
             'scan_known': True,
             'unsafe_detached_ids': [],
             'unresolved_launches': [],
             'wsl_live_run_ids': [612],
             'participant_generations': {'dispatcher': 11,
                                         'landing': 11,
                                         'services': 11,
                                         'api': 11}},
 'attempts': {'window_started': '2026-10-17T18:00:00Z', 'count': 1, 'fallback_used': False},
 'quarantined_sha': None,
 'updated_at': '2026-10-17T18:00:06Z',
 'last_journal_seq': 104}


CHILD = {'protocol': 1,
 'operation_id': '20000000-0000-4000-8000-000000000001',
 'generation': 12,
 'instance': '10000000-0000-4000-8000-000000000002',
 'pid': 7200,
 'creation_identity': 'spawn-12',
 'runtime_sha': 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
 'phase': 'validating',
 'ledger_schema': 4,
 'heartbeat_seq': 3,
 'challenge': '30000000-0000-4000-8000-000000000002',
 'recovery_complete': True,
 'dispatcher_lock': True,
 'barrier': {'active_sections': 0,
             'open_gate_ids': [],
             'landing_idle': True,
             'services_idle': True,
             'recovery_complete': True,
             'scan_known': True,
             'unsafe_detached_ids': [],
             'unresolved_launches': [],
             'wsl_live_run_ids': [612],
             'participant_generations': {'dispatcher': 12,
                                         'landing': 12,
                                         'services': 12,
                                         'api': 12}},
 'blockers': []}


class BarrierTests(unittest.TestCase):
    def test_stale_generation_cannot_reopen(self):
        barrier = ActivityBarrier()
        barrier.close(12)
        barrier.reopen(11)
        self.assertTrue(barrier.snapshot()['closed'])
        self.assertIsNone(barrier.try_enter('claim'))
        barrier.reopen(13)
        self.assertTrue(barrier.snapshot()['closed'])
        barrier.reopen(12)
        self.assertIsNotNone(barrier.try_enter('claim'))

    def test_acquisition_counts_before_enter_and_exception_releases(self):
        barrier = ActivityBarrier()
        section = barrier.try_enter('launch')
        self.assertEqual(barrier.snapshot()['counts'], {'launch': 1})
        barrier.close(1)
        self.assertIsNone(barrier.try_enter('launch'))
        with self.assertRaisesRegex(RuntimeError, 'worker'):
            with section:
                raise RuntimeError('worker')
        self.assertEqual(barrier.snapshot()['active_sections'], 0)
        # Double exit must not decrement another section.
        section.__exit__(None, None, None)
        self.assertEqual(barrier.snapshot()['active_sections'], 0)

    def test_controls_are_counted_until_sealed(self):
        barrier = ActivityBarrier()
        barrier.close(2)
        control = barrier.try_enter('preference', control=True)
        self.assertEqual(barrier.snapshot()['active_sections'], 1)
        barrier.seal(1)
        self.assertFalse(barrier.snapshot()['sealed'])
        barrier.seal(2)
        self.assertIsNone(barrier.try_enter('preference', control=True))
        with control:
            pass
        barrier.close(1)
        barrier.reopen(1)
        self.assertTrue(barrier.snapshot()['sealed'])
        barrier.reopen(2)
        self.assertFalse(barrier.snapshot()['sealed'])
        self.assertIsNotNone(barrier.try_enter('work'))

    def test_close_races_with_acquisition(self):
        for _ in range(30):
            barrier = ActivityBarrier()
            start = threading.Barrier(3)
            sections = []
            def acquire():
                start.wait()
                sections.append(barrier.try_enter('claim'))
            def close():
                start.wait()
                barrier.close(1)
            with ThreadPoolExecutor(max_workers=2) as pool:
                a, c = pool.submit(acquire), pool.submit(close)
                start.wait()
                a.result(timeout=5)
                c.result(timeout=5)
            self.assertIsNone(barrier.try_enter('claim'))
            self.assertEqual(barrier.snapshot()['active_sections'], int(sections[0] is not None))
            if sections[0] is not None:
                with sections[0]:
                    pass
            self.assertEqual(barrier.snapshot()['active_sections'], 0)


class RecordTests(unittest.TestCase):
    def test_worked_examples_and_complete_contracts(self):
        self.assertEqual((PROTOCOL, RUN_FORMAT), (1, 1))
        for kind, record in [('state', STATE), ('child', CHILD),
                             ('enrollment', enrollment()), ('release', release())]:
            with self.subTest(kind=kind):
                self.assertEqual(validate_record(kind, record), record)
        self.assertIsNone(validate_record('state', STATE)['request']['operation_id'])
        self.assertEqual(validate_record('state', STATE)['handoff_schema'], STATE['handoff_schema'])

    def test_control_identity_required(self):
        for action in ('request', 'cancel', 'retry', 'stop', 'set_auto'):
            request = copy.deepcopy(STATE['request'])
            request.update(action=action, operation_id=OP, enabled=True if action == 'set_auto' else None)
            self.assertEqual(validate_record('request', request), request)
            for key in ('operation_id', 'generation', 'instance'):
                bad = dict(request)
                del bad[key]
                with self.subTest(action=action, key=key), self.assertRaises(ValueError):
                    validate_record('request', bad)
            if action in ('cancel', 'retry'):
                request['operation_id'] = None
                with self.assertRaises(ValueError):
                    validate_record('request', request)

    def test_child_schema_required(self):
        for schema in (True, False, -1, '4', 4.0):
            bad = dict(CHILD, ledger_schema=schema)
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                validate_record('child', bad)
        bad = dict(CHILD)
        del bad['ledger_schema']
        with self.assertRaises(ValueError):
            validate_record('child', bad)
        self.assertIsNone(validate_record('child', dict(CHILD, ledger_schema=None))['ledger_schema'])

    def test_required_schema_pair(self):
        for kind, fixture in [('enrollment', enrollment), ('release', release)]:
            for field in ('ledger_schema', 'required_schema'):
                for value in ('missing', True, False, -1, 3, None, 4.0):
                    bad = fixture()
                    if value == 'missing':
                        del bad[field]
                    else:
                        bad[field] = value
                    with self.subTest(kind=kind, field=field, value=value), self.assertRaises(ValueError):
                        validate_record(kind, bad)
            for schema in (0, 4):
                good = fixture()
                good.update(ledger_schema=schema, required_schema=schema)
                self.assertEqual(validate_record(kind, good), good)
            with self.assertRaises(ValueError):
                validate_record(kind, dict(protocol=1, ledger_schema=4, required_schema=4))

    def test_missing_fields_in_full_examples_reject(self):
        for kind, record in [('state', STATE), ('child', CHILD), ('request', STATE['request']),
                             ('enrollment', enrollment()), ('release', release())]:
            for key in record:
                bad = copy.deepcopy(record)
                del bad[key]
                with self.subTest(kind=kind, key=key), self.assertRaises(ValueError):
                    validate_record(kind, bad)

    def test_nested_worked_fields_are_required(self):
        for kind, record, path in (
                ('state', STATE, ('active',)), ('state', STATE, ('child',)),
                ('state', STATE, ('handoff_schema',)), ('state', STATE, ('drained',)),
                ('state', STATE, ('drained', 'participant_generations')),
                ('state', STATE, ('attempts',)), ('child', CHILD, ('barrier',)),
                ('child', CHILD, ('barrier', 'participant_generations'))):
            nested = record
            for key in path:
                nested = nested[key]
            for field in nested:
                bad = copy.deepcopy(record)
                target = bad
                for key in path:
                    target = target[key]
                del target[field]
                with self.subTest(kind=kind, path=path, field=field), self.assertRaises(ProtocolError):
                    validate_record(kind, bad)

    def test_validation_preserves_and_isolates_observed_identity(self):
        original = copy.deepcopy(STATE)
        checked = validate_record('state', original)
        self.assertEqual(checked['request']['generation'], 10)
        self.assertIsNone(checked['request']['operation_id'])
        checked['request']['generation'] = 99
        self.assertEqual(original, STATE)

    def test_strict_identity_sha_versions_and_nested_fields(self):
        cases = [
            ('state', 'generation', True), ('state', 'generation', -1),
            ('state', 'operation_id', 'not-uuid'), ('child', 'instance', None),
            ('child', 'operation_id', 'not-uuid'), ('child', 'pid', False),
            ('child', 'heartbeat_seq', -1), ('child', 'runtime_sha', 'B' * 40),
            ('child', 'runtime_sha', 'b' * 39), ('child', 'runtime_sha', 'b' * 41),
            ('child', 'phase', 'unknown'), ('child', 'protocol', True),
            ('state', 'protocol', 2), ('state', 'auto_enabled', 1),
        ]
        for kind, key, value in cases:
            bad = copy.deepcopy(STATE if kind == 'state' else CHILD)
            bad[key] = value
            with self.subTest(kind=kind, key=key, value=value), self.assertRaises(ValueError):
                validate_record(kind, bad)
        for key, value in [('manifest_digest', None), ('sha', 'HEAD'), ('instance', 'bad')]:
            bad = copy.deepcopy(STATE)
            bad['active'][key] = value
            with self.assertRaises(ValueError):
                validate_record('state', bad)
        bad = copy.deepcopy(STATE)
        bad['handoff_schema']['ledger_schema'] = None
        with self.assertRaises(ValueError):
            validate_record('state', bad)
        bad['handoff_schema'] = None
        self.assertEqual(validate_record('state', bad), bad)

    def test_command_and_ready(self):
        command = dict(protocol=1, operation_id=OP, generation=12, instance=INSTANCE,
                       request_id=STATE['request']['id'], action='probe', target_sha=B,
                       challenge=CHALLENGE)
        self.assertEqual(validate_record('command', command), command)
        for key in command:
            bad = dict(command)
            del bad[key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_record('command', bad)
        for change in ({'challenge': None}, {'operation_id': 'bad'}, {'generation': True}):
            with self.assertRaises(ValueError):
                validate_record('command', dict(command, **change))
        with self.assertRaises(ProtocolError):
            validate_record('command', dict(command, action='cancel', challenge=None,
                                            operation_id=None))
        ready = {k: CHILD[k] for k in ('protocol', 'runtime_sha', 'operation_id', 'generation',
                                      'instance', 'ledger_schema', 'heartbeat_seq',
                                      'recovery_complete', 'dispatcher_lock')}
        self.assertEqual(validate_record('ready', ready), ready)
        with self.assertRaises(ValueError):
            validate_record('ready', dict(ready, ledger_schema=None))


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.journal = Journal(self.root)

    def test_torn_state_replays_once_with_stable_event_id(self):
        atomic_json(self.root / 'state.json', STATE)
        first = self.journal.append('hub.reload.validating', event_data())
        # Crash after durable event, before replacing state: no state mutation.
        self.assertEqual(json.loads((self.root / 'state.json').read_text()), STATE)
        records = Journal(self.root).replay(after=0)
        self.assertEqual(records, [first])
        self.assertEqual(records[0]['data']['operation_id'], OP)
        applied = dict(STATE, last_journal_seq=first['seq'])
        atomic_json(self.root / 'state.json', applied)
        self.assertEqual(Journal(self.root).replay(after=applied['last_journal_seq']), [])
        second = Journal(self.root).append('hub.reload.succeeded', event_data())
        self.assertEqual(second['seq'], first['seq'] + 1)
        self.assertNotEqual(second['id'], first['id'])

    def test_partial_files_ignored_but_complete_corruption_fails_closed(self):
        first = self.journal.append('hub.reload.validating', event_data())
        atomic_json(self.root / 'state.json', STATE)
        active_state = (self.root / 'state.json').read_bytes()
        directory = self.root / 'journal'
        (directory / '.2.partial').write_text('{')
        self.assertEqual(self.journal.replay(0), [first])
        for content in ('{', json.dumps(dict(first, protocol=2)), json.dumps(dict(first, seq=2))):
            (directory / '1.json').write_text(content)
            with self.assertRaises(ValueError):
                self.journal.replay(0)
            with self.assertRaises(ValueError):
                self.journal.append('hub.reload.failed', event_data())
            self.assertFalse((directory / '2.json').exists())
            self.assertEqual((self.root / 'state.json').read_bytes(), active_state)

    def test_gaps_and_corruption_before_cursor_detected(self):
        first = self.journal.append('hub.reload.validating', event_data())
        directory = self.root / 'journal'
        (directory / '1.json').rename(directory / '2.json')
        with self.assertRaises(ValueError):
            self.journal.replay(2)
        (directory / '2.json').unlink()
        with self.assertRaises(ValueError):
            self.journal.replay(first['seq'])

    def test_concurrent_writers_never_overwrite(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: Journal(self.root).append(
                'hub.reload.validating', event_data()), range(20)))
        records = self.journal.replay(0)
        self.assertEqual([r['seq'] for r in records], list(range(1, 21)))
        self.assertEqual({r['id'] for r in results}, {r['id'] for r in records})

    def test_regressing_generation_and_invalid_data_never_published(self):
        self.journal.append('hub.reload.validating', event_data(12))
        for data in (event_data(11), dict(event_data(), generation=True)):
            with self.assertRaises(ValueError):
                self.journal.append('hub.reload.validating', data)
        self.assertEqual(len(self.journal.replay(0)), 1)

    def test_replay_checks_duplicate_ids_and_regressing_generations_before_cursor(self):
        first = self.journal.append('hub.reload.validating', event_data(12))
        second = self.journal.append('hub.reload.validating', event_data(13))
        path = self.root / 'journal' / '2.json'
        for corrupted in (dict(second, id=first['id']),
                          dict(second, data=event_data(11))):
            path.write_text(json.dumps(corrupted))
            with self.assertRaises(ProtocolError):
                self.journal.replay(2)

    def test_duplicate_json_keys_rejected(self):
        record = self.journal.append('hub.reload.validating', event_data())
        path = self.root / 'journal' / '1.json'
        path.write_text('{"protocol":2,' + json.dumps(record)[1:])
        with self.assertRaises(ProtocolError):
            self.journal.replay(0)

    def test_failed_file_sync_never_publishes_or_changes_other_writers_files(self):
        child = self.root / 'child.json'
        command = self.root / 'command.json'
        atomic_json(child, CHILD)
        atomic_json(command, {'sentinel': 'parent'})
        before = {p: p.read_bytes() for p in (child, command)}
        with patch('hub.reload_protocol.os.fsync', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                self.journal.append('hub.reload.validating', event_data())
        self.assertEqual(list((self.root / 'journal').iterdir()), [])
        self.assertEqual({p: p.read_bytes() for p in (child, command)}, before)

    def test_atomic_write_flushes_before_replace_and_cleans_failure(self):
        target = self.root / 'state.json'
        atomic_json(target, {'value': 'old'})
        real_replace = os.replace
        calls = []
        def replace(src, dst):
            self.assertIn('fsync', calls)
            self.assertEqual(json.loads(Path(src).read_text()), {'value': 'new'})
            self.assertEqual(json.loads(target.read_text()), {'value': 'old'})
            return real_replace(src, dst)
        with patch('hub.reload_protocol.os.fsync', side_effect=lambda fd: calls.append('fsync')):
            with patch('hub.reload_protocol.os.replace', side_effect=replace):
                atomic_json(target, {'value': 'new'})
        with patch('hub.reload_protocol.os.replace', side_effect=OSError('crash')):
            with self.assertRaises(OSError):
                atomic_json(target, {'value': 'later'})
        self.assertEqual(json.loads(target.read_text()), {'value': 'new'})
        self.assertEqual(set(self.root.iterdir()), {target, self.root / 'journal'})

    def test_standalone_import(self):
        module = Path(__file__).resolve().parents[1] / 'hub' / 'reload_protocol.py'
        code = ('import runpy,sys; runpy.run_path(sys.argv[1]); '
                'assert not any(x == "drover" or x.startswith(("drover.", "fastapi")) '
                'for x in sys.modules)')
        result = subprocess.run([sys.executable, '-I', '-S', '-c', code, str(module)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
