"""Durable run stats, aggregate retention and task lifecycle with temporary ledgers."""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from drover import config, decide, stats, usage
from drover.ledger import connect, ops

NOW = datetime(2026, 10, 17, 16, tzinfo=timezone.utc).timestamp()


class HubClient(unittest.TestCase):
    def setUp(self):
        config.use(config.validate({}))
        self.addCleanup(config.reset)

    def test_both_clis_discover_windows_gateway_under_wsl_nat(self):
        gateway = '192.0.2.1'
        port = config.get()['hub']['api_port']
        for cli, argv, endpoint in ((stats, ['--json'], '/stats?'),
                                    (usage, ['order', '--json'], '/usage/order')):
            with self.subTest(cli=cli.__name__), patch.dict(os.environ, {'DROVER_HUB_TOKEN': 'test-token'}), \
                    patch.object(stats.os, 'name', 'posix'), \
                    patch.object(decide, '_is_wsl', return_value=True), \
                    patch('subprocess.run', return_value=subprocess.CompletedProcess(
                        [], 0, f'default via {gateway} dev eth0\n')) as run, \
                    patch.object(stats, 'urlopen', return_value=io.BytesIO(b'{}')) as fetch, \
                    contextlib.redirect_stdout(io.StringIO()):
                os.environ.pop('DROVER_HUB_URL', None)
                os.environ.pop('DROVER_LEDGER', None)
                self.assertEqual(cli.main(argv), 0)
                self.assertEqual(fetch.call_args.args[0].full_url, f'http://{gateway}:{port}/api/v1{endpoint}')
                run.assert_called_once()
                self.assertEqual(run.call_args.args[0], ['ip', 'route'])

    def test_explicit_hub_url_skips_gateway_discovery(self):
        with patch.dict(os.environ, {'DROVER_HUB_URL': 'http://acme-hub:9100/', 'DROVER_HUB_TOKEN': 'test-token'}), \
                patch.object(decide, '_is_wsl', return_value=True), \
                patch('subprocess.run') as run, \
                patch.object(stats, 'urlopen', return_value=io.BytesIO(b'{}')) as fetch:
            self.assertEqual(stats.hub_request('/stats'), {})
            self.assertEqual(fetch.call_args.args[0].full_url, 'http://acme-hub:9100/api/v1/stats')
            run.assert_not_called()

    def test_localhost_when_not_wsl_or_gateway_is_unavailable(self):
        for wsl, route in ((False, ''), (True, '192.0.2.0/24 dev eth0\n'), (True, OSError('no route command'))):
            with self.subTest(wsl=wsl, route=route), \
                    patch.dict(os.environ, {'DROVER_HUB_TOKEN': 'test-token'}), \
                    patch.object(stats.os, 'name', 'posix'), \
                    patch.object(decide, '_is_wsl', return_value=wsl), \
                    patch('subprocess.run') as run, \
                    patch.object(stats, 'urlopen', return_value=io.BytesIO(b'{}')) as fetch:
                os.environ.pop('DROVER_HUB_URL', None)
                if isinstance(route, OSError):
                    run.side_effect = route
                else:
                    run.return_value = subprocess.CompletedProcess([], 0, route)
                stats.hub_request('/stats')
                self.assertEqual(fetch.call_args.args[0].full_url,
                                 f"http://127.0.0.1:{config.get()['hub']['api_port']}/api/v1/stats")
                self.assertEqual(run.call_count, int(wsl))


class Stats(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'ledger.db'
        self.conn = connect(self.path)
        self.addCleanup(self.conn.close)
        self.tid = ops.create_task(self.conn, 'Measure acme-lib', 'acme-lib', scope_files=['fixture-79'])['id']

    def run_one(self):
        rid = ops.start_run(self.conn, self.tid, 'worker', 'claude', 'sonnet-5.5', 'high')
        stats.update_run(self.conn, rid, seat='acme-seat', placement='wsl', peak_cpu_pct=25, peak_ram_mb=80,
                         peak_disk_bytes=2048)
        ops.finish_run(self.conn, rid, 'rotated', tokens_in=10, tokens_out=20)
        return rid

    def test_run_and_task_totals_and_readonly_cli(self):
        rid = self.run_one()
        rid2 = ops.start_run(self.conn, self.tid, 'worker', 'claude', 'opus-5.5', 'high')
        ops.finish_run(self.conn, rid2, 'done', tokens_in=5, tokens_out=7,
                       stats={'sessions': 2, 'compactions': 1})
        data = stats.query(self.conn, task=self.tid)
        totals = data['tasks'][self.tid]
        self.assertEqual(totals['runs'], 2)
        self.assertEqual(totals['sessions'], 3)
        self.assertEqual(totals['handoffs'], 1)
        self.assertEqual(totals['compactions'], 1)
        self.assertEqual(totals['escalations'], 1)
        self.assertEqual(totals['seats'], ['acme-seat'])
        self.assertEqual(totals['peak_ram_mb'], 80)
        self.assertEqual(totals['tokens_by_model']['sonnet-5.5']['tokens_out'], 20)
        before = self.conn.total_changes
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(stats.main(['--db', str(self.path), '--json', '--task', self.tid]), 0)
        self.assertEqual(json.loads(out.getvalue()), data)
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(stats.query(self.conn, since='2099-01-01')['runs'], [])
        self.assertEqual(stats.query(self.conn, task='missing')['tasks'], {})

    def test_hourly_sample_rollup_prune_and_daily_schedule(self):
        for ago, n in ((40, 10), (40 + 1 / 24, 15), (400, 8)):
            epoch = NOW - ago * 86400
            stats.sample(self.conn, {'facts': {'ram_available_gb': n, 'disk_free_gb': {'acme': n}}}, epoch)
            stats.landing(self.conn, epoch)
        stats.sample(self.conn, {}, NOW)
        self.assertTrue(stats.prune(self.conn, NOW))
        self.assertFalse(stats.prune(self.conn, NOW))
        data = stats.query(self.conn)
        self.assertEqual(len(data['hourly']), 1)
        self.assertEqual(sum(x['landings'] for x in data['daily']), 2)
        self.assertTrue(all(x['ts'] > stats.stamp(NOW - 365 * 86400) for x in data['daily']))
        stats.prune(self.conn, NOW + 86400)
        self.assertEqual(stats.query(self.conn)['daily'], data['daily'])

    def test_cached_input_is_counted_once_for_each_backend(self):
        for backend, expected in (("codex", 24), ("claude", 29)):
            rid = ops.start_run(self.conn, self.tid, "worker", backend, "acme-model")
            ops.finish_run(self.conn, rid, "done", tokens_in=20, tokens_out=4, tokens_cached=5)
            self.assertEqual(stats.query(self.conn)['runs'][-1]['stats']['tokens_by_model']['acme-model']['tokens_total'], expected)

    def test_claude_cache_writes_survive_completed_and_interrupted_streams(self):
        from drover.launch import Usage
        for completed in (False, True):
            with self.subTest(completed=completed):
                u = Usage()
                ev = {'type': 'assistant', 'message': {'id': 'message-1', 'model': 'acme-model',
                      'usage': {'input_tokens': 10, 'output_tokens': 4, 'cache_read_input_tokens': 5,
                                'cache_creation_input_tokens': 7}}}
                u.feed(ev)
                u.feed(ev)  # Repeated assistant messages must not duplicate usage.
                if completed:
                    u.feed({'type': 'result', 'modelUsage': {'acme-model': {'inputTokens': 20,
                            'outputTokens': 8, 'cacheReadInputTokens': 10, 'cacheCreationInputTokens': 14}}})
                expected = 52 if completed else 26
                rid = ops.start_run(self.conn, self.tid, 'worker', 'claude', 'acme-model')
                ops.finish_run(self.conn, rid, 'done' if completed else 'timeout',
                               stats={'tokens_by_model': u.as_dict()['tokens_by_model']})
                counts = stats.query(self.conn)['runs'][-1]['stats']['tokens_by_model']['acme-model']
                self.assertEqual(counts['tokens_total'], expected)
                self.assertEqual(counts['tokens_cache_write'], 14 if completed else 7)
        data = stats.query(self.conn)
        self.assertEqual(data['tasks'][self.tid]['tokens_by_model']['acme-model']['tokens_total'], 78)
        self.assertEqual(data['hourly'][0]['tokens_by_model']['acme-model']['tokens_total'], 78)
        end = ops._parse(data['runs'][-1]['ended']).timestamp()
        stats.prune(self.conn, end + 40 * 86400)
        self.assertEqual(stats.query(self.conn)['daily'][0]['tokens_by_model']['acme-model']['tokens_total'], 78)

    @unittest.skipUnless(importlib.util.find_spec('markupsafe'), 'UiView needs the hub environment (markupsafe)')
    def test_archive_hides_task_questions_from_inbox_and_restore_preserves_them(self):
        from hub.store import Store, UiView
        store = Store(self.path)
        self.addCleanup(store.close)
        view = UiView(store)
        qid = store.ask_question('task', self.tid, 'Choose acme-lib option?')['id']
        eid = store.create_epic('acme-app', 'Acme plan')['id']
        other = store.ask_question('epic', eid, 'Choose acme-app option?')['id']
        self.assertEqual({q['id'] for q in view.inbox_questions()}, {str(qid), str(other)})
        store.archive_task(self.tid)
        self.assertEqual([q['id'] for q in view.inbox_questions()], [str(other)])
        self.assertEqual(ops.get_question(self.conn, qid)['status'], 'open')
        store.archive_task(self.tid, False)
        self.assertEqual({q['id'] for q in view.inbox_questions()}, {str(qid), str(other)})

    def test_flow_counters_survive_gauge_updates(self):
        self.run_one()
        stats.landing(self.conn)
        stats.sample(self.conn)
        stats.sample(self.conn)
        row = stats.query(self.conn)['hourly'][0]
        self.assertEqual(row['landings'], 1)
        self.assertEqual(row['completed_runs'], 1)
        self.assertEqual(row['tokens_by_model']['sonnet-5.5']['tokens_in'], 10)

    def test_archive_restore_and_owner_confirmed_delete_keeps_aggregates(self):
        self.run_one()
        ops.archive_task(self.conn, self.tid)
        self.assertEqual(ops.list_tasks(self.conn), [])
        self.assertEqual(len(ops.list_tasks(self.conn, include_archived=True)), 1)
        ops.archive_task(self.conn, self.tid, False)
        self.assertEqual(len(ops.list_tasks(self.conn)), 1)
        for kw in ({'confirm': False}, {'confirm': True, 'actor': 'dispatcher'}):
            with self.assertRaises((ValueError, PermissionError)):
                ops.delete_task(self.conn, self.tid, **kw)
        hourly = stats.query(self.conn)['hourly']
        ops.delete_task(self.conn, self.tid, confirm=True)
        self.assertEqual(ops.list_runs(self.conn, self.tid), [])
        self.assertEqual(stats.query(self.conn)['hourly'], hourly)
        with self.assertRaises(ops.NotFound):
            ops.get_task(self.conn, self.tid)

    def test_archived_ready_task_cannot_be_claimed_and_active_delete_refused(self):
        ops.approve(self.conn, self.tid, actor='owner')
        ops.archive_task(self.conn, self.tid)
        self.assertIsNone(ops.claim_next(self.conn))
        ops.archive_task(self.conn, self.tid, False)
        ops.claim_next(self.conn)
        ops.start_run(self.conn, self.tid, 'worker', 'claude')
        with self.assertRaises(ValueError):
            ops.delete_task(self.conn, self.tid, confirm=True)

    def test_prunes_finished_logs_only(self):
        rid = self.run_one()
        self.conn.execute('UPDATE runs SET ended=? WHERE id=?', (stats.stamp(NOW - 31 * 86400), rid))
        root = Path(self.tmp.name) / 'runs'
        d = root / str(rid)
        d.mkdir(parents=True)
        for name in ('out.log', 'runner.log', 'tail.log', 'record.json'):
            (d / name).write_text('{}', encoding='utf-8')
        active = ops.start_run(self.conn, self.tid, 'worker', 'claude')
        recent = self.run_one()
        self.conn.execute('UPDATE runs SET ended=? WHERE id=?', (stats.stamp(NOW), recent))
        for run_id in (active, recent):
            path = root / str(run_id)
            path.mkdir()
            (path / 'tail.log').write_text('worker output', encoding='utf-8')
        stats.prune(self.conn, NOW, root)
        for name in ('out.log', 'runner.log', 'tail.log'):
            self.assertFalse((d / name).exists())
        self.assertTrue((d / 'record.json').exists())
        for run_id in (active, recent):
            self.assertTrue((root / str(run_id) / 'tail.log').exists())

    def test_deleted_landing_does_not_break_board(self):
        from drover.ledger import board
        ops.event(self.conn, 'status', 'owner', {'from': 'reviewed', 'to': 'landed'}, task_id=self.tid)
        ops.delete_task(self.conn, self.tid, confirm=True)
        self.assertIn('Landed today', board.render(self.conn))

    def test_archive_refuses_open_run_even_if_task_is_still_proposed(self):
        ops.start_run(self.conn, self.tid, 'worker', 'claude')
        with self.assertRaises(ValueError):
            ops.archive_task(self.conn, self.tid)


class Telemetry(unittest.TestCase):
    def test_peaks_from_fake_samples(self):
        from drover.metrics import Peaks
        rows = iter([(1, 1048576, 100), (2, 2097152, 200), (2.1, 1048576, 200)])
        clock = iter([0, 2, 4])
        peaks = Peaks(lambda: next(rows), now=lambda: next(clock))
        for _ in range(3):
            peaks.sample()
        self.assertEqual(peaks.values, {'peak_cpu_pct': 50, 'peak_ram_mb': 2, 'peak_disk_bytes': 200})

    def test_empty_usage_has_no_model_sessions(self):
        from drover.launch import Usage
        self.assertEqual(Usage().as_dict()['sessions'], 0)

    def test_model_tokens_sessions_compactions(self):
        from drover.launch import Usage
        u = Usage()
        for session in ('a', 'b'):
            u.feed({'type': 'system', 'subtype': 'init', 'session_id': session})
        u.feed({'type': 'system', 'subtype': 'compact_boundary'})
        u.feed({'type': 'result', 'modelUsage': {'sonnet-5.5': {'inputTokens': 20, 'outputTokens': 5},
                                               'haiku-5.5': {'inputTokens': 2, 'outputTokens': 1}}})
        self.assertEqual(u.as_dict()['sessions'], 2)
        self.assertEqual(u.as_dict()['compactions'], 1)
        self.assertEqual(u.as_dict()['tokens_by_model']['haiku-5.5']['tokens_in'], 2)


class ProcessFacts(unittest.TestCase):
    def test_proc_group_with_fake_proc_files(self):
        from drover.metrics import proc_group
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for pid, group in ((100, 100), (101, 100), (200, 200)):
                d = root / str(pid)
                d.mkdir()
                fields = ['0'] * 22
                fields[0], fields[2], fields[11], fields[12], fields[21] = 'R', str(group), '100', '100', '2'
                (d / 'stat').write_text(f'{pid} (fake worker) ' + ' '.join(fields), encoding='utf-8')
                (d / 'io').write_text('read_bytes: 100\nwrite_bytes: 200\n', encoding='utf-8')
            self.assertEqual(proc_group(100, root, ticks=100, pages=4096), (4, 16384, 600))

    def test_stats_resources_request_wsl_memory_even_without_thresholds(self):
        from unittest.mock import patch
        from drover import resources
        cfg = {'trackers': [{'name': 'wsl', 'kind': 'wsl', 'enabled': True, 'distro': 'Acme',
                             'queue_mem_pct': 0, 'hold_mem_pct': 0}]}
        with patch.object(resources, 'gather', return_value={
                'platform': 'windows', 'load5': None, 'cores': 1, 'ram_available_gb': None,
                'ollama_ram_gb': 0, 'read_only': False,
                'disk_free_gb': {}, 'wsl_up': True, 'wsl_mem': {'used_gb': 2, 'cap_gb': 8}, 'vram': None}) as gather:
            self.assertEqual(resources.check(config=cfg, with_wsl_memory=True)['facts']['wsl_mem']['used_gb'], 2)
            self.assertTrue(gather.call_args.kwargs['with_wsl_memory'])

    def test_migration_preserves_existing_run_stats_and_open_run_can_finish(self):
        from drover.ledger import migrate
        from drover.ledger.schema import MIGRATIONS
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / 'old.db', migrate_db=False)
            try:
                self.assertEqual(migrate(conn, MIGRATIONS[:4]), 4)  # main's schema version (T-7 migration)
                conn.execute("INSERT INTO tasks(id,repo,title,rank,status,created,updated) VALUES(?,?,?,?,?,?,?)",
                             ('T-1', 'acme-lib', 'Legacy run', 1, 'proposed', stats.stamp(NOW), stats.stamp(NOW)))
                for ended in (stats.stamp(NOW + 30), None):
                    conn.execute('INSERT INTO runs(task_id,role,backend,model,started,ended,tokens_in,tokens_out) '
                                 'VALUES(?,?,?,?,?,?,?,?)', ('T-1', 'worker', 'claude', 'sonnet-5.5', stats.stamp(NOW), ended, 10, 20))
                self.assertEqual(migrate(conn), MIGRATIONS[-1][0])
                self.assertEqual(stats.query(conn)['runs'][0]['stats']['tokens_by_model']['sonnet-5.5']['tokens_out'], 20)
                ops.finish_run(conn, 2, 'done', tokens_in=15, tokens_out=25)
                self.assertEqual(stats.query(conn)['runs'][1]['stats']['tokens_by_model']['sonnet-5.5']['tokens_out'], 25)
            finally:
                conn.close()
