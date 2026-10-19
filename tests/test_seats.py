"""Seat routing: priority, headroom, max_workers, the director's protected reserve, platforms, and the seat in the spec."""
import random
import unittest

from drover import config, dispatcher, launch, route, runner
from tests import test_dispatcher

CLAUDE = {"name": "claude", "backend": "claude", "priority": 1, "max_workers": 0, "reserve_pct": 10.0,
          "director_reserve_pct": 25.0, "director": True, "placement": "windows", "binary": "claude", "config_dir": ""}
CODEX = {"name": "codex", "backend": "codex", "priority": 1, "max_workers": 0, "reserve_pct": 10.0,
         "director_reserve_pct": 0.0, "director": False, "placement": "wsl", "binary": "codex", "config_dir": ""}
WORKER = {"role": "worker", "size": "M"}
LOTS = {"remaining_pct": 90.0, "hours_to_reset": 100.0}


def heads(claude=LOTS, codex=LOTS):
    return {"claude": claude, "codex": codex}


def pick(seats, task=WORKER, h=None, seeds=range(60), **kw):
    return [route.choose(task, {}, h or heads(), seed=s, seats=seats, **kw) for s in seeds]


class Priority(unittest.TestCase):
    def test_equal_priority_keeps_the_old_headroom_split(self):
        picks = {r["backend"] for r in pick([CLAUDE, CODEX])}
        self.assertEqual(picks, {"claude", "codex"})

    def test_lower_priority_number_is_tried_first(self):
        for r in pick([CLAUDE, {**CODEX, "priority": 2}]):
            self.assertEqual((r["backend"], r["seat"]), ("claude", "claude"))
        for r in pick([{**CLAUDE, "priority": 2}, CODEX]):
            self.assertEqual((r["backend"], r["seat"]), ("codex", "codex"))

    def test_falls_to_the_next_priority_when_the_first_has_no_allowance(self):
        spent = {"remaining_pct": 5.0, "hours_to_reset": 10.0}
        for r in pick([CLAUDE, {**CODEX, "priority": 2}], h=heads(claude=spent)):
            self.assertEqual(r["backend"], "codex")
        self.assertIsNone(route.choose(WORKER, {}, heads(claude=spent, codex=spent), seed=1,
                                       seats=[CLAUDE, {**CODEX, "priority": 2}])["backend"])

    def test_same_backend_seats_pick_by_priority_then_load(self):
        a, b = {**CLAUDE, "name": "a", "priority": 2}, {**CLAUDE, "name": "b", "director": False, "priority": 1,
                                                       "director_reserve_pct": 0.0}
        self.assertEqual(route.choose(WORKER, {}, heads(), seed=1, seats=[a, b])["seat"], "b")
        tie = [{**a, "priority": 1}, b]
        self.assertEqual(route.choose(WORKER, {}, heads(), seed=1, seats=tie, seat_load={"b": 2, "a": 1})["seat"], "a")
        self.assertEqual(route.choose(WORKER, {}, heads(), seed=1, seats=tie, seat_load={"a": 2, "b": 1})["seat"], "b")


class MaxWorkers(unittest.TestCase):
    def test_a_full_seat_is_skipped(self):
        seats = [{**CLAUDE, "max_workers": 2}, CODEX]
        picks = {r["backend"] for r in pick(seats, seat_load={"claude": 2})}
        self.assertEqual(picks, {"codex"})
        picks = {r["backend"] for r in pick(seats, seat_load={"claude": 1})}
        self.assertEqual(picks, {"claude", "codex"})

    def test_all_seats_full_gives_a_reason(self):
        seats = [{**CLAUDE, "max_workers": 1}, {**CODEX, "max_workers": 1}]
        r = route.choose(WORKER, {}, heads(), seed=1, seats=seats, seat_load={"claude": 1, "codex": 1})
        self.assertIsNone(r["backend"])
        self.assertIn("max_workers", r["reason"])

    def test_zero_means_no_limit(self):
        r = route.choose(WORKER, {}, heads(), seed=1, seats=[CLAUDE], seat_load={"claude": 50})
        self.assertEqual(r["backend"], "claude")


class DirectorReserve(unittest.TestCase):
    def test_workers_leave_the_directors_reserve_but_the_director_may_use_it(self):
        near = {"remaining_pct": 34.0, "hours_to_reset": 10.0}          # reserve 10 + director reserve 25 = 35
        self.assertIsNone(route.choose(WORKER, {}, heads(claude=near, codex=None), seed=1, seats=[CLAUDE, CODEX])["backend"])
        d = route.choose({"role": "director"}, {}, heads(claude=near, codex=None), seed=1, seats=[CLAUDE, CODEX])
        self.assertEqual((d["backend"], d["model"]), ("claude", "opus-5.5"))
        ok = {"remaining_pct": 40.0, "hours_to_reset": 10.0}
        self.assertEqual(route.choose(WORKER, {}, heads(claude=ok, codex=None), seed=1, seats=[CLAUDE, CODEX])["backend"],
                         "claude")

    def test_the_reserve_follows_the_seats(self):
        big = {**CLAUDE, "director_reserve_pct": 50.0, "reserve_pct": 5.0}
        h = heads(claude={"remaining_pct": 54.0, "hours_to_reset": 10.0}, codex=None)
        self.assertIsNone(route.choose(WORKER, {}, h, seed=1, seats=[big, CODEX])["backend"])
        h = heads(claude={"remaining_pct": 57.0, "hours_to_reset": 10.0}, codex=None)
        self.assertEqual(route.choose(WORKER, {}, h, seed=1, seats=[big, CODEX])["backend"], "claude")

    def test_director_and_planner_run_only_on_the_director_seat(self):
        other = {**CLAUDE, "name": "second", "director": False, "director_reserve_pct": 0.0, "priority": 0}
        for role in ("director", "planner"):
            r = route.choose({"role": role}, {}, heads(), seed=1, seats=[other, CLAUDE, CODEX])
            self.assertEqual(r["seat"], "claude")
        self.assertEqual(route.choose(WORKER, {}, heads(), seed=1, seats=[other, CLAUDE])["seat"], "second")

    def test_director_work_falls_back_claude_then_personal_then_codex(self):
        cfg = config.validate({"seats": [
            {"name": "codex", "backend": "codex", "priority": 1},
            {"name": "claude", "backend": "claude", "priority": 2, "director": True},
            {"name": "claude-personal", "backend": "claude", "priority": 3}]})
        config.use(cfg)
        self.addCleanup(config.reset)
        self.assertEqual([s["name"] for s in config.director_seats()], ["claude", "claude-personal", "codex"])
        full, gone = {"remaining_pct": 80.0}, {"remaining_pct": 0.0}
        for h, want in (({"claude": full, "claude-personal": full, "codex": full}, "claude"),
                        ({"claude": gone, "claude-personal": full, "codex": full}, "claude-personal"),
                        ({"claude": gone, "claude-personal": gone, "codex": full}, "codex")):
            r = route.choose({"role": "planner"}, {}, seats=cfg["seats"], seat_headroom=h)
            self.assertEqual(r["seat"], want)
            self.assertEqual(config.seat_env(config.seat_by_name(r["seat"])).keys(),
                             {"CODEX_HOME"} if want == "codex" else {"CLAUDE_CONFIG_DIR"})

    def test_the_five_hour_formula_gates_a_seat_by_reset_time(self):
        seat = {**CLAUDE, "five_hour_base_pct": 5.0, "five_hour_per_hour_pct": 5.0}
        def h(left, hours): return {"remaining_pct": left, "windows": {"five_hour": {"remaining_pct": left, "hours_to_reset": hours}}}
        self.assertTrue(route._seat_admits(seat, h(13.0, 1.5), True))
        self.assertFalse(route._seat_admits(seat, h(12.0, 1.5), True))
        self.assertTrue(route._seat_admits(seat, h(6.0, 0.0), True))

    def test_the_allowance_module_reads_the_same_reserves(self):
        from drover import allowance
        with_cfg = config.load(env={}, home_path="/nonexistent", overlay="/nonexistent")
        config.use(with_cfg)
        self.addCleanup(config.reset)
        self.assertEqual(allowance.defaults()["claude"]["director_reserve_pct"], 25.0)
        self.assertEqual(allowance.defaults()["codex"]["director_reserve_pct"], 0.0)


class Platforms(unittest.TestCase):
    def test_windows_dotnet_codex_review_can_use_a_wsl_seat(self):
        task = {"role": "review", "platform": "windows", "scope_files": ["src/app.cs"]}
        policy = {"routes": {"review": [{"backend": "codex", "model": "gpt-6.1-sol"}]}}
        r = route.choose(task, policy, heads(), seed=1, seats=[CLAUDE, CODEX],
                         platforms=["windows", "wsl"])
        self.assertEqual((r["backend"], r["seat"]), ("codex", "codex"))
        self.assertEqual(dispatcher.place(task, r, r"R:\repos\acme-app", {
            "run": {"platforms": ["windows", "wsl"]}}), ("wsl", None))

    def test_windows_dotnet_review_keeps_seat_and_allowance_limits(self):
        task = {"role": "review", "platform": "windows", "scope_files": ["src/app.cs"]}
        policy = {"routes": {"review": [{"backend": "codex", "model": "gpt-6.1-sol"}]}}
        for reason, h, load, platforms in (
                ("max_workers", heads(), {"codex": 1}, ["windows", "wsl"]),
                ("platform-compatible", heads(), {}, ["windows"]),
                ("allowance", heads(codex={"remaining_pct": 5}), {}, ["windows", "wsl"])):
            with self.subTest(reason=reason):
                r = route.choose(task, policy, h, seed=1, seats=[{**CODEX, "max_workers": 1}],
                                 seat_load=load, platforms=platforms)
                self.assertIsNone(r["backend"])
                self.assertIn(reason, r["reason"])

    def test_seats_that_cannot_run_on_the_repos_platforms_are_not_drawn(self):
        for r in pick([CLAUDE, CODEX], platforms=["windows"]):
            self.assertEqual(r["backend"], "claude")                    # a codex seat is WSL only
        for r in pick([CLAUDE, CODEX], platforms=["wsl"]):
            self.assertIn(r["backend"], ("claude", "codex"))            # a Windows Claude seat may also run in WSL
        self.assertEqual({r["backend"] for r in pick([CLAUDE, CODEX], platforms=["wsl"])}, {"claude", "codex"})

    def test_a_wsl_claude_seat_is_wsl_only(self):
        self.assertEqual(route.seat_placements({**CLAUDE, "placement": "wsl"}), ("wsl",))
        self.assertEqual(route.seat_placements(CLAUDE), ("windows", "wsl"))
        self.assertEqual(route.seat_placements(CODEX), ("wsl",))
        r = route.choose(WORKER, {}, heads(codex=None), seed=1, seats=[{**CLAUDE, "placement": "wsl"}], platforms=["windows"])
        self.assertIsNone(r["backend"])


class SeatInTheLaunch(unittest.TestCase):
    def test_binary_and_config_dir_reach_the_worker(self):
        spec = {"backend": "claude", "model": "sonnet-5.5", "effort": "high", "system_append": "S", "user_prompt": "U",
                "cwd": "/tmp/acme-app",
                "binary": "claude-b", "env": {"CLAUDE_CONFIG_DIR": "C:\\acct", "PATH": "no", "CODEX_HOME": ""}}
        argv, _ = launch.build_argv(spec)
        self.assertEqual(argv[0], "claude-b")
        self.assertEqual(launch.seat_env(spec), {"CLAUDE_CONFIG_DIR": "C:\\acct"})       # only the two names, nothing empty
        self.assertEqual(launch.build_argv({**spec, "backend": "codex", "binary": "codex-b"})[0][0], "codex-b")

    def test_wsl_runner_command_carries_the_env_through_usr_bin_env(self):
        cmd = runner.wsl_argv("Ubuntu", ["--run-id", "1", "--runs-dir", "C:\\x", "--", "claude"], cd="C:\\d",
                              env={"CLAUDE_CONFIG_DIR": "C:\\acct"})
        i = cmd.index("--exec")
        self.assertEqual(cmd[i + 1:i + 4], ["/usr/bin/env", "CLAUDE_CONFIG_DIR=/mnt/c/acct", "python3"])
        self.assertEqual(cmd[-1], "claude")
        with self.assertRaises(ValueError):
            runner.wsl_argv("Ubuntu", ["--"], env={"bad name": "x"})
        self.assertEqual(runner.wsl_argv("Ubuntu", ["--"])[3:5], ["--exec", "python3"])

    def test_windows_command_gets_the_env(self):
        lr = launch.RunnerLauncher(runs_dir="C:\\r", distro="Ubuntu", windows=False, which=lambda n: n)
        import tempfile
        from pathlib import Path
        spec = {"run_id": 1, "nonce": "n" * 16, "placement": "windows", "cwd": "R:\\x", "env": {"CODEX_HOME": "C:\\h"},
                "backend": "claude", "model": "m"}
        _, env = lr.command(spec, Path(tempfile.gettempdir()), ["claude"])
        self.assertEqual(env["CODEX_HOME"], "C:\\h")


class Dispatching(test_dispatcher.Base):
    """The dispatcher takes its cap, seats, efforts and model ids from the config."""

    def use(self, **raw):
        config.use(config.validate(raw))
        self.addCleanup(config.reset)

    def test_claude_only_config_admits_with_real_allowance(self):
        from drover import allowance
        self.use(seats=[CLAUDE])
        reading = {"source": "manual", "observed_at": 1000,
                   "windows": {"seven_day": {"used_pct": 5, "resets_at": None, "window_hours": 168}}}
        self.d = self.make(admit_fn=lambda plan, role, pct: allowance.admit(
            plan, role, pct, state=self.tmp.name, reading=reading))
        tid = self.task()
        out = self.d.tick()
        self.assertEqual(out["admitted"], ["claude"])
        self.assertEqual(out["launched"], [tid])

    def test_unconfigured_backend_is_denied_without_reading_machine_state(self):
        from drover import allowance
        self.use(seats=[CLAUDE])
        verdict = allowance.admit("codex", "worker", 1, state=self.tmp.name)
        self.assertFalse(verdict["allow"])
        self.assertIn("configured seat", verdict["reason"])

    def test_parallel_cap_comes_from_config(self):
        self.use(capacity={"parallel_cap": 2})
        for _ in range(4):
            self.task(size="S")
        self.assertEqual(len(self.d.tick()["launched"]), 2)

    def test_the_policy_cap_still_beats_config(self):
        self.use(capacity={"parallel_cap": 2})
        ops = test_dispatcher.ops
        ops.add_policy_version(self.conn, "v1", {"parallel_cap": 3})
        for _ in range(5):
            self.task(size="S")
        self.assertEqual(len(self.d.tick()["launched"]), 3)

    def test_retry_waits_for_its_backend_seat_instead_of_overfilling_it(self):
        self.use(seats=[{**CLAUDE, "max_workers": 1, "priority": 0}, CODEX])
        busy = self.task(priority="P0")
        self.assertEqual(self.d.tick()["launched"], [busy])
        tid = self.task()
        ops = test_dispatcher.ops
        ops.set_status(self.conn, tid, "claimed")
        rid = ops.start_run(self.conn, tid, "worker", "claude", "sonnet-5.5")
        ops.finish_run(self.conn, rid, "error")
        ops.set_status(self.conn, tid, "ready")
        self.assertEqual(self.d.tick()["launched"], [])
        self.assertEqual(self.status(tid), "ready")
        self.assertEqual(len(self.fl.specs), 1)
        self.assertIn("max_workers", self.events("backoff")[-1]["data"]["why"])
        self.finish(0, test_dispatcher.res("done", test_dispatcher.DONE))
        now = self.d.now()
        self.d.now = lambda: now + 61
        self.assertEqual(self.d.tick()["launched"], [tid])
        self.assertEqual(self.fl.specs[-1]["seat"], "claude")

    def test_seat_max_workers_limits_launches(self):
        self.use(seats=[{**CLAUDE, "max_workers": 1}])
        self.allow["codex"] = False
        a, b = self.task(size="S"), self.task(size="S")
        out = self.d.tick()
        self.assertEqual(len(out["launched"]), 1)
        self.assertEqual(self.fl.specs[0]["seat"], "claude")
        self.assertEqual(self.status(b if out["launched"] == [a] else a), "ready")
        self.d.tick()                                         # the seat is still full, so nothing more starts
        self.assertEqual(len(self.fl.specs), 1)

    def test_the_spec_carries_the_seats_binary_and_env(self):
        self.use(seats=[{**CLAUDE, "binary": "claude-work", "config_dir": "C:\\acct2"}])
        self.allow["codex"] = False
        self.task(size="S")
        self.d.tick()
        spec = self.fl.specs[0]
        self.assertEqual((spec["seat"], spec["binary"], spec["env"]), ("claude", "claude-work",
                                                                       {"CLAUDE_CONFIG_DIR": "C:\\acct2"}))

    def test_seat_priority_decides_the_backend(self):
        self.use(seats=[{**CLAUDE, "priority": 2}, {**CODEX, "priority": 1}])
        for _ in range(4):
            self.task(size="S")
        self.d = self.make(wsl_up_fn=lambda: True)
        self.d.tick()
        self.assertEqual({s["backend"] for s in self.fl.specs}, {"codex"})

    def test_efforts_and_ladders_come_from_config(self):
        self.use(models={"efforts": {"cheap": "max", "top": "low"}, "ladders": {"claude": ["haiku-5.5", "opus-5.5"]},
                        "review": "haiku-5.5"})
        self.assertEqual(dispatcher.effort_for("haiku-5.5"), "max")
        self.assertEqual(dispatcher.effort_for("opus-5.5"), "low")
        self.assertEqual(dispatcher.effort_for("opus-5.5", "high"), "high")           # the router's effort still wins
        self.assertEqual(dispatcher.ladders()["claude"], ["haiku-5.5", "opus-5.5"])
        self.assertEqual(dispatcher.next_tier([], {"backend": "claude", "model": "haiku-5.5"}),
                         {"backend": "claude", "model": "haiku-5.5"})
        self.assertEqual(route.default_routes()["worker:M"][0], ("claude", "opus-5.5"))       # start index 1 of 2 rungs

    def test_model_ids_come_from_config(self):
        self.use(models={"ids": {"sonnet-5.5": "claude-sonnet-9"}})
        argv, _ = launch.build_argv({"backend": "claude", "model": "sonnet-5.5", "effort": "high",
                                     "system_append": "S", "user_prompt": "U"})
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-9")
        self.assertEqual(launch.MODEL_IDS["sonnet-5.5"], "claude-sonnet-9")
        self.assertEqual(launch.MODEL_IDS["opus-5.5"], "claude-opus-5-5")

    def test_default_effort_rules_are_unchanged(self):
        self.assertEqual(dispatcher.effort_for("haiku-5.5"), "xhigh")
        self.assertEqual(dispatcher.effort_for("gpt-6-luna", "high"), "xhigh")
        self.assertEqual(dispatcher.effort_for("sonnet-5.5"), "high")
        self.assertEqual(dispatcher.effort_for("opus-5.5", "max"), "max")


if __name__ == "__main__":
    unittest.main()


class DotnetOwnerRule(unittest.TestCase):
    """dotnet: (windows + claude) or (wsl + (claude or codex)), within the repo's platforms."""
    SPENT = {"remaining_pct": 5.0, "hours_to_reset": 10.0}
    CS = {"role": "worker", "size": "M", "scope_files": ["src/App.cs"]}

    def both_paths(self, task, seats, h, **kw):
        observed = {s["name"]: h[s["backend"]] for s in seats}
        return (route.choose(task, {}, h, seed=1, seats=seats, **kw),
                route.choose(task, {}, None, seed=1, seats=seats, seat_headroom=observed, **kw))

    def test_wsl_only_repo_with_only_codex_allowance_gets_codex_on_wsl(self):
        wsl_claude = {**CLAUDE, "placement": "wsl"}
        tasks = [self.CS, {"role": "worker", "size": "S", "stack": "dotnet"},
                 {"role": "worker", "size": "L", "stack": "msbuild"}, {"role": "worker", "size": "M", "tags": ["dotnet"]}]
        for task in tasks:
            for r in self.both_paths(task, [wsl_claude, CODEX], heads(claude=self.SPENT), platforms=["wsl"]):
                self.assertEqual((r["backend"], r["seat"]), ("codex", "codex"), task)

    def test_a_windows_only_repo_keeps_windows_claude(self):
        for r in self.both_paths(self.CS, [CLAUDE, CODEX], heads(), platforms=["windows"]):
            self.assertEqual((r["backend"], r["seat"]), ("claude", "claude"))

    def test_codex_never_takes_dotnet_on_windows(self):
        win_codex = {**CODEX, "placement": "windows"}          # config refuses this; the router must too
        self.assertFalse(route.dotnet_seat_ok(win_codex, ["windows"]))
        self.assertFalse(route.dotnet_seat_ok(win_codex, ["windows", "wsl"]))
        for platforms in (["windows"], ["windows", "wsl"], None):
            for r in self.both_paths(self.CS, [win_codex], heads(), platforms=platforms):
                self.assertIsNone(r["backend"])
        for seed in range(40):          # both platforms, both backends: Codex only ever lands on its WSL seat
            r = route.choose(self.CS, {}, heads(), seed=seed, seats=[CLAUDE, CODEX], platforms=["windows", "wsl"])
            self.assertIn(r["backend"], ("claude", "codex"))
        self.assertEqual(route.dotnet_backends(["windows"]), ["claude"])
        self.assertEqual(route.dotnet_backends(["windows", "wsl"]), ["claude", "codex"])

    def test_explicit_windows_task_never_uses_codex(self):
        task = {**self.CS, "platform": "windows"}
        for r in self.both_paths(task, [CLAUDE, CODEX], heads(claude=self.SPENT)):
            self.assertIsNone(r["backend"])

    def test_non_dotnet_work_is_unchanged(self):
        for r in self.both_paths(WORKER, [{**CLAUDE, "placement": "wsl"}, CODEX], heads(claude=self.SPENT),
                                 platforms=["wsl"]):
            self.assertEqual(r["backend"], "codex")

    def test_no_seat_reason_names_the_dotnet_constraint(self):
        for platforms in (["windows"], ["wsl"]):
            seats = [CODEX] if platforms == ["windows"] else [{**CLAUDE, "placement": "windows"}]
            r = route.choose(self.CS, {}, heads(), seed=1, seats=seats, platforms=platforms)
            if platforms == ["wsl"]:
                self.assertEqual(r["backend"], "claude")           # a Windows Claude seat can also run in WSL
                continue
            self.assertIsNone(r["backend"])
            self.assertTrue(r["needs_owner"])
            self.assertIn(".NET", r["reason"])
            self.assertIn("Codex", r["reason"])
            self.assertNotIn("max_workers", r["reason"])

    def test_full_dotnet_seats_say_max_workers_and_defer(self):
        seats = [{**CODEX, "max_workers": 1}]
        r = route.choose(self.CS, {}, heads(), seed=1, seats=seats, seat_load={"codex": 1}, platforms=["wsl"])
        self.assertIsNone(r["backend"])
        self.assertFalse(r["needs_owner"])
        self.assertIn("max_workers", r["reason"])
        self.assertIn(".NET", r["reason"])
