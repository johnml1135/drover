"""Hub website tests: TestClient with a fake store injected through dependency override."""
import unittest

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from hub import web
except ImportError as exc:  # hub deps are third-party; skip when absent
    raise unittest.SkipTest(f"hub web deps missing: {exc}")


class FakeStore:
    def __init__(self):
        self.calls = []
        self.epics = [
            {"id": "e1", "title": "Epic One", "tasks": [
                {"id": "t1", "title": "Task A", "state": "ready", "repo": "acme-lib", "model": "haiku"},
                {"id": "t2", "title": "Task B", "state": "gated", "repo": "acme-lib", "model": "opus"}]},
            {"id": "e2", "title": "Epic Two", "tasks": [
                {"id": "t3", "title": "Task C", "state": "ready", "repo": "acme-app", "model": "haiku"}]},
        ]

    def last_updated(self): return "12:00:00"
    def inbox_questions(self):
        return [{"id": "q1", "title": "Pick a backend", "body": "Which one?", "options": [
            {"id": "a", "label": "Alpha", "recommended": True}, {"id": "b", "label": "Beta"}]}]
    def needs_owner_items(self): return [{"id": "n1", "title": "Need sign-off", "reason": "licence"}]
    def import_proposals(self): return [{"id": "i1", "title": "Imported One", "repo": "acme-lib"}, {"id": "i2", "title": "Imported Two"}]
    def tuner_proposals(self): return [{"id": "p1", "title": "Tune tau", "summary": "raise to 0.8"}]
    def garden_batches(self): return [{"id": "g1", "title": "Delete 2 branches", "items": ["b1", "b2"]}]
    def failed_blocked_items(self): return [{"id": "t9", "title": "Broken", "state": "failed", "reason": "gate red"}]
    def plan_epics(self): return self.epics
    def runs(self):
        return [{"id": "r1", "task": "Task A", "model": "haiku-5.5", "effort": "high", "backend": "claude",
                 "tokens": 12345, "allowance_used": "0.4%", "outcome": "landed", "wall_time": "3m"}]
    def decider_accuracy(self): return [{"question": "retry?", "shadow": "91%", "live": "88%", "n": 40}]
    def landing(self):
        return {"queue": [{"id": "t1", "title": "Task A", "state": "reviewed"}],
                "batch": {"status": "gate running", "tasks": [{"title": "Task A"}]},
                "gate": {"status": "green", "median": "8m", "red_rate": "5%"}}
    def health(self):
        return {"resources": [{"name": "RAM", "verdict": "ok", "value": "9 GB free"}, {"name": "C:", "verdict": "hold", "value": "20 GB"}],
                "allowance": [{"plan": "Claude", "window": "5h", "used_pct": 40, "resets_in": "2h 10m", "reserve": "10%"}],
                "heartbeat_age": "12s", "watchdog": [{"at": "11:00", "action": "restarted dispatcher"}]}
    def comments(self, kind, iid): return [{"author": "owner", "at": "t", "body": "earlier note", "urgent": False, "replan": False}]
    def answer_question(self, qid, option=None, text=None): self.calls.append(("answer", qid, option, text))
    def decide_item(self, kind, iid, decision): self.calls.append(("decide", kind, iid, decision))
    def approve_imports(self, ids): self.calls.append(("approve_imports", ids))
    def reorder(self, kind, iid, before=None, after=None): self.calls.append(("reorder", kind, iid, before, after))
    def add_comment(self, kind, iid, body, urgent=False, replan=False): self.calls.append(("comment", kind, iid, body, urgent, replan))


class WebTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        app = FastAPI()
        app.include_router(web.router)
        app.dependency_overrides[web.get_store] = lambda: self.store
        self.c = TestClient(app, follow_redirects=False)

    def test_root_redirects_to_inbox(self):
        self.assertEqual(self.c.get("/").headers["location"], "/inbox")

    def test_viewport_and_no_external_urls(self):
        for tab in ("inbox", "plan", "runs", "landing", "health"):
            html = self.c.get(f"/{tab}").text
            self.assertIn('<meta name="viewport" content="width=device-width, initial-scale=1">', html)
            self.assertNotIn("http://", html.replace("http://www.w3.org", ""))
            self.assertNotIn("https://", html)
        css = self.c.get("/static/app.css").text + self.c.get("/static/app.js").text
        self.assertNotIn("http", css)

    def test_inbox_renders_all_sections(self):
        html = self.c.get("/inbox").text
        for s in ("Pick a backend", "Recommended", "Need sign-off", "Imported One", "Approve selected",
                  "Tune tau", "Delete 2 branches", "gate red", 'name="text"'):
            self.assertIn(s, html)

    def test_partial_for_htmx(self):
        r = self.c.get("/inbox", headers={"HX-Request": "true"})
        self.assertNotIn("<html", r.text)
        self.assertIn('id="updated"', r.text)

    def test_plan_renders_with_handles_and_move_buttons(self):
        html = self.c.get("/plan").text
        for s in ("Epic One", "Task C", "Move up", "Move down", 'class="grip', "Sortable"):
            self.assertIn(s, html)

    def test_plan_group_and_filter(self):
        html = self.c.get("/plan?group=repo").text
        self.assertIn("acme-app", html)
        self.assertNotIn('class="grip', html)
        html = self.c.get("/plan?group=epic&state=gated").text
        self.assertIn("Task B", html)
        self.assertNotIn("Task A", html)

    def test_runs_landing_health(self):
        self.assertIn("haiku-5.5", self.c.get("/runs").text)
        self.assertIn("91%", self.c.get("/runs").text)
        self.assertIn("gate running", self.c.get("/landing").text)
        h = self.c.get("/health").text
        for s in ("9 GB free", "2h 10m", "12s", "restarted dispatcher", "Protected director reserve"):
            self.assertIn(s, h)

    def test_answer_question(self):
        r = self.c.post("/questions/q1/answer", content="option=a&text=", headers={"content-type": "application/x-www-form-urlencoded"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.store.calls, [("answer", "q1", "a", None)])
        r = self.c.post("/questions/q1/answer", data={"text": "my own"})
        self.assertEqual(self.store.calls[-1], ("answer", "q1", None, "my own"))
        self.assertEqual(self.c.post("/questions/q1/answer", data={"text": " "}).status_code, 422)

    def test_reorder_before_after_and_move(self):
        self.assertEqual(self.c.post("/plan/reorder", data={"kind": "task", "id": "t2", "after": "t1"}).status_code, 200)
        self.assertEqual(self.store.calls[-1], ("reorder", "task", "t2", None, "t1"))
        self.c.post("/plan/reorder", data={"kind": "epic", "id": "e2", "move": "up"})
        self.assertEqual(self.store.calls[-1], ("reorder", "epic", "e2", "e1", None))
        self.c.post("/plan/reorder", data={"kind": "task", "id": "t1", "move": "down"})
        self.assertEqual(self.store.calls[-1], ("reorder", "task", "t1", None, "t2"))
        n = len(self.store.calls)
        self.c.post("/plan/reorder", data={"kind": "epic", "id": "e1", "move": "up"})  # already first
        self.assertEqual(len(self.store.calls), n)
        self.assertEqual(self.c.post("/plan/reorder", data={"kind": "task", "id": "t1"}).status_code, 422)

    def test_approve_reject_and_bulk(self):
        self.c.post("/items/import/i1/approve")
        self.c.post("/items/tuner/p1/reject")
        self.assertEqual(self.store.calls, [("decide", "import", "i1", "approve"), ("decide", "tuner", "p1", "reject")])
        self.assertEqual(self.c.post("/items/import/i1/explode").status_code, 404)
        self.c.post("/imports/approve", content="id=i1&id=i2", headers={"content-type": "application/x-www-form-urlencoded"})
        self.assertEqual(self.store.calls[-1], ("approve_imports", ["i1", "i2"]))
        self.assertEqual(self.c.post("/imports/approve").status_code, 422)

    def test_comment_sheet_and_post(self):
        self.assertIn("earlier note", self.c.get("/items/task/t1/comments").text)
        r = self.c.post("/items/task/t1/comments", data={"body": "do it now", "urgent": "on", "replan": "on"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.store.calls[-1], ("comment", "task", "t1", "do it now", True, True))
        self.c.post("/items/task/t1/comments", data={"body": "plain"})
        self.assertEqual(self.store.calls[-1], ("comment", "task", "t1", "plain", False, False))
        self.assertEqual(self.c.post("/items/task/t1/comments", data={"body": "  "}).status_code, 422)

    def test_static_vendor_and_traversal(self):
        self.assertEqual(self.c.get("/static/vendor/htmx.min.js").status_code, 200)
        self.assertEqual(self.c.get("/static/vendor/Sortable.min.js").status_code, 200)
        self.assertEqual(self.c.get("/static/../web.py").status_code, 404)

    def test_xss_escaped(self):
        evil = "<script>alert(1)</script>"
        self.store.epics[0]["tasks"][0]["title"] = evil
        self.store.epics[0]["tasks"][0]["id"] = 'a"b\\c\''
        self.store.comments = lambda kind, iid: [{"author": "x", "at": "t", "body": evil}]
        for url in ("/plan", "/items/task/t1/comments"):
            html = self.c.get(url).text
            self.assertNotIn(evil, html)
            self.assertIn("&lt;script&gt;", html)
        html = self.c.get("/plan").text
        self.assertNotIn('"id":"a"b', html)

    def test_filter_chip_values_urlencoded(self):
        self.store.epics[0]["tasks"][0]["repo"] = "a&b c#"
        self.assertIn("repo=a%26b%20c%23", self.c.get("/plan").text)

    def test_recommended_not_prechecked(self):
        html = self.c.get("/inbox").text
        self.assertNotIn("checked", html)

    def test_both_option_and_text_sent(self):
        self.c.post("/questions/q1/answer", data={"option": "b", "text": "because"})
        self.assertEqual(self.store.calls[-1], ("answer", "q1", "b", "because"))

    def test_kind_whitelist(self):
        self.assertEqual(self.c.get("/items/evil/t1/comments").status_code, 400)
        self.assertEqual(self.c.post("/items/evil/t1/comments", data={"body": "x"}).status_code, 400)
        self.assertEqual(self.c.post("/items/evil/t1/approve").status_code, 400)
        for k in ("owner", "import", "tuner", "garden", "task", "epic", "question", "run"):
            self.assertEqual(self.c.get(f"/items/{k}/x/comments").status_code, 200)
        self.assertEqual(self.store.calls, [])

    def test_reorder_rejects_non_siblings(self):
        self.assertEqual(self.c.post("/plan/reorder", data={"kind": "task", "id": "t1", "after": "t3"}).status_code, 400)
        self.assertEqual(self.c.post("/plan/reorder", data={"kind": "task", "id": "t1", "after": "t1"}).status_code, 400)
        self.assertEqual(self.c.post("/plan/reorder", data={"kind": "bad", "id": "t1", "after": "t2"}).status_code, 422)
        self.assertEqual(self.store.calls, [])

    def test_422_messages_are_json_detail(self):
        r = self.c.post("/questions/q1/answer", data={})
        self.assertEqual(r.status_code, 422)
        self.assertIn("choose an option", r.json()["detail"])
        self.assertIn('class="err"', self.c.get("/inbox").text)


if __name__ == "__main__":
    unittest.main()
