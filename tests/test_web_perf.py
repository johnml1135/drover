"""Deterministic UI process budgets and request instrumentation."""
import logging
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from drover import repos, origin
from drover import placement_git
from drover.ledger import ops
from hub.perf import Perf
from hub.app import create_app
from hub.settings import Settings
from fastapi.testclient import TestClient

class WebPerfTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repos_dir = root / 'repos'
        self.repos_dir.mkdir()
        for name in ('one', 'two'):
            (self.repos_dir / (name + '.toml')).write_text(f'name = "{name}"\nrepo = "{root.as_posix()}"\n')
        self.app = create_app(root / 'ledger.db', Settings(token='tok', run_watchdog=False,
                              run_dispatcher=False, run_services=False, trusted_ui_hosts=('testclient',),
                              tailscale=lambda _: '', ui_allowed_hosts=('testserver',)))
        self.addCleanup(self.app.state.shutdown)
        self.client = TestClient(self.app, follow_redirects=False)
        self.addCleanup(self.client.close)
        self.app.state.ui_view.repo_states.loader = lambda prog: dict(branches=set(), paths=set(), branch_paths={}, ahead=2)
        self.patches = [patch('drover.repos.repos_dir', return_value=self.repos_dir),
                        patch('drover.placement_git.subprocess.run', side_effect=self.git)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.spawns = []
    def git(self, argv, **kwargs):
        self.spawns.append(argv)
        if 'rev-parse' in argv:
            output = 'abc\n'
        elif 'ls-tree' in argv:
            output = '100644 blob abc .drover/config.toml\n'
        else:
            output = 'schema_version = 1\n'
        return subprocess.CompletedProcess(argv, 0, output, '')
    def cold_repos(self):
        view = self.app.state.ui_view
        view._repo_cache, view._repo_at = None, 0
    def test_windows_registration_path_is_toml_safe(self):
        import tomllib
        from pathlib import PureWindowsPath
        path = PureWindowsPath(r"C:\work\repo")
        self.assertEqual(tomllib.loads(f'repo = "{path.as_posix()}"')['repo'], 'C:/work/repo')
        self.assertEqual(repos.read_machine(self.repos_dir / 'one.toml')['repo'], str(Path(self.tmp.name).as_posix()))
    def test_landing_cold_and_timed_out_snapshots(self):
        view = self.app.state.ui_view
        with patch.object(view.repo_states, 'get', return_value=None):
            page = self.client.get('/landing').text
            self.assertIn('Local main', page)
            self.assertIn('checking...', page)
            self.assertIn('disabled>Push main', page)
        with patch.object(view.repo_states, 'get', return_value={'ahead': None}):
            page = self.client.get('/landing').text
            self.assertIn('unknown', page)
        with patch('drover.placement_git.subprocess.run', side_effect=subprocess.TimeoutExpired('git', 30)) as run:
            self.assertIsNone(origin.ahead(self.tmp.name, timeout=30))
            self.assertEqual(run.call_args.kwargs['timeout'], 30)
    def test_health_snapshot_age(self):
        service = self.app.state.services
        service.health_snapshot_at = 100
        with patch.object(service, 'clock', return_value=127):
            self.assertIn('updated 27s ago', self.client.get('/health').text)
        service.health_snapshot_at = None
        self.assertIn('not yet updated', self.client.get('/health').text)
    def test_route_templates_and_unhandled_error_timing(self):
        @self.app.get('/perf-test/{tid}')
        def endpoint(tid: str):
            if tid == 'broken':
                raise RuntimeError('failure')
            return {'id': tid}
        client = TestClient(self.app, raise_server_exceptions=False)
        self.addCleanup(client.close)
        client.get('/perf-test/one')
        client.get('/perf-test/two')
        failed = client.get('/perf-test/broken')
        self.assertEqual(failed.status_code, 500)
        self.assertIn('total;dur=', failed.headers['server-timing'])
        routes = self.app.state.perf.snapshot()['routes']
        self.assertEqual([(r['path'], r['count']) for r in routes], [('/perf-test/{tid}', 3)])
    def test_cold_and_warm_page_budgets(self):
        # A real task makes /inbox and /plan exercise the view chain.
        self.app.state.store.create_task('one task', repo='one')
        for path in ('/inbox', '/plan', '/runs', '/stats', '/health', '/landing', '/'):
            with self.subTest(path=path):
                self.cold_repos()
                self.spawns.clear()
                response = self.client.get(path)
                self.assertIn(response.status_code, (200, 303))
                self.assertLessEqual(len(self.spawns), 6)
                if path == '/inbox':
                    self.assertEqual(len(self.spawns), 6)
                self.spawns.clear()
                response = self.client.get(path)
                self.assertIn(response.status_code, (200, 303))
                self.assertEqual(len(self.spawns), 0)
        connections = list(self.app.state.store._conns)
        self.app.state.shutdown()
        for conn in connections:
            with self.assertRaisesRegex(sqlite3.ProgrammingError, 'closed database'):
                conn.execute('SELECT 1')
    def test_cache_ttl_events_and_fresh_operational_load(self):
        view = self.app.state.ui_view
        with patch('hub.store_ui.time.monotonic', side_effect=[0, 0, 29, 30, 30]):
            view.ui_repos()      # cold: waits for the one load
            view.ui_repos()      # fresh
            self.assertEqual(len(self.spawns), 6)
            view.ui_repos()      # 29 s: still fresh
            self.assertEqual(len(self.spawns), 6)
            view.ui_repos()      # 30 s: expired, refreshes in the background
            view._repo_pending.result(5)
            self.assertEqual(len(self.spawns), 12)
        for kind, data in [('repo.changed', {}), ('settings', {}), ('landing.changed', {}),
                           ('ledger', {'kind': 'origin-synced'}), ('event', {'kind': 'config.changed'})]:
            self.app.state.bus.publish('unrelated', {})
            self.app.state.bus.publish(kind, data)
            self.assertIsNone(view._repo_at, kind)
            self.assertIsNotNone(view._repo_cache, kind)  # the last good copy is kept
            view.ui_repos()
            view._repo_pending.result(5)
        self.spawns.clear()
        repos.load_all()
        self.assertEqual(len(self.spawns), 6)
    def test_expired_or_invalidated_repo_cache_serves_old_data_and_refreshes_once(self):
        view = self.app.state.ui_view
        old = view.ui_repos()
        self.spawns.clear()
        release, started = threading.Event(), threading.Event()
        def slow_load():
            started.set()
            release.wait(5)
            return {'new': {}}
        self.addCleanup(release.set)
        with patch('hub.store_ui.repos.load_all', side_effect=slow_load) as load:
            view.invalidate_repos()
            self.assertIs(view.ui_repos(), old)          # stale data, no wait
            self.assertTrue(started.wait(5))
            view.invalidate_repos()
            self.assertIs(view.ui_repos(), old)          # refresh already running: not started twice
            self.assertIs(view.ui_repos(), old)
            self.assertEqual(load.call_count, 1)
            release.set()
            view._repo_pending.result(5)
        self.assertEqual(self.spawns, [])
        self.assertIsNone(view._repo_at)                 # invalidated mid-load: next call refreshes again
        with patch('hub.store_ui.repos.load_all', return_value={'newer': {}}):
            view.ui_repos()
            view._repo_pending.result(5)
        self.assertEqual(view.ui_repos(), {'newer': {}})
    def test_failed_background_refresh_keeps_last_good_repos(self):
        view = self.app.state.ui_view
        old = view.ui_repos()
        view.invalidate_repos()
        with patch('hub.store_ui.repos.load_all', side_effect=OSError('git down')), \
             self.assertLogs('hub', level=logging.ERROR):
            self.assertIs(view.ui_repos(), old)
            with self.assertRaises(OSError):
                view._repo_pending.result(5)
        self.assertIs(view.ui_repos(), old)
    def test_health_and_events_read_only_what_they_return(self):
        store = self.app.state.store
        for n in range(30):
            store.add_event('noise', 'test', {'n': n})
        store.add_event('watchdog.boot-wsl', 'test', {'message': 'old'})
        for n in range(600):
            store.add_event('noise', 'test', {'n': n})
        conn = store.conn
        seen = []
        conn.set_trace_callback(seen.append)
        self.addCleanup(conn.set_trace_callback, None)
        rows = store.list_events(limit=5)
        self.assertEqual([e['data']['n'] for e in rows], [595, 596, 597, 598, 599])
        self.assertEqual([e['data']['n'] for e in store.list_events(kind='noise', since_id=rows[0]['id'], limit=2)],
                         [598, 599])
        self.assertTrue(any('LIMIT' in q for q in seen), seen)
        # rows decoded by the health page: the newest 500, not the whole ledger
        with patch.object(ops, 'events', wraps=ops.events) as ev:
            self.app.state.ui_view.health()
        self.assertEqual(ev.call_args.kwargs['limit'], 500)
    def test_stats_page_loads_series_only(self):
        store = self.app.state.store
        rows = store.stats(series_only=True)
        self.assertEqual(sorted(rows), ['daily', 'hourly'])
        self.assertIn('runs', store.stats())
        self.assertEqual(self.client.get('/api/v1/stats?series=1', headers={'Authorization': 'Bearer tok'}).json().keys(),
                         {'daily': 1, 'hourly': 1}.keys())
        self.assertIn('stats?series=1', Path('hub/static/stats.js').read_text())
    def test_nav_counts_without_building_task_views(self):
        store = self.app.state.store
        view = self.app.state.ui_view
        store.create_task('proposal', repo='one')
        with patch.object(view, '_task_view', side_effect=AssertionError('nav built a task view')):
            self.assertEqual(view.inbox_count(), 1)

    def test_repo_state_probe_records_ahead_in_background_snapshot(self):
        from concurrent.futures import Future
        from hub.task_views import RepoStates, probe
        prog = repos.normalize({'name': 'one', 'repo': self.tmp.name})
        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, '', '')
        with patch('hub.task_views.subprocess.run', side_effect=run), \
             patch('hub.task_views.origin.ahead', return_value=3) as ahead:
            snapshot = probe(prog)
        self.assertEqual(snapshot['ahead'], 3)
        ahead.assert_called_once()
        self.assertEqual(ahead.call_args.kwargs['timeout'], 30)
        state = RepoStates(loader=lambda _: snapshot)
        self.addCleanup(state.close)
        future = Future()
        future.set_result(snapshot)
        key = (repos.repo_path(prog), repos.worktree_root(prog))
        state.pending[key] = future
        self.assertEqual(state.get(prog)['ahead'], 3)

    def test_background_snapshots_and_bounded_metrics(self):
        view = self.app.state.ui_view
        service = view.services
        with patch.object(service, 'ollama_status', return_value={'reachable': True, 'p95_ms': 42}), \
             patch.object(service, 'disk_free', return_value=[{'name': 'drive', 'free_gb': 7}]), \
             patch('drover.directors.service_tick'), patch('drover.ledger.disk_notices.replay', return_value=[]), \
             patch.object(service, 'check_wsl'), patch.object(service, 'check_tuner'):
            service.tick()
        self.assertIsNotNone(service.health_snapshot_at)
        self.assertEqual(view.health()['ollama']['p95_ms'], 42)
        self.assertEqual(view.health()['disks'][0]['free_gb'], 7)
        view.repo_states.get = lambda prog: {'ahead': 3}
        self.assertEqual([r['ahead'] for r in view.landing()['repos']], [3, 3])
        perf = Perf()
        for n in range(501):
            perf.record('/route', 'GET', n, 0)
        data = perf.snapshot()
        self.assertEqual(data['routes'][0], dict(method='GET', path='/route', count=500, p50_ms=250, p95_ms=475))
        before = placement_git.git_spawns
        placement_git.git(self.tmp.name, 'rev-parse', 'HEAD')
        self.assertEqual(placement_git.git_spawns, before + 1)

    def test_snapshots_and_request_metrics(self):
        view = self.app.state.ui_view
        view.services.health_snapshot = ({'reachable': True, 'p95_ms': 14}, [{'name': 'disk', 'free_gb': 4}])
        with patch.object(view.services, 'ollama_status', side_effect=AssertionError('request probe')), \
             patch.object(view.services, 'disk_free', side_effect=AssertionError('request probe')):
            self.assertTrue(view.health()['ollama']['reachable'])
            self.assertEqual(view.health()['disks'][0]['free_gb'], 4)
        with patch('hub.app.time', SimpleNamespace(perf_counter=iter([10.0, 11.5, 11.5]).__next__)), \
             self.assertLogs('hub', level=logging.WARNING) as logs:
            response = self.client.get('/')
        self.assertIn('total;dur=1500.00', response.headers['server-timing'])
        self.assertIn('path=/ method=GET duration_ms=1500.00 git_spawns=0', logs.output[0])
        perf = self.client.get('/api/v1/perf', headers={'Authorization': 'Bearer tok'}).json()
        self.assertEqual(perf['slow'][0]['path'], '/')
        self.assertEqual(perf['routes'][0]['p95_ms'], 1500)
        self.assertIn('Recent slow requests', self.client.get('/stats').text)
    def test_request_timing_includes_sending_the_body(self):
        with patch('hub.app.time', SimpleNamespace(perf_counter=iter([10.0, 11.0, 13.0]).__next__)), \
             self.assertLogs('hub', level=logging.WARNING) as logs:
            response = self.client.get('/')
        self.assertIn('total;dur=1000.00', response.headers['server-timing'])  # time to first byte
        self.assertIn('duration_ms=3000.00', logs.output[0])                   # until the body was sent
        self.assertEqual(self.app.state.perf.snapshot()['routes'][0]['p95_ms'], 3000)


if __name__ == '__main__':
    unittest.main()
