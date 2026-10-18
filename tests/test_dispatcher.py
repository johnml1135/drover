import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

from drover import brief, dispatcher, launch
from drover.ledger import connect, ops
from drover.port import DirectPort, HttpPort
from drover.runner import ScanError

WORKTREE = r"R:\repos\wt"      # a Windows path, so placement does not depend on the host running the tests
GO = {"verdict": "go", "slots": 10, "reasons": []}
DONE = '{"status": "done", "sha": "abc1234", "blocker": null, "note": "ok"}'
BLOCKED = '{"status": "blocked", "sha": "", "blocker": {"kind": "plan", "detail": "api differs"}, "note": ""}'


class FakeHandle:
    pid = 4242

    def __init__(self, result=None):
        self.result = result

    def poll(self):
        return self.result

    def terminal(self):
        return None

    def cancel(self):
        pass


class FakeLauncher:
    def __init__(self):
        self.specs, self.handles = [], []

    def launch(self, spec):
        self.specs.append(spec)
        h = FakeHandle()
        self.handles.append(h)
        return h


def res(status, text="", ctx=1000):
    return {"status": status, "record": launch.parse_action_record(text), "text": text, "handoff": "H" if status == "rotate" else None,
            "usage": {"tokens_in": 10, "tokens_out": 5, "tokens_cached": 1, "context": ctx}, "returncode": 0}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.conn = connect(Path(self.tmp.name) / "l.db")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.conn.close)
        ops.set_setting(self.conn, "dispatch_enabled", "true")      # the switch defaults to off
        self.port, self.fl = DirectPort(self.conn), FakeLauncher()
        self.verdict, self.allow, self.gate_ok, self.decisions = GO, {"claude": True, "codex": True}, True, []
        self.d = self.make()

    def make(self, **kw):
        args = dict(launcher=self.fl, resources_fn=lambda new_workers=1: self.verdict,
                    admit_fn=lambda plan, role, pct: {"allow": self.allow[plan]},
                    decide_fn=self.decide, gate_fn=lambda t, r: {"ok": self.gate_ok, "excerpt": "FAIL x\n" * 3},
                    worktree_fn=lambda t: WORKTREE, state=self.tmp.name,
                    headroom_fn=lambda plan: {"remaining_pct": 90.0, "hours_to_reset": 100.0})
        args.update(kw)
        return dispatcher.Dispatcher(self.port, **args)

    def decide(self, name, state, rule_answer=None):
        self.decisions.append(name)
        return {"answer": None, "shadow_answer": rule_answer, "acted": False, "confidence": 0.9,
                "criteria_version": "1", "model": "nimble-v3", "state_hash": "h", "probabilities": None}

    def task(self, ready=True, **kw):
        t = ops.create_task(self.conn, kw.pop("title", "t"), "p", repo_path=kw.pop("repo_path", None), **kw)
        if ready:
            ops.approve(self.conn, t["id"])
        return t["id"]

    def status(self, tid):
        return ops.get_task(self.conn, tid)["status"]

    def finish(self, i, result):
        self.fl.handles[i].result = result

    def events(self, kind):
        return ops.events(self.conn, kind=kind)


class Admission(Base):
    def test_queue_and_hold_claim_nothing(self):
        t = self.task()
        for v in ("queue", "hold"):
            self.verdict = {"verdict": v, "slots": 0, "reasons": ["r"]}
            self.assertEqual(self.d.tick()["launched"], [])
            self.assertEqual(self.status(t), "ready")
        self.assertEqual(len(self.events("resources")), 2)
        self.verdict = GO
        self.assertEqual(self.d.tick()["launched"], [t])

    def test_no_allowance_no_claim(self):
        t = self.task()
        self.allow = {"claude": False, "codex": False}
        self.d.tick()
        self.assertEqual(self.status(t), "ready")

    def test_unadmitted_backend_unclaims(self):
        t = self.task(scope_files=["a/x.cs"])      # dotnet: claude or codex, neither admitted
        self.allow["claude"] = False
        self.allow["codex"] = False
        self.d.tick()
        self.assertEqual(self.status(t), "ready")

    def test_never_starts_backlog(self):
        a, b = self.task(ready=False), self.task(ready=False)
        ops.set_status(self.conn, b, "draft")
        self.assertEqual(self.d.tick()["launched"], [])
        self.assertEqual((self.status(a), self.status(b)), ("proposed", "draft"))

    def test_heartbeat(self):
        self.d.tick()
        self.assertIsNotNone(ops.get_setting(self.conn, "dispatcher_heartbeat"))


class Cap(Base):
    def test_default_cap_four(self):
        for _ in range(6):
            self.task()
        self.assertEqual(len(self.d.tick()["launched"]), 4)
        self.assertEqual(len(self.d.tick()["launched"]), 0)

    def test_policy_cap_and_resource_slots(self):
        ops.add_policy_version(self.conn, "v1", {"parallel_cap": 2})
        for _ in range(5):
            self.task()
        self.assertEqual(len(self.d.tick()["launched"]), 2)
        self.finish(0, res("done", DONE))
        self.verdict = {"verdict": "go", "slots": 1, "reasons": []}
        out = self.d.tick()
        self.assertEqual((len(out["reaped"]), len(out["launched"])), (1, 1))


class Routing(Base):
    def test_size_and_dotnet(self):
        s = self.task(size="S", priority="P0")
        m = self.task(size="M", priority="P1", scope_files=["x/a.csproj"])
        self.allow["codex"] = False
        self.d.tick()
        by = {sp["task"]["id"]: sp for sp in self.fl.specs}
        self.assertEqual((by[s]["backend"], by[s]["model"]), ("claude", "haiku-5.5"))
        self.assertEqual((by[m]["backend"], by[m]["model"]), ("claude", "sonnet-5.5"))

    def test_real_router_picks_within_tiers_and_effort_rules(self):
        self.task(size="S", priority="P0")
        self.task(size="M", priority="P1")
        self.d.tick()
        by = {sp["task"]["size"]: sp for sp in self.fl.specs}
        self.assertIn((by["S"]["backend"], by["S"]["model"]), {("claude", "haiku-5.5"), ("codex", "gpt-6-luna")})
        self.assertIn((by["M"]["backend"], by["M"]["model"]), {("claude", "sonnet-5.5"), ("codex", "gpt-6.1-sol")})
        self.assertEqual(by["S"]["effort"], "xhigh")          # Haiku and Luna always xhigh

    def test_router_gets_policy_data_and_headroom_of_admitted_plans_only(self):
        seen = []

        def fake(task, policy, headroom):
            seen.append((task["id"], policy, headroom))
            return {"backend": "claude", "model": "sonnet-5.5", "effort": "max", "harness": "default"}
        ops.add_policy_version(self.conn, "v1", {"routes": {"x": []}})
        self.allow["codex"] = False
        self.d = self.make(route_fn=fake)
        self.task(size="M")
        self.d.tick()
        self.assertEqual(seen[0][1], {"routes": {"x": []}})
        self.assertEqual(list(seen[0][2]), ["claude"])
        self.assertEqual(self.fl.specs[0]["effort"], "max")   # non-cheap models keep the router's effort

    def test_router_with_no_backend_unclaims(self):
        t = self.task()
        self.d = self.make(route_fn=lambda t, p, h: {"backend": None, "reason": "no allowance"})
        self.assertEqual(self.d.tick()["launched"], [])
        self.assertEqual(self.status(t), "ready")

    def test_dotnet_reaches_router_as_a_stack_not_a_platform(self):
        seen = []
        self.d = self.make(route_fn=lambda t, p, h: seen.append(t) or {"backend": "claude", "model": "sonnet-5.5"})
        self.task(scope_files=["a/b.csproj"])
        self.d.tick()
        self.assertEqual((seen[0].get("platform"), seen[0]["stack"]), (None, "dotnet"))

    def test_rule_fallback_only_when_route_missing(self):
        with unittest.mock.patch.object(dispatcher, "_route_module", lambda: None):
            d = self.make()
        self.allow["codex"] = False
        self.task(size="S")
        d.tick()
        self.assertEqual(self.fl.specs[0]["model"], "haiku-5.5")

    def test_model_ids_mapped_for_cli(self):
        argv, _ = launch.build_argv({"backend": "claude", "model": "haiku-5.5", "effort": "xhigh",
                                     "system_append": "", "user_prompt": ""})
        self.assertEqual(argv[argv.index("--model") + 1], "claude-haiku-5-5")
        self.assertEqual(launch.MODEL_IDS, {"haiku-5.5": "claude-haiku-5-5", "sonnet-5.5": "claude-sonnet-5-5",
                                            "opus-5.5": "claude-opus-5-5"})
        argv, _ = launch.build_argv({"backend": "codex", "model": "gpt-6.1-sol", "effort": "xhigh",
                                     "system_append": "", "user_prompt": "", "cwd": "/tmp/acme-app"})
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-6.1-sol")


class RetryEscalate(Base):
    def run_fail(self, i):
        self.gate_ok = False
        self.finish(i, res("done", DONE))
        self.d.tick()

    def test_retry_then_escalate_then_replan(self):
        self.allow["codex"] = False
        t = self.task(size="S")
        self.d.tick()
        for i in range(6):
            self.run_fail(i)
        models = [sp["model"] for sp in self.fl.specs]
        self.assertEqual(models, ["haiku-5.5", "haiku-5.5", "sonnet-5.5", "sonnet-5.5", "opus-5.5", "opus-5.5"])
        self.assertEqual(self.status(t), "blocked")
        ev = self.events("replan-request")
        self.assertEqual([e["data"]["trigger"] for e in ev], ["retries-exhausted"])
        self.assertEqual(len(self.events("escalate")), 2)

    def test_retry_brief_has_excerpt_and_shadow_logged(self):
        self.task(size="S")
        self.d.tick()
        self.run_fail(0)
        self.assertIn("FAIL x", self.fl.specs[1]["user_prompt"])
        self.assertIn("retry_or_escalate", self.decisions)
        row = self.conn.execute("SELECT shadow, rule_answer FROM decisions WHERE question_name='retry_or_escalate'").fetchone()
        self.assertEqual((row[0], row[1]), (1, '"retry"'))

    def test_no_record_counts_as_failure(self):
        t = self.task(size="S")
        self.d.tick()
        self.finish(0, res("no-record", "just chatter"))
        self.d.tick()
        self.assertEqual(ops.list_runs(self.conn, t)[0]["outcome"], "no-record")
        self.assertEqual(len(self.fl.specs), 2)

    def test_gate_pass(self):
        t = self.task()
        self.d.tick()
        self.finish(0, res("done", DONE))
        self.d.tick()
        self.assertEqual(self.status(t), "gated")
        self.assertEqual(ops.list_runs(self.conn, t)[0]["tokens_in"], 10)


class Replan(Base):
    def test_blocker_trigger_holds_dependents(self):
        a = self.task()
        b = self.task(deps=[a], ready=True) if False else self.task(deps=[a])
        self.d.tick()
        self.finish(0, res("blocked", BLOCKED))
        self.d.tick()
        self.assertEqual(self.status(a), "blocked")
        self.assertEqual(self.status(b), "blocked")
        e = self.events("replan-request")[0]
        self.assertEqual((e["data"]["trigger"], e["data"]["blocker"]["kind"]), ("blocker", "plan"))

    def test_plan_wrong_acted(self):
        def yes(name, state, rule_answer=None):
            r = self.decide(name, state, rule_answer)
            if name == "plan_wrong":
                r.update(acted=True, answer=True)
            return r
        self.d = self.make(decide_fn=yes)
        t = self.task(size="S")
        self.d.tick()
        self.gate_ok = False
        self.finish(0, res("done", DONE))
        self.d.tick()
        self.assertEqual(self.events("replan-request")[0]["data"]["trigger"], "plan-wrong")
        self.assertEqual(self.status(t), "blocked")

    def test_stale_brief_trigger(self):
        a = self.task()
        b = self.task(deps=[a])
        self.d = self.make(decide_fn=lambda n, s, rule_answer=None: {**self.decide(n, s, rule_answer), "acted": True, "answer": True})
        self.verdict = {"verdict": "queue", "slots": 0, "reasons": []}
        self.d.tick()                                   # sets the cursor
        ops.set_status(self.conn, a, "claimed", "x")
        ops.set_status(self.conn, a, "running", "x")
        for s in ("gated", "reviewed", "landed"):
            ops.set_status(self.conn, a, s, "x")
        self.d.tick()
        self.assertEqual(self.events("replan-request")[0]["data"]["trigger"], "stale-brief")
        self.assertEqual(self.status(b), "blocked")


class Rotation(Base):
    def test_rotate_writes_handoff_then_continues(self):
        t = self.task(size="S")
        self.d.tick()
        self.finish(0, res("rotate", ctx=81000))
        self.d.tick()
        self.assertEqual(ops.list_runs(self.conn, t)[0]["outcome"], "rotated")
        self.assertIn("## Handoff", self.fl.specs[1]["user_prompt"])
        self.assertEqual(ops.get_task(self.conn, t)["handoff"], "")

    def test_sonnet_has_no_80k_ceiling(self):
        self.assertIsNone(launch.RunnerLauncher(runs_dir=self.tmp.name).ceiling("sonnet-5.5"))


class Launching(Base):
    def test_argv_claude_and_codex(self):
        spec = {"backend": "claude", "model": "m", "effort": "high", "system_append": "SYS", "user_prompt": "U",
                "cwd": "/tmp/acme-app"}
        argv, stdin = launch.build_argv(spec)
        self.assertEqual(argv[:4], ["claude", "-p", "--output-format", "stream-json"])
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], "SYS")
        self.assertEqual((argv[argv.index("--model") + 1], argv[argv.index("--effort") + 1], stdin), ("m", "high", "U"))
        argv, _ = launch.build_argv({**spec, "backend": "codex", "effort": "xhigh"})
        self.assertEqual(argv[:3], ["codex", "exec", "--json"])
        self.assertIn("model_reasoning_effort=xhigh", argv)
        self.assertIn('developer_instructions="SYS"', argv)

    def test_action_record_parsing(self):
        self.assertEqual(launch.parse_action_record("blah\n```json\n" + DONE + "\n```")["sha"], "abc1234")
        self.assertIsNone(launch.parse_action_record('{"status": "blocked", "blocker": null}'))
        self.assertIsNone(launch.parse_action_record("nothing"))
        self.assertEqual(len(launch.parse_action_record('{"status":"done","note":"' + "n" * 500 + '"}')["note"]), 300)

    def test_stubs(self):
        with self.assertRaises(NotImplementedError):
            launch.PaneLauncher().launch({})
        with self.assertRaises(NotImplementedError):
            HttpPort().claim_next()


class StaleClaims(Base):
    def claim(self):
        tid = self.task()
        ops.claim_next(self.conn, {})
        return tid

    def test_old_claim_with_no_run_requeued_every_tick(self):
        t = self.claim()
        self.assertEqual(self.make(now_fn=lambda: time.time() + 299).requeue_stale_claims(), [])
        d = self.make(now_fn=lambda: time.time() + 301)
        self.assertEqual(d.requeue_stale_claims(), [t])
        self.assertEqual(self.status(t), "ready")
        self.assertEqual(self.events("claim-expired")[0]["task_id"], t)
        self.assertTrue(ops.get_task(self.conn, t)["approved_at"])
        t2 = ops.claim_next(self.conn, {})["id"]
        out = d.tick()                                      # on a tick too (and it may be reclaimed after)
        self.assertIn(t2, out["recovered"])

    def test_fresh_claim_and_claim_with_open_run_untouched(self):
        t = self.claim()
        self.assertEqual(self.make(now_fn=lambda: time.time() + 200).requeue_stale_claims(), [])
        self.assertEqual(self.status(t), "claimed")
        ops.start_run(self.conn, t, "worker", "claude", "m", "e")
        self.assertEqual(self.make(now_fn=lambda: time.time() + 400).requeue_stale_claims(), [])


class LaunchErrors(Base):
    def failing(self, exc):
        class L:
            n = 0

            def launch(s, spec):
                s.n += 1
                raise exc
        self.fl = L()
        self.d = self.make(launcher=self.fl)

    def test_launch_errors_do_not_escalate_and_cap_at_three(self):
        t = self.task(size="S")
        self.failing(OSError("exec format"))
        for _ in range(5):
            self.d.tick()
        runs = ops.list_runs(self.conn, t)
        self.assertEqual([r["outcome"] for r in runs], ["launch-error"] * 3)
        self.assertEqual(self.status(t), "needs-owner")
        self.assertEqual(self.fl.n, 3)
        self.assertEqual(dispatcher.failures(runs), 0)

    def test_missing_binary_marks_backend_unavailable(self):
        self.task(size="S")
        self.allow["codex"] = False
        self.failing(FileNotFoundError("claude"))
        self.d.tick()
        self.allow["codex"] = True
        self.assertEqual(set(self.d.unavailable), {("claude", "windows")})
        t2 = self.task(size="M", priority="P0")
        self.fl = FakeLauncher()
        self.d.launcher = self.fl
        self.d.tick()
        self.assertTrue(all(s["backend"] == "codex" for s in self.fl.specs))

    def test_real_launcher_wraps_popen_errors(self):
        def boom(*a, **k):
            raise FileNotFoundError("nope")
        with self.assertRaises(launch.LaunchError) as c:
            launch.RunnerLauncher(runs_dir=self.tmp.name, run=boom).launch(SPEC)
        self.assertTrue(c.exception.missing)



class TailError(Base):
    def test_dispatcher_logs_tail_error_event(self):
        t = self.task()
        self.d.tick()
        self.finish(0, {**res("done", DONE), "tail_error": "OSError('disk full')"})
        self.d.tick()
        self.assertEqual(len(self.events("tail-error")), 1)


class FailClosed(Base):
    def test_no_gate_means_needs_owner(self):
        d = self.make(gate_fn=None)
        t = self.task()
        d.tick()
        self.finish(0, res("done", DONE))
        d.tick()
        self.assertEqual(self.status(t), "needs-owner")

    def test_main_wires_the_pipeline_without_the_flag_and_stays_paused(self):
        import contextlib
        import io
        lock = str(Path(self.tmp.name) / "d.lock")       # never the real C:\\drover lock
        orig = dispatcher.IS_WINDOWS
        dispatcher.IS_WINDOWS = True
        self.addCleanup(setattr, dispatcher, "IS_WINDOWS", orig)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = dispatcher.main(["--db", str(Path(self.tmp.name) / "m.db"), "--once", "--json", "--lock", lock])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out.getvalue())["paused"])        # dispatch_enabled defaults to false

    def test_main_refuses_off_windows(self):
        import io
        from contextlib import redirect_stderr
        orig = dispatcher.IS_WINDOWS
        dispatcher.IS_WINDOWS = False
        self.addCleanup(setattr, dispatcher, "IS_WINDOWS", orig)
        err = io.StringIO()
        with redirect_stderr(err):
            rc = dispatcher.main(["--db", str(Path(self.tmp.name) / "m.db"), "--once", "--no-gate-dry-run",
                                  "--lock", str(Path(self.tmp.name) / "d.lock")])
        self.assertEqual(rc, 2)
        self.assertIn("only inside the Windows hub", err.getvalue())
        self.assertFalse((Path(self.tmp.name) / "m.db").exists())

    def test_empty_sha_is_blocked(self):
        rec = launch.parse_action_record('{"status": "done", "sha": "", "note": "x"}')
        self.assertEqual((rec["status"], rec["blocker"]["kind"]), ("blocked", "no-sha"))
        t = self.task()
        self.d.tick()
        self.finish(0, res("blocked", '{"status": "done", "sha": ""}'))
        self.d.tick()
        self.assertEqual(self.events("replan-request")[0]["data"]["blocker"]["kind"], "no-sha")


class ParserAndRotation(Base):
    def test_parser_is_bounded_on_brace_soup(self):
        t0 = time.monotonic()
        self.assertIsNone(launch.parse_action_record("{" * 500000))
        self.assertIsNone(launch.parse_action_record('{"status": ' * 50000))
        self.assertLess(time.monotonic() - t0, 2)
        self.assertEqual(launch.parse_action_record("{" * 100000 + DONE)["sha"], "abc1234")

    def test_blocker_before_status_key(self):
        rec = launch.parse_action_record('{"blocker": {"kind": "k", "detail": "d"}, "status": "blocked"}')
        self.assertEqual(rec["blocker"], {"kind": "k", "detail": "d"})

        self.allow["codex"] = False
        self.task(size="S")
        self.d.tick()
        self.gate_ok = False
        for i in range(2):
            self.finish(i, res("done", DONE))
            self.d.tick()
        self.assertEqual(self.events("escalate")[0]["data"]["to"], "sonnet-5.5")


class Timeouts(Base):
    def test_dispatcher_records_timeout_and_counts_it(self):
        t = self.task(size="S")
        self.d.tick()
        self.assertEqual(self.fl.specs[0]["wall_s"], 30 * 60)
        self.assertEqual(self.fl.specs[0]["idle_s"], 600)
        self.finish(0, {**res("timeout"), "timeout": "wall-clock limit 1800s"})
        self.d.tick()
        self.assertEqual(ops.list_runs(self.conn, t)[0]["outcome"], "timeout")
        self.assertEqual(len(self.fl.specs), 2)

    def test_policy_overrides_limits(self):
        ops.add_policy_version(self.conn, "v1", {"wall_min": {"M": 5}, "idle_min": 2})
        self.task(size="M")
        self.d.tick()
        self.assertEqual((self.fl.specs[0]["wall_s"], self.fl.specs[0]["idle_s"]), (300, 120))


SPEC = {"run_id": 3, "backend": "claude", "model": "sonnet-5.5", "cwd": ".", "system_append": "SYS",
        "user_prompt": "BRIEF", "task": {"id": "T", "title": "t", "goal": "g"}}


# ---- ADR 0002: placement, adoption through runner files, head of line, the data contract -----------------

class Placement(Base):
    def test_rules(self):
        pf = dispatcher.placement_for
        claude, codex = {"backend": "claude"}, {"backend": "codex"}
        plain = {"title": "t", "goal": "g"}
        self.assertEqual(pf(plain, claude), "windows")
        self.assertEqual(pf(plain, codex), "wsl")
        self.assertEqual(pf({**plain, "platform": "windows"}, codex), "wsl")        # Codex is always WSL
        self.assertEqual(pf({**plain, "platform": "linux"}, claude), "wsl")
        self.assertEqual(pf({**plain, "tags": ["platform: linux"]}, claude), "wsl")
        self.assertEqual(pf({**plain, "tags": ["platform:linux"]}, claude), "wsl")
        self.assertEqual(pf({"title": "t", "goal": "Port it. platform: linux"}, claude), "wsl")
        self.assertEqual(pf({**plain, "scope_files": ["a/x.csproj"]}, claude), "windows")
        self.assertEqual(pf({"title": "fix msbuild target", "goal": ""}, claude), "windows")
        self.assertEqual(pf(plain, {"backend": "claude", "platform": "linux"}), "wsl")   # the router may override
        self.assertEqual(pf({**plain, "platform": "linux"}, {"backend": "claude", "platform": "windows"}), "windows")
        self.assertEqual(pf({**plain, "scope_files": ["a/x.csproj"]}, claude, None, ["wsl"]), "wsl")   # wsl-only repo
        self.assertEqual(pf({**plain, "scope_files": ["a/x.cs"]}, codex, None, ["windows", "wsl"]), "wsl")
        self.assertEqual(pf(plain, claude, "/home/u/wt"), "wsl")                    # only WSL can reach it
        self.assertEqual(pf(plain, claude, "/mnt/r/repos/wt"), "windows")

    def test_rule_route_dotnet_follows_the_owner_rule(self):
        rr = dispatcher.rule_route
        cs = {"title": "t", "goal": "g", "scope_files": ["a/x.cs"], "size": "M"}
        self.assertEqual(rr(cs, ["claude", "codex"], {"claude": 3}, ["windows"])["backend"], "claude")
        self.assertEqual(rr(cs, ["codex"], None, ["windows"])["backend"], "claude")       # never Codex on Windows
        self.assertEqual(rr(cs, ["codex"], None, ["wsl"])["backend"], "codex")
        self.assertEqual(rr({**cs, "scope_files": [], "title": "fix msbuild"}, ["codex"], None, ["wsl"])["backend"],
                         "codex")
        self.assertEqual(rr(cs, ["claude", "codex"], {"claude": 3}, ["windows", "wsl"])["backend"], "codex")

    def test_rule_route_honours_an_explicit_platform_tag_for_dotnet(self):
        rr = dispatcher.rule_route
        cs = {"title": "t", "goal": "g", "scope_files": ["a/x.cs"], "size": "M"}
        both = ["windows", "wsl"]
        self.assertEqual(rr({**cs, "goal": "platform: windows"}, ["codex", "claude"], {"claude": 5}, both)["backend"],
                         "claude")          # Windows tag: Claude only, never Codex
        self.assertEqual(rr({**cs, "platform": "windows"}, ["codex"], None, both)["backend"], "claude")
        self.assertEqual(rr({**cs, "goal": "platform: linux"}, ["codex", "claude"], {"claude": 5}, both)["backend"],
                         "codex")

    def test_dispatch_records_placement_on_spec_and_event(self):
        self.allow["codex"] = False
        self.task(size="S", goal="platform: linux")
        self.d = self.make(wsl_up_fn=lambda: True)
        self.d.tick()
        self.assertEqual(self.fl.specs[0]["placement"], "wsl")
        self.assertEqual(self.events("run-process")[0]["data"]["placement"], "wsl")

    def test_wsl_task_with_wsl_down_defers_and_tells_the_watchdog(self):
        self.allow["codex"] = False
        t = self.task(size="S", goal="platform: linux")
        w = self.task(size="S", priority="P4")
        self.d = self.make(wsl_up_fn=lambda: False)
        out = self.d.tick()
        self.assertEqual(self.status(t), "ready")
        self.assertIn(w, out["launched"])                        # the Windows task behind it is not blocked
        note = json.loads(ops.get_setting(self.conn, "_wsl_needed"))
        self.assertEqual(note["task"], t)
        self.assertEqual(self.port.get_backoffs()[t]["min"], 1)

    def test_windows_task_never_asks_for_wsl(self):
        self.allow["codex"] = False
        self.task(size="S")
        self.d = self.make(wsl_up_fn=lambda: self.fail("WSL must not be probed"))
        self.d.tick()
        self.assertIsNone(ops.get_setting(self.conn, "_wsl_needed"))


class AdoptHandle(FakeHandle):
    def __init__(self, status=None, result=None):
        super().__init__(result)
        self.status, self.cancelled = status, False

    def terminal(self):
        return self.status

    def cancel(self):
        self.cancelled = True


class AdoptLauncher(FakeLauncher):
    def __init__(self):
        super().__init__()
        self.adopted, self.adopt_with = [], {}

    def adopt(self, spec, started=None):
        h = self.adopt_with.get(spec["run_id"]) or AdoptHandle()
        self.adopted.append((spec, h))
        return h


class Recovery(Base):
    def setUp(self):
        super().setUp()
        self.fl = AdoptLauncher()
        self.allow["codex"] = False
        self.found, self.scans = {}, []
        self.d = self.restart()

    def restart(self, **kw):
        def scan(want_wsl):
            self.scans.append(want_wsl)
            if isinstance(self.found, Exception):
                raise self.found
            return self.found
        return self.make(launcher=self.fl, scan_fn=scan, scan_every_s=0, **kw)

    def start(self, **kw):
        t = self.task(size="S", **kw)
        self.d.tick()
        return t, ops.list_runs(self.conn, t)[0]["id"]

    def test_runner_found_means_adopted_by_run_id(self):
        t, rid = self.start()
        d2 = self.restart()
        self.found = {rid: "windows"}
        self.assertEqual(d2.tick()["recovered"], [])
        self.assertIn(rid, d2.live)
        self.assertEqual(self.fl.adopted[-1][0]["run_id"], rid)
        self.assertEqual(self.events("run-adopted")[0]["data"], {"run": rid, "placement": "windows"})
        self.assertEqual(self.status(t), "running")
        self.fl.adopted[-1][1].result = res("done", DONE)
        d2.tick()
        self.assertEqual(self.status(t), "gated")

    def test_recovery_restores_account_for_polling_and_worker_limits(self):
        from drover import config, stats, usage
        cfg = config.validate({'seats': [
            {'name': 'acme-primary', 'backend': 'claude', 'director': True},
            {'name': 'acme-secondary', 'backend': 'claude', 'max_workers': 1}]})
        config.use(cfg)
        self.addCleanup(config.reset)
        primary, secondary = cfg['seats']
        runs = []
        for seat in (primary, secondary):
            tid = self.task(size='S')
            ops.set_status(self.conn, tid, 'claimed')
            ops.set_status(self.conn, tid, 'running')
            rid = ops.start_run(self.conn, tid, 'worker', 'claude')
            stats.update_run(self.conn, rid, seat=seat['name'])
            runs.append(rid)
        self.found = dict.fromkeys(runs, 'windows')
        d2 = self.restart()
        self.assertEqual(d2.recover_orphans(), [])
        expected = {'acme-primary': 1, 'acme-secondary': 1}
        self.assertEqual(d2._seat_load(), expected)
        self.assertEqual([d2.live[rid][2]['seat'] for rid in runs], list(expected))
        d2.usage_poller = usage.Poller(self.conn)
        with unittest.mock.patch.object(d2.usage_poller, 'poll') as poll:
            d2._seat_tick()
        poll.assert_called_once_with(cfg['seats'], expected)
        now = time.time()
        for seat in cfg['seats']:
            usage.manual(self.conn, seat['name'], 'seven_day', 10, now + 3600, now=now)
        order = usage.order(self.conn, cfg['seats'], now=now, seat_load=d2._seat_load())
        self.assertEqual(next(r for r in order if r['seat'] == 'acme-secondary')['reason'], 'max_workers reached')

    def test_string_id_scan_keeps_live_wsl_run_alive(self):
        t, rid = self.start(goal="platform: linux")
        self.found = {str(rid): "wsl"}
        for _ in range(dispatcher.ORPHAN_SCANS + 2):
            self.assertEqual(self.d.watch_runs(), [])
        self.assertIn(rid, self.d.live)
        self.assertEqual(self.status(t), "running")
        self.assertIsNone(self.first(t))
        self.assertNotIn(rid, self.d.missed)

    def test_recover_orphans_adopts_string_id_scan_with_found_placement(self):
        t, rid = self.start()
        self.found = {str(rid): "wsl"}
        d2 = self.restart()
        self.assertEqual(d2.recover_orphans(), [])
        self.assertIn(rid, d2.live)
        self.assertEqual(self.fl.adopted[-1][0]["placement"], "wsl")
        self.assertNotIn(rid, d2.missed)
        self.assertEqual(self.status(t), "running")

    def test_task_with_found_wsl_run_is_never_reclaimed(self):
        t, rid = self.start(goal="platform: linux")
        self.found = {str(rid): "wsl"}
        for _ in range(dispatcher.ORPHAN_SCANS + 2):
            self.assertEqual(self.d.tick()["launched"], [])
        self.assertEqual(len(self.fl.specs), 1)
        self.assertEqual(len(self.port.list_runs(t)), 1)
        claims = [e for e in self.port.events("status")
                  if e["task_id"] == t and e["data"]["to"] == "claimed"]
        self.assertEqual(len(claims), 1)
        self.assertEqual(self.status(t), "running")

    def test_terminal_status_file_finishes_the_run_even_with_no_runner(self):
        t, rid = self.start()
        h = AdoptHandle(status={"state": "exited"}, result=res("done", DONE))
        self.fl.adopt_with[rid] = h
        d2 = self.restart()
        self.found = {}
        for _ in range(4):
            d2.tick()
        self.assertEqual(ops.list_runs(self.conn, t)[0]["outcome"], "gated")
        self.assertFalse(h.cancelled)

    def first(self, t):
        return ops.list_runs(self.conn, t)[0]["outcome"]

    def test_open_run_with_no_runner_and_no_terminal_status_is_orphaned_after_two_clean_scans(self):
        t, rid = self.start()
        d2 = self.restart()
        self.found = {}
        self.assertEqual(d2.tick()["recovered"], [])          # startup scan: 1 clean scan, still running
        self.assertIsNone(self.first(t))
        self.assertEqual(d2.tick()["recovered"], [t])         # second consecutive clean scan
        self.assertEqual(self.first(t), "orphaned")
        self.assertNotIn(rid, d2.live)
        self.assertEqual(self.events("status")[-3]["data"]["reason"], "orphaned run recovered")

    def test_a_runner_seen_in_between_resets_the_count(self):
        t, rid = self.start()
        d2 = self.restart()
        self.found = {}
        d2.tick()
        self.found = {rid: "windows"}
        d2.tick()
        self.found = {}
        d2.tick()
        self.assertIsNone(self.first(t))
        d2.tick()
        self.assertEqual(self.first(t), "orphaned")

    def test_scan_error_at_startup_recovers_nothing_and_retries(self):
        t, rid = self.start()
        d2 = self.restart()
        self.found = ScanError("powershell died")
        for _ in range(3):
            self.assertEqual(d2.tick()["recovered"], [])
        self.assertEqual(self.status(t), "running")
        self.assertIsNone(ops.list_runs(self.conn, t)[0]["ended"])
        self.assertEqual(len(self.events("scan-unknown")), 1)       # logged once, not every tick
        self.assertEqual(self.fl.adopted, [])
        self.found = {rid: "windows"}
        d2.tick()
        self.assertIn(rid, d2.live)

    def test_scan_error_while_running_never_requeues(self):
        t, rid = self.start()
        self.found = {}
        self.d.tick()                                         # own run, not terminal, runner "missing" once
        self.assertEqual(self.d.missed[rid], 1)
        self.found = ScanError("wsl find failed")
        for _ in range(5):
            self.assertEqual(self.d.tick()["recovered"], [])
        self.assertEqual(self.status(t), "running")
        self.assertEqual(self.d.missed, {})
        self.found = {}
        self.d.tick()
        self.assertIsNone(self.first(t))                      # the count restarted from zero
        self.d.tick()
        self.assertEqual(self.first(t), "orphaned")

    def test_own_run_with_a_terminal_status_is_not_orphaned_when_runner_is_gone(self):
        t, rid = self.start()
        self.fl.handles[0].terminal = lambda: {"state": "exited"}
        self.found = {}
        for _ in range(4):
            self.d.tick()
        self.assertIsNone(ops.list_runs(self.conn, t)[0]["outcome"])      # the handle will deliver its result
        self.fl.handles[0].result = res("done", DONE)
        self.d.tick()
        self.assertEqual(self.status(t), "gated")

    def test_no_scan_function_means_no_orphan_decisions(self):
        t, rid = self.start()
        d = self.make(launcher=FakeLauncher(), scan_every_s=0)
        d.scan_fn = None
        self.assertEqual(d.watch_runs(), [])
        self.assertEqual(d.recover_orphans(), [])
        self.assertEqual(self.status(t), "running")

    def test_wsl_is_scanned_only_when_a_wsl_run_is_open(self):
        self.allow["codex"] = False
        self.d = self.restart(wsl_up_fn=lambda: True)
        t, rid = self.start(goal="platform: linux")
        self.scans.clear()
        d2 = self.restart()
        self.found = {rid: "wsl"}
        d2.tick()
        self.assertEqual(self.scans[0], True)
        d3 = self.restart()
        d3.live.clear()
        d3.live[rid] = (AdoptHandle(), t, {"placement": "windows", "backend": "claude"})
        self.scans.clear()
        d3.last_scan = None
        d3.watch_runs()
        self.assertEqual(self.scans, [False])

    def test_running_task_with_no_open_run_is_requeued_with_event(self):
        t = self.task()
        ops.claim_next(self.conn, {})
        ops.set_status(self.conn, t, "running", "x")
        self.assertEqual(self.d.recover_orphans(), [t])
        self.assertEqual(self.status(t), "ready")
        self.assertEqual(self.events("running-no-run")[0]["task_id"], t)


class HeadOfLine(Base):
    def setUp(self):
        super().setUp()
        self.t = 1_000_000.0
        self.no_route = set()
        self.allow["codex"] = False

        def route(task, policy, heads):
            if task["id"] in self.no_route:
                return {"backend": None, "reason": "no allowance above reserve"}
            return {"backend": "claude", "model": "haiku-5.5", "effort": "xhigh", "harness": "default"}
        self.d = self.make(route_fn=route, now_fn=lambda: self.t)

    def test_task_with_no_backend_is_skipped_so_the_next_one_launches_and_is_not_retried_this_tick(self):
        a = self.task(title="a", priority="P0")
        b = self.task(title="b", priority="P1")
        self.no_route.add(a)
        out = self.d.tick()
        self.assertEqual(out["launched"], [b])
        self.assertEqual(self.status(a), "ready")
        self.assertEqual(self.port.get_backoffs()[a]["min"], 1)
        self.assertEqual(len([e for e in self.events("status") if e["task_id"] == a and e["data"]["to"] == "claimed"]), 1)

    def test_backoff_doubles_to_thirty_minutes_and_is_cleared_on_launch(self):
        a = self.task(title="a")
        self.no_route.add(a)
        mins = []
        for _ in range(8):
            self.d.tick()
            mins.append(self.port.get_backoffs()[a]["min"])
            self.t += mins[-1] * 60 + 1
        self.assertEqual(mins, [1, 2, 4, 8, 16, 30, 30, 30])
        self.no_route.clear()
        self.assertEqual(self.d.tick()["launched"], [a])
        self.assertNotIn(a, self.port.get_backoffs())

    def test_a_task_in_backoff_is_not_claimed_and_makes_no_status_churn(self):
        a = self.task(title="a")
        self.no_route.add(a)
        self.d.tick()
        n = len(self.events("status"))
        for _ in range(5):
            self.t += 5
            self.d.tick()
        self.assertEqual(len(self.events("status")), n)
        self.t += 61
        self.no_route.clear()
        self.assertEqual(self.d.tick()["launched"], [a])

    def test_unadmitted_backend_and_missing_binary_also_back_off(self):
        a = self.task(title="a", scope_files=["a/x.cs"], priority="P0")        # dotnet -> claude
        self.allow["claude"], self.allow["codex"] = False, True
        self.d.tick()
        self.assertIn(a, self.port.get_backoffs())
        b = self.task(title="b", priority="P1")
        self.allow["claude"] = True
        self.d.unavailable[("claude", "windows")] = self.d.now()
        self.d.unavailable[("codex", "wsl")] = self.d.now()
        self.d.tick()
        self.assertEqual(self.status(b), "ready")                             # nothing admitted: not even claimed

    def test_backend_missing_on_wsl_only_defers_just_wsl_tasks(self):
        self.d.unavailable[("claude", "wsl")] = self.d.now()
        a = self.task(title="a", goal="platform: linux", priority="P0")
        b = self.task(title="b", priority="P1")
        self.d.wsl_up_fn = lambda: True
        self.assertEqual(self.d.tick()["launched"], [b])
        self.assertIn(a, self.port.get_backoffs())


class ContractRoundTrip(Base):
    """What the dispatcher writes is what the tuner parses."""

    def setUp(self):
        super().setUp()
        self.allow["codex"] = False
        self.used = [10.0, 40.0]          # seven_day, five_hour used-% (moved by the test)
        self.gate = {"ok": True, "diff_lines": 12, "duration_s": 700}
        self.d = self.make(headroom_fn=self.headroom, gate_fn=lambda t, r: self.gate,
                           resources_fn=lambda new_workers=1: {"verdict": "go", "slots": 1, "reasons": []})

    def headroom(self, plan):
        return {"windows": {"seven_day": {"remaining_pct": 100 - self.used[0]},
                            "five_hour": {"remaining_pct": 100 - self.used[1]}}}

    def one(self, result, **kw):
        t = self.task(priority="P0", **kw)
        self.d.tick()
        h = next(h for h, tid, _ in self.d.live.values() if tid == t)
        h.result = result
        self.verdict = {"verdict": "hold", "slots": 0, "reasons": []}
        self.d.resources_fn = lambda new_workers=1: self.verdict
        self.d.tick()
        self.d.resources_fn = lambda new_workers=1: GO
        return t

    def test_allowance_used_is_the_delta_and_the_tuner_reads_it(self):
        from drover import contract, tuner
        t = self.task()
        self.d.tick()
        self.used = [10.75, 41.5]
        self.finish(0, res("done", DONE))
        self.d.tick()
        run = ops.list_runs(self.conn, t)[0]
        self.assertEqual(run["allowance_used"], {"seven_day_pct": 0.75, "five_hour_pct": 1.5})
        row = [r for r in tuner._runs(self.conn)][0]
        self.assertEqual(row["pct"], 0.75)
        self.assertEqual(contract.format_allowance(self.conn.execute("select allowance_used from runs").fetchone()[0]),
                         "0.75% week, 1.5% 5h")

    def test_gate_receipt_event_and_first_pass_are_what_the_tuner_wants(self):
        from drover import contract, tuner
        t = self.task()
        self.d.tick()
        self.finish(0, res("done", DONE))
        self.d.tick()
        row = [r for r in tuner._runs(self.conn)][0]
        self.assertEqual(tuner._diff_lines(row), 12)
        self.assertEqual(contract.parse_gate_receipt(row["gate_receipt"])["ok"], True)
        ev = self.events("gate")
        self.assertEqual(ev[0]["data"], {"duration_s": 700, "red": False, "run": row["id"]})
        stats = tuner.route_stats([row], {})
        self.assertEqual(stats["worker:M"][(row["backend"], row["model"])]["first_pass_ok"], 1)

    def test_failed_gate_keeps_the_excerpt_for_the_retry_and_diff_lines_for_the_audit(self):
        from drover import contract, tuner
        self.gate = {"ok": False, "diff_lines": 8, "excerpt": "FAIL a\nFAIL b", "duration_s": 5}
        t = self.task()
        self.d.tick()
        self.finish(0, res("done", DONE))
        self.d.tick()
        run = ops.list_runs(self.conn, t)[0]
        self.assertEqual((run["outcome"], contract.receipt_diff_lines(run["gate_receipt"])), ("gate-fail", 8))
        self.assertEqual(contract.receipt_excerpt(run["gate_receipt"]), "FAIL a\nFAIL b")
        self.assertIn("FAIL b", self.fl.specs[1]["user_prompt"])           # the retry brief carries it
        self.assertEqual(self.events("gate")[0]["data"]["red"], True)
        self.assertEqual(tuner._diff_lines({"gate_receipt": run["gate_receipt"]}), 8)

    def test_tune_batch_reads_the_dispatchers_gate_events(self):
        from drover import tuner
        for _ in range(5):
            self.gate = {"ok": True, "duration_s": 700}
            self.one(res("done", DONE))
        self.assertEqual(len(self.events("gate")), 5)
        self.assertEqual(tuner.tune_batch(self.conn, 1), tuner.BATCH_DEFAULT)

    def test_every_outcome_the_dispatcher_writes_is_in_the_vocabulary_and_all_of_it_is_used(self):
        from drover import contract
        outcomes = set()
        scen = [res("done", DONE), res("done", DONE), res("no-record", "x"), res("error", "boom"),
                {**res("timeout"), "timeout": "wall"}, res("rotate"), res("blocked", BLOCKED), res("done", DONE)]
        gates = [{"ok": True}, {"ok": False, "excerpt": "x"}, None, None, None, None, None, {"needs_owner": "why"}]
        for result, gate in zip(scen, gates):
            self.gate = gate or {"ok": True}
            t = self.one(result)
            ops.set_status(self.conn, t, "cancelled", "owner")
        t = self.task(priority="P0")
        self.d.launcher = type("L", (), {"launch": lambda s, sp: (_ for _ in ()).throw(OSError("x"))})()
        self.d.tick()
        ops.set_status(self.conn, t, "cancelled", "owner")
        t, rid = self.task(priority="P0"), None
        self.d.launcher = self.fl
        self.d.tick()
        rid = ops.list_runs(self.conn, t)[0]["id"]
        self.d.live.pop(rid)
        self.d.scan_fn, self.d.scan_every_s, self.d.last_scan = (lambda w: {}), 0, None
        self.d.live[rid] = (FakeHandle(), t, {"placement": "windows", "backend": "claude"})
        self.d.watch_runs()
        self.d.watch_runs()
        for r in self.conn.execute("select outcome from runs where ended is not null"):
            outcomes.add(r[0])
        self.assertEqual(outcomes, set(contract.OUTCOMES) - set(contract.PIPELINE_OUTCOMES))
        self.assertTrue(set(contract.FAIL_OUTCOMES) < set(contract.OUTCOMES))
        self.assertEqual(set(dispatcher.FAIL_OUTCOMES), set(contract.FAIL_OUTCOMES))


class HardeningFixtures(Base):
    def setUp(self):
        super().setUp()
        self.allow["codex"] = False
        self.t = 2_000_000.0


class LaunchPersistence(HardeningFixtures):
    def test_placement_is_recorded_before_the_launcher_runs(self):
        seen = []

        class L(FakeLauncher):
            def launch(s, spec):
                seen.append([e["data"] for e in ops.events(self.conn, kind="run-launching")])
                return super().launch(spec)
        self.d = self.make(launcher=L(), wsl_up_fn=lambda: True)
        self.task(size="S", goal="platform: linux")
        self.d.tick()
        data = seen[0][0]
        self.assertEqual((data["run"], data["placement"]), (1, "wsl"))
        self.assertRegex(data["nonce"], r"^[0-9a-f]{32}$")
        self.assertEqual(self.d.live[1][2]["nonce"], data["nonce"])        # the same nonce goes to the runner

    def test_run_of_unknown_placement_is_looked_for_on_both_sides_and_adopted_from_wsl(self):
        t = self.task()
        ops.claim_next(self.conn, {})
        rid = ops.start_run(self.conn, t, "worker", "claude", "sonnet-5.5", "high")      # no launching event: a crash
        fl, scans = AdoptLauncher(), []
        d2 = self.make(launcher=fl, scan_every_s=0, scan_fn=lambda w: scans.append(w) or {rid: "wsl"})
        d2.tick()
        self.assertEqual(scans[0], True)
        self.assertEqual(fl.adopted[0][0]["placement"], "wsl")
        d2.tick()
        self.assertIn(True, scans[1:])                      # still a WSL scan while the placement is unknown/wsl
        self.assertIsNone(ops.list_runs(self.conn, t)[0]["ended"])

    def test_run_of_unknown_placement_is_orphaned_only_after_scanning_both_sides(self):
        t = self.task()
        ops.claim_next(self.conn, {})
        rid = ops.start_run(self.conn, t, "worker", "claude", "sonnet-5.5", "high")
        scans = []
        d2 = self.make(launcher=AdoptLauncher(), scan_every_s=0, scan_fn=lambda w: scans.append(w) or {})
        d2.tick()
        d2.tick()
        self.assertEqual(scans[:2], [True, True])
        self.assertEqual(ops.list_runs(self.conn, t)[0]["outcome"], "orphaned")


class LaunchCancel(HardeningFixtures):
    def setUp(self):
        super().setUp()
        from drover.launch import LaunchError

        class L(FakeLauncher):
            stops = []

            def launch(s, spec):
                raise LaunchError("runner still starting", cancel_requested=True)

            def stop_run(s, rid, nonce=None):
                s.stops.append((rid, nonce))
        self.fl = L()
        self.found = {}
        self.d = self.make(launcher=self.fl, now_fn=lambda: self.t, scan_every_s=0,
                           scan_fn=lambda w: self._scan())

    def _scan(self):
        if isinstance(self.found, Exception):
            raise self.found
        return self.found

    def test_cancelled_launch_is_not_requeued_until_a_scan_shows_no_runner(self):
        t = self.task(size="S")
        self.d.tick()
        run = ops.list_runs(self.conn, t)[0]
        self.assertEqual(run["outcome"], "launch-error")
        self.assertIn("cancel-requested", run["note"])
        self.assertEqual(self.status(t), "running")                 # NOT requeued
        self.assertEqual(self.events("launch-cancel")[0]["data"]["run"], run["id"])
        self.found = {run["id"]: "windows"}                         # the runner did come up
        self.d.tick()
        self.assertEqual(self.status(t), "running")
        self.assertEqual(self.fl.stops, [(run["id"], self.events("run-launching")[0]["data"]["nonce"])])
        self.found = ScanError("unknown")
        self.d.tick()
        self.assertEqual(self.status(t), "running")                 # unknown is never "gone"
        self.found = {}
        self.d.fl = None
        self.d.launcher = FakeLauncher()
        self.d.tick()
        self.assertEqual(len([e for e in self.events("status") if e["task_id"] == t
                              and e["data"].get("reason", "").startswith("cancelled launch")]), 1)
        self.assertEqual(len(ops.list_runs(self.conn, t)), 2)        # requeued, then launched again

    def test_after_a_hub_restart_a_cancelled_launch_is_still_not_requeued_blindly(self):
        t = self.task(size="S")
        self.d.tick()
        rid = ops.list_runs(self.conn, t)[0]["id"]
        self.found = {rid: "windows"}
        d2 = self.make(launcher=FakeLauncher(), now_fn=lambda: self.t, scan_every_s=0, scan_fn=lambda w: self._scan())
        d2.tick()
        self.assertEqual(self.status(t), "running")
        self.assertIn(rid, d2.cancelling)
        self.assertEqual(d2.cancelling[rid][2], self.events("run-launching")[0]["data"]["nonce"])   # restored from the event
        self.found = {}
        d2.tick()
        self.assertNotIn(rid, d2.cancelling)


class FinishRetry(HardeningFixtures):
    def test_failing_gate_keeps_the_run_live_and_never_runs_the_gate_twice(self):
        calls = []

        def gate(task, result):
            calls.append(1)
            raise RuntimeError("gate bug")
        d = self.make(gate_fn=gate)
        t = self.task()
        d.tick()
        d.launcher.handles[0].result = res("done", DONE)
        d.tick()
        self.assertEqual(len(d.live), 1)                            # still followed
        self.assertEqual(self.events("finish-error")[0]["data"]["try"], 1)
        d.tick()
        self.assertEqual(calls, [1])                                # not run again
        self.assertEqual(ops.list_runs(self.conn, t)[0]["outcome"], "unverified")
        self.assertEqual(self.status(t), "needs-owner")
        self.assertEqual(d.live, {})

    def test_gate_done_is_reused_when_a_later_step_fails(self):
        calls = []
        d = self.make(gate_fn=lambda t, r: calls.append(1) or {"ok": True})
        t = self.task()
        d.tick()
        d.launcher.handles[0].result = res("done", DONE)
        orig = self.port.set_status
        boom = [True]

        def flaky(*a, **k):
            if k.get("reason") == "gate passed" and boom[0]:
                boom[0] = False
                raise RuntimeError("db busy")
            return orig(*a, **k)
        self.port.set_status = flaky
        d.tick()
        self.assertEqual(len(d.live), 1)
        d.tick()
        self.assertEqual(calls, [1])

    def test_three_failures_send_the_task_to_the_owner_with_the_error(self):
        d = self.make()
        t = self.task()
        d.tick()
        d.launcher.handles[0].result = res("done", DONE)
        d._finish = lambda *a: (_ for _ in ()).throw(RuntimeError("persistent"))
        for _ in range(3):
            d.tick()
        self.assertEqual(d.live, {})
        self.assertEqual(self.status(t), "needs-owner")
        self.assertEqual([e["data"]["try"] for e in self.events("finish-error") if "try" in e["data"]], [1, 2, 3])
        reason = [e["data"].get("reason", "") for e in self.events("status") if e["data"].get("to") == "needs-owner"][-1]
        self.assertIn("persistent", reason)


class Polish(HardeningFixtures):
    def test_platform_needs_a_left_boundary(self):
        pf = dispatcher.placement_for
        c = {"backend": "claude"}
        self.assertEqual(pf({"title": "cross-platform: linux and mac", "goal": ""}, c), "windows")
        self.assertEqual(pf({"title": "x_platform: linux", "goal": ""}, c), "windows")
        self.assertEqual(pf({"title": "(platform: linux)", "goal": ""}, c), "wsl")

    def test_unavailable_expires_after_ten_minutes(self):
        d = self.make(now_fn=lambda: self.t)
        d.unavailable[("claude", "windows")] = self.t
        self.assertTrue(d._backend_down("claude"))
        self.t += dispatcher.UNAVAILABLE_S + 1
        self.assertFalse(d._backend_down("claude"))
        self.assertEqual(d.unavailable, {})

    def test_backoff_entries_of_non_ready_tasks_are_pruned(self):
        d = self.make(now_fn=lambda: self.t)
        a = self.task(title="a")
        self.port.set_backoff(a, self.t + 999, 4)
        ops.set_status(self.conn, a, "cancelled", "owner")
        d.tick()
        self.assertEqual(self.port.get_backoffs(), {})

    def test_wsl_is_rechecked_next_tick_after_a_boot_not_after_the_backoff(self):
        up = [False]
        d = self.make(now_fn=lambda: self.t, wsl_up_fn=lambda: up[0])
        t = self.task(size="S", goal="platform: linux")
        d.tick()
        self.assertIn(t, self.port.get_backoffs())
        d.tick()
        self.assertEqual(self.fl.specs, [])
        up[0] = True                                         # the watchdog booted it; no time has passed
        self.assertEqual(d.tick()["launched"], [t])


class Briefs(unittest.TestCase):
    T = {"id": "T1", "title": "Do it", "goal": "g", "acceptance": ["a"], "size": "S", "scope_files": ["a.py:10-20"]}

    def test_review_relies_on_the_gate_and_forbids_write_commands(self):
        sha = "a" * 40
        b = brief.assemble_review(self.T, "+fix", sha, "main")
        u = b["user_prompt"]
        self.assertIn(f"Drover's gate already ran the repo's test command on this exact SHA ({sha}) and passed", u)
        self.assertIn("must NOT run tests, builds or other commands that need write access", u)
        self.assertIn("Acceptance items about tests passing are satisfied by the gate", u)
        self.assertIn("Checks you cannot perform are not grounds for HOLD; mention them in note", u)

    def test_pinned_system_only_and_order(self):
        pin = brief.pinned_text()
        b = brief.assemble(self.T, retry_excerpt="boom", handoff="HAND", notes=["NOTE"])
        self.assertEqual(b["system_append"], pin)
        self.assertNotIn(pin.splitlines()[0], b["user_prompt"])
        u = b["user_prompt"]
        order = [u.index(x) for x in ("## Task T1", "## Scope", "boom", "HAND", "NOTE", '"status"')]
        self.assertEqual(order, sorted(order))
        self.assertIn("a.py:10-20", u)

    def test_pinned_file_limits(self):
        self.assertLessEqual(len(brief.pinned_text().splitlines()), 40)

    def test_excerpt_capped_at_60_lines(self):
        b = brief.assemble(self.T, retry_excerpt="\n".join(f"line{i}" for i in range(500)))
        self.assertIn("line499", b["user_prompt"])
        self.assertNotIn("line439", b["user_prompt"])
        self.assertIn("line440", b["user_prompt"])

    def test_budgets(self):
        big = "x" * 40000
        with self.assertRaises(brief.BriefTooLarge):
            brief.assemble({**self.T, "goal": big})
        m = brief.assemble({**self.T, "size": "M", "goal": "y" * 20000})
        self.assertLessEqual(m["tokens"], 15000)
        with self.assertRaises(brief.BriefTooLarge):
            brief.assemble({**self.T, "size": "M", "goal": "y" * 50000})
        s = brief.assemble(self.T, retry_excerpt="\n".join("z" * 400 for _ in range(60)))
        self.assertLessEqual(s["tokens"], 6000)


if __name__ == "__main__":
    unittest.main()
