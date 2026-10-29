"""Owner-UI operations: pause toggle, garden executor, tuner scheduler, WSL discovery, run tail, import filters,
install scripts. TestClient plus a temp ledger; machine facts are injected."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

try:
    from fastapi.testclient import TestClient
    from hub.app import create_app
    from hub.services import DISCOVERY_SCRIPT, Services, parse_discovery, valid_tool_path
    from hub.settings import Settings
except ImportError as exc:  # hub deps are third-party; skip when absent
    raise unittest.SkipTest(f"hub deps missing: {exc}")

from drover import importer, tuner
from drover.ledger import ops
from tests.test_importer import QUESTIONS, sample

SAME = {"Sec-Fetch-Site": "same-origin"}
TOKEN = {"Authorization": "Bearer tok"}
SCRIPTS = Path(__file__).resolve().parents[1] / "hub" / "scripts"


class FakeWsl:
    def __init__(self, discovery="DROVER_TOOL|claude|/home/u/.local/bin/claude\nDROVER_TOOL|codex|/usr/bin/codex\n",
                 running="Ubuntu (Default)"):
        self.calls, self.discovery, self.running, self.rc = [], discovery, running, 0

    def __call__(self, args, timeout):
        self.calls.append(list(args))
        if args[:2] == ["-l", "--running"]:
            return (0, self.running) if self.running else (1, "There are no running distributions.")
        return self.rc, self.discovery


class FakeTuner:
    def __init__(self, due):
        self.due, self.passes = due, 0

    def status(self, conn, now=None):
        return {"due": {"due": self.due, "reason": "x"}}

    def run_pass(self, conn, now=None, force=False):
        self.passes += 1
        return {"ran": True, "version": "p2", "proposals": [7]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        from drover import config
        import copy
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg['paths'].update(state_dir=str(self.root / 'state'), repos_root=str(self.root / 'repos'))
        cfg['repos_dir'] = str(self.root / 'state' / 'repos')
        environment = patch.dict(os.environ, {'DROVER_HOME': str(self.root / 'state')})
        environment.start()
        self.addCleanup(environment.stop)
        context = patch.object(config, 'get', return_value=cfg)
        context.start()
        self.addCleanup(context.stop)
        self.removed, self.wsl, self.tuner = [], FakeWsl(), FakeTuner(False)
        self.remove_result = lambda repo, row: {"ok": True}
        self.now = [1_000_000.0]

        def remove(repo, row):
            self.removed.append((repo, row["branch"]))
            return self.remove_result(repo, row)

        self.s = Settings(token="tok", run_watchdog=False, trusted_ui_hosts=("testclient",),
                          tailscale=lambda a: "", ui_allowed_hosts=("testserver",), runs_dir=self.root / "runs")
        self.app = create_app(self.root / "l.db", self.s, wsl=self.wsl)
        self.addCleanup(self.app.state.store.close)
        self.store = self.app.state.store
        self.svc = Services(self.store, self.s, wsl=self.wsl, remove=remove, tuner_mod=self.tuner,
                            disks=lambda: {"C:": 61.5, "R:": 90.0}, ollama=lambda url: True,
                            clock=lambda: self.now[0])
        self.ui = self.app.state.ui_view
        self.ui.services = self.svc  # the tests drive the executor by hand, not on a thread
        self.c = TestClient(self.app, follow_redirects=False)

    def do_import(self):
        importer.run_import(self.store.conn, sample(), QUESTIONS)

    def batch(self, title_part):
        return next(b for b in self.ui.garden_batches() if title_part in b["title"])


class TestPauseToggle(Base):
    def test_default_is_paused_and_header_says_so(self):
        self.assertFalse(self.store.dispatch_enabled())
        html = self.c.get("/inbox").text
        self.assertIn("PAUSED", html)
        self.assertIn("/dispatch/confirm", html)
        self.assertNotIn("RUNNING", html)

    def test_token_cannot_toggle(self):
        r = self.c.post("/api/v1/dispatch", json={"enabled": True}, headers=TOKEN)
        self.assertEqual(r.status_code, 403)
        r = self.c.put("/api/v1/settings", json={"dispatch_enabled": "true"}, headers=TOKEN)
        self.assertEqual(r.status_code, 403)
        r = self.c.post("/dispatch/toggle", data={"enable": "1", "confirm": "yes"}, headers=TOKEN)
        self.assertEqual(r.status_code, 403)  # not from our own page
        self.assertFalse(self.store.dispatch_enabled())

    def test_owner_turns_on_only_after_confirm_and_it_is_logged_and_published(self):
        r = self.c.post("/dispatch/toggle", data={"enable": "1"}, headers=SAME)
        self.assertEqual(r.status_code, 422)
        self.assertFalse(self.store.dispatch_enabled())
        self.assertIn("Yes, turn dispatch on", self.c.get("/dispatch/confirm").text)
        r = self.c.post("/dispatch/toggle", data={"enable": "1", "confirm": "yes", "next": "/inbox"}, headers=SAME)
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/inbox"))
        self.assertTrue(self.store.dispatch_enabled())
        self.assertIn("RUNNING", self.c.get("/health").text)
        self.assertEqual(len(self.store.list_events(kind="dispatch.enabled")), 1)
        self.assertIn("dispatch", [e["kind"] for e in self.app.state.bus.recent()])
        r = self.c.post("/dispatch/toggle", data={"enable": "0"}, headers=SAME)  # pausing needs no confirm
        self.assertEqual(r.status_code, 303)
        self.assertFalse(self.store.dispatch_enabled())
        self.assertEqual(len(self.store.list_events(kind="dispatch.paused")), 1)

    def test_owner_api_toggle(self):
        r = self.c.post("/api/v1/dispatch", json={"enabled": True}, headers=SAME)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.c.get("/api/v1/dispatch", headers=TOKEN).json()["enabled"])


class TestGardenExecutor(Base):
    def setUp(self):
        super().setUp()
        self.do_import()
        self.g = self.batch("acme-lib: script-delete")

    def approve(self):
        r = self.c.post(f"/items/garden/{self.g['id']}/approve", headers=SAME)
        self.assertEqual(r.status_code, 200)

    def test_approve_records_exact_rows_then_executes_only_them(self):
        self.approve()
        ev = ops.events(self.store.conn, kind="garden.approve")[0]["data"]
        self.assertEqual(ev["rows"], self.g["entries"])
        self.assertEqual(self.removed, [])  # nothing runs on the request thread
        self.assertEqual(len(self.svc.process_pending()), 1)
        self.assertEqual(self.removed, [("acme-lib", "a1"), ("acme-lib", "a2")])
        self.assertEqual([e["data"]["branch"] for e in self.store.list_events(kind="garden.row.removed")],
                         ["a1", "a2"])
        progress = [e["data"] for e in self.app.state.bus.recent() if e["data"].get("kind") == "garden.progress"]
        self.assertEqual([(p["done"], p["total"]) for p in progress], [(1, 2), (2, 2)])
        self.assertEqual(len(self.store.list_events(kind="garden.done")), 1)
        self.assertNotIn(self.g["id"], [b["id"] for b in self.ui.garden_batches()])

    def test_unapproved_and_rejected_batches_never_execute(self):
        other = self.batch("acme-lib: propose-delete (high")
        self.assertEqual(self.svc.run_garden(other["id"]), {"removed": 0, "kept-repropose": 0, "error": 0})
        self.c.post(f"/items/garden/{self.g['id']}/reject", headers=SAME)
        self.svc.submit_garden(self.g["id"])
        self.svc.process_pending()
        self.assertEqual(self.removed, [])

    def test_review_individually_approves_a_subset(self):
        html = self.c.get(f"/inbox?review={self.g['id']}").text
        self.assertIn('name="branch" value="a1"', html)
        self.assertIn('name="branch" value="a2"', html)
        r = self.c.post(f"/garden/{self.g['id']}/approve-rows", data={"branch": "a2"}, headers=SAME)
        self.assertEqual(r.status_code, 200)
        self.svc.process_pending()
        self.assertEqual(self.removed, [("acme-lib", "a2")])
        bad = self.c.post(f"/garden/{self.g['id']}/approve-rows", data={"branch": "nope"}, headers=SAME)
        self.assertEqual(bad.status_code, 404)  # the batch is decided, so it cannot be approved again
        self.assertEqual(self.c.post("/garden/x/approve-rows", data={}, headers=SAME).status_code, 422)
        # the unticked row stays open in a remainder batch with its own question
        left = self.batch("left over")
        self.assertEqual([r["branch"] for r in left["entries"]], ["a1"])
        self.assertIn(f"Approve batch? {left['title']}", [q["title"] for q in self.store.list_questions(open_only=True)])
        self.assertEqual(importer.decided_rows(self.store.conn)[("acme-lib", "script-delete")], {"a2"})
        importer.run_import(self.store.conn, sample(), QUESTIONS)  # a re-import neither loses nor duplicates it
        self.assertEqual([r["branch"] for b in self.ui.garden_batches() if b["repo"] == "acme-lib" and b["group"] == "script-delete"
                          for r in b["entries"]], ["a1"])

    def test_subset_with_unknown_row_is_refused(self):
        with self.assertRaises(Exception):
            self.ui.approve_garden(self.g["id"], ["a1", "zzz"])
        self.assertEqual(ops.events(self.store.conn, kind="garden.approve"), [])

    def test_row_outcomes_and_no_repeat_after_restart(self):
        results = {"a1": {"ok": False, "repropose": True, "reason": "dirt differs"}}

        def result(repo, row):
            if row["branch"] == "a2":
                raise RuntimeError("git exploded")
            return results[row["branch"]]
        self.remove_result = result
        self.approve()
        self.svc.process_pending()
        self.assertEqual([e["data"]["branch"] for e in self.store.list_events(kind="garden.row.kept-repropose")], ["a1"])
        err = self.store.list_events(kind="garden.row.error")
        self.assertEqual([e["data"]["branch"] for e in err], ["a2"])
        self.assertIn("git exploded", err[0]["data"]["reason"])
        self.svc.run_garden(self.g["id"])  # a second pass (restart) repeats nothing
        self.assertEqual(len(self.removed), 2)

    def test_failed_rows_are_reproposed_and_can_be_retried(self):
        self.remove_result = lambda repo, row: {"ok": False, "reason": "checked out"} if row["branch"] == "a1" else {"ok": True}
        self.approve()
        self.svc.process_pending()
        done = self.store.list_events(kind="garden.done")
        self.assertEqual(len(done), 1)
        retry = self.batch("left over")
        self.assertEqual([r["branch"] for r in retry["entries"]], ["a1"])
        self.assertEqual(done[0]["data"]["reproposed"], retry["id"])
        self.assertEqual(importer.decided_rows(self.store.conn)[("acme-lib", "script-delete")], {"a2"})
        self.remove_result = lambda repo, row: {"ok": True}
        self.c.post(f"/items/garden/{retry['id']}/approve", headers=SAME)
        self.svc.process_pending()
        self.assertEqual(self.removed, [("acme-lib", "a1"), ("acme-lib", "a2"), ("acme-lib", "a1")])

    def test_one_batch_is_queued_once_and_finishes_once(self):
        self.approve()
        self.svc.resume_garden()
        self.svc.submit_garden(self.g["id"])
        self.assertEqual(len(self.svc.process_pending()), 1)
        self.assertEqual(len(self.store.list_events(kind="garden.done")), 1)

    def test_rows_without_a_worktree_are_deleted_as_branches(self):
        import hub.services as services
        from tests.gitfix import Repo
        fx = Repo()
        self.addCleanup(fx.cleanup)
        fx.git("remote", "set-head", "origin", "main")
        fx.branch("a1", {"x.txt": "x\n"})
        fx.branch("a2", {"y.txt": "y\n"})
        fx.branch("untouched", {"z.txt": "z\n"})
        real = services.repo_path
        services.repo_path = lambda name: str(fx.repo)
        self.addCleanup(setattr, services, "repo_path", real)
        self.svc.remove = self.svc.default_remove
        self.svc.rescue_root = self.root / "rescue"
        self.c.post(f"/garden/{self.g['id']}/approve-rows", data={"branch": "a1"}, headers=SAME)
        self.svc.process_pending()
        branches = fx.git("branch", "--format=%(refname:short)").split()
        self.assertNotIn("a1", branches)
        self.assertIn("a2", branches)  # unticked: untouched
        self.assertIn("untouched", branches)
        self.assertEqual([e["kind"] for e in self.store.list_events(limit=500) if e["kind"].startswith("garden.branch.")],
                         ["garden.branch.checked", "garden.branch.rescue-ref", "garden.branch.deleted"])
        self.assertEqual(len(self.store.list_events(kind="garden.row.removed")), 1)

    def test_resume_requeues_unfinished_approvals_only(self):
        self.approve()
        self.svc.resume_garden()
        self.svc.process_pending()
        self.assertEqual(len(self.removed), 2)
        self.svc.resume_garden()  # done now
        self.svc.process_pending()
        self.assertEqual(len(self.removed), 2)

    def test_token_cannot_approve_garden(self):
        r = self.c.post(f"/items/garden/{self.g['id']}/approve", headers=TOKEN)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(ops.events(self.store.conn, kind="garden.approve"), [])


class TestHardening(Base):
    def test_repo_names_cannot_traverse(self):
        import hub.services as services
        for bad in ("..", ".", "../x", "a/b", "a\\b", "/etc", "C:\\x", "", "a b", "x\n"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                services.repo_path(bad)
        old = os.environ.get("DROVER_REPOS_DIR")
        os.environ["DROVER_REPOS_DIR"] = str(self.root)
        try:
            self.assertEqual(Path(services.repo_path("acme-lib")), (self.root / "acme-lib").resolve())
            outside = self.root.parent / "evil-link"
            self.assertNotEqual(Path(services.repo_path("acme-lib")).parent, outside)
        finally:
            if old is None:
                del os.environ["DROVER_REPOS_DIR"]
            else:
                os.environ["DROVER_REPOS_DIR"] = old

    def test_token_cannot_forge_garden_events_or_settings(self):
        for kind in ("garden.approve", "import-batch", "dispatch.enabled", "garden.row.removed"):
            r = self.c.post("/api/v1/events", json={"kind": kind, "data": {"id": "G-x", "rows": []}}, headers=TOKEN)
            self.assertEqual(r.status_code, 403, kind)
        self.assertEqual(self.c.put("/api/v1/settings", json={"garden_batches": "[]"}, headers=TOKEN).status_code, 403)
        self.assertEqual(self.c.post("/api/v1/events", json={"kind": "note"}, headers=TOKEN).status_code, 201)

    def test_a_non_owner_approval_event_executes_nothing(self):
        do = self.do_import
        do()
        g = self.batch("acme-lib: script-delete")
        ops.event(self.store.conn, "garden.approve", "dispatcher", {"id": g["id"], "rows": g["entries"]})
        self.assertEqual(importer.approved_rows(self.store.conn, g["id"]), [])
        self.svc.resume_garden()
        self.svc.process_pending()
        self.assertEqual(self.removed, [])

    def test_token_cannot_import(self):
        body = {"tasks": [{"title": "sneaky"}], "dry_run": False}
        self.assertEqual(self.c.post("/api/v1/import", json=body, headers=TOKEN).status_code, 403)
        self.assertEqual(self.store.list_tasks(), [])
        self.assertEqual(self.c.post("/api/v1/import", json=body, headers=SAME).status_code, 200)
