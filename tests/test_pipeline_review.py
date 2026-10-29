"""The pipeline: pause switch, repo config, worktrees, gate runs, reviews, decisions and the landing loop."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from drover import config, contract, dispatcher, ci, placement, landing, launch, pipeline, repos, runner
from drover.launch import RunnerLauncher
from drover.ledger import connect, ops
from drover.port import DirectPort
from hub.dispatch import DispatcherService, LandingService
from hub.settings import Settings
from hub.store import Store
from tests.tiers import integration
from tests.fakeci import execute_gate, resources_go
from tests.gitfix import Repo
from tests.dispatch_fixture import Observations, make_dispatcher
from tests.pipeline_fixture import MemoryRepo, MemoryRepository, CheckoutGit, finish_gate, read_checkouts

from tests.test_pipeline import Base, PolicyBase, PolicyLauncher, FakeHandle, result, policy_prog as prog_dict


class CodexReview(PolicyBase):
    def setUp(self):
        super().setUp()
        self.prog["review"] = repos.normalize({"name": "p", "repo": str(self.fix.repo),
            "review": {"backend": "codex", "model": "gpt-6.1-sol", "effort": "xhigh"}})["review"]

    def test_no_compatible_review_seat_records_a_visible_hold(self):
        tid = self.task()
        self.assertEqual(self.d.tick()["launched"], [tid])
        self.run_worker(tid, self.worker_commits(tid))
        self.d.tick()
        self.prog["run"]["platforms"] = ["windows"]
        self.d.tick()
        self.assertEqual(self.fl.of("review"), [])
        holds = ops.events(self.conn, task_id=tid, kind="review-hold")
        self.assertTrue(holds, "a gated task without a review route must explain why it is waiting")
        self.assertIn("no platform-compatible seat", holds[-1]["data"]["reason"])
        self.assertEqual(self.status(tid), "gated")
        self.prog["run"]["platforms"].append("wsl")
        self.assertEqual(self.d.tick()["reviewed"], [tid])
        self.assertEqual(self.fl.of("review")[0]["backend"], "codex")

    def test_wsl_repo_runs_worker_wsl_gate_and_codex_review_without_windows(self):
        self.prog["run"] = {"platforms": ["wsl"], "default": "wsl"}
        self.prog["gate"]["platform"] = "wsl"
        runs = self.runs
        class HostRunsWslGate(PolicyLauncher):
            """Check WSL routing and its plan, then return a finished receipt."""
            def launch(self, spec):
                if spec.get("role") == "gate":
                    assert spec["placement"] == "wsl" and spec["argv"][0] == "python3", spec["argv"]
                    d = runner.run_dir(runs, spec["run_id"])
                    body = json.loads((d / "plan.json").read_text())
                    assert body["repo"].startswith("/"), body["repo"]
                    return super().launch(spec)
                return super().launch(spec)
        self.fl = HostRunsWslGate(self.runs)
        self.d = self.make(allow=lambda backend: backend == "codex", wsl_up=lambda: True,
                           launcher=self.fl)
        tid = self.task(scope_files=["src/source.txt"])
        self.assertEqual(self.d.tick()["launched"], [tid])
        worker = self.fl.of("worker")[0]
        self.assertEqual((worker["backend"], worker["placement"]), ("codex", "wsl"))
        self.run_worker(tid, self.worker_commits(tid))
        self.d.tick()
        self.d.tick()
        gates = ops.events(self.conn, task_id=tid, kind="gate-started")
        self.assertEqual([g["data"]["platform"] for g in gates], ["wsl"])
        review = self.fl.of("review")[0]
        self.assertEqual((review["backend"], review["placement"]), ("codex", "wsl"))
        for spec in self.fl.specs:
            self.assertNotEqual(spec.get("placement"), "windows")

    def test_codex_reviewer_uses_repo_route_and_read_only_then_lands_the_exact_sha(self):
        tid, sha = self.to_gated()
        spec = self.fl.of("review")[0]
        self.assertEqual((spec["backend"], spec["model"], spec["effort"], spec["placement"], spec["sandbox"]),
                         ("codex", "gpt-6.1-sol", "xhigh", "wsl", "read-only"))
        self.fl.handles[spec["run_id"]].result = result("done", json.dumps(
            {"status": "done", "sha": sha, "verdict": f"LAND@{sha}", "findings": []}))
        self.d.tick()
        self.assertEqual(self.status(tid), "reviewed")

    def test_codex_hold_returns_the_findings_to_the_worker(self):
        tid, sha = self.to_gated()
        spec = self.fl.of("review")[0]
        self.assertEqual(spec["backend"], "codex")
        self.fl.handles[spec["run_id"]].result = result("done", json.dumps(
            {"status": "done", "sha": sha, "verdict": "HOLD", "findings": ["work.txt:1 fix output"]}))
        self.d.tick()
        self.assertIn(("ready", "retry"), self.states(tid))
        self.assertIn("work.txt:1 fix output", self.fl.of("worker")[-1]["user_prompt"])

    def test_review_seats_follow_priority_capacity_and_explicit_pinning(self):
        original = config.get()
        self.addCleanup(config.use, original)
        cfg = config.validate({"seats": [
            {"name": "director", "backend": "claude", "director": True},
            {"name": "codex-b", "backend": "codex", "priority": 2},
            {"name": "codex-a", "backend": "codex", "priority": 1, "max_workers": 1},
        ]})
        cfg["paths"]["wsl_worktrees"] = str(Path(self.tmp.name) / "ext4")
        config.use(cfg)
        tid, sha = self.to_gated()
        first = self.fl.of("review")[0]
        self.assertEqual(first["seat"], "codex-a")
        tid2 = self.task()
        self.d.tick()
        self.run_worker(tid2, self.worker_commits(tid2))
        self.d.tick()
        self.d.tick()
        self.assertEqual(self.fl.of("review")[-1]["seat"], "codex-b")
        self.prog["review"]["seat"] = "codex-b"
        first_handle = self.fl.handles[first["run_id"]]
        first_handle.result = result("no-record")
        self.d.tick()
        self.assertEqual(self.fl.of("review")[-1]["seat"], "codex-b")

    def test_high_risk_codex_review_keeps_the_configured_codex_model(self):
        tid = self.task(risk="high")
        self.d.tick()
        self.run_worker(tid, self.worker_commits(tid))
        self.d.tick()
        self.d.tick()
        self.assertEqual(self.fl.of("review")[0]["model"], "gpt-6.1-sol")

    def test_configured_review_effort_is_honored_for_a_cheap_model(self):
        self.prog["review"].update(model="gpt-6-luna", effort="low")
        self.to_gated()
        self.assertEqual(self.fl.of("review")[0]["effort"], "low")



class GateAndReview(PolicyBase):
    def test_ci_planning_failure_finishes_the_run_and_releases_capacity(self):
        tid = self.task()
        self.d.tick()
        self.run_worker(tid, self.worker_commits(tid))
        with patch.object(self.pipe.ci, 'prepare', side_effect=ci.CIError('invalid CI script config')):
            self.d.tick()
        checks = [r for r in ops.list_runs(self.conn, tid) if r['role'] == 'gate']
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]['outcome'], 'launch-error')
        self.assertIsNotNone(checks[0]['ended'])
        self.assertEqual(self.d.ci_runners.snapshot()['waiting'], 0)

    def test_review_brief_includes_the_passed_gate_receipt_summary(self):
        tid, sha = self.to_gated()
        g = ops.events(self.conn, task_id=tid, kind="gate-done")[-1]["data"]["g"]
        u = self.fl.of("review")[0]["user_prompt"]
        self.assertIn(f"exact SHA ({sha}) and passed", u)
        self.assertIn("kind: checks", u)
        self.assertIn("status: passed", u)
        self.assertIn(f"duration: {g['duration_s']:.3f} s", u)

    def test_green_gate_goes_to_gated_with_a_receipt_for_the_exact_sha(self):
        tid, sha = self.to_gated()
        self.assertEqual(self.status(tid), "gated")
        runs = ops.list_runs(self.conn, tid)
        self.assertEqual([(r["role"], r["outcome"]) for r in runs],
                         [("worker", "gated"), ("gate", "passed"), ("review", None)])
        rec = contract.parse_gate_receipt(runs[0]["gate_receipt"])
        self.assertTrue(rec["ok"])
        self.assertEqual(rec["receipt"]["sha"], sha)
        self.assertGreater(rec["diff_lines"], 0)
        receipt = json.loads(Path(rec["receipt"]["path"]).read_text())
        self.assertEqual((receipt["sha"], receipt["status"]), (sha, "passed"))
        d, red = contract.parse_gate_event(ops.events(self.conn, task_id=tid, kind="gate")[0]["data"])
        self.assertFalse(red)
        self.assertEqual(len(ops.events(self.conn, kind="gate-started")), 1)
        self.assertEqual(len(ops.events(self.conn, kind="gate-done")), 1)

    def test_red_gate_retries_the_worker_with_the_excerpt_and_does_not_double_count(self):
        self.fl.gate_code, self.fl.gate_output = 1, 'boom: it broke'
        tid = self.task()
        self.d.tick()
        self.run_worker(tid, self.worker_commits(tid))
        self.d.tick()
        self.d.tick()                                                     # the gate is red; the retry starts at once
        self.assertIn(("ready", "retry"), self.states(tid))
        runs = ops.list_runs(self.conn, tid)
        self.assertEqual([(r["role"], r["outcome"]) for r in runs],
                         [("worker", "gate-fail"), ("gate", "failed"), ("worker", None)])
        self.assertEqual(dispatcher.failures(runs), 1)                    # the gate row is not a second failure
        self.assertIn("boom: it broke", contract.receipt_excerpt(runs[0]["gate_receipt"]))
        self.assertTrue(contract.parse_gate_event(ops.events(self.conn, task_id=tid, kind="gate")[0]["data"])[1])
        spec = self.fl.of("worker")[-1]                                   # the relaunch: same worktree, excerpt in the brief
        self.assertIn("boom: it broke", spec["user_prompt"])
        self.assertEqual(spec["cwd"], self.get(tid)["worktree"])

    def infra_task(self, script):
        self.fl.gate_script = list(script)
        tid = self.task()
        self.d.tick()
        sha = self.worker_commits(tid)
        self.run_worker(tid, sha)
        return tid, sha

    def tick(self, n):
        for _ in range(n):
            self.d.tick()

    def test_a_gate_timeout_reruns_only_the_gate_on_the_same_sha(self):
        tid, sha = self.infra_task([("timeout", 1, "")])
        self.tick(4)
        runs = ops.list_runs(self.conn, tid)
        self.assertEqual([(r["role"], r["outcome"]) for r in runs][:3], [("worker", "gated"), ("gate", "failed"), ("gate", "passed")])
        self.assertEqual(sum(r["role"] == "worker" for r in runs), 1)
        self.assertEqual(dispatcher.failures(runs), 0)
        for kind in ("retry", "escalate", "replan-request"):
            self.assertEqual(ops.events(self.conn, task_id=tid, kind=kind), [])
        (ev,) = ops.events(self.conn, task_id=tid, kind="gate-infra-retry")
        self.assertIn("timed out", ev["data"]["reason"])
        self.assertEqual([e["data"]["sha"] for e in ops.events(self.conn, task_id=tid, kind="gate-started")], [sha, sha])
        self.assertEqual(self.status(tid), "gated")

    def test_a_refusal_with_no_receipt_and_unparsed_output_are_infrastructure(self):
        tid, _ = self.infra_task([("error", 2, "usage: CI"), ("error", 1, "something odd happened\n")])
        self.tick(5)
        runs = ops.list_runs(self.conn, tid)
        self.assertEqual(dispatcher.failures(runs), 0)
        self.assertEqual(len(ops.events(self.conn, task_id=tid, kind="gate-infra-retry")), 2)
        self.assertEqual(self.status(tid), "gated")

    def test_infra_failures_past_the_limit_ask_the_owner_without_a_replan(self):
        tid, _ = self.infra_task([("timeout", 1, "")] * 3)
        self.tick(8)
        self.assertEqual(self.status(tid), "needs-owner")
        runs = ops.list_runs(self.conn, tid)
        self.assertEqual([r["role"] for r in runs], ["worker", "gate", "gate", "gate"])
        self.assertEqual(dispatcher.failures(runs), 0)
        self.assertIn("timed out", self.states(tid)[-1][1])
        self.assertEqual(ops.events(self.conn, task_id=tid, kind="replan-request"), [])
        self.assertEqual(ops.events(self.conn, task_id=tid, kind="retry"), [])

    def test_the_infra_retry_limit_is_the_gate_infra_retries_setting(self):
        ops.set_setting(self.conn, "gate_infra_retries", "0")
        tid, _ = self.infra_task([("timeout", 1, "")])
        self.tick(3)
        self.assertEqual(self.status(tid), "needs-owner")
        self.assertEqual(len(self.fl.of("gate")), 1)

    def test_an_infra_failure_then_a_real_failure_takes_the_normal_failure_path(self):
        self.fl.gate_code, self.fl.gate_output = 1, 'boom: it broke'
        tid, _ = self.infra_task([("timeout", 1, "")])
        self.tick(4)
        runs = ops.list_runs(self.conn, tid)
        self.assertEqual(len(ops.events(self.conn, task_id=tid, kind="gate-infra-retry")), 1)
        self.assertEqual(runs[0]["outcome"], "gate-fail")
        self.assertEqual(dispatcher.failures(runs), 1)
        self.assertIn(("ready", "retry"), self.states(tid))

    def test_a_worker_sha_that_is_not_the_worktree_head_is_a_failure_not_a_gate(self):
        tid = self.task()
        self.d.tick()
        self.worker_commits(tid)
        self.run_worker(tid, "a" * 40)
        self.d.tick()
        self.assertEqual(self.fl.of("gate"), [])
        self.assertIn(("ready", "retry"), self.states(tid))
        self.assertEqual(ops.list_runs(self.conn, tid)[0]["outcome"], "gate-fail")


    def test_land_verdict_goes_to_reviewed_and_landing_reads_it(self):
        tid, sha = self.to_gated()
        (spec,) = self.fl.of("review")
        self.assertEqual(spec["model"], "sonnet-5.5")
        self.assertIn(f"git diff main...{sha}", spec["user_prompt"])
        self.assertIn("+x", spec["user_prompt"])
        text = json.dumps({"status": "done", "sha": sha, "verdict": f"LAND@{sha}", "findings": [], "note": "fine"})
        self.fl.handles[spec["run_id"]].result = result("done", text)
        self.d.tick()
        self.assertEqual(self.status(tid), "reviewed")
        rev = [r for r in ops.list_runs(self.conn, tid) if r["role"] == "review"][0]
        self.assertEqual((rev["outcome"], rev["review_verdict"]), ("land", f"LAND@{sha}"))
        self.assertTrue(landing.review_landed(self.conn, tid, sha))

    def test_a_bare_land_or_a_land_for_another_sha_is_not_a_verdict(self):
        tid, sha = self.to_gated()
        (spec,) = self.fl.of("review")
        self.fl.handles[spec["run_id"]].result = result("done", json.dumps(
            {"status": "done", "sha": sha, "verdict": "LAND@" + "b" * 40, "findings": []}))
        self.d.tick()
        self.assertEqual(self.status(tid), "gated")
        self.assertEqual([r["outcome"] for r in ops.list_runs(self.conn, tid) if r["role"] == "review"],
                         ["no-record", None])                              # a fresh review was derived at once
        self.assertEqual(len(self.fl.of("review")), 2)

    def test_high_risk_reviews_use_opus(self):
        tid = self.task(risk="high")
        self.d.tick()
        self.run_worker(tid, self.worker_commits(tid))
        self.d.tick()
        self.d.tick()
        self.assertEqual(self.fl.of("review")[0]["model"], "opus-5.5")

    def test_hold_goes_back_to_ready_counts_against_the_worker_and_carries_the_findings(self):
        tid, sha = self.to_gated()
        (spec,) = self.fl.of("review")
        self.fl.handles[spec["run_id"]].result = result("done", json.dumps(
            {"status": "done", "sha": sha, "verdict": "HOLD", "findings": ["work.txt:1 should say y"]}))
        self.d.tick()
        self.assertIn(("ready", "retry"), self.states(tid))
        runs = ops.list_runs(self.conn, tid)
        self.assertEqual(dispatcher.failures(runs), 1)
        self.assertEqual(dispatcher.fail_runs(runs)[0]["role"], "worker")
        self.assertEqual([r["review_verdict"] for r in runs if r["role"] == "review"], ["HOLD"])
        self.assertFalse(landing.review_landed(self.conn, tid, sha))
        self.assertIn("work.txt:1 should say y", self.fl.of("worker")[-1]["user_prompt"])
        # a second failure steps the worker up a tier, from the worker's model not the reviewer's
        nxt = dispatcher.next_tier(runs, {"backend": "claude", "model": "haiku-5.5"})
        self.assertEqual(nxt["backend"], dispatcher.fail_runs(runs)[0]["backend"])

    def test_hold_without_findings_retries_review_until_the_review_limit(self):
        tid, sha = self.to_gated()
        for attempt in range(1, dispatcher.MAX_REVIEW_TRIES + 1):
            spec = self.fl.of("review")[-1]
            self.fl.handles[spec["run_id"]].result = result("done", json.dumps(
                {"verdict": "HOLD", "findings": [], "note": "No blocking code findings; no writable temporary directory"}))
            self.d.tick()
            runs = ops.list_runs(self.conn, tid)
            reviews = [r for r in runs if r["role"] == "review"]
            with self.subTest(attempt=attempt):
                self.assertEqual(dispatcher.failures(runs), 0)
                self.assertEqual(runs[0]["outcome"], "gated")
                self.assertEqual([r["outcome"] for r in reviews[:attempt]], ["no-record"] * attempt)
                self.assertTrue(all(r["review_verdict"] is None for r in reviews))
                self.assertEqual(len(self.fl.of("worker")), 1)
                self.assertEqual(len(self.fl.of("gate")), 1)
                self.assertFalse(landing.review_landed(self.conn, tid, sha))
                if attempt < dispatcher.MAX_REVIEW_TRIES:
                    self.assertEqual(self.status(tid), "gated")
                    self.assertEqual(len(reviews), attempt + 1)
                    self.assertIsNone(reviews[-1]["outcome"])
                else:
                    self.assertEqual(self.status(tid), "needs-owner")
                    self.assertEqual(len(reviews), dispatcher.MAX_REVIEW_TRIES)
        for kind in ("retry", "escalate", "replan-request"):
            self.assertEqual(ops.events(self.conn, task_id=tid, kind=kind), [])

    def test_a_review_in_flight_is_adopted_after_a_restart_not_requeued(self):
        tid, sha = self.to_gated()
        d2 = self.make()                                                  # a new dispatcher: the hub restarted
        d2.launcher = type("L", (), {"adopt": lambda s, spec, started: FakeHandle(), "launch": self.fl.launch,
                                     "scan": lambda s, want: {self.fl.of("review")[0]["run_id"]: "windows"}})()
        d2.recover_orphans()
        self.assertEqual(self.status(tid), "gated")
        self.assertEqual(len(d2.live), 1)
        self.assertEqual(list(d2.live.values())[0][2]["role"], "review")
