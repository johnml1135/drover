"""Owner-UI operations: pause toggle, garden executor, tuner scheduler, WSL discovery, run tail, import filters,
install scripts. TestClient plus a temp ledger; machine facts are injected."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
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


class TestTunerScheduler(Base):
    def test_runs_only_when_due_and_at_most_daily(self):
        self.assertIsNone(self.svc.check_tuner())
        self.assertEqual(self.tuner.passes, 0)
        self.tuner.due = True
        self.assertIsNone(self.svc.check_tuner())   # checked a moment ago
        self.now[0] += 86401
        self.assertEqual(self.svc.check_tuner()["version"], "p2")
        self.assertEqual(self.tuner.passes, 1)
        self.assertEqual(len(self.store.list_events(kind="tuner.pass")), 1)

    def test_real_tuner_with_too_few_runs_is_not_due(self):
        self.svc.tuner = tuner
        self.assertIsNone(self.svc.check_tuner())
        self.assertEqual(self.store.list_events(kind="tuner.pass"), [])

    def test_tuner_questions_render_as_proposals_once(self):
        tuner._propose(self.store.conn, "Promote retry_or_escalate?", "model 91% vs rules 80%",
                       [("Promote to live", "acts above tau", True), ("Keep shadow", "", False)])
        html = self.c.get("/inbox").text
        self.assertIn("Tuner proposals", html)
        self.assertEqual(html.count("Promote retry_or_escalate?"), 1)
        self.assertIn("Promote to live", html)
        qid = self.store.list_questions(open_only=True)[0]["id"]
        self.c.post(f"/questions/{qid}/answer", data={"option": "Keep shadow"}, headers=SAME)
        self.assertNotIn("Promote retry_or_escalate?", self.c.get("/inbox").text)


class TestWslDiscovery(Base):
    def test_validation(self):
        ok = ["/usr/bin/claude", "/home/u/.local/bin/claude", "/opt/tools/bin/codex"]
        for p in ok:
            self.assertEqual(valid_tool_path(p, p.rsplit("/", 1)[1]), p)
        for p, tool in [("claude", "claude"), ("./claude", "claude"), ("/usr/../bin/claude", "claude"),
                        ("/usr/bin/evil", "claude"), ("/usr/bin/claude;rm -rf /", "claude"),
                        ("/usr/bin/claude extra", "claude"), ("/usr//bin/claude", "claude"),
                        ("alias claude='x'", "claude"), ("/usr/bin/$(id)/claude", "claude"), ("/usr/bin/", "claude"),
                        ("/usr/bin/codex", "claude"), ("/mnt/c/Users/x/bin/claude", "claude"), ("", "claude"), ("C:\\bin\\claude", "claude")]:
            self.assertIsNone(valid_tool_path(p, tool), p)

    def test_parse_ignores_noise_and_takes_the_first_valid(self):
        out = ("Welcome to Ubuntu\nDROVER_TOOL|claude|/usr/bin/claude\n"
               "claude is aliased\nDROVER_TOOL|claude|/opt/claude\nDROVER_TOOL|codex|/usr/local/bin/codex\n")
        self.assertEqual(parse_discovery(out), {"claude": "/usr/bin/claude", "codex": "/usr/local/bin/codex"})
        self.assertEqual(parse_discovery("DROVER_TOOL|codex|/usr/bin/codex\n"), {"codex": "/usr/bin/codex"})

    def test_discover_runs_the_fixed_command_and_stores_settings(self):
        found = self.svc.discover_tools()
        self.assertEqual(self.wsl.calls[-1], ["-d", "Ubuntu", "--exec", "bash", "-lc", DISCOVERY_SCRIPT])
        s = self.store.get_settings()
        self.assertEqual((s["wsl_bin_claude"], s["wsl_bin_codex"]), ("/home/u/.local/bin/claude", "/usr/bin/codex"))
        self.assertEqual(found, {"claude": s["wsl_bin_claude"], "codex": s["wsl_bin_codex"]})
        health = self.c.get("/health").text
        self.assertIn("/home/u/.local/bin/claude", health)

    def test_bad_output_stores_nothing_and_failure_keeps_old_values(self):
        self.wsl.discovery = "claude\n/bin/sh -c evil\n"
        self.assertEqual(self.svc.discover_tools(), {})
        self.assertNotIn("wsl_bin_claude", self.store.get_settings())
        self.svc.discover_tools.__self__.wsl.discovery = "DROVER_TOOL|claude|/usr/bin/claude\n"
        self.svc.discover_tools()
        self.wsl.rc, self.wsl.discovery = 1, "/usr/bin/evil/claude\n"
        self.svc.discover_tools()
        self.assertEqual(self.store.get_settings()["wsl_bin_claude"], "/usr/bin/claude")


    def test_discovery_when_wsl_first_comes_up(self):
        self.wsl.running = ""
        self.svc.check_wsl()
        self.assertEqual([c for c in self.wsl.calls if c[:1] == ["-d"]], [])
        self.wsl.running = "Ubuntu (Default)"
        self.svc.check_wsl()
        self.assertEqual(len([c for c in self.wsl.calls if c[:1] == ["-d"]]), 1)
        self.svc.check_wsl()  # already up and both tools known: no repeat
        self.assertEqual(len([c for c in self.wsl.calls if c[:1] == ["-d"]]), 1)

    def test_wsl_keepalive_and_dispatcher_state_on_health(self):
        html = self.c.get("/health").text
        for text in ("WSL keepalive", "idle", "paused", "reachable", "61.5", "90.0", "No bench data"):
            self.assertIn(text, html)
        self.store.update_settings({"ollama_bench": json.dumps({"p95_ms": 1420})})
        self.assertIn("1420", self.c.get("/health").text)


class TestRunTail(Base):
    def make_run(self, finish=False):
        t = self.store.create_task("Tail me")["id"]
        rid = self.store.start_run(t, "worker", "claude", "haiku", "high")["id"]
        if finish:
            self.store.finish_run(rid, "landed", allowance_used={"seven_day_pct": 1.5, "five_hour_pct": 4})
        return rid

    def log(self, rid, text):
        d = self.s.runs_dir / str(rid)
        d.mkdir(parents=True)
        (d / "out.log").write_bytes(text if isinstance(text, bytes) else text.encode())

    def test_tail_is_escaped_and_live_runs_poll(self):
        rid = self.make_run()
        self.log(rid, '<script>alert(1)</script> & <img src=x onerror=y>\n')
        html = self.c.get(f"/runs/{rid}").text
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; &amp; &lt;img", html)
        self.assertNotIn("<script>alert(1)", html)
        self.assertNotIn("<img src=x", html)
        self.assertIn(f'hx-get="/runs/{rid}/tail"', html)
        frag = self.c.get(f"/runs/{rid}/tail").text
        self.assertIn("&lt;script&gt;", frag)
        self.assertNotIn("<html", frag)

    def test_finished_run_does_not_poll_and_binary_is_safe(self):
        rid = self.make_run(finish=True)
        self.log(rid, b"ok \xff\xfe bytes\n" + b"x" * 100)
        html = self.c.get(f"/runs/{rid}").text
        self.assertNotIn("every 3s", html)
        self.assertIn("ok", html)

    def test_big_log_shows_only_the_end(self):
        rid = self.make_run()
        self.log(rid, "START\n" + "line\n" * 40000 + "THE END\n")
        html = self.c.get(f"/runs/{rid}").text
        self.assertIn("THE END", html)
        self.assertNotIn("START", html)
        self.assertIn("earlier output not shown", html)

    def test_missing_log_and_bad_ids(self):
        rid = self.make_run()
        self.assertIn("no output yet", self.c.get(f"/runs/{rid}").text)
        for bad in ("999", "abc", "..%2f..%2fsecret", "1.5", "-1", "0", "%EF%BC%91"):
            self.assertEqual(self.c.get(f"/runs/{bad}").status_code, 404, bad)
            self.assertEqual(self.c.get(f"/runs/{bad}/tail").status_code, 404, bad)

    def test_runs_tab_formats_allowance_and_links_detail(self):
        rid = self.make_run(finish=True)
        html = self.c.get("/runs").text
        self.assertIn("1.5% week, 4% 5h", html)
        self.assertIn(f'href="/runs/{rid}"', html)


class TestImportControls(Base):
    def setUp(self):
        super().setUp()
        self.do_import()

    def by_title(self):
        return {t["title"]: t for t in self.store.list_tasks()}

    def test_low_confidence_chip_filters_inbox_and_plan(self):
        html = self.c.get("/inbox").text
        self.assertIn("low confidence (2)", html)
        self.assertIn("task 1", html)
        low = self.c.get("/inbox?low=1").text
        self.assertIn("task 2", low)
        self.assertNotIn("task 1<", low)
        self.assertNotIn("task 3", low)
        plan = self.c.get("/plan?low=1").text
        self.assertIn("task 2", plan)
        self.assertNotIn("task 1<", plan)
        self.assertNotIn("task 3", plan)
        self.assertIn("low confidence", self.c.get("/plan").text)

    def test_approve_all_in_epic_is_owner_only_and_scoped(self):
        epics = {e["title"]: e["id"] for e in self.store.list_epics()}
        eid = epics["acme-lib: one"]
        self.assertEqual({t["status"] for t in self.store.list_tasks()}, {"proposed"})
        r = self.c.post(f"/api/v1/epics/{eid}/approve-proposed", headers=TOKEN)
        self.assertEqual(r.status_code, 403)
        r = self.c.post("/imports/approve-epic", data={"epic_id": eid})  # no same-origin proof
        self.assertEqual(r.status_code, 403)
        self.assertEqual({t["status"] for t in self.store.list_tasks()}, {"proposed"})
        html = self.c.get("/inbox").text
        self.assertIn("Approve all in epic", html)
        r = self.c.post("/imports/approve-epic", data={"epic_id": eid}, headers=SAME)
        self.assertEqual(r.status_code, 200)
        t = self.by_title()
        self.assertEqual([t[n]["status"] for n in ("task 1", "task 2")], ["ready", "ready"])
        self.assertTrue(all(t[n]["approved_at"] for n in ("task 1", "task 2")))
        self.assertEqual(t["task 3"]["status"], "proposed")
        self.assertIsNone(t["task 3"]["approved_at"])
        self.assertEqual(self.c.post("/imports/approve-epic", data={}, headers=SAME).status_code, 422)
        self.assertEqual(self.c.post("/imports/approve-epic", data={"epic_id": "E-nope"}, headers=SAME).status_code, 404)

    def test_per_task_and_bulk_controls_remain(self):
        html = self.c.get("/inbox").text
        self.assertIn("Approve selected", html)
        self.assertIn("/items/import/", html)


def powershell():
    for name in ("powershell.exe", "powershell", "pwsh"):
        p = shutil.which(name)
        if p:
            return p
    p = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    return p if os.path.exists(p) else None


def win_path(p: Path) -> str:
    if sys.platform == "win32":
        return str(p)
    out = subprocess.run(["wslpath", "-w", str(p)], capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else str(p)


@unittest.skipUnless(sys.platform == "win32" and powershell(), "needs Windows with PowerShell")
class TestScripts(unittest.TestCase):
    def test_every_script_parses(self):
        scripts = sorted(SCRIPTS.glob("*.ps1"))
        self.assertTrue({"Install-Drover.ps1", "Add-OllamaFirewallRule.ps1"} <= {s.name for s in scripts})
        for s in scripts:
            cmd = ("$e=$null;$t=$null;[void][System.Management.Automation.Language.Parser]::ParseFile("
                   f"'{win_path(s)}',[ref]$t,[ref]$e);if($e){{$e|ForEach-Object{{$_.Message}};exit 1}}")
            r = subprocess.run([powershell(), "-NoProfile", "-Command", cmd], capture_output=True, text=True,
                               timeout=120)
            self.assertEqual(r.returncode, 0, f"{s.name}: {r.stdout}{r.stderr}")

    def test_install_script_lists_its_steps_in_order(self):
        text = (SCRIPTS / "Install-Drover.ps1").read_text(encoding="utf-8")
        order = ["the configuration", "state directory", "hub token", "uv sync", "scheduled task", "tailscale serve"]
        pos = [text.lower().index(o.lower()) for o in order]
        self.assertEqual(pos, sorted(pos))
        self.assertIn("SupportsShouldProcess", text)
        self.assertNotIn("C:\\drover", text)

    def test_install_script_runs_the_wizard_only_when_config_is_missing_and_not_on_whatif(self):
        text = (SCRIPTS / "Install-Drover.ps1").read_text(encoding="utf-8")
        self.assertIn("python -m drover", text)
        self.assertIn("Test-Path $configPath", text)
        self.assertIn("ShouldProcess($configPath", text)

    def test_install_script_follows_a_state_dir_the_owner_chose(self):
        text = (SCRIPTS / "Install-Drover.ps1").read_text(encoding="utf-8")
        self.assertIn("'--state-dir', $Root", text)                       # -Root is the wizard's first default
        self.assertIn("did not write $configPath", text)
        self.assertIn("SetEnvironmentVariable('DROVER_HOME'", text)
        self.assertIn("$env:DROVER_HOME = $configHome", text)
        self.assertIn("-Root $Root", text)                                # Install-DroverHub.ps1 gets the same state dir

    def test_ollama_rule_is_dry_by_default_and_scoped(self):
        text = (SCRIPTS / "Add-OllamaFirewallRule.ps1").read_text(encoding="utf-8")
        self.assertIn("11434", text)
        self.assertIn("-InterfaceAlias", text)
        self.assertIn("[switch]$Apply", text)
        self.assertNotIn("-RemoteAddress Any", text)


if __name__ == "__main__":
    unittest.main()
