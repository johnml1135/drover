import json
import time

from drover import dispatcher, launch, repos
from drover.ledger import ops

from tests.test_dispatcher import Base, DONE, FakeLauncher, SPEC, res


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
        self.assertEqual(pf(plain, {"backend": "claude", "platform": "linux"}), "wsl")   # the router may override
        self.assertEqual(pf({**plain, "platform": "linux"}, {"backend": "claude", "platform": "windows"}), "windows")
        self.assertEqual(pf({**plain, "scope_files": ["a/source.txt"]}, codex, None, ["windows", "wsl"]), "wsl")
        self.assertEqual(pf(plain, claude, "/home/u/wt"), "wsl")                    # only WSL can reach it
        self.assertEqual(pf(plain, claude, "/mnt/r/repos/wt"), "windows")


    def test_an_ext4_worktree_cannot_be_handed_to_a_windows_route(self):
        prog = repos.normalize({"name": "p", "repo": "/r", "scripts": {"test": "t.ps1"}})
        task, ext4 = {"title": "t", "goal": "g"}, "/home/u/work/p.worktrees/drover-T"
        self.assertEqual(dispatcher.place(task, {"backend": "claude", "seat": "claude"}, ext4, prog), ("wsl", None))
        got, why = dispatcher.place(task, {"backend": "claude", "seat": "claude", "platform": "windows"}, ext4, prog)
        self.assertIsNone(got)
        self.assertIn("WSL filesystem", why)
        self.assertEqual(dispatcher.place(task, {"backend": "claude", "platform": "windows"}, "/mnt/h/wt", prog)[0],
                         "windows")

    def test_dispatch_records_placement_on_spec_and_event(self):
        self.allow["codex"] = False
        self.task(size="S", goal="platform: linux")
        self.d = self.make(wsl_up=lambda: True)
        self.d.tick()
        self.assertEqual(self.fl.specs[0]["placement"], "wsl")
        self.assertEqual(self.events("run-process")[0]["data"]["placement"], "wsl")

    def test_wsl_task_with_wsl_down_defers_and_tells_the_watchdog(self):
        self.allow["codex"] = False
        t = self.task(size="S", goal="platform: linux")
        w = self.task(size="S", priority="P4")
        self.d = self.make(wsl_up=lambda: False)
        out = self.d.tick()
        self.assertEqual(self.status(t), "ready")
        self.assertIn(w, out["launched"])                        # the Windows task behind it is not blocked
        note = json.loads(ops.get_setting(self.conn, "_wsl_needed"))
        self.assertEqual(note["task"], t)
        self.assertEqual(next(row["data"]["minutes"] for row in self.port.waiting.pending(("task", t)) if row["kind"] == "deadline"), 1)

    def test_windows_task_never_asks_for_wsl(self):
        self.allow["codex"] = False
        self.task(size="S")
        self.d = self.make(wsl_up=lambda: self.fail("WSL must not be probed"))
        self.d.tick()
        self.assertIsNone(ops.get_setting(self.conn, "_wsl_needed"))
