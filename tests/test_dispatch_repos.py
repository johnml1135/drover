"""The owner's per-repo dispatch filter: claims, gates, reviews, planners, landing and the CLI."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from drover import dispatch_repos
from drover.ledger import connect, ops
from tests import test_pipeline as tp
from tests import test_replans as tr


def only(conn, *names):
    if names:
        dispatch_repos.store(conn, names)
    else:
        dispatch_repos.clear(conn)


class Filter(tp.PolicyBase):
    def setUp(self):
        super().setUp()
        self.pipe._repos["q"] = self.prog                     # a second repo; the tests filter between "p" and "q"

    def test_unset_and_empty_mean_all_repos(self):
        a, b = self.task(repo="p"), self.task(repo="q")
        self.assertEqual(set(self.d.tick()["launched"]), {a, b})
        self.assertIsNone(dispatch_repos.allowed(lambda k, d=None: d))     # only an absent key means all repos

    def test_an_invalid_value_blocks_every_repo_and_logs(self):
        a, b = self.task(repo="p"), self.task(repo="q")
        for raw in ("not json", '"motif"', "motif", "[]", "{}", '[""]', "[1]", ""):
            ops.set_setting(self.conn, "dispatch_repos", raw)
            with self.assertLogs("drover.dispatch_repos", "ERROR"):
                out = self.d.tick()
            self.assertEqual(out["launched"], [], raw)
            self.assertEqual((self.status(a), self.status(b)), ("ready", "ready"), raw)
        self.conn.execute("DELETE FROM settings WHERE key='dispatch_repos'")
        self.assertEqual(set(self.d.tick()["launched"]), {a, b})

    def test_the_ledger_claim_itself_is_filtered_and_an_invalid_value_never_means_unfiltered(self):
        a, b = self.task(repo="p"), self.task(repo="q")
        only(self.conn, "q")
        self.assertEqual(ops.claim_next(self.conn, {})["id"], b)
        self.assertIsNone(ops.claim_next(self.conn, {}))
        ops.set_setting(self.conn, "dispatch_repos", "p")          # bare string
        with self.assertLogs("drover.dispatch_repos", "ERROR"):
            self.assertIsNone(ops.claim_next(self.conn, {"repo": "p"}))
        self.assertEqual(self.status(a), "ready")

    def test_ledger_cli_claim_obeys_the_filter(self):
        from drover.ledger import cli
        a, b = self.task(repo="p"), self.task(repo="q")
        db = str(self.conn.execute("PRAGMA database_list").fetchone()[2])
        only(self.conn, "q")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--db", db, "--json", "claim"]), 0)
            self.assertEqual(cli.main(["--db", db, "--json", "claim"]), 3)
        self.assertEqual((self.status(a), self.status(b)), ("ready", "claimed"))

    def test_a_list_claims_only_those_repos_and_matches_case_insensitively(self):
        only(self.conn, "P")
        ready_q, ready_p = self.task(repo="q"), self.task(repo="p")
        self.assertEqual(self.d.tick()["launched"], [ready_p])
        self.assertEqual(self.status(ready_q), "ready")
        self.assertEqual([sp["task"]["id"] for sp in self.fl.of("worker")], [ready_p])
        only(self.conn)                                      # an empty store clears it
        self.assertEqual(self.d.tick()["launched"], [ready_q])

    def test_global_pause_still_overrides_an_allowed_repo(self):
        only(self.conn, "p")
        tid = self.task(repo="p")
        ops.set_setting(self.conn, "dispatch_enabled", "false")
        self.assertEqual(self.d.tick()["launched"], [])
        self.assertEqual(self.status(tid), "ready")

    def test_gate_of_an_excluded_repo_waits_until_the_filter_allows_it(self):
        tid = self.task(repo="q")
        self.assertEqual(self.d.tick()["launched"], [tid])
        self.run_worker(tid, self.worker_commits(tid))
        only(self.conn, "p")
        self.d.tick()
        self.assertEqual(self.fl.of("gate"), [])
        only(self.conn)
        self.d.tick()
        self.assertEqual(len(self.fl.of("gate")), 1)

    def test_review_of_an_excluded_repo_waits(self):
        tid = self.task(repo="q")
        self.d.tick()
        self.run_worker(tid, self.worker_commits(tid))
        self.d.tick()                                        # worker reaped, gate started and runs at once
        only(self.conn, "p")
        self.d.tick()                                        # gate reaped: gated, but no review for q
        self.assertEqual(self.status(tid), "gated")
        self.assertEqual(self.fl.of("review"), [])
        only(self.conn)
        self.d.tick()
        self.assertEqual(len(self.fl.of("review")), 1)


class Planner(tr.PlannerDispatchCase):
    def setUp(self):
        super().setUp()
        self.pipe._repos["q"] = self.prog

    def test_replan_of_an_excluded_repo_is_not_launched(self):
        w = self.waiting()
        tid = self.task(repo="q")
        w.request_replan(tid, "owner-request")
        only(self.conn, "p")
        self.d.tick()
        self.d.tick()
        self.assertEqual(self.planners(), [])
        only(self.conn)
        self.d.tick()
        self.d.tick()
        self.assertEqual(len(self.planners()), 1)


class Landing(unittest.TestCase):
    def test_landing_skips_an_excluded_repo_like_pause(self):
        from hub.dispatch import LandingService
        from hub.settings import Settings
        from hub.store import Store
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "l.db")
        self.addCleanup(store.close)
        ops.set_setting(store.conn, "dispatch_enabled", "true")
        t = ops.create_task(store.conn, "t", "q", scope_files=['fixture-134'])
        ops.approve(store.conn, t["id"])
        from tests.ledger_fixture import stage
        stage(store.conn, t["id"], 'reviewed')
        seen = []
        service = LandingService(store, Settings(runs_dir=Path(tmp.name) / "r"), repos_dir=tmp.name)
        service._land_repo = lambda conn, name, prog: seen.append(name) or {}
        from drover import pipeline
        orig = pipeline.Pipeline.repo
        pipeline.Pipeline.repo = lambda self, task: {"name": task["repo"]}
        self.addCleanup(setattr, pipeline.Pipeline, "repo", orig)
        from drover import repos
        orig_ci = repos.ci_policy
        repos.ci_policy = lambda prog: {"unknown": False}
        self.addCleanup(setattr, repos, "ci_policy", orig_ci)
        only(store.conn, "p")
        service.tick()
        self.assertEqual(seen, [])
        only(store.conn)
        service.tick()
        self.assertEqual(seen, ["q"])


class Cli(unittest.TestCase):
    def test_set_show_clear_round_trip_and_is_logged(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "l.db")

        def run(*argv):
            out = io.StringIO()
            self.assertEqual(dispatch_repos.main(["--db", db, *argv], out=out), 0)
            return out.getvalue().strip()
        self.assertEqual(run("show"), "dispatch_repos: all repos")
        self.assertEqual(run("set", "--force", "motif"), "dispatch_repos: motif")
        self.assertEqual(run("show"), "dispatch_repos: motif")
        conn = connect(db)
        self.addCleanup(conn.close)
        self.assertEqual(json.loads(ops.get_setting(conn, "dispatch_repos")), ["motif"])
        self.assertEqual(len(ops.events(conn, kind="dispatch.repos")), 1)
        self.assertEqual(run("clear"), "dispatch_repos: all repos")
        self.assertIsNone(ops.get_setting(conn, "dispatch_repos"))          # clear deletes the key
        self.assertEqual(run("show"), "dispatch_repos: all repos")

    def test_set_rejects_an_unknown_repo_unless_forced_and_show_flags_an_invalid_value(self):
        from unittest import mock
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "l.db")
        err = io.StringIO()
        with mock.patch("drover.repos.load_all", return_value={"motif": {}}):
            self.assertEqual(dispatch_repos.main(["--db", db, "set", "mtoif"], out=io.StringIO(), err=err), 2)
            self.assertIn("unknown repo", err.getvalue())
            self.assertEqual(dispatch_repos.main(["--db", db, "set", "Motif"], out=io.StringIO(), err=err), 0)
        conn = connect(db)
        self.addCleanup(conn.close)
        ops.set_setting(conn, "dispatch_repos", "motif")
        out = io.StringIO()
        self.assertEqual(dispatch_repos.main(["--db", db, "show"], out=out), 1)
        self.assertIn("INVALID", out.getvalue())


class HubClaim(unittest.TestCase):
    def test_store_and_http_claim_obey_the_filter(self):
        try:
            from tests import test_hub_api as th
        except ImportError:
            self.skipTest("hub dependencies missing")
        if not hasattr(th, "TestClient"):
            self.skipTest("fastapi missing")
        tmp, app = th.make()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(app.state.store.close)
        c = th.TestClient(app)
        store = app.state.store
        ids = {}
        for repo in ("p", "q"):
            t = c.post("/api/v1/tasks", json={"title": repo, "repo": repo, "scope_files": [repo]}, headers=th.AUTH).json()["id"]
            store.approve(t)
            ids[repo] = t
        only(store.conn, "q")
        r = c.post("/api/v1/tasks/claim", json={}, headers=th.AUTH)
        self.assertEqual(r.json()["task"]["id"], ids["q"])
        self.assertIsNone(c.post("/api/v1/tasks/claim", json={}, headers=th.AUTH).json()["task"])
        ops.set_setting(store.conn, "dispatch_repos", "p")
        with self.assertLogs("drover.dispatch_repos", "ERROR"):
            self.assertIsNone(c.post("/api/v1/tasks/claim", json={}, headers=th.AUTH).json()["task"])


if __name__ == "__main__":
    unittest.main()
