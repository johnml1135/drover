"""Per-account readings and allowance ordering, using fictional seats only."""
import json
import ntpath
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from drover import config, usage
from drover.ledger import connect

NOW = datetime(2026, 10, 17, 16, tzinfo=timezone.utc).timestamp()
TEXT = ("Current session: 48% used · resets Oct 17, 1:20pm (America/New_York)\n"
        "Current week (all models): 93% used · resets Oct 20, 4am (America/New_York)\n"
        "Current week (Fable): 0% used · resets Oct 20, 4am (America/New_York)\n"
        "What's contributing to your limits usage?\n")


class Readings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = connect(Path(self.tmp.name) / 'ledger.db')
        self.addCleanup(self.conn.close)
        self.seat = config.validate({})['seats'][0]

    def test_real_shape_and_missing_session_reset(self):
        rows = usage.parse_claude(TEXT, NOW)
        self.assertEqual([r['used_pct'] for r in rows], [48, 93])
        self.assertEqual(rows[0]['resets_at'], datetime(2026, 10, 17, 17, 20, tzinfo=timezone.utc).timestamp())
        rows = usage.parse_claude(TEXT.replace(TEXT.splitlines()[0], 'Current session: 0% used'), NOW)
        self.assertIsNone(rows[0]['resets_at'])
        with self.assertRaises(ValueError):
            usage.parse_claude('format changed', NOW)

    def test_reset_uses_next_year_and_zone(self):
        now = datetime(2026, 12, 31, 23, tzinfo=timezone.utc).timestamp()
        self.assertEqual(usage.next_reset('Jan 1, 4am', 'America/New_York', now),
                         datetime(2027, 1, 1, 9, tzinfo=timezone.utc).timestamp())
        self.assertGreater(usage.next_reset('Oct 17, 1am', 'America/New_York', NOW), NOW)

    def test_injectable_command_cache_and_failure_alert(self):
        calls = []
        def run(argv, **kw):
            calls.append((argv, kw))
            return subprocess.CompletedProcess(argv, 0, json.dumps({'result': TEXT}), '')
        poller = usage.Poller(self.conn, run=run, now=lambda: NOW)
        seat = {**self.seat, 'config_dir': str(Path(self.tmp.name) / 'acme-seat')}
        poller.refresh(seat)
        poller.refresh(seat)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], ['claude', '-p', '/usage', '--output-format', 'json'])
        self.assertEqual(calls[0][1]['timeout'], 30)
        self.assertEqual(calls[0][1]['env']['CLAUDE_CONFIG_DIR'], seat['config_dir'])
        self.assertEqual(len(usage.readings(self.conn)), 2)
        bad = usage.Poller(self.conn, run=lambda *a, **k: subprocess.CompletedProcess([], 0, '{"result":"changed"}'),
                           now=lambda: NOW + 61)
        bad.refresh(seat)
        self.assertEqual(usage.readings(self.conn)[-1]['raw_text'], 'changed')
        self.assertFalse(usage.readings(self.conn)[-1]['ok'])
        self.assertTrue(self.conn.execute("SELECT 1 FROM task_events WHERE kind='usage.alert'").fetchone())
        self.assertFalse(usage.headroom(self.conn, seat['name'], NOW + 61))

    def test_command_output_decodes_utf8_even_with_a_legacy_default(self):
        encoded = json.dumps({'result': TEXT}, ensure_ascii=False).encode('utf-8')
        for windows in (False, True):
            with self.subTest(windows=windows):
                def run(argv, **kw):
                    # Model Windows' legacy default while executing only a canned byte writer.
                    kw.setdefault('encoding', 'cp1252')
                    return subprocess.run([sys.executable, '-c',
                                           'import sys; sys.stdout.buffer.write(bytes.fromhex(sys.argv[1]))',
                                           encoded.hex()], **kw)
                seat = {**self.seat, 'placement': 'wsl' if windows else 'windows'}
                poller = usage.Poller(self.conn, run=run, now=lambda: NOW, windows=windows)
                self.assertEqual((poller.refresh(seat) or {}).get('remaining_pct'), 7)
                self.assertEqual(usage.readings(self.conn)[-1]['raw_text'], TEXT)

    def test_poll_only_active_seats_every_five_minutes(self):
        calls = []
        poller = usage.Poller(self.conn, run=lambda *a, **k: calls.append(a) or
                             subprocess.CompletedProcess([], 0, json.dumps({'result': TEXT})), now=lambda: NOW)
        poller.poll([self.seat], {})
        self.assertEqual(calls, [])
        poller.poll([self.seat], {self.seat['name']: 1})
        poller.poll([self.seat], {self.seat['name']: 1})
        self.assertEqual(len(calls), 1)
        poller.now = lambda: NOW + 301
        poller.poll([self.seat], {self.seat['name']: 1})
        self.assertEqual(len(calls), 2)

    def test_codex_newest_rollout_last_event_and_null_windows(self):
        root = Path(self.tmp.name) / 'sessions'
        root.mkdir()
        p = root / 'rollout-a.jsonl'
        event = lambda n: {'type': 'event_msg', 'payload': {'type': 'token_count', 'rate_limits': n}}
        rate = {'primary': {'used_percent': 42, 'window_minutes': 10080, 'resets_at': NOW + 400}, 'secondary': None}
        p.write_text('\n'.join(json.dumps(event(x)) for x in (None, rate)) + '\n{broken', encoding='utf-8')
        rows, raw = usage.read_codex(Path(self.tmp.name))
        self.assertEqual(rows[0]['window'], 'seven_day')
        self.assertEqual(rows[0]['used_pct'], 42)
        p.write_text(json.dumps(event({'primary': None, 'secondary': None})), encoding='utf-8')
        self.assertEqual(usage.read_codex(Path(self.tmp.name))[0], [])

    def test_manual_validation(self):
        usage.manual(self.conn, self.seat['name'], 'seven_day', 25, NOW + 3600, now=NOW)
        self.assertEqual(usage.headroom(self.conn, self.seat['name'], NOW)['remaining_pct'], 75)
        for pct in (-1, 101, float('nan')):
            with self.assertRaises(ValueError):
                usage.manual(self.conn, self.seat['name'], 'seven_day', pct, NOW + 3600, now=NOW)

    def test_order_projection_reserve_and_reset_boundary(self):
        a = {**self.seat, 'name': 'a', 'priority': 1, 'reserve_pct': 25}
        b = {**self.seat, 'name': 'b', 'priority': 2, 'reserve_pct': 0}
        for seat, old, new in ((a, 30, 80), (b, 20, 21)):
            usage.manual(self.conn, seat['name'], 'seven_day', old, NOW + 3600, now=NOW - 86400)
            usage.manual(self.conn, seat['name'], 'seven_day', new, NOW + 3600, now=NOW)
        order = usage.order(self.conn, [a, b], now=NOW)
        self.assertEqual(order[0]['seat'], 'b')
        self.assertIn('waste', order[0]['reason'])
        self.assertFalse(order[1]['eligible'])
        self.assertTrue(usage.order(self.conn, [a], now=NOW, director=True)[0]['eligible'])
        self.assertEqual(usage.burn_rate(self.conn, 'b', 'seven_day', NOW), 1 / 24)
        self.assertIsNone(usage.headroom(self.conn, 'b', NOW + 3601))


class MeasuredRouting(unittest.TestCase):
    def test_accounts_of_same_backend_have_independent_reserves_and_waste(self):
        from drover import route
        seats = config.validate({})['seats']
        a = {**seats[0], 'name': 'a', 'priority': 1, 'reserve_pct': 25}
        b = {**seats[0], 'name': 'b', 'priority': 2, 'director': False}
        heads = {'a': {'remaining_pct': 20}, 'b': {'remaining_pct': 70}}
        r = route.choose({'size': 'M'}, {}, seats=[a, b], seat_headroom=heads)
        self.assertEqual(r['seat'], 'b')
        heads['a']['remaining_pct'] = 70
        r = route.choose({'size': 'M'}, {}, seats=[a, b], seat_headroom=heads,
                         seat_order=[{'seat': 'b', 'urgent': True, 'reason': 'projected waste 35% at reset'}])
        self.assertEqual(r['seat'], 'b')
        self.assertIn('waste', r['reason'])
        self.assertIsNone(route.choose({'size': 'M'}, {}, seats=[a, b], seat_headroom={})['backend'])

    def test_director_model_effort_reserve_and_borrow_approval(self):
        from drover import route
        cfg = config.validate({'director': {'model': 'opus-5.5', 'effort': 'max'}})
        config.use(cfg)
        self.addCleanup(config.reset)
        a = cfg['seats'][0]
        b = {**a, 'name': 'other', 'director': False}
        h = {'claude': {'remaining_pct': 1}, 'other': {'remaining_pct': 90}}
        r = route.choose({'role': 'director'}, {}, seats=[a, b], seat_headroom=h)
        self.assertEqual((r['seat'], r['model'], r['effort']), ('claude', 'opus-5.5', 'max'))
        task = {'role': 'director', 'director_seat': 'other'}
        self.assertTrue(route.choose(task, {}, seats=[a, b], seat_headroom=h)['needs_owner'])
        h['claude'] = {'remaining_pct': 0}
        self.assertEqual(route.choose({'role': 'director'}, {}, seats=[a, b], seat_headroom=h)['seat'], 'other')
        self.assertEqual(route.choose(task, {}, seats=[a, b], seat_headroom=h, director_borrow_approved=True)['seat'], 'other')


class DispatcherSeats(unittest.TestCase):
    def make_dispatcher(self, seats=None, slots=1):
        from drover import dispatcher
        from drover.port import DirectPort
        from drover.ledger import ops
        from tests.test_dispatcher import FakeLauncher, WORKTREE
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = connect(Path(tmp.name) / 'ledger.db')
        self.addCleanup(conn.close)
        config.use(config.validate({'seats': seats or [
            {'name': 'acme-seat', 'backend': 'claude', 'director': True}]}))
        self.addCleanup(config.reset)
        ops.set_setting(conn, 'dispatch_enabled', 'true')
        clock, calls, launcher = [NOW], [], FakeLauncher()
        d = dispatcher.Dispatcher(DirectPort(conn), launcher=launcher,
                                  resources_fn=lambda **k: {'verdict': 'go', 'slots': slots},
                                  worktree_fn=lambda t: WORKTREE, now_fn=lambda: clock[0])
        def run(*a, **k):
            calls.append(clock[0])
            return subprocess.CompletedProcess([], 0, json.dumps({'result': TEXT.replace('93%', '20%')}))
        d.usage_poller.run = run
        return conn, d, launcher, clock, calls

    def test_default_windows_account_is_used_by_probes_and_cross_placement_workers(self):
        from drover import launch
        from drover.ledger import ops
        for platform, placement in (('windows', 'windows'), ('linux', 'wsl')):
            with self.subTest(platform=platform):
                conn, d, launcher, _, _ = self.make_dispatcher()
                d.usage_poller.windows = True
                probes = []
                def run(argv, **kw):
                    probes.append(kw['env'].get('CLAUDE_CONFIG_DIR'))
                    return subprocess.CompletedProcess(argv, 0, json.dumps({'result': TEXT.replace('93%', '20%')}))
                d.usage_poller.run = run
                tid = ops.create_task(conn, 'Build acme-lib', 'acme-lib', goal=f'platform: {platform}')['id']
                ops.approve(conn, tid)
                windows_os = SimpleNamespace(name='nt', path=ntpath, environ=os.environ)
                with patch.object(config, 'os', windows_os), patch.dict(os.environ, {'USERPROFILE': r'R:\Users\acme'}):
                    self.assertEqual(d.tick()['launched'], [tid])
                account = ntpath.normpath(r'R:\Users\acme\.claude')
                self.assertEqual([ntpath.normpath(p) if p else p for p in probes], [account])
                spec = launcher.specs[0]
                self.assertEqual(spec['placement'], placement)
                self.assertEqual(ntpath.normpath(spec['env']['CLAUDE_CONFIG_DIR']), account)
                real_launcher = launch.RunnerLauncher(runs_dir='R:/runs', drover_dir='R:/repos/acme-lib',
                                                     windows=True, which=lambda n: 'R:/bin/claude.exe')
                cmd, env = real_launcher.command(spec, Path('R:/runs') / str(spec['run_id']), ['claude'])
                if placement == 'wsl':
                    self.assertIn('CLAUDE_CONFIG_DIR=/mnt/r/Users/acme/.claude', cmd)
                else:
                    self.assertEqual(ntpath.normpath(env['CLAUDE_CONFIG_DIR']), account)

    def test_dotnet_takes_the_best_seat_on_either_side(self):
        from drover.ledger import ops
        conn, d, launcher, _, _ = self.make_dispatcher([
            {'name': 'acme-linux', 'backend': 'claude', 'placement': 'wsl', 'priority': 1},
            {'name': 'acme-windows', 'backend': 'claude', 'placement': 'windows', 'priority': 2,
             'director': True}])
        tid = ops.create_task(conn, 'Build acme-lib', 'acme-lib', scope_files=['src/acme.csproj'])['id']
        ops.approve(conn, tid)
        self.assertEqual(d.tick()['launched'], [tid])
        self.assertEqual(launcher.specs[0]['seat'], 'acme-linux')       # owner rule: wsl + claude is allowed
        self.assertEqual(launcher.specs[0]['placement'], 'wsl')

    def test_idle_or_unassignable_queue_does_not_probe(self):
        from drover.ledger import ops
        for scenario in ('idle', 'no-slots', 'backoff'):
            with self.subTest(scenario=scenario):
                conn, d, _, clock, calls = self.make_dispatcher(slots=0 if scenario == 'no-slots' else 1)
                if scenario != 'idle':
                    tid = ops.create_task(conn, 'Build acme-lib', 'acme-lib')['id']
                    ops.approve(conn, tid)
                    if scenario == 'backoff':
                        ops.set_backoff(conn, tid, NOW + 1000, 1)
                d.tick()
                clock[0] += 61
                d.tick()
                self.assertEqual(calls, [])

    def test_full_workers_use_five_minute_poll_and_next_assignment_refreshes(self):
        from drover.ledger import ops
        from tests.test_dispatcher import res, DONE
        conn, d, launcher, clock, calls = self.make_dispatcher()
        ops.add_policy_version(conn, 'one-worker', {'parallel_cap': 1})
        for _ in range(2):
            tid = ops.create_task(conn, 'Build acme-lib', 'acme-lib')['id']
            ops.approve(conn, tid)
        d.tick()
        self.assertEqual(calls, [NOW])
        for elapsed in (61, 299, 300, 361):
            clock[0] = NOW + elapsed
            d.tick()
        self.assertEqual(calls, [NOW, NOW + 300])
        launcher.handles[0].result = res('done', DONE)
        self.assertEqual(d.tick()['launched'], [tid])
        self.assertEqual(calls, [NOW, NOW + 300, NOW + 361])

    def test_completion_without_pending_work_does_not_probe(self):
        from drover.ledger import ops
        from tests.test_dispatcher import res, DONE
        conn, d, launcher, clock, calls = self.make_dispatcher()
        tid = ops.create_task(conn, 'Build acme-lib', 'acme-lib')['id']
        ops.approve(conn, tid)
        d.tick()
        clock[0] += 61
        launcher.handles[0].result = res('done', DONE)
        self.assertEqual(d.tick()['reaped'], [launcher.specs[0]['run_id']])
        self.assertEqual(calls, [NOW])

    def test_assignment_still_checks_fresh_reserve(self):
        from drover.ledger import ops
        conn, d, launcher, _, _ = self.make_dispatcher([
            {'name': 'acme-seat', 'backend': 'claude', 'director': True, 'reserve_pct': 25}])
        tid = ops.create_task(conn, 'Build acme-lib', 'acme-lib')['id']
        ops.approve(conn, tid)
        d.usage_poller.run = lambda *a, **k: subprocess.CompletedProcess([], 0, json.dumps({'result': TEXT}))
        self.assertEqual(d.tick()['launched'], [])
        self.assertEqual(ops.get_task(conn, tid)['status'], 'ready')
        self.assertEqual(launcher.specs, [])

    def test_assignment_records_account_environment_and_stats(self):
        from drover import dispatcher, stats
        from drover.port import DirectPort
        from drover.ledger import ops
        from tests.test_dispatcher import FakeLauncher, WORKTREE
        with tempfile.TemporaryDirectory() as tmp, closing(connect(Path(tmp) / 'ledger.db')) as conn:
            cfg = config.validate({'seats': [{'name': 'acme-seat', 'backend': 'claude', 'director': True,
                                             'config_dir': str(Path(tmp) / 'account'), 'reserve_pct': 25}]})
            config.use(cfg)
            self.addCleanup(config.reset)
            ops.set_setting(conn, 'dispatch_enabled', 'true')
            tid = ops.create_task(conn, 'Work on acme-lib', 'acme-lib')['id']
            ops.approve(conn, tid)
            launcher = FakeLauncher()
            d = dispatcher.Dispatcher(DirectPort(conn), launcher=launcher,
                                      resources_fn=lambda **k: {'verdict': 'go', 'slots': 1},
                                      worktree_fn=lambda t: WORKTREE, now_fn=lambda: NOW)
            d.usage_poller.run = lambda *a, **k: subprocess.CompletedProcess([], 0, json.dumps({'result': TEXT.replace('93%', '20%')}))
            result = d.tick()
            self.assertEqual(result['launched'], [tid])
            spec = launcher.specs[0]
            self.assertEqual(spec['seat'], 'acme-seat')
            self.assertEqual(spec['env'], {'CLAUDE_CONFIG_DIR': str(Path(tmp) / 'account')})
            self.assertEqual(stats.query(conn, task=tid)['runs'][0]['stats']['seat'], 'acme-seat')

    def test_director_falls_back_to_the_next_claude_seat_without_asking(self):
        from drover import dispatcher
        from drover.port import DirectPort
        from drover.ledger import ops
        from tests.test_dispatcher import FakeLauncher
        with tempfile.TemporaryDirectory() as tmp, closing(connect(Path(tmp) / 'ledger.db')) as conn:
            cfg = config.validate({'seats': [{'name': 'director', 'backend': 'claude', 'director': True},
                                             {'name': 'alternate', 'backend': 'claude'}]})
            config.use(cfg)
            self.addCleanup(config.reset)
            tid = ops.create_task(conn, 'Plan acme-lib', 'acme-lib')['id']
            ops.approve(conn, tid)
            usage.manual(conn, 'director', 'seven_day', 100, NOW + 3600, now=NOW)
            usage.manual(conn, 'alternate', 'seven_day', 20, NOW + 3600, now=NOW)
            d = dispatcher.Dispatcher(DirectPort(conn), launcher=FakeLauncher(), now_fn=lambda: NOW)
            task, policy = {**ops.get_task(conn, tid), 'role': 'director'}, {'data': {}, 'version': None}
            result = d._route(task, policy, ['claude'])
            self.assertEqual(result['seat'], 'alternate')
            self.assertEqual(ops.open_questions(conn), [])


class ObservationEdges(unittest.TestCase):
    setUp = Readings.setUp

    def test_partial_codex_snapshot_clears_the_omitted_window(self):
        seat = {**self.seat, 'backend': 'codex', 'name': 'acme-codex', 'codex_home': self.tmp.name}
        root = Path(self.tmp.name) / 'sessions'
        root.mkdir()
        path = root / 'rollout-a.jsonl'
        limits = {'primary': {'used_percent': 90, 'window_minutes': 300, 'resets_at': NOW + 3600},
                  'secondary': {'used_percent': 20, 'window_minutes': 10080, 'resets_at': NOW + 86400}}
        for omitted in ('primary', 'secondary'):
            with self.subTest(omitted=omitted):
                clock = [NOW]
                poller = usage.Poller(self.conn, now=lambda: clock[0])
                def write(value):
                    path.write_text(json.dumps({'payload': {'type': 'token_count', 'rate_limits': value}}), encoding='utf-8')
                write(limits)
                poller.refresh(seat)
                self.assertEqual(len(usage.latest(self.conn, seat['name'], NOW)), 2)
                clock[0] += 61
                write({**limits, omitted: None})
                poller.refresh(seat)
                expected = 'seven_day' if omitted == 'primary' else 'five_hour'
                self.assertEqual(set(usage.latest(self.conn, seat['name'], clock[0])), {expected})
                self.assertTrue(usage.readings(self.conn)[-1]['ok'])
        self.assertFalse(self.conn.execute("SELECT 1 FROM task_events WHERE kind='usage.alert'").fetchone())

    def test_nonobject_claude_envelopes_record_raw_failure_and_alert(self):
        for raw in ('null', '[]', '"changed"', '42', '{"result": null}'):
            with self.subTest(raw=raw):
                poller = usage.Poller(self.conn, now=lambda: NOW,
                                      run=lambda *a, **k: subprocess.CompletedProcess([], 0, raw))
                self.assertIsNone(poller.refresh(self.seat))
                row = usage.readings(self.conn)[-1]
                self.assertEqual(row['raw_text'], raw)
                self.assertFalse(row['ok'])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM task_events WHERE kind='usage.alert'").fetchone()[0], 5)

    def test_invalid_codex_limits_preserve_raw_payload_locally_and_in_wsl(self):
        from unittest.mock import patch
        raw = json.dumps({'primary': {'used_percent': 'changed', 'window_minutes': 300, 'resets_at': NOW + 3600}})
        root = Path(self.tmp.name) / 'sessions'
        root.mkdir()
        (root / 'rollout-a.jsonl').write_text(json.dumps({'payload': {'type': 'token_count',
                                                                     'rate_limits': json.loads(raw)}}), encoding='utf-8')
        seat = {**self.seat, 'backend': 'codex', 'name': 'acme-codex', 'placement': 'wsl', 'codex_home': self.tmp.name}
        for windows in (False, True):
            with self.subTest(windows=windows):
                def run(argv, **kw):
                    # Execute the WSL reader with fictional local paths, without starting WSL.
                    code = argv[argv.index('-c') + 1]
                    return subprocess.run([sys.executable, '-c', code, *argv[-2:]], **kw)
                with patch('drover.runner.to_wsl_path', side_effect=lambda p: p):
                    poller = usage.Poller(self.conn, run=run, now=lambda: NOW, windows=windows)
                    self.assertIsNone(poller.refresh(seat))
                row = usage.readings(self.conn)[-1]
                self.assertEqual(row['raw_text'], raw)
                self.assertFalse(row['ok'])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM task_events WHERE kind='usage.alert'").fetchone()[0], 2)

    def test_wsl_claude_probe_uses_worker_executable_and_distro(self):
        from drover import dispatcher, launch
        from drover.port import DirectPort
        launcher = launch.RunnerLauncher(runs_dir=self.tmp.name, distro='Acme', windows=True,
                                         wsl_bins=lambda: {'claude': '/opt/acme/bin/claude'})
        d = dispatcher.Dispatcher(DirectPort(self.conn), launcher=launcher, now_fn=lambda: NOW)
        d.usage_poller.windows = True
        calls = []
        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, json.dumps({'result': TEXT}))
        d.usage_poller.run = run
        seat = {**self.seat, 'placement': 'wsl', 'config_dir': '~/.acme-seat'}
        self.assertIsNotNone(d.usage_poller.refresh(seat))
        argv = calls[0]
        self.assertEqual(argv[:3], ['wsl.exe', '-d', 'Acme'])
        self.assertEqual(json.loads(argv[-1]), ['/opt/acme/bin/claude', '-p', '/usage', '--output-format', 'json'])
        self.assertEqual(json.loads(argv[-2]), {'CLAUDE_CONFIG_DIR': '~/.acme-seat'})

    def test_null_codex_reading_clears_cache_without_a_format_alert(self):
        seat = {**self.seat, 'backend': 'codex', 'name': 'acme-codex', 'codex_home': self.tmp.name}
        usage.manual(self.conn, seat['name'], 'seven_day', 30, NOW + 3600, now=NOW - 400)
        # A provider observation, rather than a manual override, is invalidated by null windows.
        self.conn.execute("UPDATE allowance_readings SET raw_text='{}'")
        root = Path(self.tmp.name) / 'sessions'
        root.mkdir()
        (root / 'rollout-a.jsonl').write_text(json.dumps({'payload': {'type': 'token_count', 'rate_limits':
                                                         {'primary': None, 'secondary': None}}}), encoding='utf-8')
        poller = usage.Poller(self.conn, now=lambda: NOW)
        self.assertIsNone(poller.refresh(seat))
        self.assertFalse(self.conn.execute("SELECT 1 FROM task_events WHERE kind='usage.alert'").fetchone())

    def test_manual_fallback_survives_a_failed_probe_until_reset(self):
        usage.manual(self.conn, self.seat['name'], 'seven_day', 30, str(NOW + 3600), now=NOW - 400)
        poller = usage.Poller(self.conn, run=lambda *a, **k: subprocess.CompletedProcess([], 1, ''), now=lambda: NOW)
        self.assertEqual(poller.refresh(self.seat)['remaining_pct'], 70)
        self.assertIsNone(usage.headroom(self.conn, self.seat['name'], NOW + 3601))

    def test_null_codex_windows_preserve_manual_fallback_until_reset_or_replacement(self):
        root = Path(self.tmp.name) / 'sessions'
        root.mkdir()
        path = root / 'rollout-a.jsonl'
        for minutes, window in usage.WINDOWS.items():
            for i, null_limits in enumerate((None, {'primary': None, 'secondary': None})):
                with self.subTest(window=window, limits=null_limits):
                    seat = {**self.seat, 'backend': 'codex', 'name': f'acme-codex-{minutes}-{i}',
                            'codex_home': self.tmp.name, 'reserve_pct': 25}
                    clock = [NOW]
                    poller = usage.Poller(self.conn, now=lambda: clock[0])
                    def write(limits):
                        path.write_text(json.dumps({'payload': {'type': 'token_count', 'rate_limits': limits}}),
                                        encoding='utf-8')
                    usage.manual(self.conn, seat['name'], window, 30, NOW + 3600, now=NOW - 400)
                    write(null_limits)
                    self.assertEqual(poller.refresh(seat)['remaining_pct'], 70)
                    clock[0] += 301
                    self.assertEqual(poller.refresh(seat)['remaining_pct'], 70)
                    self.assertTrue(usage.order(self.conn, [seat], now=clock[0])[0]['eligible'])
                    clock[0] = NOW + 3600
                    self.assertIsNone(poller.refresh(seat))

                    # A real reading replaces the fallback; later nulls must not revive it.
                    clock[0] += 61
                    usage.manual(self.conn, seat['name'], window, 30, NOW + 7200, now=clock[0] - 400)
                    write({'primary': {'used_percent': 80, 'window_minutes': minutes, 'resets_at': NOW + 7200}})
                    self.assertEqual(poller.refresh(seat)['remaining_pct'], 20)
                    clock[0] += 61
                    write(null_limits)
                    self.assertIsNone(poller.refresh(seat))
        self.assertFalse(self.conn.execute("SELECT 1 FROM task_events WHERE kind='usage.alert'").fetchone())

    def test_projection_without_history_does_not_assume_zero_burn(self):
        usage.manual(self.conn, self.seat['name'], 'seven_day', 10, NOW + 3600, now=NOW)
        row = usage.order(self.conn, [self.seat], now=NOW)[0]
        self.assertIsNone(row['waste_pct'])
        self.assertFalse(row['urgent'])

    def test_windows_hub_reads_codex_in_its_wsl_home(self):
        raw = json.dumps({'primary': {'used_percent': 10, 'window_minutes': 10080, 'resets_at': NOW + 3600},
                          'secondary': None})
        calls = []
        def run(argv, **kw):
            calls.append((argv, kw))
            return subprocess.CompletedProcess(argv, 0, json.dumps(raw))
        seat = {**self.seat, 'backend': 'codex', 'name': 'acme-codex', 'placement': 'wsl', 'codex_home': '~/.codex'}
        poller = usage.Poller(self.conn, run=run, now=lambda: NOW, windows=True)
        self.assertEqual(poller.refresh(seat)['remaining_pct'], 90)
        self.assertEqual(calls[0][0][-1], '~/.codex')
        self.assertIn('wsl.exe', calls[0][0])
        self.assertEqual(calls[0][1]['timeout'], 30)

    def test_next_reset_handles_both_instances_of_daylight_saving_fold(self):
        now = datetime(2026, 11, 1, 6, tzinfo=timezone.utc).timestamp()
        self.assertEqual(usage.next_reset('Nov 1, 1:30am', 'America/New_York', now),
                         datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc).timestamp())
