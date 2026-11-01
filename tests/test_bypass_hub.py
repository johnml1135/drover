"""The owner's Bypass through the hub: API routes and the task page."""
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
AUTH = {"Authorization": "Bearer tok"}


class BypassHubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        settings = Settings(token="tok", run_watchdog=False, trusted_ui_hosts=("testclient",),
                            tailscale=lambda a: "", ui_allowed_hosts=("testserver",))
        self.app = create_app(Path(self.tmp.name) / "l.db", settings)
        self.addCleanup(self.app.state.store.close)
        self.store = self.app.state.store
        self.owner = TestClient(self.app, follow_redirects=False)
        self.token = TestClient(self.app, base_url='http://testserver:8766', follow_redirects=False)

    def task(self):
        return self.store.approve(self.store.create_task("A", scope_files=["fixture/a"])["id"])["id"]

    def status(self, tid):
        return self.store.get_task(tid)["status"]

    def api(self, tid, verb, **kw):
        return self.owner.post(f"/api/v1/tasks/{tid}/{verb}", headers=SAME, **kw)

    def test_the_api_token_cannot_bypass(self):
        tid = self.task()
        for verb in ("yank", "abandon", "handback"):
            r = self.token.post(f"/api/v1/tasks/{tid}/{verb}", json={}, headers=AUTH)
            self.assertEqual(r.status_code, 403, verb)
        self.assertEqual(self.status(tid), "ready")

    def test_the_api_token_cannot_forge_a_bypass_record(self):
        tid = self.task()
        for kind in ("bypass", "handback"):
            r = self.token.post("/api/v1/events", json={"kind": kind, "task_id": tid, "data": {}}, headers=AUTH)
            self.assertEqual(r.status_code, 403, kind)

    def test_owner_yanks_and_abandons_through_the_api(self):
        tid = self.task()
        r = self.api(tid, "yank", json={"reason": "by hand"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "yanked")
        self.assertEqual(self.api(tid, "yank").status_code, 200)
        self.assertEqual(self.api(tid, "abandon").json()["status"], "cancelled")
        self.assertEqual(self.api(tid, "yank").status_code, 400)

    def test_owner_hands_back_through_the_api(self):
        tid = self.task()
        self.api(tid, "yank")
        r = self.api(tid, "handback")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "draft")
        self.assertEqual(self.status(tid), "cancelled")

    def test_the_generic_status_route_yanks_with_the_same_record_and_owner_rule(self):
        tid = self.task()
        r = self.token.post(f"/api/v1/tasks/{tid}/status", json={"status": "yanked"}, headers=AUTH)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.status(tid), "ready")
        r = self.owner.post(f"/api/v1/tasks/{tid}/status", json={"status": "yanked", "reason": "by hand"}, headers=SAME)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.status(tid), "yanked")
        events = self.store.list_events(task_id=tid, kind="bypass")
        self.assertEqual([e["data"]["reason"] for e in events], ["by hand"])

    def test_task_page_offers_what_the_state_allows(self):
        tid = self.task()
        page = self.owner.get(f"/tasks/{tid}").text
        self.assertIn(f'action="/tasks/{tid}/yank"', page)
        self.assertNotIn(f'action="/tasks/{tid}/abandon"', page)
        self.assertNotIn(f'action="/tasks/{tid}/handback"', page)
        self.store.yank_task(tid, "by hand")
        page = self.owner.get(f"/tasks/{tid}").text
        self.assertNotIn(f'action="/tasks/{tid}/yank"', page)
        self.assertIn(f'action="/tasks/{tid}/abandon"', page)
        self.assertIn(f'action="/tasks/{tid}/handback"', page)
        self.store.abandon_task(tid)
        page = self.owner.get(f"/tasks/{tid}").text
        self.assertNotIn(f'action="/tasks/{tid}/abandon"', page)

    def test_task_page_forms_carry_the_action(self):
        tid = self.task()
        r = self.owner.post(f"/tasks/{tid}/yank", data={"reason": "by hand"}, headers=SAME)
        self.assertEqual(r.status_code, 303, r.text)
        self.assertEqual(self.status(tid), "yanked")
        events = self.store.list_events(task_id=tid, kind="bypass")
        self.assertEqual(events[0]["data"]["reason"], "by hand")
        r = self.owner.post(f"/tasks/{tid}/handback", headers=SAME)
        self.assertEqual(r.status_code, 303, r.text)
        draft = r.headers["location"].rsplit("/", 1)[1]
        self.assertEqual(self.status(draft), "draft")

    def test_cross_site_task_page_forms_are_refused(self):
        tid = self.task()
        r = self.owner.post(f"/tasks/{tid}/yank", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.status(tid), "ready")

    def test_tree_dims_a_yanked_task(self):
        tid = self.task()
        self.store.yank_task(tid, "")
        self.assertIn('tree-task tree-dim', self.owner.get("/tree").text)


if __name__ == '__main__':
    unittest.main()
