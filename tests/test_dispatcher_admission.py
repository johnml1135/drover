import json
import time

from drover import launch
from drover.dispatcher_recovery import _epoch
from drover.ledger import ops
from drover.port import HttpPort

from tests.test_dispatcher import Base, DONE, GO, res


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
        t = self.task(scope_files=["a/source.txt"])      # neither backend is admitted
        self.allow["claude"] = False
        self.allow["codex"] = False
        self.d.tick()
        self.assertEqual(self.status(t), "ready")

    def test_undispatched_ready_tasks_are_counted_by_reason_in_the_heartbeat(self):
        beat = lambda: json.loads(ops.get_setting(self.conn, "dispatcher_heartbeat")).get("not_dispatched")
        for _ in range(3):
            self.task()
        self.verdict = {"verdict": "queue", "slots": 0, "reasons": ["r"]}
        self.d.tick()
        self.assertEqual(beat(), {"resources-queue": 3})
        self.verdict = GO
        self.d.tick()
        self.assertEqual(len(self.fl.specs), 3)
        self.assertIsNone(beat())                        # everything dispatched: nothing to explain
        self.task()
        self.allow = {"claude": False, "codex": False}
        self.d.tick()
        self.assertEqual(beat(), {"backend-not-admitted": 1})

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

    def test_real_router_picks_within_tiers_and_effort_rules(self):
        self.task(size="S", priority="P0")
        self.task(size="M", priority="P1")
        self.d.tick()
        by = {sp["task"]["size"]: sp for sp in self.fl.specs}
        self.assertIn((by["S"]["backend"], by["S"]["model"]), {("claude", "haiku-5.5"), ("codex", "gpt-6-luna")})
        self.assertIn((by["M"]["backend"], by["M"]["model"]), {("claude", "sonnet-5.5"), ("codex", "gpt-6.1-sol")})
        self.assertEqual(by["S"]["effort"], "xhigh")          # Haiku and Luna always xhigh

    def test_router_gets_policy_platforms_load_and_measured_seat_readings(self):
        seen = []

        def fake(task, policy, *, seat_load, platforms, seat_headroom, seat_order):
            seen.append((task["id"], policy, seat_load, platforms, seat_headroom, seat_order))
            return {"backend": "claude", "model": "sonnet-5.5", "effort": "max", "harness": "default"}
        ops.add_policy_version(self.conn, "v1", {"routes": {"x": []}})
        self.allow["codex"] = False
        self.d = self.make(route=fake)
        self.task(size="M")
        self.d.tick()
        self.assertEqual(seen[0][1], {"routes": {"x": []}})
        self.assertEqual(seen[0][2], {})
        self.assertEqual(seen[0][3], self.pipe.prog["run"]["platforms"])
        self.assertEqual(seen[0][4]["codex"]["remaining_pct"], 0)
        self.assertGreater(seen[0][4]["claude"]["remaining_pct"], 0)
        self.assertEqual({row["seat"] for row in seen[0][5]}, {"claude", "codex"})
        self.assertEqual(self.fl.specs[0]["effort"], "max")   # non-cheap models keep the router's effort

    def test_router_with_no_backend_unclaims(self):
        t = self.task()
        self.d = self.make(route=lambda t, p, **kw: {"backend": None, "reason": "no allowance"})
        self.assertEqual(self.d.tick()["launched"], [])
        self.assertEqual(self.status(t), "ready")


    def test_model_ids_mapped_for_cli(self):
        argv, _ = launch.build_argv({"backend": "claude", "model": "haiku-5.5", "effort": "xhigh",
                                     "system_append": "", "user_prompt": ""})
        self.assertEqual(argv[argv.index("--model") + 1], "claude-haiku-5-5")
        self.assertEqual(launch.MODEL_IDS, {"haiku-5.5": "claude-haiku-5-5", "sonnet-5.5": "claude-sonnet-5-5",
                                            "opus-5.5": "claude-opus-5-5"})
        argv, _ = launch.build_argv({"backend": "codex", "model": "gpt-6.1-sol", "effort": "xhigh",
                                     "system_append": "", "user_prompt": "", "cwd": "/tmp/acme-app"})
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-6.1-sol")



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
        self.assertEqual(launch.parse_action_record("blah\n```json\n" + DONE + "\n```")["sha"], "a" * 40)
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
        claimed_at = _epoch(ops.get_task(self.conn, t)["updated"])
        self.assertEqual(self.make(now=lambda: claimed_at + 299).requeue_stale_claims(), [])
        d = self.make(now=lambda: time.time() + 301)
        self.assertEqual(d.requeue_stale_claims(), [t])
        self.assertEqual(self.status(t), "ready")
        self.assertEqual(self.events("claim-expired")[0]["task_id"], t)
        self.assertTrue(ops.get_task(self.conn, t)["approved_at"])
        t2 = ops.claim_next(self.conn, {})["id"]
        out = d.tick()                                      # on a tick too (and it may be reclaimed after)
        self.assertIn(t2, out["recovered"])

    def test_fresh_claim_and_claim_with_open_run_untouched(self):
        t = self.claim()
        self.assertEqual(self.make(now=lambda: time.time() + 200).requeue_stale_claims(), [])
        self.assertEqual(self.status(t), "claimed")
        ops.start_run(self.conn, t, "worker", "claude", "m", "e")
        self.assertEqual(self.make(now=lambda: time.time() + 400).requeue_stale_claims(), [])
