"""The owner's per-repo dispatch filter: claims, gates, reviews, planners, landing and the CLI."""
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
    dispatch_repos.store(conn, names)


class Filter(tp.Base):
    def setUp(self):
        super().setUp()
        self.pipe._repos["q"] = self.prog                     # a second repo; the tests filter between "p" and "q"

    def test_unset_and_empty_mean_all_repos(self):
        a, b = self.task(repo="p"), self.task(repo="q")
        self.assertEqual(set(self.d.tick()["launched"]), {a, b})
        for raw in ("", "[]", "not json", "{}"):
            self.assertIsNone(dispatch_repos.allowed(lambda k, d=None, raw=raw: raw))

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
        t = ops.create_task(store.conn, "t", "q")
        ops.approve(store.conn, t["id"])
        for s in ("claimed", "running", "gated", "reviewed"):
            ops.set_status(store.conn, t["id"], s)
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
        self.assertEqual(run("set", "motif"), "dispatch_repos: motif")
        self.assertEqual(run("show"), "dispatch_repos: motif")
        conn = connect(db)
        self.addCleanup(conn.close)
        self.assertEqual(json.loads(ops.get_setting(conn, "dispatch_repos")), ["motif"])
        self.assertEqual(len(ops.events(conn, kind="dispatch.repos")), 1)
        self.assertEqual(run("clear"), "dispatch_repos: all repos")
        self.assertEqual(run("show"), "dispatch_repos: all repos")


if __name__ == "__main__":
    unittest.main()
