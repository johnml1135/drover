"""The dispatcher inside the hub: thread, single-instance lock, CLI refusal, ledger exclusion, contract parsers."""
import io
import json
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from drover import contract, dispatcher, landing
from drover.ledger import connect, ops
from drover.lock import FileLock, LockHeld
from drover.notifications import Notifications
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

    def test_owner_wake_runs_without_an_agent_even_when_dispatch_is_paused(self):
        conn = self.store.conn
        epic = ops.create_epic(conn, 'p', 'Landing')['id']
        with patch('drover.ledger.ops_common.clock', return_value=datetime.fromtimestamp(1000, timezone.utc)):
            qid = ops.ask_question(conn, 'epic', epic, 'Push main failed', actor='owner',
                                   options=[{'label': 'Acknowledged'}])
        ops.set_setting(conn, 'dispatch_enabled', 'false')
        svc = DispatcherService(self.store, self.settings, make_dispatcher=FakeDispatcher)
        with patch('hub.dispatch.Notifications', side_effect=lambda conn: Notifications(conn, now=lambda: 2800)):
            svc.start()
            self.addCleanup(svc.stop)
            self.wait(lambda: len(Notifications(conn).owner_attention()) == 1)
        svc.stop()
        notice, = Notifications(conn).owner_attention()
        self.assertEqual(notice['question'], qid)
        self.assertEqual(ops.open_questions(conn)[0]['id'], qid)
        self.assertEqual(len(ops.open_questions(conn)), 1)

    def test_question_monitor_runs_after_a_tick_reconciles_stale_questions(self):
        conn = self.store.conn
        epic = ops.create_epic(conn, 'p', 'Landing')['id']
        qid = ops.ask_question(conn, 'epic', epic, 'Old landing question', actor='landing')
        conn.execute("UPDATE questions SET asked='1970-01-01T00:00:00Z' WHERE id=?", (qid,))

        class Reconcile(FakeDispatcher):
            def tick(self):
                if ops.get_question(self.port.conn, qid)['status'] == 'open':
                    ops.answer_question(self.port.conn, qid, text='Superseded by current landing state')
                super().tick()

        svc = DispatcherService(self.store, self.settings, make_dispatcher=Reconcile)
        svc.start()
        self.addCleanup(svc.stop)
        self.wait(lambda: svc.dispatcher and svc.dispatcher.ticks >= 2)
        svc.stop()
        self.assertEqual(Notifications(conn).owner_attention(), [])
        self.assertEqual(ops.events(conn, kind='notification.landing-owner-wake'), [])


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


class LandingOwnerWakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'landing.db'
        self.now = 1000.
        self.open_ledger()

    def open_ledger(self):
        self.conn = connect(self.path)
        self.addCleanup(self.conn.close)
        self.service = Notifications(self.conn, now=lambda: self.now)

    def question(self, *, base=False, actor='landing', epic_title='Landing'):
        with patch('drover.ledger.ops_common.clock', return_value=datetime.fromtimestamp(self.now, timezone.utc)):
            if base:
                landing.base_red(self.conn, {}, f'{int(self.now):040x}', [], 'p')
                return ops.open_questions(self.conn)[-1]['id']
            epic = ops.create_epic(self.conn, 'p', epic_title)['id']
            return ops.ask_question(self.conn, 'epic', epic, 'Push main failed', actor=actor,
                                    options=[{'label': 'Acknowledged'}])

    def test_deadline_and_single_wake_survive_restarts_without_an_agent(self):
        qid = self.question()
        self.assertEqual(self.service.wake_landing_questions(), [])
        state = json.loads(ops.get_setting(self.conn, 'landing_owner_wakes'))
        self.assertEqual(state[str(qid)]['deadline'], 2800.)
        self.conn.close()
        self.open_ledger()
        self.now = 2799.
        self.assertEqual(self.service.wake_landing_questions(), [])
        self.now = 2800.
        notice, = self.service.wake_landing_questions()
        self.assertEqual(notice['question'], qid)
        self.assertIn('Push main failed', notice['title'])
        self.assertIn(f'question #{qid}', notice['body'])
        self.assertIn('Needs me', notice['body'])
        self.assertIn('Acknowledged', notice['body'])
        self.assertEqual(self.service.owner_attention(), [notice])
        self.conn.close()
        self.open_ledger()
        self.now = 10000.
        self.assertEqual(self.service.wake_landing_questions(), [])
        self.assertEqual(self.service.owner_attention(), [notice])
        self.assertEqual(len(ops.events(self.conn, kind='notification.landing-owner-wake')), 1)
        self.assertEqual([q['id'] for q in ops.open_questions(self.conn)], [qid])

    def test_overdue_at_first_observation_wakes_immediately(self):
        qid = self.question(base=True)
        self.now = 2800.
        self.assertEqual([n['question'] for n in self.service.wake_landing_questions()], [qid])

    def test_owner_push_failure_without_a_wait_wakes_once_and_retracts_on_answer(self):
        from drover.ledger.waiting import Waiting
        qid = self.question(actor='owner')
        self.assertEqual(Waiting(self.conn).pending(), [])
        self.assertEqual([q['id'] for q in self.service._landing_questions()], [qid])
        self.assertEqual(self.service.wake_landing_questions(), [])
        self.conn.close()
        self.open_ledger()
        self.now = 2799.
        self.assertEqual(self.service.wake_landing_questions(), [])
        self.assertEqual(self.service.owner_attention(), [])
        self.now = 2800.
        notice, = self.service.wake_landing_questions()
        self.assertEqual(notice['question'], qid)
        self.assertIn('Push main failed', notice['title'])
        self.assertIn(f'question #{qid}', notice['body'])
        self.assertIn('Acknowledged', notice['body'])
        self.assertEqual(self.service.owner_attention(), [notice])
        self.assertEqual([q['id'] for q in ops.open_questions(self.conn)], [qid])
        self.conn.close()
        self.open_ledger()
        self.now += 1800
        self.assertEqual(self.service.wake_landing_questions(), [])
        self.assertEqual(self.service.owner_attention(), [notice])
        ops.answer_question(self.conn, qid, choice='Acknowledged')
        self.assertEqual(self.service.owner_attention(), [])
        self.assertEqual(self.service.wake_landing_questions(), [])
        self.conn.close()
        self.open_ledger()
        self.now += 1800
        self.assertEqual(self.service.wake_landing_questions(), [])
        self.assertEqual(self.service.owner_attention(), [])
        self.assertEqual(len(ops.events(self.conn, kind='notification.landing-owner-wake')), 1)
        self.assertEqual(ops.open_questions(self.conn), [])

    def test_answered_and_superseded_questions_are_retracted_and_never_woken_again(self):
        for wake_first in (False, True):
            for superseded in (False, True):
                with self.subTest(wake_first=wake_first, superseded=superseded):
                    qid = self.question(base=True)
                    self.service.wake_landing_questions()
                    self.now += 1800
                    if wake_first:
                        self.assertEqual(len(self.service.wake_landing_questions()), 1)
                    if superseded:
                        ops.event(self.conn, 'base-observed', 'landing', {'repo': 'p', 'sha': 'b' * 40})
                    else:
                        ops.answer_question(self.conn, qid, choice='Retry landing')
                    self.assertEqual(self.service.owner_attention(), [])
                    self.assertEqual(self.service.wake_landing_questions(), [])
                    self.conn.close()
                    self.open_ledger()
                    self.now += 1800
                    self.assertEqual(self.service.wake_landing_questions(), [])
                    self.assertEqual(self.service.owner_attention(), [])

    def test_keep_holding_is_owner_action_even_though_question_stays_open(self):
        qid = self.question(base=True)
        self.service.wake_landing_questions()
        ops.answer_question(self.conn, qid, choice='Keep holding')
        self.assertEqual(ops.get_question(self.conn, qid)['status'], 'open')
        self.now += 1800
        self.assertEqual(self.service.wake_landing_questions(), [])
        self.assertEqual(self.service.owner_attention(), [])
        self.conn.close()
        self.open_ledger()
        self.now += 1800
        self.assertEqual(self.service.wake_landing_questions(), [])
        self.assertEqual(self.service.owner_attention(), [])
        self.assertEqual(ops.events(self.conn, kind='notification.landing-owner-wake'), [])

    def test_keep_holding_retracts_an_existing_wake(self):
        qid = self.question(base=True)
        self.now += 1800
        self.assertEqual(len(self.service.wake_landing_questions()), 1)
        ops.answer_question(self.conn, qid, choice='Keep holding')
        self.assertEqual(self.service.owner_attention(), [])
        self.now += 1800
        self.assertEqual(self.service.wake_landing_questions(), [])
        self.conn.close()
        self.open_ledger()
        self.now += 1800
        self.assertEqual(self.service.wake_landing_questions(), [])
        self.assertEqual(self.service.owner_attention(), [])
        self.assertEqual(len(ops.events(self.conn, kind='notification.landing-owner-wake')), 1)

    def test_new_question_has_its_own_deadline_and_identity(self):
        first = self.question()
        self.now += 1800
        self.service.wake_landing_questions()
        ops.answer_question(self.conn, first, choice='Acknowledged')
        second = self.question()
        self.assertEqual(self.service.wake_landing_questions(), [])
        self.now += 1800
        self.assertEqual([n['question'] for n in self.service.wake_landing_questions()], [second])
        self.assertEqual([n['question'] for n in self.service.owner_attention()], [second])

    def test_wake_and_marker_roll_back_together_and_retry_once(self):
        self.question()
        self.service.wake_landing_questions()
        self.now += 1800
        with patch('drover.notifications.core.ops.set_setting', side_effect=RuntimeError('interrupted')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                self.service.wake_landing_questions()
        self.assertEqual(self.service.owner_attention(), [])
        self.assertEqual(ops.events(self.conn, kind='notification.landing-owner-wake'), [])
        self.assertEqual(len(self.service.wake_landing_questions()), 1)
        self.assertEqual(self.service.wake_landing_questions(), [])

    def test_landing_epic_questions_do_not_require_a_landing_actor(self):
        qid = self.question(actor='tuner')
        self.now += 1800
        self.assertEqual([n['question'] for n in self.service.wake_landing_questions()], [qid])

    def test_landing_actor_questions_on_other_epics_are_still_selected(self):
        qid = self.question(epic_title='Release planning')
        self.assertEqual([q['id'] for q in self.service._landing_questions()], [qid])

    def test_unrelated_owner_questions_are_not_landing_wakes(self):
        self.question(actor='owner', epic_title='Release planning')
        epics = ops.list_epics(self.conn)
        self.assertEqual(self.service._landing_questions(), [])
        self.now += 1800
        self.assertEqual(self.service.wake_landing_questions(), [])
        self.assertEqual(self.service.owner_attention(), [])
        self.assertEqual(ops.events(self.conn, kind='notification.landing-owner-wake'), [])
        self.assertEqual(ops.list_epics(self.conn), epics)

    def test_selecting_landing_questions_does_not_create_an_epic(self):
        self.assertEqual(ops.list_epics(self.conn), [])
        changes = self.conn.total_changes
        self.assertEqual(self.service._landing_questions(), [])
        self.assertEqual(ops.list_epics(self.conn), [])
        self.assertEqual(self.conn.total_changes, changes)


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
