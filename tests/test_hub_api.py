"""Hub API tests: auth, owner gate, tasks, claim race, import, SSE."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
    from hub.app import create_app
    from hub.settings import Settings
except ImportError as exc:  # hub deps are third-party; skip when absent
    raise unittest.SkipTest(f"hub deps missing: {exc}")

from hub.dispatch import DispatcherService, LandingService

AUTH = {"Authorization": "Bearer tok"}
SAME = {"Sec-Fetch-Site": "same-origin"}


def fake_tailscale(login="owner@example.com", uid=7):
    import json

    def run(args):
        assert args == ["status", "--json"]
        return json.dumps({"Self": {"UserID": uid},
                           "User": {str(uid): {"LoginName": login}, "9": {"LoginName": "other@x"}}})
    return run


def no_tailscale(args):
    raise OSError("tailscale not installed")


def make(**kw):
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    kw.setdefault("tailscale", no_tailscale)
    kw.setdefault("ui_allowed_hosts", ("testserver", "testserver:8766"))
    kw.setdefault("run_watchdog", False)
    s = Settings(token="tok", **kw)
    app = create_app(Path(tmp.name) / "l.db", s)
    return tmp, app


class HubCase(unittest.TestCase):
    def setUp(self):
        self.tmp, self.app = make()
        self.c = TestClient(self.app)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.app.state.store.close)

    def post(self, path, body=None, **kw):
        return self.c.post("/api/v1" + path, json=body or {}, headers=AUTH, **kw)

    def get(self, path, **kw):
        return self.c.get("/api/v1" + path, headers=AUTH, **kw)

    def task(self, title="A", **kw):
        r = self.post("/tasks", {"title": title, **kw})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["id"]

    def ready_task(self, title="A", **kw):
        """A ready task, approved through the real owner path."""
        t = self.task(title, **kw)
        self.app.state.store.approve(t)
        return t


class HubLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.lock = Path(self.tmp.name) / "dispatcher.lock"

    def assert_stop_during_generation_snapshot_exits_promptly(self, service, wake, ticks):
        snapshot_entered, release_snapshot = threading.Event(), threading.Event()
        waiting, stopped = threading.Event(), threading.Event()
        generation = service._wake.generation

        def pause_before_snapshot():
            snapshot_entered.set()
            release_snapshot.wait(2)
            return generation()

        def observe_wait(*_):
            waiting.set()

        service._wake.generation = pause_before_snapshot
        service._wake.wait = observe_wait

        def stop_service():
            service.stop(timeout=1)
            stopped.set()

        stopper = None
        try:
            service.start()
            self.assertTrue(snapshot_entered.wait(2))
            stopper = threading.Thread(target=stop_service, daemon=True)
            stopper.start()
            self.assertTrue(service._stop.wait(2))
            release_snapshot.set()  # stop() has already woken the generation being snapshotted
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not (stopped.is_set() or waiting.is_set()):
                time.sleep(0.005)
            self.assertTrue(stopped.is_set(), "stop remained blocked after its wake was included in the snapshot")
            self.assertFalse(waiting.is_set(), "loop waited after shutdown began")
            self.assertEqual(ticks(), 0)
        finally:
            release_snapshot.set()
            wake()
            if stopper is not None:
                stopper.join(2)
            if getattr(service, "running", False) or getattr(getattr(service, "_thread", None), "is_alive", lambda: False)():
                service.stop(timeout=2)

    def test_dispatcher_and_landing_stop_races_skip_the_periodic_wait(self):
        tmp, app = make(dispatcher_lock=self.lock, dispatch_interval_s=3600)
        self.addCleanup(tmp.cleanup)
        self.addCleanup(app.state.store.close)
        made, landing_ticks = [], []
        class Tick:
            ticks = 0

            def tick(self):
                self.ticks += 1
        dispatcher = DispatcherService(app.state.store, app.state.settings,
                                       make_dispatcher=lambda p: made.append(Tick()) or made[-1])
        landing = LandingService(app.state.store, app.state.settings, interval_s=3600)
        landing.tick = lambda: landing_ticks.append(1)
        self.assert_stop_during_generation_snapshot_exits_promptly(
            dispatcher, dispatcher.wake_dispatcher, lambda: sum(d.ticks for d in made))
        self.assert_stop_during_generation_snapshot_exits_promptly(landing, landing.wake, lambda: len(landing_ticks))
        dispatcher.lock.acquire().release()
        self.assertFalse(landing._thread.is_alive())

    def test_start_and_shutdown_run_and_stop_the_dispatcher_thread(self):
        class Tick:
            ticks = 0

            def tick(self):
                self.ticks += 1

        made = []
        s = Settings(token="tok", dispatcher_lock=self.lock, dispatch_interval_s=0.02, run_watchdog=False,
                     run_dispatcher=True)
        app = create_app(Path(self.tmp.name) / "app.db", s, make_dispatcher=lambda port: made.append(Tick()) or made[-1])
        app.state.start_background()
        end = time.time() + 5
        while time.time() < end and not (made and made[0].ticks):
            time.sleep(0.01)
        self.assertTrue(app.state.dispatcher.running)
        self.assertTrue(made[0].ticks)
        app.state.shutdown()
        self.assertFalse(app.state.dispatcher.running)
        from drover.lock import FileLock
        FileLock(self.lock).acquire().release()

    def test_dispatcher_is_off_unless_asked_for(self):
        app = create_app(Path(self.tmp.name) / "app2.db", Settings(token="tok", dispatcher_lock=self.lock,
                                                                   run_watchdog=False))
        app.state.start_background()
        self.assertFalse(app.state.dispatcher.running)
        app.state.shutdown()

    def test_wsl_api_listener_is_off_by_default_and_the_ledger_setting_turns_it_on(self):
        from hub import __main__ as m
        app = create_app(Path(self.tmp.name) / "app3.db", Settings(token="tok", run_watchdog=False))
        self.assertFalse(m.wsl_listener_enabled(app, app.state.settings))
        app.state.store.update_settings({"api_listen_wsl": "true"})
        self.assertTrue(m.wsl_listener_enabled(app, app.state.settings))
        self.assertTrue(m.wsl_listener_enabled(object(), Settings(api_listen_wsl=True)))
        app.state.shutdown()


class AuthTests(HubCase):
    def test_settings_history_preserves_authenticated_provenance(self):
        from drover import config, decisions
        from hub import api, auth
        from starlette.requests import Request

        def update(app, headers, note):
            request = Request({'type': 'http', 'method': 'PUT', 'path': '/api/v1/settings',
                               'client': ('testclient', 1234), 'app': app,
                               'headers': [(k.lower().encode(), v.encode()) for k, v in
                                           {'Host': 'testserver', **headers}.items()]})
            return api.put_settings({'note': note}, request, who=auth.require_api(request))

        with tempfile.TemporaryDirectory() as d:
            cfg = config.validate({'paths': {'state_dir': d}})
            decisions.initialize(d)
            with patch.object(config, 'get', return_value=cfg):
                self.assertEqual(update(self.app, AUTH, 'Worker observation')['note'], 'Worker observation')
                records = list((Path(d) / 'decisions').glob('*.md'))
                self.assertEqual(len(records), 1)
                text = records[0].read_text()
                self.assertIn('author: dispatcher', text)
                self.assertIn('Dispatcher changed hub settings.', text)
                self.assertNotIn('Owner changed', text)
                tmp, app = make(trusted_ui_hosts=('testclient',))
                self.addCleanup(tmp.cleanup)
                self.addCleanup(app.state.store.close)
                self.assertEqual(update(app, SAME, 'Owner choice')['note'], 'Owner choice')
                owner_records = [p.read_text() for p in (Path(d) / 'decisions').glob('*.md')]
                self.assertTrue(any('author: owner' in t and 'Owner choice' in t for t in owner_records))

    def test_health_is_open(self):
        self.assertEqual(self.c.get("/api/v1/health").json()["ok"], True)

    def test_missing_and_wrong_token_rejected(self):
        self.assertEqual(self.c.get("/api/v1/tasks").status_code, 401)
        r = self.c.get("/api/v1/tasks", headers={"Authorization": "Bearer nope"})
        self.assertEqual(r.status_code, 401)
        r = self.c.get("/api/v1/tasks", headers={"Authorization": "Basic tok"})
        self.assertEqual(r.status_code, 401)

    def test_token_accepted(self):
        self.assertEqual(self.get("/tasks").status_code, 200)

    def test_ui_rejected_from_non_loopback(self):
        self.assertEqual(self.c.get("/").status_code, 403)
        r = self.c.get("/", headers={"Tailscale-User-Login": "a@b.c"})
        self.assertEqual(r.status_code, 403)

    def test_ui_accepted_from_loopback_and_tailscale(self):
        tmp, app = make(trusted_ui_hosts=("testclient",), tailscale=fake_tailscale())
        self.addCleanup(tmp.cleanup)
        self.addCleanup(app.state.store.close)
        c = TestClient(app, follow_redirects=False)
        self.assertEqual(c.get("/inbox").status_code, 200)
        r = c.get("/api/v1/tasks", headers={"Tailscale-User-Login": "owner@example.com"})
        self.assertEqual(r.status_code, 200)

    def test_default_allowlist_is_the_discovered_owner_only(self):
        tmp, app = make(trusted_ui_hosts=("testclient",), tailscale=fake_tailscale())
        self.addCleanup(tmp.cleanup)
        self.addCleanup(app.state.store.close)
        self.assertEqual(app.state.allowed_logins, ("owner@example.com",))
        c = TestClient(app, follow_redirects=False)
        other = c.get("/inbox", headers={"Tailscale-User-Login": "other@x"})
        self.assertEqual(other.status_code, 403)
        ok = c.get("/inbox", headers={"Tailscale-User-Login": "owner@example.com"})
        self.assertEqual(ok.status_code, 200)

    def test_undiscoverable_owner_means_loopback_only(self):
        for runner in (no_tailscale, lambda a: "not json", lambda a: "{}"):
            tmp, app = make(trusted_ui_hosts=("testclient",), tailscale=runner)
            self.addCleanup(tmp.cleanup)
            self.addCleanup(app.state.store.close)
            self.assertEqual(app.state.allowed_logins, ())
            c = TestClient(app, follow_redirects=False)
            r = c.get("/inbox", headers={"Tailscale-User-Login": "owner@example.com"})
            self.assertEqual(r.status_code, 403)
            self.assertEqual(c.get("/inbox").status_code, 200)  # plain loopback browser

    def test_ui_login_allowlist(self):
        tmp, app = make(trusted_ui_hosts=("testclient",), ui_allowed_logins=("me@x",))
        self.addCleanup(tmp.cleanup)
        self.addCleanup(app.state.store.close)
        c = TestClient(app, follow_redirects=False)
        ok = c.get("/inbox", headers={"Tailscale-User-Login": "me@x"})
        self.assertEqual(ok.status_code, 200)
        bad = c.get("/inbox", headers={"Tailscale-User-Login": "evil@x"})
        self.assertEqual(bad.status_code, 403)

    def test_ui_paths_refused_on_api_port(self):
        tmp, app = make(trusted_ui_hosts=("testclient",))
        self.addCleanup(tmp.cleanup)
        self.addCleanup(app.state.store.close)
        c = TestClient(app, base_url="http://testserver:8766")
        self.assertEqual(c.get("/inbox").status_code, 403)
        self.assertEqual(c.get("/api/v1/tasks", headers=AUTH).status_code, 200)
        self.assertEqual(c.get("/api/v1/tasks").status_code, 401)


class OwnerGateTests(HubCase):
    """The WSL bearer token must never be able to approve backlog work."""

    def test_token_cannot_approve(self):
        t = self.task()
        r = self.post(f"/tasks/{t}/approve")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.get(f"/tasks/{t}").json()["status"], "proposed")

    def test_token_cannot_set_ready_by_status(self):
        t = self.task()
        r = self.post(f"/tasks/{t}/status", {"status": "ready"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.get(f"/tasks/{t}").json()["status"], "proposed")
        t2 = self.task("draft one", status="draft")
        self.assertEqual(self.post(f"/tasks/{t2}/status", {"status": "ready"}).status_code, 403)

    def test_token_cannot_create_ready_or_import_approved(self):
        r = self.post("/tasks", {"title": "x", "status": "ready"})
        self.assertEqual(r.status_code, 422)
        r = self.post("/import", {"tasks": [{"title": "i", "status": "ready"}], "dry_run": False})
        self.assertEqual(r.status_code, 403)  # importing is owner-only
        t = self.get("/tasks").json()
        self.assertEqual(t, [])
        self.assertEqual(self.get("/tasks?status=ready").json(), [])

    def test_token_owner_author_is_downgraded_and_cannot_answer(self):
        t = self.ready_task()
        self.post("/tasks/claim", {})
        self.post(f"/tasks/{t}/status", {"status": "needs-owner", "reason": "q"})
        r = self.post("/comments", {"target_id": t, "body": "go", "author": "owner"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.get(f"/tasks/{t}").json()["status"], "needs-owner")
        q = self.post("/questions", {"target_id": t, "title": "ok?",
                                     "options": [{"label": "yes"}]}).json()
        self.assertEqual(self.post(f"/questions/{q['id']}/answer", {"choice": "yes"}).status_code, 403)
        self.assertEqual(self.post("/tasks/reorder", {"kind": "task", "item_id": t,
                                                       "before_id": t}).status_code, 403)

    def test_owner_via_ui_can_approve(self):
        tmp, app = make(trusted_ui_hosts=("testclient",))
        self.addCleanup(tmp.cleanup)
        self.addCleanup(app.state.store.close)
        c = TestClient(app)
        t = app.state.store.create_task("T")["id"]
        r = c.post(f"/api/v1/tasks/{t}/approve", headers=SAME)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ready")
        self.assertIsNotNone(r.json()["approved_at"])

    def test_wait_control_events_are_owner_reserved_and_cause_scoped(self):
        from drover.ledger.waiting import cause_key

        tmp, app = make(trusted_ui_hosts=("testclient",))
        self.addCleanup(tmp.cleanup)
        self.addCleanup(app.state.store.close)
        c = TestClient(app)

        def owner_post(path, body):
            return c.post("/api/v1" + path, json=body, headers=SAME)

        def manual_hold(title):
            created = owner_post("/tasks", {"title": title})
            self.assertEqual(created.status_code, 201, created.text)
            task_id = created.json()["id"]
            approved = owner_post(f"/tasks/{task_id}/approve", {})
            self.assertEqual(approved.status_code, 200, approved.text)
            blocked = owner_post(f"/tasks/{task_id}/status",
                                 {"status": "blocked", "reason": "manual pause"})
            self.assertEqual(blocked.status_code, 200, blocked.text)
            return task_id, cause_key("manual", task_id)

        held_task, held_cause = manual_hold("Owner hold")
        other_task, other_cause = manual_hold("Other owner hold")

        for kind in ("waiting.release", "waiting.clear", "waiting.injected"):
            with self.subTest(kind=kind):
                denied = c.post("/api/v1/events",
                                json={"kind": kind, "data": {"cause": held_cause},
                                      "task_id": held_task}, headers=AUTH)
                self.assertEqual(denied.status_code, 403, denied.text)
                self.assertEqual(c.get(f"/api/v1/tasks/{held_task}", headers=AUTH).json()["status"],
                                 "blocked")

        released = owner_post("/events", {"kind": "waiting.release",
                                          "data": {"cause": held_cause},
                                          "task_id": held_task})
        self.assertEqual(released.status_code, 201, released.text)
        self.assertEqual(c.get(f"/api/v1/tasks/{held_task}", headers=AUTH).json()["status"],
                         "ready")
        self.assertEqual(c.get(f"/api/v1/tasks/{other_task}", headers=AUTH).json()["status"],
                         "blocked")
        self.assertNotEqual(held_cause, other_cause)


class TaskTests(HubCase):
    def test_invalid_risk_is_rejected_on_create_and_patch(self):
        self.c = TestClient(self.app, raise_server_exceptions=False)
        expected = ("low", "high")
        r = self.post("/tasks", {"title": "x", "risk": "medium"})
        self.assertEqual(r.status_code, 422, r.text)
        for value in expected:
            self.assertIn(value, r.text)
        task_id = self.task()
        r = self.c.patch(f"/api/v1/tasks/{task_id}", json={"risk": "medium"}, headers=AUTH)
        self.assertEqual(r.status_code, 422, r.text)
        for value in expected:
            self.assertIn(value, r.text)

    def test_invalid_priority_is_rejected_on_create_and_patch(self):
        self.c = TestClient(self.app, raise_server_exceptions=False)
        expected = ("P0", "P1", "P2", "P3", "P4")
        r = self.post("/tasks", {"title": "x", "priority": "P9"})
        self.assertEqual(r.status_code, 422, r.text)
        for value in expected:
            self.assertIn(value, r.text)
        task_id = self.task()
        r = self.c.patch(f"/api/v1/tasks/{task_id}", json={"priority": "P9"}, headers=AUTH)
        self.assertEqual(r.status_code, 422, r.text)
        for value in expected:
            self.assertIn(value, r.text)

    def test_invalid_size_is_rejected_on_create_and_patch(self):
        self.c = TestClient(self.app, raise_server_exceptions=False)
        expected = ("S", "M", "L")
        r = self.post("/tasks", {"title": "x", "size": "XL"})
        self.assertEqual(r.status_code, 422, r.text)
        for value in expected:
            self.assertIn(value, r.text)
        task_id = self.task()
        r = self.c.patch(f"/api/v1/tasks/{task_id}", json={"size": "XL"}, headers=AUTH)
        self.assertEqual(r.status_code, 422, r.text)
        for value in expected:
            self.assertIn(value, r.text)

    def test_invalid_status_is_rejected_with_allowed_values(self):
        self.c = TestClient(self.app, raise_server_exceptions=False)
        initial_states = ("proposed", "draft")
        r = self.post("/tasks", {"title": "x", "status": "ready"})
        self.assertEqual(r.status_code, 422, r.text)
        for value in initial_states:
            self.assertIn(value, r.text)
        task_id = self.task()
        r = self.post(f"/tasks/{task_id}/status", {"status": "unknown"})
        self.assertEqual(r.status_code, 422, r.text)
        for value in ("proposed", "draft", "ready", "claimed", "running", "gated",
                      "reviewed", "landed", "blocked", "failed", "needs-owner", "cancelled"):
            self.assertIn(value, r.text)

    def test_task_api_enum_literals_match_ledger_constraints(self):
        import re
        from typing import Literal, get_args, get_origin

        from hub import api
        from drover.ledger.schema import V1
        from drover.ledger.states import CREATION_STATES, STATES

        def literal_values(field):
            annotation = field.annotation
            candidates = (annotation, *get_args(annotation))
            return next(get_args(item) for item in candidates if get_origin(item) is Literal)

        for name in ("priority", "size", "risk"):
            match = re.search(rf"{name} TEXT NOT NULL DEFAULT '[^']+' CHECK \({name} IN \(([^)]+)\)\)", V1)
            self.assertIsNotNone(match, f"missing {name} ledger CHECK")
            ledger_values = tuple(re.findall(r"'([^']+)'", match.group(1)))
            self.assertEqual(literal_values(api.TaskIn.model_fields[name]), ledger_values)
            self.assertEqual(literal_values(api.TaskPatch.model_fields[name]), ledger_values)

        self.assertEqual(literal_values(api.TaskIn.model_fields["status"]), CREATION_STATES)
        self.assertEqual(literal_values(api.StatusIn.model_fields["status"]), STATES)

    def test_crud_filter_status_board(self):
        e = self.post("/epics", {"repo": "p", "title": "E1"}).json()["id"]
        a = self.ready_task("A", epic_id=e)
        b = self.task("B")
        self.assertEqual(len(self.get("/tasks").json()), 2)
        self.assertEqual([t["id"] for t in self.get(f"/tasks?epic_id={e}").json()], [a])
        self.assertEqual([t["id"] for t in self.get("/tasks?status=ready").json()], [a])
        r = self.c.patch(f"/api/v1/tasks/{b}", json={"goal": "do it"}, headers=AUTH)
        self.assertEqual(r.json()["goal"], "do it")
        r = self.post(f"/tasks/{b}/status", {"status": "blocked", "reason": "x"})
        self.assertEqual(r.json()["status"], "blocked")
        self.assertEqual(self.post(f"/tasks/{b}/status", {"status": "landed"}).status_code, 400)
        self.assertEqual(self.get("/tasks/T-zzz").status_code, 404)
        board = self.get("/board").text
        self.assertIn("# Drover board", board)

    def test_runs_events_comments_questions_decisions(self):
        t = self.ready_task("A")
        claimed = self.post("/tasks/claim", {"model": "m"}).json()["task"]
        self.assertEqual(claimed["id"], t)
        run = self.post("/runs", {"task_id": t, "backend": "claude", "model": "m"}).json()
        self.assertEqual(self.get(f"/tasks/{t}").json()["status"], "running")
        done = self.post(f"/runs/{run['id']}/finish", {"outcome": "ok", "tokens_in": 5})
        self.assertEqual(done.json()["tokens_in"], 5)
        self.assertEqual(self.post("/events", {"kind": "note", "task_id": t}).status_code, 201)
        self.assertEqual(self.post("/comments", {"target_id": t, "body": "hi"}).status_code, 201)
        self.assertEqual(self.post("/comments", {"target_id": "T-nope", "body": "x"}).status_code, 404)
        q = self.post("/questions", {"target_id": t, "title": "which?",
                                     "options": [{"label": "a"}, {"label": "b"}]}).json()
        self.assertEqual(len(self.get("/questions?open=true").json()), 1)
        self.app.state.store.answer_question(q["id"], "a")
        self.assertEqual(self.get("/questions?open=true").json(), [])
        d = self.post("/decisions", {"question_name": "q", "criteria_version": "v1",
                                     "model": "m", "state_hash": "h", "answer": "yes",
                                     "confidence": 0.9}).json()
        o = self.post(f"/decisions/{d['id']}/outcome", {"outcome": "good"}).json()
        self.assertEqual(o["outcome"], "good")
        kinds = [e["kind"] for e in self.get(f"/events?task_id={t}").json()]
        self.assertIn("run-finished", kinds)

    def test_heartbeat_and_settings(self):
        self.assertEqual(self.get("/heartbeat").json(), {})
        self.post("/heartbeat", {"resources": {"load": 1}, "allowance": {"a": 2}, "workers": ["w"]})
        hb = self.get("/heartbeat").json()
        self.assertEqual(hb["resources"], {"load": 1})
        self.assertIn("ts", hb)
        r = self.c.put("/api/v1/settings", json={"note": "Debian"}, headers=AUTH)
        self.assertEqual(r.json()["note"], "Debian")
        self.assertNotIn("_heartbeat", self.get("/settings").json())
        bad = self.c.put("/api/v1/settings", json={"seq_T": "9"}, headers=AUTH)
        self.assertEqual(bad.status_code, 400)
