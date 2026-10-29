import unittest
import unittest.mock

from drover import brief, dispatcher
from drover.ledger import ops
from drover.runner import ScanError

from tests.test_dispatcher import AdoptLauncher, DONE, FakeLauncher, HardeningFixtures, res


class LaunchPersistence(HardeningFixtures):
    def test_placement_is_recorded_before_the_launcher_runs(self):
        seen = []

        class L(FakeLauncher):
            def launch(s, spec):
                seen.append([e["data"] for e in ops.events(self.conn, kind="run-launching")])
                return super().launch(spec)
        self.d = self.make(launcher=L(), wsl_up=lambda: True)
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
        d2 = self.make(launcher=fl, scan_every_s=0, scan=lambda w: scans.append(w) or {rid: "wsl"})
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
        d2 = self.make(launcher=AdoptLauncher(), scan_every_s=0, scan=lambda w: scans.append(w) or {})
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
        self.d = self.make(launcher=self.fl, now=lambda: self.t, scan_every_s=0,
                           scan=lambda w: self._scan())

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
        self.d.launcher.scan = lambda want: self._scan()
        self.d.tick()
        self.assertEqual(len([e for e in self.events("status") if e["task_id"] == t
                              and e["data"].get("reason", "").startswith("cancelled launch")]), 1)
        self.assertEqual(len(ops.list_runs(self.conn, t)), 2)        # requeued, then launched again

    def test_after_a_hub_restart_a_cancelled_launch_is_still_not_requeued_blindly(self):
        t = self.task(size="S")
        self.d.tick()
        rid = ops.list_runs(self.conn, t)[0]["id"]
        self.found = {rid: "windows"}
        d2 = self.make(launcher=FakeLauncher(), now=lambda: self.t, scan_every_s=0, scan=lambda w: self._scan())
        d2.tick()
        self.assertEqual(self.status(t), "running")
        self.assertIn(rid, d2.cancelling)
        self.assertEqual(d2.cancelling[rid][2], self.events("run-launching")[0]["data"]["nonce"])   # restored from the event
        self.found = {}
        d2.tick()
        self.assertNotIn(rid, d2.cancelling)



class FinishRetry(HardeningFixtures):


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
        d = self.make(now=lambda: self.t)
        d.unavailable[("claude", "windows")] = self.t
        self.assertTrue(d._backend_down("claude"))
        self.t += dispatcher.UNAVAILABLE_S + 1
        self.assertFalse(d._backend_down("claude"))
        self.assertEqual(d.unavailable, {})

    def test_backoff_entries_of_non_ready_tasks_are_pruned(self):
        d = self.make(now=lambda: self.t)
        a = self.task(title="a")
        from drover.ledger.waiting import DEADLINE, cause_key
        self.port.waiting.wait(("task", a), DEADLINE, cause_key(a, "worker", "admission"),
                               cause=cause_key("deadline", a, "worker", "admission"), timeout_at=self.t + 999,
                               data={"role": "worker", "reason_code": "admission", "minutes": 4, "mode": "preserve"})
        ops.set_status(self.conn, a, "cancelled", "owner")
        d.tick()
        self.assertEqual(self.port.waiting.pending(), [])

    def test_wsl_is_rechecked_next_tick_after_a_boot_not_after_the_backoff(self):
        up = [False]
        d = self.make(now=lambda: self.t, wsl_up=lambda: up[0])
        t = self.task(size="S", goal="platform: linux")
        d.tick()
        self.assertTrue([row for row in self.port.waiting.pending(("task", t)) if row["kind"] == "deadline"])
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

    def test_finish_block_says_the_gate_runs_full_suites_and_sandbox_limits_are_not_blockers(self):
        text = brief.ACTION_RECORD
        for phrase in ("full WSL and Windows suites", "targeted tests only", "not a blocker", "approved design"):
            self.assertIn(phrase, text)

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
