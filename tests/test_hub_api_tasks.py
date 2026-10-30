"""Hub API tests: auth, owner gate, tasks, claim race, import, SSE."""
import json
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


from tests.test_hub_api import AUTH, SAME, fake_tailscale, no_tailscale, make, HubCase


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
            store.approve(store.create_task(f"t{i}", scope_files=[f"fixture-{i}"])["id"])
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
        created = app.state.store.create_task("X", scope_files=['fixture-106'])
        replay = app.state.bus.recent()[0]
        c = TestClient(app)
        with c.stream("GET", "/events?limit=1", headers={"Last-Event-ID": "0"}) as r:
            self.assertEqual(r.headers["content-type"].split(";")[0], "text/event-stream")
            body = "".join(r.iter_text())
        self.assertIn("event: ledger", body)
        fields = dict(line.split(":", 1) for line in body.splitlines() if ":" in line)
        payload = json.loads(fields["data"].lstrip())
        self.assertEqual(payload, {"kind": "task.created", **replay["data"]})
        self.assertEqual(payload["id"], created["id"])
        self.assertEqual(int(fields["id"]), replay["id"])

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
