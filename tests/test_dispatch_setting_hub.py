"""Who dispatches, through the hub: the API routes, the plan page and the health page."""
import unittest

try:
    from fastapi.testclient import TestClient
except ImportError as exc:  # hub deps are third-party; skip when absent
    raise unittest.SkipTest(f"hub deps missing: {exc}")

from tests.test_hub_api import HubCase, SAME, make


class DispatchSettingApiTests(HubCase):
    def setUp(self):
        self.tmp, self.app = make(trusted_ui_hosts=("testclient",))     # loopback UI requests act as the owner
        self.c = TestClient(self.app)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.app.state.store.close)

    def owner_post(self, path, body):
        return self.c.post("/api/v1" + path, json=body, headers=SAME)

    def test_hub_claim_route_obeys_the_setting(self):
        eid = self.post("/epics", {"repo": "default", "title": "E"}).json()["id"]
        self.assertEqual(self.post(f"/epics/{eid}/dispatch", {"mode": "director"}).status_code, 403)
        self.assertEqual(self.owner_post(f"/epics/{eid}/dispatch", {"mode": "director"}).json()["dispatch"], "director")
        tid = self.ready_task("A", epic_id=eid)
        self.assertIsNone(self.post("/tasks/claim", {}).json()["task"])
        self.assertEqual(self.post(f"/tasks/{tid}/pick").status_code, 200)    # the API token is a director
        self.assertEqual(self.post("/tasks/claim", {}).json()["task"]["id"], tid)

    def test_default_is_owner_only_and_readable(self):
        self.assertEqual(self.get("/dispatch/default").json(), {"mode": "drover"})
        self.assertEqual(self.post("/dispatch/default", {"mode": "director"}).status_code, 403)
        self.assertEqual(self.owner_post("/dispatch/default", {"mode": "director"}).json(), {"mode": "director"})
        self.assertEqual(self.get("/dispatch/default").json(), {"mode": "director"})
        self.assertEqual(self.owner_post("/dispatch/default", {"mode": "x"}).status_code, 422)

    def test_pick_of_a_task_that_is_not_ready_is_refused(self):
        tid = self.task("D")
        self.assertEqual(self.post(f"/tasks/{tid}/pick").status_code, 400)

    def form(self, path, **data):
        return self.c.post(path, data=data, headers=SAME, follow_redirects=False)

    def test_the_plan_page_switches_an_epic(self):
        store = self.app.state.store
        eid = store.create_epic("default", "E", "", "owner")["id"]
        self.assertIn(f'action="/epics/{eid}/dispatch"', self.c.get("/plan").text)
        self.assertEqual(self.form(f"/epics/{eid}/dispatch", mode="director").status_code, 303)
        mode = lambda: next(e["dispatch"] for e in store.list_epics() if e["id"] == eid)
        self.assertEqual(mode(), "director")
        self.assertEqual(self.form(f"/epics/{eid}/dispatch", mode="anyone").status_code, 422)
        self.assertEqual(mode(), "director")

    def test_the_task_page_picks_a_task(self):
        tid = self.ready_task("A")
        self.assertEqual(self.form(f"/tasks/{tid}/pick").status_code, 303)
        self.assertEqual(self.app.state.store.get_task(tid)["picked_by"], "owner")

    def test_the_health_page_sets_the_default(self):
        store = self.app.state.store
        self.assertIn('action="/dispatch/default"', self.c.get("/health").text)
        self.assertEqual(self.form("/dispatch/default", mode="director").status_code, 303)
        self.assertEqual(store.get_default_dispatch(), "director")
        self.assertEqual(self.form("/dispatch/default", mode="anyone").status_code, 422)
        self.assertEqual(store.get_default_dispatch(), "director")
        cross = self.c.post("/dispatch/default", data={"mode": "drover"}, headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(cross.status_code, 403)
        self.assertEqual(store.get_default_dispatch(), "director")


if __name__ == '__main__':
    unittest.main()
