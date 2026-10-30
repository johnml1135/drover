"""Per-repo platforms: placement within [run] platforms, gate platform both, and the wsl tracker."""
import json
import tempfile
import unittest
from pathlib import Path

from drover import config, contract, dispatcher, repos, stats
from hub.dispatch import LandingService
from drover.ledger import ops
from tests.ledger_fixture import claim as fixture_claim, stage as fixture_stage
from tests import test_dispatcher, test_pipeline
from tests.test_pipeline import prog_dict
from tests.rcw_fixture import PipelineBase as Base

def repo(repo, worktrees, platforms=None, gate_platform="windows", default=None):
    p = prog_dict(repo, worktrees)
    raw = {"name": "p", "repo": p["repo"], "worktrees": p["worktrees"],
           "run": {"platforms": ["windows", "wsl"] if platforms is None else platforms, **({"default": default} if default else {})},
           "scripts": {"test": ".drover/scripts/test.ps1"}, "gate": {"platform": gate_platform}}
    return repos.normalize(raw)

class Normalize(unittest.TestCase):
    def test_defaults_are_both_platforms_with_windows_default(self):
        p = prog_dict("R:\\r", "R:\\w")
        self.assertEqual((p["run"]["platforms"], p["run"]["default"]), (["windows", "wsl"], "windows"))
        self.assertEqual(p["gate"]["platform"], "windows")

    def test_wsl_only_repo_defaults_to_wsl(self):
        p = repo("R:\\r", "R:\\w", ["wsl"], "wsl")
        self.assertEqual((p["run"]["platforms"], p["run"]["default"]), (["wsl"], "wsl"))

    def test_gate_both(self):
        p = repo("R:\\r", "R:\\w", gate_platform="both")
        self.assertEqual(repos.gate_platforms(p), ["windows", "wsl"])
        self.assertEqual(repos.gate_platforms(repo("R:\\r", "R:\\w", gate_platform="wsl")), ["wsl"])

    def test_errors(self):
        for kw, pattern in (({"platforms": ["mac"]}, "run.platforms"), ({"platforms": []}, "run.platforms"),
                            ({"platforms": ["windows"], "gate_platform": "both"}, "needs run.platforms"),
                            ({"platforms": ["windows"], "gate_platform": "wsl"}, "needs run.platforms"),
                            ({"platforms": ["windows"], "default": "wsl"}, "run.default"),
                            ({"gate_platform": "linux"}, "gate.platform must be one of")):
            with self.assertRaisesRegex(repos.RepoError, pattern):
                repo("R:\\r", "R:\\w", **kw)

    def test_worktrees_default_next_to_the_repo_or_under_the_shared_root(self):
        raw = {"name": "a", "repo": "R:\\repos\\a", "gate": {"unknown": True}}
        self.assertEqual(repos.normalize(raw)["worktrees"], "R:\\repos\\a.worktrees")
        self.assertEqual(repos.normalize({**raw, "worktrees": "D:\\mine"})["worktrees"], "D:\\mine")
        config.use(config.validate({"paths": {"worktrees": "D:\\wts"}}))
        self.addCleanup(config.reset)
        self.assertEqual(repos.normalize(raw)["worktrees"], "D:\\wts\\a.worktrees")
        self.assertEqual(repos.normalize({**raw, "worktrees": "D:\\mine"})["worktrees"], "D:\\mine")
        with self.assertRaisesRegex(repos.RepoError, "repo is required"):
            repos.normalize({"name": "a"})

    def test_the_shipped_examples_all_load(self):
        examples = repos.load_all(repos.REPOS_DIR)
        self.assertEqual(set(examples), {"acme-lib", "acme-app"})
        self.assertEqual(repos.gate_platforms(examples['acme-app']), ['windows'])
        self.assertTrue(examples['acme-app']['gate']['unknown'])

    def test_state_dir_repos_win_once_the_folder_exists(self):
        from drover import config
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "repos").mkdir()
            (Path(d) / "repos" / "mine.toml").write_text(
                'name="mine"\nrepo="R:\\\\r"\nworktrees="R:\\\\w"\n[gate]\nunknown=true\n')
            config.use(config.load(Path(d), {"DROVER_PATHS_STATE_DIR": d}))
            self.addCleanup(config.reset)
            self.assertEqual(list(repos.load_all()), ["mine"])

class FakeRepoPipeline(Base):
    """A pipeline whose repo is wsl-only or windows-only."""

    def setUp(self):
        super().setUp()
        self.pipe.repository.resolve = lambda path, ref: 'b' * 40

    def repo(self, platforms, gate_platform="windows"):
        self.prog.update(repo(self.prog["repo"], self.prog["worktrees"], platforms, gate_platform))

    def tick_with_wsl(self):
        self.d = self.make(wsl_up=lambda: True, worktree=lambda t, where=None: "R:\\repos\\wt")
        return self.d.tick()

    def reason(self, tid):
        return [r for to, r in self.states(tid) if to == "needs-owner"][-1]

    def test_retry_reselects_a_platform_compatible_seat_on_its_backend(self):
        self.repo(["windows"])
        config.use(config.validate({"seats": [
            {**config.DEFAULT_SEATS[0], "placement": "wsl"},
            {**config.DEFAULT_SEATS[0], "name": "claude-win", "director": False, "priority": 2},
            config.DEFAULT_SEATS[1]]}))
        self.addCleanup(config.reset)
        tid = self.task()
        fixture_claim(self.conn, tid)
        rid = ops.start_run(self.conn, tid, "worker", "claude", "sonnet-5.5")
        ops.finish_run(self.conn, rid, "error")
        ops.set_status(self.conn, tid, "ready")
        # A current Codex route is overridden by the retry's Claude backend.
        self.d = self.make(route=lambda t, p, **kw: {"backend": "codex", "model": "gpt-6.1-sol"},
                           worktree=lambda t, where=None: "R:\\repos\\wt")
        self.d.pipeline.status = lambda cwd: ""
        self.assertEqual(self.d.tick()["launched"], [tid])
        self.assertEqual(self.fl.of("worker")[0]["seat"], "claude-win")

    def test_no_platform_compatible_seat_goes_to_owner_without_backoff(self):
        self.repo(["windows"])
        config.use(config.validate({"seats": [{**config.DEFAULT_SEATS[0], "placement": "wsl"}]}))
        self.addCleanup(config.reset)
        tid = self.task()
        self.assertEqual(self.tick_with_wsl()["launched"], [])
        self.assertEqual(self.status(tid), "needs-owner")
        self.assertIn("no platform-compatible seat", self.reason(tid))
        self.assertIn("windows", self.reason(tid))
        self.assertFalse([row for row in self.port.waiting.pending(("task", tid)) if row["kind"] == "deadline"])

    def test_retry_without_a_measured_compatible_seat_waits(self):
        self.repo(["windows"])
        tid = self.task()
        fixture_claim(self.conn, tid)
        rid = ops.start_run(self.conn, tid, "worker", "codex", "gpt-6-luna")
        ops.finish_run(self.conn, rid, "error")
        ops.set_status(self.conn, tid, "ready")
        self.assertEqual(self.tick_with_wsl()["launched"], [])
        self.assertEqual(self.status(tid), "ready")
        self.assertTrue([row for row in self.port.waiting.pending(("task", tid)) if row["kind"] == "deadline"])

    def test_unconstrained_claude_launch_honours_the_repo_default(self):
        self.prog["run"]["default"] = "wsl"
        tid = self.task()
        self.assertEqual(self.tick_with_wsl()["launched"], [tid])
        self.assertEqual(self.fl.of("worker")[0]["placement"], "wsl")

    def test_default_does_not_override_hard_task_seat_or_path_constraints(self):
        plain, claude = {"title": "t"}, {"backend": "claude", "seat": "claude"}
        wsl_default = repo("R:\\r", "R:\\w", default="wsl")
        win_default = repo("R:\\r", "R:\\w", default="windows")
        for task, selected, path, prog, expected in (
                ({"title": "task"}, claude, "R:\\wt", wsl_default, "wsl"),
                ({"title": "t", "platform": "windows"}, claude, "R:\\wt", wsl_default, "windows"),
                (plain, {**claude, "platform": "windows"}, "R:\\wt", wsl_default, "windows"),
                (plain, {"backend": "codex", "seat": "codex"}, "R:\\wt", win_default, "wsl"),
                (plain, claude, "/home/acme-lib/wt", win_default, "wsl")):
            with self.subTest(task=task, route=selected, path=path):
                self.assertEqual(dispatcher.place(task, selected, path, prog), (expected, None))

    def test_a_wsl_only_repo_places_a_plain_claude_task_in_wsl(self):
        self.repo(["wsl"], "wsl")
        tid = self.task()
        out = self.tick_with_wsl()
        self.assertEqual(out["launched"], [tid])
        spec = self.fl.of("worker")[0]
        self.assertEqual(spec["placement"], "wsl")

    def test_a_windows_only_repo_never_uses_wsl_or_codex(self):
        self.repo(["windows"])
        for _ in range(6):
            self.task()
        self.tick_with_wsl()
        specs = self.fl.of("worker")
        self.assertTrue(specs)
        self.assertEqual({(s["placement"], s["backend"]) for s in specs}, {("windows", "claude")})

    def test_a_linux_tag_on_a_windows_only_repo_goes_to_the_owner_with_a_reason(self):
        self.repo(["windows"])
        tid = self.task(goal="platform: linux")
        self.tick_with_wsl()
        self.assertEqual(self.status(tid), "needs-owner")
        self.assertIn("needs wsl", self.reason(tid))
        self.assertIn("windows", self.reason(tid))
        self.assertEqual(self.fl.of("worker"), [])

    def test_a_platform_windows_tag_on_a_wsl_claude_seat_also_goes_to_the_owner(self):
        claude = {"backend": "claude", "seat": "claude"}
        config.use(config.validate({"seats": [{**config.DEFAULT_SEATS[0], "placement": "wsl"}]}))
        self.addCleanup(config.reset)
        none, why = dispatcher.place({"title": "t", "goal": "platform: windows"}, claude, "R:\\wt", None)
        self.assertIsNone(none)
        self.assertIn("runs only in wsl", why)
        self.assertEqual(dispatcher.place({"title": "t", "goal": "g"}, claude, "R:\\wt", None), ("wsl", None))

    def test_place_function(self):
        windows_only, wsl_only = repo("R:\\r", "R:\\w", ["windows"]), repo("R:\\r", "R:\\w", ["wsl"], "wsl")
        claude = {"backend": "claude", "seat": "claude"}
        plain = {"title": "t", "goal": "g"}
        self.assertEqual(dispatcher.place(plain, claude, "R:\\wt", windows_only), ("windows", None))
        self.assertEqual(dispatcher.place(plain, claude, "R:\\wt", wsl_only), ("wsl", None))
        self.assertEqual(dispatcher.place(plain, claude, "R:\\wt", None), ("windows", None))
        self.assertEqual(dispatcher.place({**plain, "platform": "linux"}, claude, "R:\\wt", wsl_only), ("wsl", None))
        none, why = dispatcher.place({**plain, "platform": "windows"}, claude, "R:\\wt", wsl_only)
        self.assertIsNone(none)
        self.assertIn("runs only on wsl", why)
        codex = {"backend": "codex", "seat": "codex"}
        self.assertEqual(dispatcher.place(plain, codex, "R:\\wt", wsl_only), ("wsl", None))
        self.assertIsNone(dispatcher.place(plain, codex, "R:\\wt", windows_only)[0])

class GateBoth(test_dispatcher.Base):
    def setUp(self):
        super().setUp()
        self.prog = self.pipe.prog
        self.fl = test_pipeline.FakeLauncher(Path(self.tmp.name) / 'runs')
        self.prog.update(repo(self.prog["repo"], self.prog["worktrees"], gate_platform="both"))
        self.verdicts = {}                                   # platform -> passed?
        self.fl.launch = self.launch

    def launch(self, spec):
        self.fl.specs.append(spec)
        if spec.get("role") == "gate":
            ev = next(e["data"] for e in ops.events(self.conn, kind="gate-started") if e["data"].get("gate_run") == spec["run_id"])
            ok = self.verdicts.get(ev["platform"], True)
            Path(ev["receipt"]).write_text(json.dumps({"passed": ok, "sha": ev["sha"], "status": "passed" if ok else "failed",
                                                       "exit_code": 0 if ok else 1, "steps": []}))
            h = test_pipeline.FakeHandle({"status": "done" if ok else "error", "record": None, "text": "", "handoff": None,
                                          "usage": {"tokens_in": 0, "tokens_out": 0, "tokens_cached": 0, "context": 0},
                                          "returncode": 0 if ok else 1, "timeout": None})
        else:
            h = test_pipeline.FakeHandle()
        self.fl.handles[spec["run_id"]] = h
        return h

    def run_worker(self, tid, sha):
        spec = self.fl.of('worker')[-1]
        self.fl.handles[spec['run_id']].result = test_dispatcher.res(
            'done', json.dumps({'status': 'done', 'sha': sha, 'blocker': None, 'note': 'ok'}))

    states = Base.states

    def drive(self, ticks=6):
        self.d = self.make(wsl_up=lambda: True)
        tid = self.task()
        self.d.tick()
        sha = 'a' * 40
        self.run_worker(tid, sha)
        for _ in range(ticks):
            self.d.tick()
        return tid, sha

    def test_the_gate_runs_on_each_platform_in_turn_and_passes_only_when_both_pass(self):
        tid, sha = self.drive()
        gates = self.fl.of("gate")
        self.assertEqual([g["placement"] for g in gates], ["wsl", "windows"])
        self.assertEqual(self.status(tid), "gated")
        runs = ops.list_runs(self.conn, tid)
        self.assertEqual([(r["role"], r["outcome"]) for r in runs if r["role"] != "review"],
                         [("worker", "gated"), ("gate", "passed"), ("gate", "passed")])
        self.assertEqual(len(ops.events(self.conn, kind="gate-platform-passed")), 1)
        self.assertEqual(len(ops.events(self.conn, kind="gate-done")), 1)

    def test_gate_stats_record_each_placement_before_launch_and_after_finish(self):
        placements = []
        def launch(spec):
            if spec['role'] == 'gate':
                row = next(r for r in stats.query(self.conn)['runs'] if r['id'] == spec['run_id'])
                placements.append(row['stats']['placement'])
            return self.launch(spec)
        self.fl.launch = launch
        tid, _ = self.drive()
        self.assertEqual(placements, ['wsl', 'windows'])
        gates = [r for r in stats.query(self.conn, task=tid)['runs'] if r['role'] == 'gate']
        self.assertEqual([r['stats']['placement'] for r in gates], ['wsl', 'windows'])
        self.assertTrue(all(r['ended'] for r in gates))

    def test_a_red_second_platform_fails_the_gate_and_names_the_platform(self):
        self.verdicts["windows"] = False
        tid, sha = self.drive()
        self.assertEqual([g["placement"] for g in self.fl.of("gate")][:2], ["wsl", "windows"])
        runs = ops.list_runs(self.conn, tid)
        self.assertEqual(runs[0]["outcome"], "gate-fail")
        self.assertIn("[windows gate failed]", contract.receipt_excerpt(runs[0]["gate_receipt"]))
        self.assertIn(("ready", "retry"), self.states(tid))

    def test_a_red_first_platform_never_starts_the_second(self):
        self.verdicts["wsl"] = False
        tid, sha = self.drive()
        self.assertEqual([g["placement"] for g in self.fl.of("gate")][:1], ["wsl"])
        self.assertNotIn("windows", [g["placement"] for g in self.fl.of("gate")[:1]])
        self.assertEqual(ops.list_runs(self.conn, tid)[0]["outcome"], "gate-fail")

    def test_wsl_down_waits_for_the_wsl_half_and_tells_the_watchdog(self):
        up = {"v": True}
        self.d = self.make(wsl_up=lambda: up["v"])
        tid = self.task()
        self.d.tick()
        self.run_worker(tid, "a" * 40)
        up["v"] = False
        for _ in range(4):
            self.d.tick()
        self.assertEqual([g["placement"] for g in self.fl.of("gate")], [])
        self.assertEqual(self.status(tid), "running")
        self.assertEqual(json.loads(ops.get_setting(self.conn, "_wsl_needed"))["task"], tid)
        up["v"] = True
        for _ in range(4):
            self.d.tick()
        self.assertEqual([g["placement"] for g in self.fl.of("gate")], ["wsl", "windows"])
        self.assertEqual(self.status(tid), "gated")


class WslTracker(test_dispatcher.Base):
    def test_wsl_pressure_blocks_only_work_that_needs_wsl(self):
        self.allow["codex"] = False
        wsl_task = self.task(size="S", goal="platform: linux")
        win_task = self.task(size="S", priority="P4")
        self.verdict = {**test_dispatcher.GO, "wsl": {"verdict": "queue", "reasons": ["WSL memory 90% of its 16.0 GB cap > 85%"]}}
        self.d = self.make(wsl_up=lambda: True)
        out = self.d.tick()
        self.assertIn(win_task, out["launched"])
        self.assertEqual(self.status(wsl_task), "ready")
        reasons = [e["data"].get("reason", "") for e in self.events("status") if e["data"].get("to") == "ready"]
        self.assertTrue(any("WSL memory" in r for r in reasons))
        self.verdict = test_dispatcher.GO
        self.d.tick()
        self.assertEqual(self.status(wsl_task), "ready")                       # still in backoff, but not blocked by WSL
        self.assertFalse(self.d._wsl_blocked())

    def test_a_short_ram_side_blocks_only_claims_placed_on_that_side(self):
        self.allow["codex"] = False
        wsl_task = self.task(size="S", goal="platform: linux")
        win_task = self.task(size="S", priority="P4")
        sides = {"windows": {"verdict": "go", "reasons": []},
                 "wsl": {"verdict": "hold", "reasons": ["wsl RAM available 1.0 GB < 4 GB"]}}
        self.verdict = {**test_dispatcher.GO, "ram_sides": sides}
        self.d = self.make(wsl_up=lambda: True)
        out = self.d.tick()
        self.assertIn(win_task, out["launched"])
        self.assertEqual(self.status(wsl_task), "ready")
        reasons = [e["data"].get("reason", "") for e in self.events("status") if e["data"].get("to") == "ready"]
        self.assertTrue(any("wsl RAM" in r for r in reasons))

    def test_the_global_verdict_never_includes_the_wsl_tracker(self):
        from drover import resources
        r = resources.check(fake={"platform": "windows", "ram_available_gb": 30, "wsl_up": False})
        self.assertEqual((r["verdict"], r["wsl"]["verdict"]), ("go", "queue"))
        self.assertEqual(r["wsl"]["reasons"], ["WSL is not running"])


if __name__ == "__main__":
    unittest.main()
