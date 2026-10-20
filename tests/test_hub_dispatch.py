"""The dispatcher inside the hub: thread, single-instance lock, CLI refusal, ledger exclusion, contract parsers."""
import io
import json
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from drover import contract, dispatcher, gate
from drover.ledger import connect, ops
from drover.lock import FileLock, LockHeld
from hub.dispatch import DispatcherService, HubPort, LandingService
from hub.events import Bus
from hub.settings import Settings
from hub.store import Store

try:
    import fastapi  # noqa: F401
    from hub.app import create_app
except ImportError:
    create_app = None


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.lock = str(Path(self.tmp.name) / "dispatcher.lock")
        self.bus = Bus()
        self.store = Store(Path(self.tmp.name) / "l.db", publish=self.bus.publish)
        self.addCleanup(self.store.close)
        self.settings = Settings(token="t", dispatcher_lock=Path(self.lock), dispatch_interval_s=0.02,
                                 runs_dir=Path(self.tmp.name) / "runs")


class WslGate(Base):
    @unittest.skipIf(__import__('os').name == 'nt', 'exercise WSL adapter using the local POSIX interpreter')
    def test_composed_wsl_landing_plan_runs_scripts_with_docs_and_environment(self):
        import os
        import sys
        from types import SimpleNamespace
        from unittest.mock import patch
        from drover import repos
        from tests.gitfix import Repo
        fix = Repo()
        self.addCleanup(fix.cleanup)
        script = 'scripts/drover/check action.ps1'
        fix.commit(script, 'Write-Output "kind:$env:DROVER_CI_KIND"\n'
                   'Write-Output "value:$env:LOCAL_VALUE"\n'
                   'Write-Output "changed:$env:DROVER_CHANGED_FILES"\nexit 0\n')
        fix.commit('drover.toml', f'[scripts]\ntest="{script}"\ndocs="{script}"\n'
                   '[gate]\nplatform="wsl"\n[run]\nplatforms=["wsl"]\ndefault="wsl"\n')
        fix.git('checkout', '-b', 'topic')
        tip = fix.commit('docs/a.md', 'docs change\n')
        calls = []

        def remote(argv, **kw):
            calls.append(argv)
            command = argv[argv.index('--exec') + 1:]
            return subprocess.run([sys.executable, *command[1:]],
                                  cwd=argv[argv.index('--cd') + 1], **kw)

        svc = LandingService(self.store, self.settings, run=remote)
        machine = {'name': 'example', 'repo': str(fix.repo), 'run': {'env': {'LOCAL_VALUE': 'machine'}}}
        check = svc.gate_fn(repos.load(machine), 'main', self.settings.runs_dir, None)
        with patch('hub.dispatch.os', SimpleNamespace(name='nt', environ=os.environ)):
            receipt = check(fix.repo, tip, [{'id': 'T-1'}])
        self.assertTrue(receipt['passed'])
        self.assertEqual(receipt['kind'], 'docs')
        self.assertEqual(receipt['steps'][-1]['command'], repos.script_argv(repos.load(machine), 'docs'))
        tail = '\n'.join(receipt['log_tail'])
        self.assertIn('kind:docs', tail)
        self.assertIn('value:machine', tail)
        self.assertIn('docs/a.md', tail)
        self.assertEqual(calls[0][0], 'wsl.exe')

    def test_missing_receipt_reports_return_code_and_last_twenty_stderr_lines(self):
        stderr = '\n'.join(['old diagnostic'] + [f'diagnostic {n}' for n in range(19)] + ['fatal: invalid gitdir'])

        def run(argv, **kw):
            return subprocess.CompletedProcess(argv, 2, '', stderr)

        svc = LandingService(self.store, self.settings, run=run)
        from tests.gitfix import Repo
        fix = Repo()
        self.addCleanup(fix.cleanup)
        check = svc._wsl_gate(['pwsh', '-NoLogo', '-NoProfile', '-NonInteractive', '-File', 'scripts/drover/test.ps1'], 'main', self.settings.runs_dir)
        with self.assertRaises(gate.GateError) as cm:
            check(fix.repo, fix.git('rev-parse', 'HEAD'), [])
        message = str(cm.exception)
        self.assertIn('return code 2', message)
        self.assertIn('fatal: invalid gitdir', message)
        self.assertIn('diagnostic 0', message)
        self.assertNotIn('old diagnostic', message)


class FakeDispatcher:
    def __init__(self, port):
        self.port, self.ticks, self.thread, self.conns = port, 0, None, []

    def tick(self):
        self.ticks += 1
        self.thread = threading.current_thread().name
        self.port.heartbeat({"live": 0})


class Lock(Base):
    def setUp(self):
        super().setUp()
        self.orig = dispatcher.IS_WINDOWS
        dispatcher.IS_WINDOWS = True
        self.addCleanup(setattr, dispatcher, "IS_WINDOWS", self.orig)

    def test_second_holder_is_refused_and_release_frees_it(self):
        a = FileLock(self.lock).acquire()
        with self.assertRaises(LockHeld):
            FileLock(self.lock).acquire()
        a.release()
        b = FileLock(self.lock).acquire()
        self.assertTrue(b.held)
        b.release()
        self.assertFalse(b.held)

    def test_cli_once_refuses_while_the_hub_holds_the_lock(self):
        db = str(Path(self.tmp.name) / "cli.db")
        held = FileLock(self.lock).acquire()
        self.addCleanup(held.release)
        err = io.StringIO()
        with redirect_stderr(err):
            rc = dispatcher.main(["--db", db, "--once", "--lock", self.lock])
        self.assertEqual(rc, 2)
        self.assertIn("refusing to start", err.getvalue())
        self.assertFalse(Path(db).exists())          # it never opened the ledger

    def test_cli_runs_when_the_lock_is_free_and_releases_it(self):
        db = str(Path(self.tmp.name) / "cli.db")
        out = io.StringIO()
        import contextlib
        sub = type("S", (), {})
        orig = dispatcher.Dispatcher
        dispatcher.Dispatcher = lambda port, pipeline: type(
            "D", (), {"tick": lambda s: {"verdict": "go", "launched": [], "reaped": []}})()
        self.addCleanup(setattr, dispatcher, "Dispatcher", orig)
        with contextlib.redirect_stdout(out):
            rc = dispatcher.main(["--db", db, "--once", "--lock", self.lock])
        self.assertEqual(rc, 0)
        FileLock(self.lock).acquire().release()


class Service(Base):
    def wait(self, fn, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            if fn():
                return True
            time.sleep(0.01)
        self.fail("timed out")

    def test_start_runs_ticks_on_its_own_thread_with_its_own_connection_and_stop_releases_the_lock(self):
        made = []
        svc = DispatcherService(self.store, self.settings, make_dispatcher=lambda port: made.append(FakeDispatcher(port)) or made[-1])
        self.assertTrue(svc.start())
        self.wait(lambda: made and made[0].ticks >= 3)
        self.assertEqual(made[0].thread, "dispatcher")
        self.assertIsNot(made[0].port.conn, self.store.conn)          # per-thread connection
        self.assertIsNotNone(self.store.get_heartbeat())              # the health page's heartbeat is kept
        with self.assertRaises(LockHeld):
            FileLock(self.lock).acquire()
        svc.stop()
        self.assertFalse(svc.running)
        n = made[0].ticks
        time.sleep(0.1)
        self.assertEqual(made[0].ticks, n)
        FileLock(self.lock).acquire().release()

    def test_a_second_service_is_refused_and_says_so(self):
        a = DispatcherService(self.store, self.settings, make_dispatcher=FakeDispatcher)
        b = DispatcherService(self.store, self.settings, make_dispatcher=FakeDispatcher)
        self.assertTrue(a.start())
        self.addCleanup(a.stop)
        self.assertFalse(b.start())
        self.assertFalse(b.running)
        self.assertIn("hub.dispatcher", [e["kind"] for e in self.store.list_events()])

    def test_a_failing_tick_does_not_end_the_thread(self):
        class Bad(FakeDispatcher):
            def tick(self):
                super().tick()
                raise RuntimeError("boom")
        made = []
        svc = DispatcherService(self.store, self.settings, make_dispatcher=lambda p: made.append(Bad(p)) or made[-1])
        svc.start()
        self.addCleanup(svc.stop)
        self.wait(lambda: made and made[0].ticks >= 3)
        self.assertIn("boom", svc.error)

    def test_hub_port_publishes_dispatcher_changes_to_the_ui_bus(self):
        t = self.store.create_task("t", "p")
        self.store.approve(t["id"])
        port = HubPort(self.store)
        got = port.claim_next({})
        rid = port.start_run(got["id"], "worker", "claude", "m", "high")
        port.finish_run(rid, "error")
        kinds = [e["kind"] for e in self.bus.recent()]
        for k in ("task.claimed", "run.started", "run.finished"):
            self.assertIn(k, kinds)

    def test_default_dispatcher_uses_the_runner_launcher_with_settings(self):
        svc = DispatcherService(self.store, self.settings)
        self.store.update_settings({"wsl_distro": "Other"})
        d = svc._default_dispatcher(HubPort(self.store))
        self.assertEqual((d.launcher.distro, d.launcher.runs_dir), ("Other", str(self.settings.runs_dir)))
        self.store.update_settings({"wsl_distro": "New-choice"})
        self.assertEqual(d.launcher.distro, "New-choice")
        self.assertEqual(d.pipeline.runs_dir, str(self.settings.runs_dir))
        self.assertEqual(d.usage_poller.conn, self.store.conn)


@unittest.skipUnless(create_app, "fastapi is not installed")
class HubApp(Base):
    def test_start_and_shutdown_run_and_stop_the_dispatcher_thread(self):
        made = []
        s = Settings(token="t", dispatcher_lock=Path(self.lock), dispatch_interval_s=0.02, run_watchdog=False,
                     run_dispatcher=True)
        app = create_app(Path(self.tmp.name) / "app.db", s,
                         make_dispatcher=lambda port: made.append(FakeDispatcher(port)) or made[-1])
        app.state.start_background()
        end = time.time() + 5
        while time.time() < end and not (made and made[0].ticks):
            time.sleep(0.01)
        self.assertTrue(app.state.dispatcher.running)
        self.assertTrue(made[0].ticks)
        app.state.shutdown()
        self.assertFalse(app.state.dispatcher.running)
        FileLock(self.lock).acquire().release()

    def test_dispatcher_is_off_unless_asked_for(self):
        app = create_app(Path(self.tmp.name) / "app2.db", Settings(token="t", dispatcher_lock=Path(self.lock),
                                                                   run_watchdog=False))
        app.state.start_background()
        self.assertFalse(app.state.dispatcher.running)
        app.state.shutdown()

    def test_wsl_api_listener_is_off_by_default_and_the_ledger_setting_turns_it_on(self):
        from hub import __main__ as m
        app = create_app(Path(self.tmp.name) / "app3.db", Settings(token="t", run_watchdog=False))
        self.assertFalse(m.wsl_listener_enabled(app, app.state.settings))
        app.state.store.update_settings({"api_listen_wsl": "true"})
        self.assertTrue(m.wsl_listener_enabled(app, app.state.settings))
        self.assertTrue(m.wsl_listener_enabled(object(), Settings(api_listen_wsl=True)))
        app.state.shutdown()


class LedgerExclusion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.conn = connect(Path(self.tmp.name) / "l.db")
        self.addCleanup(self.conn.close)

    def ready(self, title, **kw):
        t = ops.create_task(self.conn, title, "p", **kw)
        ops.approve(self.conn, t["id"])
        return t["id"]

    def test_claim_next_passes_over_excluded_ids_and_is_backward_compatible(self):
        a, b = self.ready("a", priority="P0"), self.ready("b", priority="P1")
        self.assertEqual(ops.claim_next(self.conn, {}, exclude=[a])["id"], b)
        self.assertEqual(ops.get_task(self.conn, a)["status"], "ready")
        self.assertIsNone(ops.claim_next(self.conn, {}, exclude={a}))
        self.assertEqual(ops.claim_next(self.conn, {})["id"], a)

    def test_backoff_round_trip(self):
        self.assertEqual(ops.get_backoffs(self.conn), {})
        ops.set_backoff(self.conn, "T-1", 5.0, 2)
        ops.set_backoff(self.conn, "T-2", 9.0, 4)
        self.assertEqual(ops.get_backoffs(self.conn), {"T-1": {"until": 5.0, "min": 2}, "T-2": {"until": 9.0, "min": 4}})
        ops.set_backoff(self.conn, "T-1", None, 0)
        self.assertEqual(list(ops.get_backoffs(self.conn)), ["T-2"])


class ContractParsers(unittest.TestCase):
    def test_allowance_used_shapes(self):
        self.assertEqual(contract.allowance_used(1.5, 0.25), {"seven_day_pct": 1.5, "five_hour_pct": 0.25})
        self.assertIsNone(contract.allowance_used())
        self.assertEqual(contract.parse_allowance_used('{"seven_day_pct": 2, "five_hour_pct": 1}'), (2.0, 1.0))
        self.assertEqual(contract.parse_allowance_used("0.5"), (0.5, None))          # legacy bare number
        self.assertEqual(contract.parse_allowance_used({"pct": 3}), (3.0, None))     # legacy dict
        self.assertEqual(contract.parse_allowance_used({"seven_day": 4}), (4.0, None))
        self.assertEqual(contract.parse_allowance_used("junk"), (None, None))
        self.assertEqual(contract.cost_pct({"five_hour_pct": 1}), None)

    def test_used_delta(self):
        self.assertEqual(contract.used_delta({"seven_day": 10, "five_hour": 20}, {"seven_day": 10.5, "five_hour": 19}),
                         {"seven_day_pct": 0.5, "five_hour_pct": 0.0})
        self.assertIsNone(contract.used_delta({}, {"seven_day": 1}))
        self.assertEqual(contract.used_from_headroom({"remaining_pct": 90}), {"seven_day": 10.0})
        self.assertEqual(contract.used_from_headroom(None), {})

    def test_receipts_and_events(self):
        r = contract.gate_receipt(False, 7, "a\nb", receipt={"x": 1})
        self.assertEqual(json.loads(r), {"ok": False, "diff_lines": 7, "excerpt": "a\nb", "receipt": {"x": 1}})
        self.assertEqual(contract.receipt_diff_lines(r), 7)
        self.assertEqual(contract.receipt_excerpt("plain old excerpt"), "plain old excerpt")
        self.assertIsNone(contract.receipt_diff_lines("plain old excerpt"))
        self.assertEqual(contract.parse_gate_event({"duration_s": 3, "red": True}), (3.0, True))
        self.assertEqual(contract.parse_gate_event({"result": "red"}), (None, True))

    def test_decision_outcomes(self):
        self.assertTrue(contract.decision_hit("a", "a"))
        self.assertFalse(contract.decision_hit("a", "b"))
        self.assertIsNone(contract.decision_hit("a", {"audit": "missed"}))
        self.assertTrue(contract.decision_hit("a", {"label": "a"}))
        self.assertFalse(contract.decision_hit("a", {"correct": False}))
        self.assertTrue(contract.decision_rule_hit("x", {"rule_correct": True}))
        self.assertIsNone(contract.decision_rule_hit(None, "a"))
        self.assertEqual(contract.decision_audit(contract.decision_outcome(audit="needless")), "needless")
        self.assertIsNone(contract.decision_audit("x"))


if __name__ == "__main__":
    unittest.main()
