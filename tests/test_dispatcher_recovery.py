import time
import unittest
import unittest.mock

from drover import dispatcher
from drover.ledger import ops
from tests.ledger_fixture import claim as fixture_claim, stage as fixture_stage
from drover.runner import ScanError

from tests.test_dispatcher import AdoptHandle, AdoptLauncher, Base, DONE, res

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
        return self.make(launcher=self.fl, scan=scan, scan_every_s=0, **kw)

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
            fixture_claim(self.conn, tid)
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
        self.d.tick()
        self.assertEqual(self.status(t), "gated")


    def test_wsl_is_scanned_only_when_a_wsl_run_is_open(self):
        self.allow["codex"] = False
        self.d = self.restart(wsl_up=lambda: True)
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
        rid = ops.start_run(self.conn, t, "worker", "local")
        ops.finish_run(self.conn, rid, "error")
        self.assertEqual(self.d.recover_orphans(), [t])
        self.assertEqual(self.status(t), "ready")
        self.assertEqual(self.events("running-no-run")[0]["task_id"], t)



class HeadOfLine(Base):
    def setUp(self):
        super().setUp()
        self.t = 1_700_000_000.0
        self.no_route = set()
        self.allow["codex"] = False

        def route(task, policy, **kw):
            if task["id"] in self.no_route:
                return {"backend": None, "reason": "no allowance above reserve"}
            return {"backend": "claude", "model": "haiku-5.5", "effort": "xhigh", "harness": "default"}
        self.d = self.make(route=route, now=lambda: self.t)

    def test_task_with_no_backend_is_skipped_so_the_next_one_launches_and_is_not_retried_this_tick(self):
        a = self.task(title="a", priority="P0")
        b = self.task(title="b", priority="P1")
        self.no_route.add(a)
        out = self.d.tick()
        self.assertEqual(out["launched"], [b])
        self.assertEqual(self.status(a), "ready")
        self.assertEqual(next(row["data"]["minutes"] for row in self.port.waiting.pending(("task", a)) if row["kind"] == "deadline"), 1)
        self.assertEqual(len([e for e in self.events("status") if e["task_id"] == a and e["data"]["to"] == "claimed"]), 1)

    def test_backoff_doubles_to_thirty_minutes_and_is_cleared_on_launch(self):
        a = self.task(title="a")
        self.no_route.add(a)
        mins = []
        for _ in range(8):
            self.d.tick()
            mins.append(next(row["data"]["minutes"] for row in self.port.waiting.pending(("task", a)) if row["kind"] == "deadline"))
            self.t += mins[-1] * 60 + 1
        self.assertEqual(mins, [1, 2, 4, 8, 16, 30, 30, 30])
        self.no_route.clear()
        self.assertEqual(self.d.tick()["launched"], [a])
        self.assertFalse([row for row in self.port.waiting.pending(("task", a)) if row["kind"] == "deadline"])

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
        a = self.task(title="a", scope_files=["a/source.txt"], priority="P0")        # highest-priority task
        self.allow["claude"], self.allow["codex"] = False, True
        self.d.tick()
        self.assertTrue([row for row in self.port.waiting.pending(("task", a)) if row["kind"] == "deadline"])
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
        self.d.launcher.wsl_up = lambda: True
        self.assertEqual(self.d.tick()["launched"], [b])
        self.assertTrue([row for row in self.port.waiting.pending(("task", a)) if row["kind"] == "deadline"])
