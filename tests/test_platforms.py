"""Per-repo platforms: placement within [run] platforms, gate platform both, and the wsl tracker."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from drover import config, contract, dispatcher, pipeline, repos, route
from hub.dispatch import LandingService
from drover.ledger import ops
from tests import test_dispatcher, test_pipeline
from tests.test_pipeline import Base, done, prog_dict, result


def repo(repo, worktrees, platforms=None, gate_platform="windows", default=None):
    p = prog_dict(repo, worktrees)
    raw = {"name": "p", "repo": p["repo"], "worktrees": p["worktrees"],
           "run": {"platforms": ["windows", "wsl"] if platforms is None else platforms, **({"default": default} if default else {})},
           "gate": {"platform": gate_platform, "commands": {"full": ["x"]}}}
    return repos.normalize(raw)


class Normalize(unittest.TestCase):
    def test_defaults_are_both_platforms_with_windows_default(self):
        p = prog_dict("R:\\r", "R:\\w")
        self.assertEqual(p["run"], {"platforms": ["windows", "wsl"], "default": "windows"})
        self.assertEqual(p["gate"]["platform"], "windows")

    def test_wsl_only_repo_defaults_to_wsl(self):
        p = repo("R:\\r", "R:\\w", ["wsl"], "wsl")
        self.assertEqual(p["run"], {"platforms": ["wsl"], "default": "wsl"})

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
        self.assertEqual(repos.gate_platforms(examples['acme-app']), ['windows', 'wsl'])
        for platform, python in [('windows', 'python'), ('wsl', 'python3')]:
            self.assertEqual(repos.gate_commands(examples['acme-app'], platform)['full'],
                             [python, '-m', 'unittest', 'discover'])

    def test_state_dir_repos_win_once_the_folder_exists(self):
        from drover import config
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "repos").mkdir()
            (Path(d) / "repos" / "mine.toml").write_text(
                'name="mine"\nrepo="R:\\\\r"\nworktrees="R:\\\\w"\n[gate]\nunknown=true\n')
            config.use(config.load(Path(d), Path(d) / "none", {"DROVER_PATHS_STATE_DIR": d}))
            self.addCleanup(config.reset)
            self.assertEqual(list(repos.load_all()), ["mine"])


class FakeRepoPipeline(Base):
    """A pipeline whose repo is wsl-only or windows-only."""

    def repo(self, platforms, gate_platform="windows"):
        self.prog.update(repo(self.prog["repo"], self.prog["worktrees"], platforms, gate_platform))

    def tick_with_wsl(self):
        self.d = self.make(wsl_up_fn=lambda: True, worktree_fn=lambda t: "R:\\repos\\wt")
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
        ops.set_status(self.conn, tid, "claimed")
        rid = ops.start_run(self.conn, tid, "worker", "claude", "sonnet-5.5")
        ops.finish_run(self.conn, rid, "error")
        ops.set_status(self.conn, tid, "ready")
        # A current Codex route is overridden by the retry's Claude backend.
        self.d = self.make(route_fn=lambda t, p, h: {"backend": "codex", "model": "gpt-6.1-sol"},
                           worktree_fn=lambda t: "R:\\repos\\wt")
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
        self.assertNotIn(tid, self.port.get_backoffs())

    def test_retry_with_permanently_incompatible_backend_goes_to_owner(self):
        self.repo(["windows"])
        tid = self.task()
        ops.set_status(self.conn, tid, "claimed")
        rid = ops.start_run(self.conn, tid, "worker", "codex", "gpt-6-luna")
        ops.finish_run(self.conn, rid, "error")
        ops.set_status(self.conn, tid, "ready")
        self.assertEqual(self.tick_with_wsl()["launched"], [])
        self.assertEqual(self.status(tid), "needs-owner")
        self.assertIn("no platform-compatible seat", self.reason(tid))
        self.assertNotIn(tid, self.port.get_backoffs())

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
                ({"title": "msbuild"}, claude, "R:\\wt", wsl_default, "windows"),
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

    def test_dotnet_on_a_wsl_only_repo_goes_to_the_owner_with_a_reason(self):
        self.repo(["wsl"], "wsl")
        tid = self.task(title="fix the msbuild target")
        self.tick_with_wsl()
        self.assertEqual(self.status(tid), "needs-owner")
        self.assertIn("needs windows", self.reason(tid))

    def test_dotnet_on_a_wsl_claude_seat_goes_to_the_owner_not_to_wsl(self):
        seat = {**config.DEFAULT_SEATS[0], "placement": "wsl"}
        config.use(config.validate({"seats": [seat]}))
        self.addCleanup(config.reset)
        tid = self.task(title="fix the msbuild target")
        self.tick_with_wsl()
        self.assertEqual(self.status(tid), "needs-owner")
        self.assertIn("needs windows but seat claude runs only in wsl", self.reason(tid))
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


class GateBoth(Base):
    def setUp(self):
        super().setUp()
        self.prog.update(repo(self.prog["repo"], self.prog["worktrees"], gate_platform="both"))
        self.verdicts = {}                                   # platform -> passed?
        self.fl.launch = self.launch

    def launch(self, spec):
        self.fl.specs.append(spec)
        if spec.get("role") == "gate":
            ev = next(e["data"] for e in ops.events(self.conn, kind="gate-started") if e["data"].get("gate_run") == spec["run_id"])
            ok = self.verdicts.get(ev["platform"], True)
            Path(ev["receipt"]).write_text(json.dumps({"passed": ok, "sha": ev["sha"], "status": "passed" if ok else "failed",
                                                       "steps": []}))
            h = test_pipeline.FakeHandle({"status": "done" if ok else "error", "record": None, "text": "", "handoff": None,
                                          "usage": {"tokens_in": 0, "tokens_out": 0, "tokens_cached": 0, "context": 0},
                                          "returncode": 0 if ok else 1, "timeout": None})
        else:
            h = test_pipeline.FakeHandle()
        self.fl.handles[spec["run_id"]] = h
        return h

    def drive(self, ticks=6):
        self.d = self.make(wsl_up_fn=lambda: True)
        tid = self.task()
        self.d.tick()
        sha = self.worker_commits(tid)
        self.run_worker(tid, sha)
        for _ in range(ticks):
            self.d.tick()
        return tid, sha

    def test_the_gate_runs_on_each_platform_in_turn_and_passes_only_when_both_pass(self):
        tid, sha = self.drive()
        gates = self.fl.of("gate")
        self.assertEqual([g["placement"] for g in gates], ["windows", "wsl"])
        self.assertEqual(self.status(tid), "gated")
        runs = ops.list_runs(self.conn, tid)
        self.assertEqual([(r["role"], r["outcome"]) for r in runs if r["role"] != "review"],
                         [("worker", "gated"), ("gate", "passed"), ("gate", "passed")])
        self.assertEqual(len(ops.events(self.conn, kind="gate-platform-passed")), 1)
        self.assertEqual(len(ops.events(self.conn, kind="gate-done")), 1)
        self.assertEqual(self.fl.of("gate")[1]["argv"][0], "python3")                 # the WSL gate runs under python3

    def test_a_red_second_platform_fails_the_gate_and_names_the_platform(self):
        self.verdicts["wsl"] = False
        tid, sha = self.drive()
        self.assertEqual([g["placement"] for g in self.fl.of("gate")][:2], ["windows", "wsl"])
        runs = ops.list_runs(self.conn, tid)
        self.assertEqual(runs[0]["outcome"], "gate-fail")
        self.assertIn("[wsl gate failed]", contract.receipt_excerpt(runs[0]["gate_receipt"]))
        self.assertIn(("ready", "retry"), self.states(tid))

    def test_a_red_first_platform_never_starts_the_second(self):
        self.verdicts["windows"] = False
        tid, sha = self.drive()
        self.assertEqual([g["placement"] for g in self.fl.of("gate")][:1], ["windows"])
        self.assertNotIn("wsl", [g["placement"] for g in self.fl.of("gate")[:1]])
        self.assertEqual(ops.list_runs(self.conn, tid)[0]["outcome"], "gate-fail")

    def test_wsl_down_waits_for_the_wsl_half_and_tells_the_watchdog(self):
        up = {"v": True}
        self.d = self.make(wsl_up_fn=lambda: up["v"])
        tid = self.task()
        self.d.tick()
        self.run_worker(tid, self.worker_commits(tid))
        self.d.tick()                                         # reaps the worker, starts the Windows gate
        up["v"] = False
        for _ in range(4):
            self.d.tick()
        self.assertEqual([g["placement"] for g in self.fl.of("gate")], ["windows"])
        self.assertEqual(self.status(tid), "running")
        self.assertEqual(json.loads(ops.get_setting(self.conn, "_wsl_needed"))["task"], tid)
        up["v"] = True
        for _ in range(4):
            self.d.tick()
        self.assertEqual([g["placement"] for g in self.fl.of("gate")], ["windows", "wsl"])
        self.assertEqual(self.status(tid), "gated")


class PlatformCommands(unittest.TestCase):
    def setUp(self):
        self.prog = repos.normalize({'name': 'p', 'repo': 'repo', 'gate': {
            'platform': 'both', 'commands': {'windows': {'full': ['python', 'check.py']},
                                           'wsl': {'full': ['python3', 'check.py']}}}})
        self.pipe = pipeline.Pipeline(None, repos_map={'p': self.prog})
        self.task = {'id': 'T-1', 'repo': 'p', 'worktree': 'worktree', 'priority': 'P1'}
        self.sha = 'a' * 40
        for target, name, value in ((pipeline.gate, 'changed_paths', ['src/a.py']),
                                    (self.pipe, 'diff_lines', 2), (self.pipe, 'head', self.sha)):
            patched = patch.object(target, name, return_value=value)
            patched.start()
            self.addCleanup(patched.stop)

    def test_shared_full_commands_work_on_both_platforms_and_default_to_the_repos_platform(self):
        self.prog['gate']['commands'] = {'full': ['shared']}
        for platform in repos.PLATFORMS:
            plan = self.pipe.gate_plan(self.task, self.prog, self.sha, platform)
            self.assertEqual((plan['kind'], plan['commands'], plan['platform']), ('full', [['shared']], platform))
        self.prog['gate']['platform'] = 'wsl'
        plan = self.pipe.gate_plan(self.task, self.prog, self.sha)
        self.assertEqual((plan['commands'], plan['platform']), ([['shared']], 'wsl'))

    def test_dispatcher_writes_a_plan_with_each_platforms_full_command(self):
        d = dispatcher.Dispatcher.__new__(dispatcher.Dispatcher)
        d.port, d.pipeline, d.launcher, d.live = Mock(), self.pipe, Mock(), {}
        d._event_for = Mock(return_value={'sha': self.sha})
        d._last_nongate = Mock(return_value={'id': 1})
        d._next_gate_platform = Mock(side_effect=['windows', 'wsl'])
        d.wsl_up_fn, d._wsl_blocked, d._lease = lambda: True, lambda: False, lambda task: True
        d.port.start_run.side_effect = [2, 3]
        with tempfile.TemporaryDirectory() as tmp:
            self.pipe.runs_dir = tmp
            for platform, run_id, python in [('windows', 2, 'python'), ('wsl', 3, 'python3')]:
                self.assertTrue(d._begin_gate(self.task, []))
                plan = json.loads((Path(tmp) / str(run_id) / 'plan.json').read_text())
                self.assertEqual(plan['commands'], [[python, 'check.py']])
                self.assertEqual(d.launcher.launch.call_args.args[0]['placement'], platform)

    def test_scoped_overrides_expand_projects_and_fall_back_to_platform_full(self):
        self.prog['gate']['scope_map'] = 'scope.json'
        self.prog['gate']['commands']['scoped'] = ['shared', '{project}']
        self.prog['gate']['commands']['wsl']['scoped'] = ['python3', '{project}']
        scope = {'projects': ['Core.Tests', 'App.Tests'], 'fallback_used': False}
        with patch.object(pipeline.gate, 'load_map', return_value={}), \
                patch.object(pipeline.gate, 'compute_scope', return_value=scope):
            for platform, cmd in [('windows', 'shared'), ('wsl', 'python3')]:
                plan = self.pipe.gate_plan(self.task, self.prog, self.sha, platform)
                self.assertEqual((plan['kind'], plan['platform']), ('scoped', platform))
                self.assertEqual(plan['commands'], [[cmd, 'Core.Tests'], [cmd, 'App.Tests']])
            scope['fallback_used'] = True
            for platform, cmd in [('windows', 'python'), ('wsl', 'python3')]:
                plan = self.pipe.gate_plan(self.task, self.prog, self.sha, platform)
                self.assertEqual((plan['kind'], plan['commands']), ('full', [[cmd, 'check.py']]))

    def test_unmapped_scoped_command_is_resolved_for_the_selected_platform(self):
        self.prog['gate']['commands']['windows']['scoped'] = ['windows-scoped']
        windows = self.pipe.gate_plan(self.task, self.prog, self.sha, 'windows')
        wsl = self.pipe.gate_plan(self.task, self.prog, self.sha, 'wsl')
        self.assertEqual((windows['kind'], windows['commands']), ('scoped', [['windows-scoped']]))
        self.assertEqual((wsl['kind'], wsl['commands']), ('full', [['python3', 'check.py']]))
        with tempfile.TemporaryDirectory() as tmp:
            self.pipe.runs_dir = tmp
            argv, _ = self.pipe.gate_argv(wsl, 1, 'P1', self.prog)
            self.assertEqual(argv[0], 'python3')


class WslTracker(test_dispatcher.Base):
    def test_wsl_pressure_blocks_only_work_that_needs_wsl(self):
        self.allow["codex"] = False
        wsl_task = self.task(size="S", goal="platform: linux")
        win_task = self.task(size="S", priority="P4")
        self.verdict = {**test_dispatcher.GO, "wsl": {"verdict": "queue", "reasons": ["WSL memory 90% of its 16.0 GB cap > 85%"]}}
        self.d = self.make(wsl_up_fn=lambda: True)
        out = self.d.tick()
        self.assertIn(win_task, out["launched"])
        self.assertEqual(self.status(wsl_task), "ready")
        reasons = [e["data"].get("reason", "") for e in self.events("status") if e["data"].get("to") == "ready"]
        self.assertTrue(any("WSL memory" in r for r in reasons))
        self.verdict = test_dispatcher.GO
        self.d.tick()
        self.assertEqual(self.status(wsl_task), "ready")                       # still in backoff, but not blocked by WSL
        self.assertFalse(self.d._wsl_blocked())

    def test_the_global_verdict_never_includes_the_wsl_tracker(self):
        from drover import resources
        r = resources.check(fake={"platform": "windows", "ram_available_gb": 30, "wsl_up": False})
        self.assertEqual((r["verdict"], r["wsl"]["verdict"]), ("go", "queue"))
        self.assertEqual(r["wsl"]["reasons"], ["WSL is not running"])


class DotnetSeats(unittest.TestCase):
    def test_a_windows_capable_seat_wins_the_dotnet_task_whatever_the_priority(self):
        wsl = {**config.DEFAULT_SEATS[0], "name": "wsl-claude", "placement": "wsl", "priority": 1}
        win = {**config.DEFAULT_SEATS[0], "name": "win-claude", "director": False, "priority": 2,
               "director_reserve_pct": 0.0}
        task = {"role": "worker", "size": "M", "platform": "windows", "stack": "dotnet"}
        h = {"claude": {"remaining_pct": 90.0, "hours_to_reset": 10.0}, "codex": None}
        for seed in range(10):
            self.assertEqual(route.choose(task, {}, h, seed=seed, seats=[wsl, win])["seat"], "win-claude")
            self.assertEqual(route.choose({**task, "platform": None, "stack": ""}, {}, h, seed=seed,
                                          seats=[wsl, win])["seat"], "wsl-claude")

    def test_no_windows_seat_leaves_it_to_the_dispatcher_to_ask_the_owner(self):
        wsl = {**config.DEFAULT_SEATS[0], "placement": "wsl"}
        task = {"role": "worker", "size": "M", "platform": "windows", "stack": "dotnet"}
        r = route.choose(task, {}, {"claude": {"remaining_pct": 90.0, "hours_to_reset": 10.0}, "codex": None}, seed=1,
                         seats=[wsl])
        self.assertEqual((r["backend"], r["seat"]), ("claude", "claude"))


class LandingGateBoth(unittest.TestCase):
    def test_batch_gate_uses_the_effective_full_command_on_each_platform(self):
        svc = LandingService.__new__(LandingService)
        svc.store, svc.gate_kw = SimpleNamespace(path='ledger.db'), {}
        cases = [({'full': ['shared']}, [['shared'], ['shared']]),
                 ({'windows': {'full': ['python']}, 'wsl': {'full': ['python3']}},
                  [['python'], ['python3']]),
                 ({'full': ['shared'], 'wsl': {'full': ['python3']}}, [['shared'], ['python3']])]
        for commands, expected in cases:
            with self.subTest(commands=commands):
                prog = repos.normalize({'name': 'p', 'repo': 'repo',
                                        'gate': {'platform': 'both', 'commands': commands}})
                calls = []

                def make(command, *args, **kw):
                    return lambda wt, tip, tasks: calls.append(command) or {'status': 'passed'}

                with patch('hub.dispatch.os', SimpleNamespace(name='nt')), \
                        patch('hub.dispatch.landing.make_gate', side_effect=make) as windows, \
                        patch.object(svc, '_wsl_gate', side_effect=make) as wsl:
                    check = svc.gate_fn(prog, 'main', 'out', None)
                    receipt = check('worktree', 'a' * 40, [])
                self.assertEqual(calls, expected)
                self.assertEqual(windows.call_args.args[0], expected[0])
                self.assertEqual(wsl.call_args.args[0], expected[1])
                self.assertEqual((receipt['status'], receipt['platform']), ('passed', 'windows+wsl'))

    def gate(self, statuses):
        calls = []

        def one(status, name):
            return lambda worktree, tip, tasks: calls.append(name) or {"status": status, "name": name}
        runs = [one(s, n) for s, n in zip(statuses, ("windows", "wsl"))]
        return calls, LandingService._both(runs, ["windows", "wsl"])

    def test_green_on_both_is_green_and_runs_both(self):
        calls, run = self.gate(["passed", "passed"])
        r = run("wt", "tip", [])
        self.assertEqual((calls, r["status"], r["platform"]), (["windows", "wsl"], "passed", "windows+wsl"))

    def test_red_on_the_first_platform_skips_the_second(self):
        calls, run = self.gate(["failed", "passed"])
        r = run("wt", "tip", [])
        self.assertEqual((calls, r["status"], r["platform"]), (["windows"], "failed", "windows"))

    def test_red_on_the_second_platform_is_red_and_named(self):
        calls, run = self.gate(["passed", "failed"])
        r = run("wt", "tip", [])
        self.assertEqual((calls, r["status"], r["platform"]), (["windows", "wsl"], "failed", "wsl"))

    def test_gate_fn_builds_one_gate_per_platform_of_the_repo(self):
        svc = LandingService.__new__(LandingService)
        made = []
        svc._gate_on = lambda platform, *a: made.append(platform) or (lambda wt, tip, tasks: {"status": "passed"})
        for plat, want in (("windows", ["windows"]), ("wsl", ["wsl"]), ("both", ["windows", "wsl"])):
            made.clear()
            prog = repo("R:\\r", "R:\\w", gate_platform=plat)
            prog["gate"]["commands"] = {"full": ["x"]}
            svc.gate_fn(prog, "main", "out", None)
            self.assertEqual(made, want)


if __name__ == "__main__":
    unittest.main()
