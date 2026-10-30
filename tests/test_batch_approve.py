"""Batch approval across owner API, forms and CLI."""
import unittest
from unittest.mock import patch

from tests.test_hub_api import AUTH, SAME, make
from fastapi.testclient import TestClient


class BatchApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.app = make(trusted_ui_hosts=("testclient",))
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.app.state.store.close)
        self.c = TestClient(self.app)
        self.s = self.app.state.store

    def task(self, title="Task", **fields):
        return self.s.create_task(title, **fields)["id"]

    def approve(self, ids, **fields):
        return self.c.post("/api/v1/tasks/approve", json={"ids": ids, **fields}, headers=SAME)

    def test_mixed_states_missing_duplicate_ids_and_audit(self):
        proposed = self.task()
        draft = self.task(status="draft")
        ready = self.task()
        self.s.approve(ready)
        blocked = self.task()
        self.s.set_status(blocked, "blocked", "owner", "hold")
        start = len(self.s.list_events(limit=10000))
        ids = [proposed, draft, ready, blocked, "T-missing", proposed]
        response = self.approve(ids, reason="Reviewed batch")
        self.assertEqual(response.status_code, 200, response.text)
        results = response.json()["results"]
        self.assertEqual([r["id"] for r in results], ids[:-1])
        self.assertEqual([r["result"] for r in results], ["approved"] + ["skipped"] * 4)
        self.assertTrue(all(r["reason"] for r in results))
        self.assertEqual(self.s.get_task(proposed)["status"], "ready")
        self.assertEqual(self.s.get_task(draft)["status"], "draft")
        events = self.s.list_events(limit=10000)[start:]
        self.assertEqual(len(events), 6)
        self.assertEqual(events[0]["data"]["reason"], "Reviewed batch")
        self.assertEqual(events[-1]["kind"], "tasks-approved")
        self.assertTrue(all(e["actor"] == "owner" for e in events))

    def test_repeated_approval_is_idempotent(self):
        tid = self.task()
        self.approve([tid])
        original = self.s.get_task(tid)
        count = len(self.s.list_events(task_id=tid, kind="status"))
        self.assertEqual(self.approve([tid]).json()["results"][0]["result"], "skipped")
        self.assertEqual(self.s.get_task(tid)["approved_at"], original["approved_at"])
        self.assertEqual(len(self.s.list_events(task_id=tid, kind="status")), count)

    def test_owner_only_and_csrf(self):
        tid = self.task()
        path = "/api/v1/tasks/approve"
        for headers in (AUTH, {}, {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.c.post(path, json={"ids": [tid]}, headers=headers).status_code, 403)
        # API-port arrivals cannot acquire owner authority even from loopback.
        api = TestClient(self.app, base_url="http://testserver:8766")
        self.assertNotEqual(api.post(path, json={"ids": [tid]}, headers=SAME).status_code, 200)
        self.assertEqual(self.s.get_task(tid)["status"], "proposed")

    def test_explicit_policy_survives_reopen_and_existing_batch_paths(self):
        epic = self.s.create_epic("default", "Epic")["id"]
        tid = self.task(requires_explicit_approval=True, epic_id=epic)
        ordinary = self.task(epic_id=epic)
        self.s.close()
        self.assertTrue(self.s.get_task(tid)["requires_explicit_approval"])
        self.assertTrue(next(t for t in self.s.list_tasks() if t["id"] == tid)["requires_explicit_approval"])
        self.assertEqual(self.s.approve_epic(epic), [ordinary])
        self.assertEqual(self.approve([tid]).json()["results"][0]["result"], "skipped")
        self.c.post("/imports/approve", data={"id": tid}, headers=SAME)
        self.assertEqual(self.s.get_task(tid)["status"], "proposed")
        self.assertEqual(self.approve([tid], explicit=True).json()["results"][0]["result"], "approved")

    def test_single_task_semantics_unchanged(self):
        for status in ("draft", "proposed"):
            for explicit in (False, True):
                with self.subTest(status=status, explicit=explicit):
                    tid = self.task(status=status, requires_explicit_approval=explicit)
                    path = f"/api/v1/tasks/{tid}/approve"
                    response = self.c.post(path, headers=SAME)
                    self.assertEqual(response.status_code, 200, response.text)
                    task = self.s.get_task(tid)
                    self.assertEqual(task["status"], "ready")
                    self.assertIsNotNone(task["approved_at"])
                    events = self.s.list_events(task_id=tid, kind="status")
                    self.assertEqual(len(events), 1)
                    self.assertEqual(events[0]["data"], {"from": status, "to": "ready", "reason": "approved"})
                    self.assertEqual(self.c.post(path, headers=SAME).status_code, 400)
                    self.assertEqual(self.s.get_task(tid)["approved_at"], task["approved_at"])
        self.assertEqual(self.s.list_events(kind="tasks-approved"), [])

    def test_cli_dispatch_files_and_filters_use_owner_api_with_per_id_opt_in(self):
        import contextlib
        import io
        import json
        import tempfile
        from pathlib import Path
        from drover.__main__ import main

        epic = self.s.create_epic("default", "Epic")["id"]
        fields = dict(repo="r", epic_id=epic, kind="fix", priority="P1")
        ordinary = self.task("Ordinary", **fields)
        named = self.task("Named", requires_explicit_approval=True, **fields)
        filed = self.task("From file", requires_explicit_approval=True, **fields)
        excluded = self.task("Needs individual selection", requires_explicit_approval=True, **fields)
        wrong_priority = self.task("Wrong priority", repo="r", epic_id=epic, kind="fix", priority="P3")

        def request(path, body=None):
            response = (self.c.get("/api/v1" + path, headers=SAME) if body is None else
                        self.c.post("/api/v1" + path, json=body, headers=SAME))
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()

        with tempfile.TemporaryDirectory() as tmp:
            ids = Path(tmp) / "ids.json"
            ids.write_text(json.dumps([filed]), encoding="utf-8")
            for explicit in (False, True):
                with patch("drover.cli.OwnerClient") as client, contextlib.redirect_stdout(io.StringIO()) as output:
                    client.return_value.request.side_effect = request
                    argv = ["approve", named, "--from-file", str(ids), "--epic", epic,
                            "--filter", "repo=r,kind=fix", "--filter", "priority=P1", "--reason", "Reviewed"]
                    self.assertEqual(main(argv + (["--explicit"] if explicit else [])), 0)
                results = {r["id"]: r["result"] for r in json.loads(output.getvalue())["results"]}
                self.assertEqual(results[named], "approved" if explicit else "skipped")
                self.assertEqual(results[filed], "approved" if explicit else "skipped")
                self.assertEqual(results[excluded], "skipped")
                self.assertEqual(self.s.get_task(ordinary)["status"], "ready")
                for tid in (excluded, wrong_priority):
                    self.assertEqual(self.s.get_task(tid)["status"], "proposed")

    def test_cli_selector_cannot_opt_in_a_task_flagged_after_selection(self):
        from drover.cli import approve, parser
        tid = self.task()

        def request(path, body=None):
            if body is None:
                tasks = self.s.list_tasks(status="proposed")
                self.s.update_task(tid, requires_explicit_approval=True)
                return tasks
            self.assertFalse(body.get("explicit"))
            return self.approve(**body).json()

        from unittest.mock import Mock
        client = Mock()
        client.request.side_effect = request
        result = approve(parser().parse_args(["approve", "--filter", "repo=default"]), client)
        self.assertEqual(result["results"][0]["result"], "skipped")
        self.assertEqual(self.s.get_task(tid)["status"], "proposed")

    def test_worker_cannot_forge_owner_approval_events(self):
        for kind in ("tasks-approved", "task-approval-skipped", "task-approval-policy"):
            response = self.c.post("/api/v1/events", json={"kind": kind, "data": {}}, headers=AUTH)
            self.assertEqual(response.status_code, 403, response.text)

    def test_flag_create_patch_import_and_validation(self):
        response = self.c.post("/api/v1/tasks", json={"title": "Install", "requires_explicit_approval": True}, headers=AUTH)
        self.assertEqual(response.status_code, 201, response.text)
        tid = response.json()["id"]
        self.assertTrue(response.json()["requires_explicit_approval"])
        self.assertEqual(self.c.patch(f"/api/v1/tasks/{tid}", json={"requires_explicit_approval": False}, headers=AUTH).status_code, 403)
        self.assertEqual(self.c.patch(f"/api/v1/tasks/{tid}", json={"requires_explicit_approval": False}, headers=SAME).status_code, 200)
        result = self.s.import_tasks([{"title": "Publish", "requires_explicit_approval": True}], dry_run=False)
        self.assertTrue(self.s.get_task(result["created"][0])["requires_explicit_approval"])
        for body in ({"ids": []}, {"ids": [""]}, {"ids": [tid], "explicit": "false"}, {"ids": [tid], "extra": True}):
            self.assertEqual(self.c.post("/api/v1/tasks/approve", json=body, headers=SAME).status_code, 422)

    def test_failed_row_rolls_back_and_other_ids_continue(self):
        tid, other = self.task(), self.task()
        original = self.s.get_task(tid)
        from drover.ledger import ops
        real = ops.set_status

        def failing(conn, task_id, *args, **kwargs):
            if task_id == tid:
                conn.execute("UPDATE tasks SET replans_used=99 WHERE id=?", (tid,))
                raise ValueError("independent wait")
            return real(conn, task_id, *args, **kwargs)

        with patch("hub.store.ops.set_status", side_effect=failing):
            results = self.approve([tid, other]).json()["results"]
        self.assertEqual([r["result"] for r in results], ["skipped", "approved"])
        self.assertEqual(self.s.get_task(tid)["replans_used"], original["replans_used"])
        self.assertEqual(self.s.get_task(tid)["status"], "proposed")

    def test_concurrent_batches_approve_a_task_once(self):
        import threading
        tid = self.task()
        barrier = threading.Barrier(2)
        results, errors = [], []
        def worker():
            try:
                barrier.wait(timeout=5)
                results.append(self.s.approve_tasks([tid])["results"][0]["result"])
            except Exception as exc:
                errors.append(exc)
            finally:
                if getattr(self.s._local, "conn", None) is not None:
                    self.s.conn.close()
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), ["approved", "skipped"])
        self.assertEqual(len(self.s.list_events(task_id=tid, kind="status")), 1)

    def test_plan_composable_filters_and_separate_explicit_group(self):
        epic = self.s.create_epic("default", "Epic")["id"]
        ordinary = self.task("Ordinary", repo="r", epic_id=epic, kind="fix", priority="P1")
        flagged = self.task("Publish", repo="r", epic_id=epic, kind="fix", priority="P1", requires_explicit_approval=True)
        self.task("Wrong priority", repo="r", epic_id=epic, kind="fix", priority="P3")
        self.task("Wrong repo", repo="other", epic_id=epic, kind="fix", priority="P1")
        from html.parser import HTMLParser
        class Inputs(HTMLParser):
            def __init__(self):
                super().__init__()
                self.inputs = []
            def handle_starttag(self, tag, attrs):
                if tag == "input":
                    self.inputs.append(dict(attrs))
        for group in ("epic", "state", "repo"):
            html = self.c.get(f"/plan?group={group}&repo=r&epic_id={epic}&kind=fix&priority=P1").text
            parser = Inputs()
            parser.feed(html)
            selected = {i.get("value"): i.get("name") for i in parser.inputs if i.get("form") == "batch-approval"}
            self.assertEqual(selected, {ordinary: "id", flagged: "explicit_id"})
            self.assertIn("Select all visible", html)
            self.assertIn("Requires explicit approval", html)
            self.assertIn(f"/items/task/{ordinary}/approve", html)
            self.assertIn("repo=r&amp;epic_id=", html)

    def test_form_confirmation_count_and_opt_in_is_per_id(self):
        a, b = self.task(), self.task(requires_explicit_approval=True)
        flagged = self.task(requires_explicit_approval=True)
        data = {"id": [a, b], "explicit_id": flagged}
        response = self.c.post("/tasks/approve/confirm", data=data, headers=SAME)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("Approve 3 selected tasks?", response.text)
        self.assertEqual(self.s.get_task(a)["status"], "proposed")
        self.assertEqual(self.c.post("/tasks/approve/confirm", headers=SAME).status_code, 422)
        self.assertEqual(self.c.post("/tasks/approve", data={"id": a}, headers=SAME).status_code, 422)
        response = self.c.post("/tasks/approve", data={"id": [a, b, flagged], "explicit_id": flagged,
                                                      "reason": "Reviewed", "confirm": "yes"}, headers=SAME)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("skipped", response.text)
        self.assertEqual([self.s.get_task(t)["status"] for t in (a, b, flagged)], ["ready", "proposed", "ready"])
        self.assertEqual(len(self.s.list_events(kind="tasks-approved")), 1)

    def test_batch_forms_owner_csrf_and_escaping(self):
        for path in ("/tasks/approve/confirm", "/tasks/approve"):
            self.assertEqual(self.c.post(path, data={"id": "T-1"}).status_code, 403)
        response = self.c.post("/tasks/approve/confirm", data={"id": '<script>alert(1)</script>'}, headers=SAME)
        self.assertIn("&lt;script&gt;", response.text)
        self.assertNotIn("<script>alert", response.text)
        partial = self.c.post("/tasks/approve/confirm", data={"id": "T-1"}, headers={**SAME, "HX-Request": "true"})
        self.assertNotIn("<!doctype html>", partial.text)

    def test_rendered_confirmation_preserves_individual_opt_in_and_rechecks_policy(self):
        from html.parser import HTMLParser
        from urllib.parse import urlencode

        class Confirmation(HTMLParser):
            def __init__(self):
                super().__init__()
                self.action = None
                self.fields = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "form":
                    self.action = attrs.get("action")
                elif tag == "input" and attrs.get("type") == "hidden":
                    self.fields.append((attrs["name"], attrs["value"]))

        ordinary = self.task("Ordinary")
        changed = self.task("Policy changes while confirming")
        explicit = self.task("Individually selected", requires_explicit_approval=True)
        unselected = self.task("Not selected", requires_explicit_approval=True)
        response = self.c.post("/tasks/approve/confirm",
                               data={"id": [ordinary, changed, ordinary], "explicit_id": explicit},
                               headers={**SAME, "HX-Request": "true"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("Approve 3 selected tasks?", response.text)
        self.assertTrue(all(self.s.get_task(tid)["status"] == "proposed"
                            for tid in (ordinary, changed, explicit, unselected)))
        form = Confirmation()
        form.feed(response.text)
        self.assertEqual(form.action, "/tasks/approve")
        self.assertEqual(form.fields, [("id", ordinary), ("id", changed), ("id", explicit),
                                      ("explicit_id", explicit)])
        self.s.update_task(changed, actor="owner", requires_explicit_approval=True)
        form.fields.extend([("confirm", "yes"), ("reason", "Reviewed individually")])
        result = self.c.post(form.action, content=urlencode(form.fields),
                             headers={**SAME, "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIn("requires explicit approval of this ID", result.text)
        self.assertEqual([self.s.get_task(tid)["status"]
                          for tid in (ordinary, changed, explicit, unselected)],
                         ["ready", "proposed", "ready", "proposed"])
        batch = self.s.list_events(kind="tasks-approved")
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0]["data"]["explicit_ids"], [explicit])
        self.assertEqual(batch[0]["data"]["reason"], "Reviewed individually")
        self.assertEqual([r["result"] for r in batch[0]["data"]["results"]],
                         ["approved", "skipped", "approved"])

    def test_inbox_explicit_tasks_have_individual_opt_in(self):
        a = self.task("Ordinary")
        b = self.task("Publish", requires_explicit_approval=True)
        response = self.c.get("/inbox")
        self.assertIn(f'name="id" value="{a}"', response.text)
        self.assertIn(f'name="explicit_id" value="{b}"', response.text)
        self.assertIn("Requires explicit approval", response.text)

    def test_owner_can_edit_explicit_policy_in_task_form(self):
        tid = self.task()
        path = f"/tasks/{tid}/edit"
        response = self.c.post(path, data={"requires_explicit_approval": "true"}, headers=SAME)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(self.s.get_task(tid)["requires_explicit_approval"])
        self.assertIn("Requires explicit approval", self.c.get(f"/tasks/{tid}").text)
        self.assertEqual(self.c.post(path, data={"requires_explicit_approval": "typo"}, headers=SAME).status_code, 422)
        self.c.post(path, data={"requires_explicit_approval": "false"}, headers=SAME)
        self.assertFalse(self.s.get_task(tid)["requires_explicit_approval"])

