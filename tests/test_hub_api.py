"""Hub API tests: auth, owner gate, tasks, claim race, import, SSE."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
    from hub.app import create_app
    from hub.settings import Settings
except ImportError as exc:  # hub deps are third-party; skip when absent
    raise unittest.SkipTest(f"hub deps missing: {exc}")

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


class ClaimTests(HubCase):
    def test_claim_respects_deps_and_priority(self):
        a = self.task("dep", priority="P3")
        b = self.task("needs", priority="P1", deps=[a])
        for t in (a, b):
            self.app.state.store.approve(t)
        self.assertEqual(self.post("/tasks/claim", {}).json()["task"]["id"], a)
        self.assertIsNone(self.post("/tasks/claim", {}).json()["task"])

    def test_claim_race_each_task_claimed_once(self):
        store = self.app.state.store
        n = 5
        for i in range(n):
            store.approve(store.create_task(f"t{i}")["id"])
        got, lock = [], threading.Lock()

        def work(name):
            while True:
                t = store.claim_next({"actor": name, "owner_kind": name})
                if t is None:
                    return
                with lock:
                    got.append(t["id"])

        threads = [threading.Thread(target=work, args=(f"w{i}",)) for i in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(got), n)
        self.assertEqual(len(set(got)), n)

    def test_claim_via_http_threads(self):
        for i in range(4):
            self.ready_task(f"t{i}")
        out, lock = [], threading.Lock()

        def hit():
            r = self.post("/tasks/claim", {})
            with lock:
                out.append(r.json()["task"])

        ts = [threading.Thread(target=hit) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        ids = [t["id"] for t in out if t]
        self.assertEqual(len(ids), 4)
        self.assertEqual(len(set(ids)), 4)


class ImportTests(HubCase):
    def imp(self, body):
        """Import as the owner (the UI path); the HTTP route is owner-only and tests have no owner client."""
        return self.app.state.store.import_tasks(body["tasks"], dry_run=body.get("dry_run", True), actor="owner")

    def test_token_cannot_import(self):
        self.assertEqual(self.post("/import", {"tasks": self.ITEMS, "dry_run": False}).status_code, 403)
        self.assertEqual(self.get("/tasks").json(), [])

    ITEMS = [{"title": "one", "epic": "e"}, {"title": "two"}, {"title": ""},
             {"title": "ONE", "epic": "e"}, {"title": "bad dep", "deps": ["T-nope"]}]

    def test_dry_run_changes_nothing(self):
        r = self.imp({"tasks": self.ITEMS})
        self.assertTrue(r["dry_run"])
        self.assertEqual(r["counts"], {"create": 2, "duplicate": 1, "invalid": 2})
        self.assertEqual(self.get("/tasks").json(), [])
        self.assertEqual(self.get("/epics").json(), [])

    def test_real_import_creates_proposed_and_dedupes(self):
        r = self.imp({"tasks": self.ITEMS, "dry_run": False})
        self.assertEqual(len(r["created"]), 2)
        tasks = self.get("/tasks").json()
        self.assertEqual({t["status"] for t in tasks}, {"proposed"})
        self.assertEqual(len(self.get("/epics").json()), 1)
        again = self.imp({"tasks": self.ITEMS, "dry_run": False})
        self.assertEqual(again["created"], [])


class SseTests(HubCase):
    def test_mutations_publish_and_stream_replays(self):
        t = self.task("A")
        self.post(f"/tasks/{t}/status", {"status": "blocked", "reason": "r"})
        kinds = [e["kind"] for e in self.app.state.bus.recent()]
        self.assertEqual(kinds, ["task.created", "task.status"])
        tmp, app = make(trusted_ui_hosts=("testclient",))
        self.addCleanup(tmp.cleanup)
        self.addCleanup(app.state.store.close)
        app.state.store.create_task("X")
        c = TestClient(app)
        with c.stream("GET", "/events?limit=1", headers={"Last-Event-ID": "0"}) as r:
            self.assertEqual(r.headers["content-type"].split(";")[0], "text/event-stream")
            body = "".join(r.iter_text())
        self.assertIn("event: ledger", body)
        self.assertIn("task.created", body)

    def test_events_endpoint_rejects_outsiders(self):
        self.assertEqual(self.c.get("/events?limit=1").status_code, 403)

    def test_every_mutation_publishes(self):
        bus = self.app.state.bus
        t = self.ready_task("A")
        self.post("/tasks/claim", {})
        r = self.post("/runs", {"task_id": t, "backend": "claude"}).json()["id"]
        self.post(f"/runs/{r}/finish", {"outcome": "ok"})
        self.post("/events", {"kind": "n"})
        self.post("/comments", {"target_id": t, "body": "c"})
        q = self.post("/questions", {"target_id": t, "title": "q"}).json()["id"]
        self.app.state.store.answer_question(q, text="a")
        d = self.post("/decisions", {"question_name": "q", "criteria_version": "v", "model": "m",
                                     "state_hash": "h", "answer": 1}).json()["id"]
        self.post(f"/decisions/{d}/outcome", {"outcome": "o"})
        self.post("/heartbeat", {})
        self.app.state.store.import_tasks([{"title": "z"}], dry_run=False)
        kinds = {e["kind"] for e in bus.recent()}
        for k in ("task.created", "task.claimed", "run.started", "run.finished", "event",
                  "comment", "question.asked", "question.answered", "decision.recorded",
                  "decision.outcome", "heartbeat", "import", "task.status"):
            self.assertIn(k, kinds)


class AdapterRetryTests(unittest.TestCase):
    def run_serve_api(self, ips, app, ticks):
        """Drive serve_api with scripted discovery results; return server records."""
        import asyncio
        from unittest import mock

        from hub import __main__ as m

        seq = iter(ips)
        calls = []

        def discover(glob):
            calls.append(glob)
            return next(seq, ips[-1])

        servers = []

        class FakeConfig:
            def __init__(self, app, host, port, log_level, lifespan=None):
                self.host, self.port, self.lifespan = host, port, lifespan

        class FakeServer:
            def __init__(self, config):
                self.config, self.should_exit, self.stopped = config, False, False
                servers.append(self)

            async def serve(self):
                while not self.should_exit:
                    await asyncio.sleep(0.001)
                self.stopped = True

        async def go():
            s = Settings(token="t", adapter_retry_s=0.01)
            task = asyncio.create_task(m.serve_api(app, s, discover))
            while len(calls) < ticks:
                await asyncio.sleep(0.005)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        with mock.patch.object(m.uvicorn, "Server", FakeServer), \
                mock.patch.object(m.uvicorn, "Config", FakeConfig):
            asyncio.run(go())
        return servers

    def test_api_waits_for_the_wsl_adapter_then_binds_it(self):
        servers = self.run_serve_api([None, None, "10.9.8.7"], object(), ticks=5)
        self.assertEqual([(x.config.host, x.config.port) for x in servers],
                         [("10.9.8.7", 8766)])

    def test_ip_change_rebinds_gracefully_and_publishes(self):
        tmp, app = make()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(app.state.store.close)
        servers = self.run_serve_api(["10.0.0.1", "10.0.0.1", "10.0.0.2"], app, ticks=6)
        self.assertEqual([x.config.host for x in servers], ["10.0.0.1", "10.0.0.2"])
        self.assertTrue(servers[0].stopped)  # old listener shut down via should_exit
        self.assertTrue(servers[1].stopped)  # and the last one on cancellation
        msgs = [e["data"]["message"] for e in app.state.store.list_events(kind="hub.api-listener")]
        self.assertEqual(len(msgs), 2)
        self.assertIn("10.0.0.2", msgs[1])
        self.assertIn("was 10.0.0.1", msgs[1])
        alerts = [e for e in app.state.bus.recent() if e["kind"] == "alert"]
        self.assertEqual(len(alerts), 2)

    def test_adapter_disappearing_stops_the_listener(self):
        servers = self.run_serve_api(["10.0.0.1", None], object(), ticks=4)
        self.assertEqual(len(servers), 1)
        self.assertTrue(servers[0].stopped)


class HostAllowlistTests(unittest.TestCase):
    def client(self, **kw):
        tmp, app = make(trusted_ui_hosts=("testclient",), ui_allowed_hosts=(), **kw)
        self.addCleanup(tmp.cleanup)
        self.addCleanup(app.state.store.close)
        return app, TestClient(app, follow_redirects=False)

    def test_only_loopback_names_and_tailnet_name(self):
        import json
        ts = lambda a: json.dumps({"Self": {"UserID": 7, "DNSName": "Box.tail1234.ts.net."},
                                   "User": {"7": {"LoginName": "owner@example.com"}}})
        app, c = self.client(tailscale=ts)
        self.assertIn("box.tail1234.ts.net", app.state.allowed_hosts)
        for host, code in (("127.0.0.1:8765", 200), ("localhost:8765", 200),
                           ("box.tail1234.ts.net", 200), ("evil.example", 403),
                           ("127.0.0.1:9999", 403), ("localhost", 403)):
            r = c.get("/inbox", headers={"Host": host})
            self.assertEqual(r.status_code, code, host)

    def test_rebound_host_cannot_use_ui_identity_on_api(self):
        app, c = self.client()
        r = c.get("/api/v1/tasks", headers={"Host": "evil.example"})
        self.assertEqual(r.status_code, 401)
        r = c.get("/api/v1/tasks", headers={"Host": "127.0.0.1:8765"})
        self.assertEqual(r.status_code, 200)
        # a bearer caller is unaffected by Host
        r = c.get("/api/v1/tasks", headers={"Host": "evil.example", **AUTH})
        self.assertEqual(r.status_code, 200)


class TokenFileTests(unittest.TestCase):
    def test_created_owner_only_via_icacls_argv(self):
        from hub import auth
        calls = []
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            s = Settings(token_path=Path(d) / "sub" / "hub-token", icacls=lambda a: calls.append(a) or "")
            tok = auth.load_token(s)
            self.assertEqual(s.token_path.read_text().strip(), tok)
            self.assertEqual(auth.load_token(s), tok)
        user = auth.owner_name()
        self.assertEqual(calls[0][1:], ["/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F"])
        self.assertEqual(calls[1][1:], ["/inheritance:r", "/grant:r", f"{user}:F"])
        self.assertTrue(all(isinstance(x, str) for c in calls for x in c))

    def test_startup_warns_when_acl_is_broad(self):
        from hub import auth
        out = ("C:\\drover\\hub-token BUILTIN\\Users:(R)\n"
               "                    Everyone:(R)\n"
               "                    ME\\owner:(F)\n")
        self.assertEqual(len(auth.acl_warnings(lambda a: out, "x")), 2)
        self.assertEqual(auth.acl_warnings(lambda a: "x ME\\owner:(F)\n", "x"), [])
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            p = Path(d) / "hub-token"
            p.write_text("abc\n")
            with self.assertLogs("hub", "WARNING") as cm:
                auth.load_token(Settings(token_path=p, icacls=lambda a: out))
        self.assertTrue(any("broad group" in m for m in cm.output))


class SettingsGuardTests(HubCase):
    def put(self, body, **kw):
        return self.c.put("/api/v1/settings", json=body, headers=AUTH, **kw)

    def test_token_cannot_change_security_keys(self):
        for k in ("wsl_distro", "wsl_user", "ui_allowed_logins", "watchdog_x", "dispatch_enabled", "gate_cmd_acme-tools",
                  "gate_cmd_anything"):
            self.assertEqual(self.put({k: "x"}).status_code, 403, k)
        self.assertEqual(self.put({"note": "fine"}).status_code, 200)

    def test_owner_can_but_values_are_validated(self):
        tmp, app = make(trusted_ui_hosts=("testclient",))
        self.addCleanup(tmp.cleanup)
        self.addCleanup(app.state.store.close)
        c = TestClient(app)
        ok = c.put("/api/v1/settings", json={"wsl_distro": "Ubuntu-24.04"},
                   headers={**SAME, "Host": "testserver"})
        self.assertEqual(ok.status_code, 200)
        for bad in ("--shutdown", "-d", "a b", "x;y", ""):
            r = c.put("/api/v1/settings", json={"wsl_user": bad}, headers=SAME)
            self.assertEqual(r.status_code, 400, bad)

    def test_director_execution_settings_require_owner_and_csrf(self):
        body = {'director_execution_enabled': 'true', 'director_execution': '{}'}
        original = self.get('/settings').json()
        for key, value in body.items():
            self.assertEqual(self.put({key: value}).status_code, 403, key)
        self.assertEqual(self.put(body).status_code, 403)
        self.assertEqual(self.get('/settings').json(), original)
        tmp, app = make(trusted_ui_hosts=('testclient',))
        self.addCleanup(tmp.cleanup)
        self.addCleanup(app.state.store.close)
        client = TestClient(app)
        self.assertEqual(client.put('/api/v1/settings', json=body).status_code, 403)
        response = client.put('/api/v1/settings', json=body, headers=SAME)
        self.assertEqual(response.status_code, 200)
        saved = client.get('/api/v1/settings', headers=AUTH).json()
        self.assertEqual({key: saved[key] for key in body}, body)


class BusBoundTests(unittest.TestCase):
    def test_slow_subscriber_is_bounded_and_told_to_resync(self):
        import asyncio

        from hub import events

        async def go():
            bus = events.Bus()
            q = bus.subscribe()
            for i in range(events.QUEUE_MAX * 3):
                bus.publish("x", {"i": i})
            await asyncio.sleep(0.05)
            items = []
            while not q.empty():
                items.append(q.get_nowait())
            return items

        items = asyncio.run(go())
        self.assertLessEqual(len(items), events.QUEUE_MAX)
        self.assertIn("resync", [e["kind"] for e in items])
        self.assertEqual(items[-1]["data"], {"i": events.QUEUE_MAX * 3 - 1})


class RealListenerTests(unittest.TestCase):
    """serve_api with real uvicorn on loopback ports."""

    def free_port(self):
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def drive(self, app, settings, ips, seconds):
        import asyncio

        from hub import __main__ as m

        seq = iter(ips)
        last = ips[-1]

        async def go():
            task = asyncio.create_task(m.serve_api(app, settings, lambda g: next(seq, last)))
            await asyncio.sleep(seconds)
            alive = not task.done()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return alive

        return asyncio.run(go())

    def test_rebind_leaves_watchdog_and_store_running(self):
        import urllib.request
        tmp, app = make(run_watchdog=True)
        self.addCleanup(tmp.cleanup)
        port = self.free_port()
        s = Settings(token="tok", api_port=port, adapter_retry_s=0.3, run_watchdog=True,
                     watchdog_interval_s=3600)
        app.state.start_background()
        self.addCleanup(app.state.shutdown)
        dog = app.state.watchdog
        self.assertTrue(dog._thread.is_alive())
        alive = self.drive(app, s, ["127.0.0.1", "127.0.0.2"], 2.5)
        self.assertTrue(alive)
        self.assertFalse(dog._stop.is_set())
        self.assertTrue(dog._thread.is_alive())
        app.state.store.list_tasks()  # store still open
        msgs = [e["data"]["message"] for e in app.state.store.list_events(kind="hub.api-listener")]
        self.assertTrue(any("127.0.0.2" in m for m in msgs), msgs)

    def test_occupied_port_never_kills_the_hub_and_is_retried(self):
        import socket
        tmp, app = make()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(app.state.store.close)
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        s = Settings(token="tok", api_port=port, adapter_retry_s=0.2, run_watchdog=False)
        try:
            alive = self.drive(app, s, ["127.0.0.1"], 1.5)
        finally:
            blocker.close()
        self.assertTrue(alive)  # SystemExit(3) from uvicorn was contained
        msgs = [e["data"]["message"] for e in app.state.store.list_events(kind="hub.api-listener")]
        self.assertGreaterEqual(sum("failed" in m for m in msgs), 2, msgs)  # retried
        self.assertTrue(any(e["kind"] == "alert" for e in app.state.bus.recent()))

    def test_listener_recovers_when_port_frees_up(self):
        import socket
        import urllib.request
        tmp, app = make()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(app.state.store.close)
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        s = Settings(token="tok", api_port=port, adapter_retry_s=0.2, run_watchdog=False)
        import asyncio

        from hub import __main__ as m

        async def go():
            task = asyncio.create_task(m.serve_api(app, s, lambda g: "127.0.0.1"))
            await asyncio.sleep(0.8)
            blocker.close()
            await asyncio.sleep(1.0)
            code = await asyncio.to_thread(
                lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/health").status)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return code

        self.assertEqual(asyncio.run(go()), 200)


if __name__ == "__main__":
    unittest.main()
