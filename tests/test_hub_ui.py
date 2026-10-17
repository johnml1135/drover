"""Web router wired to the real store, and CSRF on UI mutations."""
import tempfile
import unittest
from pathlib import Path

try:
    from fastapi.testclient import TestClient
    from hub.app import create_app
    from hub.settings import Settings
except ImportError as exc:  # hub deps are third-party; skip when absent
    raise unittest.SkipTest(f"hub deps missing: {exc}")

SAME = {"Sec-Fetch-Site": "same-origin"}


class UiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        s = Settings(token="tok", run_watchdog=False, trusted_ui_hosts=("testclient",),
                     tailscale=lambda a: "", ui_allowed_hosts=("testserver",))
        self.app = create_app(Path(self.tmp.name) / "l.db", s)
        self.addCleanup(self.app.state.store.close)
        self.st = self.app.state.store
        self.c = TestClient(self.app, follow_redirects=False)

    def epic(self, title):
        return self.st.create_epic("p", title)["id"]

    def test_all_tabs_render_with_data(self):
        e1, e2 = self.epic("Epic One"), self.epic("Epic Two")
        t = self.st.create_task("Alpha task", epic_id=e1)
        n = self.st.create_task("Beta", epic_id=e2)["id"]
        self.st.approve(n)
        self.st.set_status(n, "needs-owner", "dispatcher", "licence")
        g = self.st.create_task("Gamma")["id"]
        self.st.set_status(g, "blocked", "dispatcher", "gate red")
        self.st.ask_question("task", t["id"], "Pick?", options=[{"label": "x"}, {"label": "y"}])
        self.st.put_heartbeat({"resources": {"RAM": {"verdict": "ok", "value": "9GB"}},
                               "allowance": {"claude": {"used_pct": 3}}})
        r = self.c.get("/inbox")
        self.assertEqual(r.status_code, 200)
        for text in ("Alpha task", "Beta", "Gamma", "Pick?", "licence", "gate red"):
            self.assertIn(text, r.text)
        for path in ("/plan", "/runs", "/landing", "/health"):
            self.assertEqual(self.c.get(path).status_code, 200, path)
        self.assertIn("RAM", self.c.get("/health").text)
        plan = self.c.get("/plan").text
        self.assertIn("Epic One", plan)
        self.assertIn(t["id"], plan)

    def test_mutations_need_same_origin(self):
        t = self.st.create_task("Imp")["id"]
        r = self.c.post("/imports/approve", data={"id": t})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.st.get_task(t)["status"], "proposed")
        r = self.c.post("/imports/approve", data={"id": t}, headers=SAME)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.st.get_task(t)["status"], "ready")

    def test_origin_matching_host_accepted_cross_origin_refused(self):
        t = self.st.create_task("Imp")["id"]
        bad = self.c.post(f"/items/import/{t}/approve", headers={"Origin": "http://evil.example"})
        self.assertEqual(bad.status_code, 403)
        cross = self.c.post(f"/items/import/{t}/approve",
                            headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(cross.status_code, 403)
        self.assertEqual(self.st.get_task(t)["status"], "proposed")
        ok = self.c.post(f"/items/import/{t}/reject", headers={"Origin": "http://testserver"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.st.get_task(t)["status"], "cancelled")

    def test_api_ui_auth_also_checks_csrf_but_bearer_does_not(self):
        r = self.c.post("/api/v1/tasks", json={"title": "x"})
        self.assertEqual(r.status_code, 403)
        r = self.c.post("/api/v1/tasks", json={"title": "x"}, headers=SAME)
        self.assertEqual(r.status_code, 201)
        r = self.c.post("/api/v1/tasks", json={"title": "y"},
                        headers={"Authorization": "Bearer tok"})
        self.assertEqual(r.status_code, 201)

    def test_answer_comment_reorder(self):
        e1, e2 = self.epic("E1"), self.epic("E2")
        a = self.st.create_task("A", epic_id=e1)["id"]
        a2 = self.st.create_task("A2", epic_id=e1)["id"]
        q = self.st.ask_question("task", a, "Pick?", options=[{"label": "x"}, {"label": "y"}])["id"]
        self.c.post(f"/questions/{q}/answer", data={"option": "y"}, headers=SAME)
        self.assertEqual(self.st.list_questions()[0]["answer_choice"], "y")
        self.c.post(f"/items/task/{a}/comments", data={"body": "note", "urgent": "on"},
                    headers=SAME)
        self.assertIn("note", self.c.get(f"/items/task/{a}/comments").text)
        self.assertIn("note", self.c.get(f"/items/question/{q}/comments").text)
        self.c.post("/plan/reorder", data={"kind": "task", "id": a2, "before": a}, headers=SAME)
        self.assertEqual([t["id"] for t in self.st.list_tasks(epic_id=e1)], [a2, a])
        self.c.post("/plan/reorder", data={"kind": "epic", "id": e2, "before": e1}, headers=SAME)
        self.assertEqual([e["id"] for e in self.st.list_epics()], [e2, e1])

    def test_owner_approve_in_ui_sets_approval(self):
        t = self.st.create_task("Imp")["id"]
        self.c.post(f"/items/import/{t}/approve", headers=SAME)
        self.assertIsNotNone(self.st.get_task(t)["approved_at"])


if __name__ == "__main__":
    unittest.main()
