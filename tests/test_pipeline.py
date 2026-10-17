"""The pipeline: pause switch, repo config, worktrees, gate runs, reviews, decisions and the landing loop."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from drover import brief, config, contract, dispatcher, gate, landing, launch, pipeline, repos, runner
from drover.launch import RunnerLauncher
from drover.ledger import connect, ops
from drover.port import DirectPort
from hub.dispatch import DispatcherService, LandingService
from hub.settings import Settings
from hub.store import Store
from tests.gitfix import Repo

GO = {"verdict": "go", "slots": 10, "reasons": []}


def prog_dict(repo, worktrees, gate_cmd=None, unknown=False, push=False, scope_map=None, **kw):
    raw = {"name": "p", "repo": str(repo), "worktrees": str(worktrees),
           "gate": {"platform": "windows", "unknown": unknown, "timeout_s": 60, "idle_s": 60,
                    "commands": {} if unknown else {"full": gate_cmd or [sys.executable, "-c", "pass"]}},
           "land": {"push": push}, **kw}
    if scope_map:
        raw["gate"]["scope_map"] = scope_map
    return repos.normalize(raw)


def done(sha):
    return json.dumps({"status": "done", "sha": sha, "blocker": None, "note": "ok"})


def result(status, text="", ctx=1000):
    return {"status": status, "record": launch.parse_action_record(text), "text": text, "handoff": None,
            "usage": {"tokens_in": 10, "tokens_out": 5, "tokens_cached": 1, "context": ctx}, "returncode": 0,
            "timeout": None}


class FakeHandle:
    pid = 1

    def __init__(self, res=None):
        self.result = res

    def poll(self):
        return self.result

    def terminal(self):
        return None

    def cancel(self):
        pass


class FakeLauncher:
    """Workers and reviews wait for the test to finish them; the gate run really executes gatejob, as the runner would."""

    def __init__(self, runs_dir):
        self.specs, self.handles, self.runs_dir = [], {}, runs_dir

    def launch(self, spec):
        self.specs.append(spec)
        if spec.get("role") == "gate":
            r = subprocess.run(spec["argv"], capture_output=True, text=True, cwd=spec["cwd"])
            out = runner.run_dir(self.runs_dir, spec["run_id"]) / runner.OUT_FILE
            out.write_text(r.stdout + r.stderr)
            h = FakeHandle({"status": "done" if r.returncode == 0 else "error", "record": None, "text": "", "handoff": None,
                            "usage": {"tokens_in": 0, "tokens_out": 0, "tokens_cached": 0, "context": 0},
                            "returncode": r.returncode, "timeout": None})
        else:
            h = FakeHandle()
        self.handles[spec["run_id"]] = h
        return h

    def of(self, role):
        return [s for s in self.specs if s.get("role") == role]


class Base(unittest.TestCase):
    def setUp(self):
        self.fix = Repo()
        self.addCleanup(self.fix.cleanup)
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.conn = connect(root / "l.db")
        self.addCleanup(self.conn.close)
        self.runs = root / "runs"
        self.runs.mkdir()
        self.port = DirectPort(self.conn)
        self.fl = FakeLauncher(self.runs)
        self.prog = prog_dict(self.fix.repo, root / "wts")
        self.pipe = pipeline.Pipeline(self.port, repos_map={"p": self.prog}, runs_dir=self.runs)
        self.decisions = []
        ops.set_setting(self.conn, "dispatch_enabled", "true")
        self.d = self.make()

    def make(self, **kw):
        args = dict(launcher=self.fl, pipeline=self.pipe, resources_fn=lambda new_workers=1: GO,
                    admit_fn=lambda plan, role, pct: {"allow": True}, decide_fn=self.decide, state=self.tmp.name,
                    headroom_fn=lambda plan: {"remaining_pct": 90.0, "hours_to_reset": 100.0})
        args.update(kw)
        return dispatcher.Dispatcher(self.port, **args)

    def decide(self, name, state, rule_answer=None):
        self.decisions.append(name)
        return {"answer": None, "shadow_answer": rule_answer, "acted": False, "confidence": 0.9,
                "criteria_version": "1", "model": "nimble-v3", "state_hash": "h", "probabilities": None}

    def task(self, branch=None, **kw):
        t = ops.create_task(self.conn, kw.pop("title", "t"), kw.pop("repo", "p"), **kw)
        ops.approve(self.conn, t["id"])
        if branch:
            ops.update_task(self.conn, t["id"], branch=branch)
        return t["id"]

    def states(self, tid):
        """(to, reason) of every status change of the task, oldest first."""
        return [(e["data"]["to"], e["data"].get("reason")) for e in ops.events(self.conn, task_id=tid, kind="status")]

    def reviewed(self, tid, sha):
        """Put a worker run, a LAND review and the reviewed status in the ledger the way the pipeline would."""
        ops.set_status(self.conn, tid, "claimed")
        rid = ops.start_run(self.conn, tid, "worker", "claude", "sonnet-5.5")
        ops.finish_run(self.conn, rid, "gated")
        rv = ops.start_run(self.conn, tid, "review", "claude", "sonnet-5.5")
        ops.finish_run(self.conn, rv, "land", review_verdict=f"LAND@{sha}")
        ops.set_status(self.conn, tid, "gated")
        ops.set_status(self.conn, tid, "reviewed")

    def get(self, tid):
        return ops.get_task(self.conn, tid)

    def status(self, tid):
        return self.get(tid)["status"]

    def worker_commits(self, tid, name="work.txt"):
        """What the worker did: a commit in the task's worktree. Returns the sha."""
        return self.fix.commit(name, "x\n", cwd=repos.native(self.get(tid)["worktree"]))

    def run_worker(self, tid, sha):
        rid = next(sp["run_id"] for sp in reversed(self.fl.of("worker")) if sp["task"]["id"] == tid)
        self.fl.handles[rid].result = result("done", done(sha))

    def to_gated(self):
        tid = self.task()
        self.assertEqual(self.d.tick()["launched"], [tid])
        sha = self.worker_commits(tid)
        self.run_worker(tid, sha)
        self.d.tick()                       # reaps the worker, starts the gate (which runs at once)
        self.d.tick()                       # reaps the gate, then starts the review of the gated task
        return tid, sha


class Switch(Base):
    def test_the_switch_reads_true_1_yes_on_only(self):
        for v, want in (("true", True), ("TRUE", True), ("1", True), ("Yes", True), ("on", True), (" on ", True),
                        ("false", False), ("0", False), ("", False), ("enabled", False), ("no", False)):
            ops.set_setting(self.conn, "dispatch_enabled", v)
            self.assertEqual(self.pipe.enabled(), want, v)
        self.conn.execute("DELETE FROM settings WHERE key='dispatch_enabled'")
        self.assertFalse(self.pipe.enabled())

    def test_paused_launches_and_claims_nothing_but_records_a_paused_heartbeat(self):
        ops.set_setting(self.conn, "dispatch_enabled", "false")
        tid = self.task()
        out = self.d.tick()
        self.assertEqual((out["launched"], out["paused"]), ([], True))
        self.assertEqual(self.status(tid), "ready")
        self.assertEqual(self.fl.specs, [])
        self.assertEqual(json.loads(ops.get_setting(self.conn, "dispatcher_heartbeat"))["state"], "paused")
        ops.set_setting(self.conn, "dispatch_enabled", "true")
        self.assertEqual(self.d.tick()["launched"], [tid])
        self.assertEqual(json.loads(ops.get_setting(self.conn, "dispatcher_heartbeat"))["state"], "running")

    def test_paused_still_reaps_a_run_in_flight_and_starts_no_gate(self):
        tid = self.task()
        self.d.tick()
        sha = self.worker_commits(tid)
        self.run_worker(tid, sha)
        ops.set_setting(self.conn, "dispatch_enabled", "false")
        out = self.d.tick()
        self.assertEqual(len(out["reaped"]), 1)
        self.assertEqual(self.fl.of("gate"), [])
        self.assertEqual(ops.list_runs(self.conn, tid)[0]["outcome"], "done")
        ops.set_setting(self.conn, "dispatch_enabled", "true")
        self.d.tick()
        self.assertEqual(len(self.fl.of("gate")), 1)               # resumed: the gate is derived from the ledger


class WslBins(unittest.TestCase):
    def test_absolute_paths_replace_the_bare_name_for_wsl_only(self):
        la = RunnerLauncher(wsl_bins={"claude": "/home/u/.local/bin/claude", "codex": "relative/codex"})
        self.assertEqual(la.wsl_exe(["claude", "-p"]), ["/home/u/.local/bin/claude", "-p"])
        self.assertEqual(la.wsl_exe(["codex", "exec"]), ["codex", "exec"])      # not absolute: bare name
        self.assertEqual(la.wsl_exe(["gitx"]), ["gitx"])
        cmd, _ = la.command({"placement": "wsl", "cwd": "R:\\x", "run_id": 1, "nonce": "n" * 16}, Path("/tmp/d"),
                            ["claude", "-p"])
        self.assertIn("/home/u/.local/bin/claude", cmd)
        self.assertNotIn("claude", cmd[cmd.index("--") + 1:][:0] + ["x"][:0] or ["x"])

    def test_service_reads_the_ledger_settings(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as t:
            store = Store(Path(t) / "l.db")
            self.addCleanup(store.close)
            svc = DispatcherService(store, Settings(token="t", dispatcher_lock=Path(t) / "lock"))
            self.assertEqual(svc._wsl_bins(), {})
            store.update_settings({"wsl_bin_claude": "/opt/claude"})
            self.assertEqual(svc._wsl_bins(), {"claude": "/opt/claude"})
            self.assertEqual(RunnerLauncher(wsl_bins=svc._wsl_bins).wsl_exe(["codex"]), ["codex"])


class Commands(unittest.TestCase):
    def test_owner_commands_become_real_argv(self):
        g = repos.gate_argv
        ps = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]
        self.assertEqual(g(r".\test.ps1 -All -Configuration Release"), ps + [r".\test.ps1", "-All", "-Configuration", "Release"])
        self.assertEqual(g("rust/tools/pg.ps1 -Mode test"), ps + ["rust/tools/pg.ps1", "-Mode", "test"])
        self.assertEqual(g("\"C:\\Program Files\\x\\run.ps1\" -Name \"a b\" --k='v w'"),
                         ps + ["C:\\Program Files\\x\\run.ps1", "-Name", "a b", "--k=v w"])
        self.assertEqual(g(r"C:\tools\gate.exe --all"), [r"C:\tools\gate.exe", "--all"])      # backslashes are kept
        self.assertEqual(g('pwsh -NoProfile -File ./test.ps1'), ["pwsh", "-NoProfile", "-File", "./test.ps1"])
        for bad in ("", "   ", '"unclosed'):
            with self.assertRaises(repos.RepoError):
                g(bad)


class Repos(unittest.TestCase):
    def test_fictional_examples_load(self):
        progs = repos.load_all(repos.REPOS_DIR)
        self.assertEqual(set(progs), {"acme-lib", "acme-app"})
        self.assertEqual(progs["acme-app"]["consumes"], ["acme-lib"])
        self.assertTrue(all(not p["land"]["push"] for p in progs.values()))


class UnknownGate(Base):
    def setUp(self):
        super().setUp()
        self.prog = prog_dict(self.fix.repo, Path(self.tmp.name) / "wts", unknown=True)
        self.pipe.all_repos()["p"] = self.prog

    def test_the_question_is_created_once_per_repo_and_answer_requeues_every_task(self):
        a, b, c = self.task(title="a"), self.task(title="b"), self.task(title="c")
        out = self.d.tick()
        self.assertEqual(out["launched"], [])
        self.assertEqual([self.status(x) for x in (a, b, c)], ["needs-owner"] * 3)
        qs = ops.open_questions(self.conn)
        self.assertEqual([q["title"] for q in qs], ["What is the gate command for p?"])
        self.d.tick()
        self.assertEqual(len(ops.open_questions(self.conn)), 1)           # not again
        ops.answer_question(self.conn, qs[0]["id"], text=f"{sys.executable} -c pass")
        self.d.tick()
        self.assertEqual(json.loads(ops.get_setting(self.conn, "gate_cmd_p"))["cmd"], f"{sys.executable} -c pass")
        self.assertEqual(len(self.fl.of("worker")), 3)                    # b and c came back too, and launched
        self.assertEqual(len(ops.open_questions(self.conn)), 0)
        self.assertFalse(self.pipe.repo({"repo": "p"})["gate"]["unknown"])

    def test_a_gate_cmd_written_without_an_owner_answer_is_ignored(self):
        t = self.task()
        self.d.tick()
        qid = int(ops.get_setting(self.conn, "_gate_question_p"))
        for forged in (f"{sys.executable} -c pass",                                     # a bare string, as a token would PUT
                       json.dumps({"cmd": "evil", "question": qid, "actor": "owner"}),   # the question is not answered
                       json.dumps({"cmd": "evil", "question": 999, "actor": "owner"})):  # not this repo's question
            ops.set_setting(self.conn, "gate_cmd_p", forged)
            self.assertTrue(self.pipe.repo({"repo": "p"})["gate"]["unknown"], forged)
            self.d.tick()
            self.assertEqual(self.status(t), "needs-owner")
        ops.answer_question(self.conn, qid, text="real-gate --flag")
        ops.set_setting(self.conn, "gate_cmd_p", json.dumps({"cmd": "evil", "question": qid, "actor": "owner"}))
        self.assertTrue(self.pipe.repo({"repo": "p"})["gate"]["unknown"])       # text differs from the answer
        self.d.tick()
        self.assertEqual(self.pipe.repo({"repo": "p"})["gate"]["commands"]["full"], ["real-gate", "--flag"])

    def test_an_answer_recorded_by_anyone_but_the_owner_is_ignored(self):
        self.task()
        self.d.tick()
        qid = int(ops.get_setting(self.conn, "_gate_question_p"))
        ops.answer_question(self.conn, qid, text="cmd-x", actor="dispatcher")
        self.d.tick()
        self.assertTrue(self.pipe.repo({"repo": "p"})["gate"]["unknown"])

    def test_no_worker_run_is_spent_on_an_unknown_gate(self):
        self.task()
        self.d.tick()
        self.assertEqual(self.fl.specs, [])


class Worktrees(Base):
    def test_worktree_add_disables_autocrlf_including_the_legacy_fallback(self):
        real = pipeline.git
        self.fix.git("config", "core.autocrlf", "true")
        committed = subprocess.run(["git", "-C", str(self.fix.repo), "show", "main:base.txt"],
                                   capture_output=True, check=True).stdout
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                calls = []

                def fake(repo, *args, **kw):
                    if "worktree" in args and "add" in args:
                        calls.append(args)
                        if legacy and "--relative-paths" in args:
                            return subprocess.CompletedProcess(args, 129, "", "error: unknown option `relative-paths'")
                    return real(repo, *args, **kw)

                tid = self.task()
                with patch.object(pipeline, "git", fake), patch.object(pipeline.Pipeline, "_cross_os", return_value=False):
                    wt = self.pipe.worktree(self.get(tid))
                self.assertEqual(len(calls), 2 if legacy else 1)
                for args in calls:
                    self.assertEqual(args[:4], ("-c", "core.autocrlf=false", "worktree", "add"))
                self.assertEqual((Path(wt) / "base.txt").read_bytes(), committed)

    def test_example_home_paths_are_persisted_and_launched_as_absolute_paths(self):
        root = Path(self.tmp.name)
        repo = root / 'repos' / 'acme-lib'
        repo.parent.mkdir()
        self.fix.git('clone', '-q', self.fix.repo, repo)
        with patch('os.path.expanduser', side_effect=lambda p: str(root) + p[1:] if p.startswith('~') else p):
            self.pipe._repos = repos.load_all(repos.REPOS_DIR)
            tid = self.task(repo='acme-lib')
            wt = self.pipe.worktree(self.get(tid))
            expected = str(root / 'repos' / 'acme-lib.worktrees' / f'drover-{tid}')
            self.assertEqual(wt, expected)
            self.assertEqual(self.get(tid)['worktree'], expected)
            self.assertTrue((Path(wt) / '.git').is_file())
            launcher = RunnerLauncher(runs_dir=self.runs)
            cmd, _ = launcher.command({'run_id': 1, 'nonce': 'test', 'cwd': wt, 'placement': 'wsl'},
                                      self.runs, ['claude'])
            self.assertEqual(cmd[cmd.index('--cwd') + 1], runner.to_wsl_path(expected))

    def test_creates_on_a_new_branch_from_origin_main_tags_it_and_reuses_it(self):
        tid = self.task()
        wt = self.pipe.worktree(self.get(tid))
        self.assertEqual(Path(wt).name, f"drover-{tid}")
        t = self.get(tid)
        self.assertEqual((t["branch"], t["worktree"]), (f"drover/{tid}", wt))
        self.assertEqual(self.fix.git("rev-parse", "HEAD", cwd=wt), self.fix.git("rev-parse", "origin/main"))
        tag = pipeline.garden.read_tag(pipeline.garden.admin_dir_of(wt))
        self.assertEqual((tag["owner"], tag["repo"]), (pipeline.OWNER_PREFIX + tid, "p"))
        self.assertEqual(self.pipe.worktree(self.get(tid)), wt)             # idempotent

    def test_uses_an_existing_branch_and_a_clean_worktree_that_has_it(self):
        self.fix.branch("feat", {"f.txt": "f\n"})
        other = Path(self.tmp.name) / "other"
        self.fix.git("worktree", "add", "-q", other, "feat")
        tid = self.task(branch="feat")
        self.assertEqual(Path(self.pipe.worktree(self.get(tid))), other)
        self.assertEqual(self.get(tid)["worktree"], str(other))

    def test_refuses_a_dirty_worktree_it_does_not_own_but_not_one_it_owns(self):
        self.fix.branch("feat", {"f.txt": "f\n"})
        other = Path(self.tmp.name) / "other"
        self.fix.git("worktree", "add", "-q", other, "feat")
        (other / "scratch.txt").write_text("owner's work\n")
        tid = self.task(branch="feat")
        with self.assertRaises(pipeline.WorktreeRefused) as cm:
            self.pipe.worktree(self.get(tid))
        self.assertIn("uncommitted", str(cm.exception))
        self.assertTrue((other / "scratch.txt").exists())
        mine = self.task()
        wt = repos.native(self.pipe.worktree(self.get(mine)))
        (Path(wt) / "wip.txt").write_text("worker in progress\n")
        self.assertEqual(self.pipe.worktree(self.get(mine)), self.get(mine)["worktree"])   # owned: fine

    def test_the_dispatcher_sends_a_refused_task_to_the_owner_and_launches_nothing(self):
        self.fix.branch("feat", {"f.txt": "f\n"})
        other = Path(self.tmp.name) / "other"
        self.fix.git("worktree", "add", "-q", other, "feat")
        (other / "scratch.txt").write_text("x\n")
        tid = self.task(branch="feat")
        self.assertEqual(self.d.tick()["launched"], [])
        t = self.get(tid)
        self.assertEqual(t["status"], "needs-owner")
        self.assertIn("uncommitted", [e for e in ops.events(self.conn, task_id=tid, kind="status")][-1]["data"]["reason"])

    def test_wsl_placement_gets_the_worktree_in_its_config_form_and_the_launcher_maps_it(self):
        self.assertEqual(runner.to_wsl_path("R:\\repos\\acme-lib.worktrees\\drover-T1"),
                         "/mnt/r/repos/acme-lib.worktrees/drover-T1")
        self.assertEqual(repos.native("R:\\repos\\x") if sys.platform != "win32" else "/mnt/r/repos/x",
                         "/mnt/r/repos/x")
        self.assertEqual(pipeline.join_cfg("R:\\repos\\acme-lib.worktrees", "drover-T1"), "R:\\repos\\acme-lib.worktrees\\drover-T1")


class Safety(Base):
    def test_the_main_checkout_and_the_default_branch_are_refused(self):
        tid = self.task(branch="main")
        with self.assertRaises(pipeline.WorktreeRefused) as cm:
            self.pipe.worktree(self.get(tid))
        self.assertIn("default branch", str(cm.exception))
        other = self.task()
        ops.update_task(self.conn, other, worktree=str(self.fix.repo))          # the owner's own checkout
        with self.assertRaises(pipeline.WorktreeRefused) as cm:
            self.pipe.worktree(self.get(other))
        self.assertIn("main checkout", str(cm.exception))

    def test_work_is_branched_from_the_land_base_local_main_when_landing_is_local(self):
        self.fix.commit("landed.txt", "landed locally\n")                        # local main runs ahead of origin/main
        tid = self.task()
        wt = repos.native(self.pipe.worktree(self.get(tid)))
        self.assertEqual(self.fix.git("rev-parse", "HEAD", cwd=wt), self.fix.git("rev-parse", "main"))
        self.assertNotEqual(self.fix.git("rev-parse", "main"), self.fix.git("rev-parse", "origin/main"))
        sha = self.fix.commit("mine.txt", "mine\n", cwd=wt)
        plan = self.pipe.gate_plan({**self.get(tid), "worktree": wt}, self.prog, sha)
        self.assertEqual((plan["base"], plan["files"]), ("main", ["mine.txt"]))     # landed.txt is not in the diff

    def test_pushing_repos_branch_from_the_remote_default(self):
        prog = prog_dict(self.fix.repo, Path(self.tmp.name) / "wts2", push=True)
        pipe = pipeline.Pipeline(self.port, repos_map={"p": prog}, runs_dir=self.runs)
        self.assertEqual(pipe.base_ref(prog), "origin/main")

    def test_a_git_without_relative_paths_is_refused_for_cross_os_worktrees_not_made_absolute(self):
        real = pipeline.git

        def fake(repo, *a, **kw):
            if "--relative-paths" in a:
                return subprocess.CompletedProcess(a, 129, "", "error: unknown option `relative-paths'")
            return real(repo, *a, **kw)
        tid = self.task()
        from unittest import mock
        with mock.patch.object(pipeline, "git", fake), mock.patch.object(pipeline.Pipeline, "_cross_os", lambda s, d: True):
            with self.assertRaises(pipeline.WorktreeRefused) as cm:
                self.pipe.worktree(self.get(tid))
        self.assertIn("relative-paths", str(cm.exception))
        self.assertFalse((Path(self.tmp.name) / "wts" / f"drover-{tid}").exists())
        with mock.patch.object(pipeline, "git", fake), mock.patch.object(pipeline.Pipeline, "_cross_os", lambda s, d: False):
            # a path only one OS can open may fall back to absolute gitdir pointers
            self.assertTrue(self.pipe.worktree(self.get(tid)))

    def test_fetch_and_landing_git_calls_are_bounded(self):
        import inspect
        self.assertIn("timeout=FETCH_TIMEOUT_S", inspect.getsource(pipeline.Pipeline._create))
        self.assertEqual((pipeline.FETCH_TIMEOUT_S, landing.FETCH_TIMEOUT_S), (120, 120))
        r = pipeline.git(self.fix.repo, "fetch", "origin", timeout=0.000001)
        self.assertEqual(r.returncode, 124)

    def test_a_land_verdict_needs_the_whole_sha(self):
        tid, sha = self.to_gated()
        (spec,) = self.fl.of("review")
        self.fl.handles[spec["run_id"]].result = result("done", json.dumps(
            {"status": "done", "sha": sha, "verdict": f"LAND@{sha[:12]}", "findings": []}))
        self.d.tick()
        self.assertEqual(self.status(tid), "gated")
        self.assertFalse(landing.review_landed(self.conn, tid, sha))


class GatePlatform(Base):
    def test_a_wsl_gate_plan_maps_every_path_and_uses_python3(self):
        prog = prog_dict("R:\\repos\\r", "R:\\repos\\r.worktrees")
        prog["gate"]["platform"] = "wsl"
        pipe = pipeline.Pipeline(self.port, repos_map={"p": prog}, runs_dir="C:\\drover\\runs")
        plan = {"repo": "R:\\repos\\r.worktrees\\drover-T1", "base": "origin/main", "sha": "a" * 40,
                "commands": [["python3", "-c", "pass"]], "scope": None}
        with tempfile.TemporaryDirectory() as t:
            pipe.runs_dir = t
            argv, receipt = pipe.gate_argv(plan, 7, "P2", prog)
            body = json.loads((Path(t) / "7" / "plan.json").read_text())
        self.assertEqual(argv[0], "python3")
        self.assertEqual(body["repo"], "/mnt/r/repos/r.worktrees/drover-T1")
        self.assertTrue(argv[1].endswith("gatejob.py"))
        self.assertEqual(body["commands"], [["python3", "-c", "pass"]])      # the gate command itself is never rewritten

    def test_scoped_acme_lib_plan_runs_one_command_per_project(self):
        mp = Path(self.tmp.name) / "scope.json"
        mp.write_text(json.dumps({"patterns": {"src/*": ["Acme.Tests.Core"]}, "fallback": ["Acme.Tests.All"]}))
        prog = repos.normalize({"name": "acme-lib", "repo": str(self.fix.repo),
                                "gate": {"scope_map": str(mp), "commands": {
                                    "full": ["check", "-All"], "scoped": ["check", "-Project", "{project}"]}}})
        prog = {**prog, "repo": str(self.fix.repo), "worktrees": str(Path(self.tmp.name) / "w")}
        tid = self.task()
        pipe = pipeline.Pipeline(self.port, repos_map={"p": prog}, runs_dir=self.runs)
        base = pipeline.Pipeline(self.port, repos_map={"p": self.prog}, runs_dir=self.runs).worktree(self.get(tid))
        self.fix.commit("src/Acme.Acme.Runner/a.cs", "x\n", cwd=base)
        plan = pipe.gate_plan({**self.get(tid), "worktree": base}, prog, self.fix.git("rev-parse", "HEAD", cwd=base))
        self.assertEqual(plan["kind"], "scoped")
        self.assertEqual(len(plan["commands"]), len(plan["scope"]["projects"]))
        self.assertTrue(all(c[-2:-1] == ["-Project"] and c[-1].startswith("Acme.Tests.") for c in plan["commands"]))
        self.fix.commit("weird/unmapped.bin", "x\n", cwd=base)
        wide = pipe.gate_plan({**self.get(tid), "worktree": base}, prog, self.fix.git("rev-parse", "HEAD", cwd=base))
        self.assertEqual((wide["kind"], len(wide["commands"])), ("full", 1))
        self.assertIn("-All", wide["commands"][0])


class ReadyToCommit(Base):
    def ready(self, tid, message="Finish scoped work\n\nExplain the change."):
        spec = self.fl.of("worker")[-1]
        text = json.dumps({"status": "ready_to_commit", "commit_message": message, "note": "verified"})
        self.fl.handles[spec["run_id"]].result = result("ready_to_commit", text)
        self.d.tick()
        return spec["run_id"]

    def test_scoped_changes_are_committed_with_the_message_and_trailers_then_gated(self):
        tid = self.task(scope_files=["work.txt"])
        self.d.tick()
        wt = repos.native(self.get(tid)["worktree"])
        (Path(wt) / "work.txt").write_text("finished\n")
        rid = self.ready(tid)
        sha = self.fix.git("rev-parse", "HEAD", cwd=wt)
        self.assertEqual(self.fix.git("show", "HEAD:work.txt", cwd=wt), "finished")
        message = self.fix.git("log", "-1", "--format=%B", cwd=wt)
        self.assertEqual(message, f"Finish scoped work\n\nExplain the change.\n\nDrover-Task: {tid}\nDrover-Run: {rid}")
        self.assertEqual(self.fix.git("status", "--porcelain", cwd=wt), "")
        event = ops.events(self.conn, task_id=tid, kind="worker-done")[-1]
        self.assertEqual(event["data"]["sha"], sha)
        self.assertEqual(ops.list_runs(self.conn, tid)[0]["outcome"], "done")
        self.d.tick()
        self.assertEqual(self.status(tid), "gated")

    def test_out_of_scope_untracked_and_staged_changes_need_owner_without_staging_more(self):
        tid = self.task(scope_files=["work.txt"])
        self.d.tick()
        wt = Path(repos.native(self.get(tid)["worktree"]))
        original = self.fix.git("rev-parse", "HEAD", cwd=wt)
        (wt / "work.txt").write_text("allowed")
        (wt / "private.txt").write_text("outside")
        (wt / "base.txt").write_text("staged outside")
        self.fix.git("add", "base.txt", cwd=wt)
        before = self.fix.git("status", "--porcelain", cwd=wt)
        self.ready(tid)
        self.assertEqual(self.status(tid), "needs-owner")
        self.assertEqual(self.fix.git("rev-parse", "HEAD", cwd=wt), original)
        self.assertEqual(self.fix.git("status", "--porcelain", cwd=wt), before)
        refused = ops.events(self.conn, task_id=tid, kind="commit-refused")[-1]["data"]
        self.assertEqual(refused["paths"], ["base.txt", "private.txt"])
        self.assertEqual(ops.list_runs(self.conn, tid)[0]["outcome"], "blocked")
        self.assertEqual(self.fl.of("gate"), [])

    def test_no_changes_blocks_the_run_and_reports_no_changes(self):
        tid = self.task(scope_files=["work.txt"])
        self.d.tick()
        self.ready(tid)
        run = ops.list_runs(self.conn, tid)[0]
        self.assertEqual((run["outcome"], run["note"]), ("blocked", "no changes"))
        self.assertEqual(self.status(tid), "needs-owner")
        self.assertEqual(self.fl.of("gate"), [])

    def test_rename_cannot_hide_an_out_of_scope_deletion(self):
        tid = self.task(scope_files=["work.txt"])
        self.d.tick()
        wt = Path(repos.native(self.get(tid)["worktree"]))
        self.fix.git("mv", "base.txt", "work.txt", cwd=wt)
        self.ready(tid)
        self.assertEqual(self.status(tid), "needs-owner")
        refused = ops.events(self.conn, task_id=tid, kind="commit-refused")[-1]["data"]
        self.assertEqual(refused["paths"], ["base.txt"])

    def test_no_file_scope_does_not_authorize_any_change(self):
        tid = self.task()
        self.d.tick()
        wt = Path(repos.native(self.get(tid)["worktree"]))
        (wt / "work.txt").write_text("change")
        self.ready(tid)
        self.assertEqual(self.status(tid), "needs-owner")

    def test_scoped_staged_edit_deletion_and_nested_untracked_file_are_all_committed(self):
        tid = self.task(scope_files=["base.txt", "src/*", "notes.md:1-4"])
        self.d.tick()
        wt = Path(repos.native(self.get(tid)["worktree"]))
        (wt / "base.txt").unlink()
        (wt / "src").mkdir()
        (wt / "src" / "new file.txt").write_text("new")
        (wt / "notes.md").write_text("notes")
        self.fix.git("add", "notes.md", cwd=wt)
        self.ready(tid)
        self.assertEqual(self.fix.git("ls-tree", "-r", "--name-only", "HEAD", cwd=wt), "notes.md\nsrc/new file.txt")
        self.assertEqual(self.fix.git("status", "--porcelain", cwd=wt), "")

    def test_retry_after_a_ledger_error_recovers_the_commit_instead_of_blocking_or_committing_twice(self):
        tid = self.task(scope_files=["work.txt"])
        self.d.tick()
        wt = Path(repos.native(self.get(tid)["worktree"]))
        (wt / "work.txt").write_text("finished")
        append = self.port.append_event
        failed = []

        def flaky_event(kind, *args, **kw):
            if kind == "worker-done" and not failed:
                failed.append(True)
                raise RuntimeError("ledger unavailable")
            return append(kind, *args, **kw)

        self.port.append_event = flaky_event
        self.ready(tid)
        sha = self.fix.git("rev-parse", "HEAD", cwd=wt)
        self.d.tick()
        self.assertEqual(self.fix.git("rev-parse", "HEAD", cwd=wt), sha)
        self.assertEqual(self.status(tid), "running")
        self.assertEqual(ops.list_runs(self.conn, tid)[0]["outcome"], "done")
        self.assertEqual(ops.events(self.conn, task_id=tid, kind="worker-done")[-1]["data"]["sha"], sha)

    def test_fresh_pipeline_recovers_only_the_same_task_and_run_commit(self):
        tid = self.task(scope_files=["work.txt"])
        self.d.tick()
        wt = Path(repos.native(self.get(tid)["worktree"]))
        (wt / "work.txt").write_text("finished")
        rid = self.fl.of("worker")[-1]["run_id"]
        sha = self.pipe.commit_ready(self.get(tid), rid, "Finish work")
        restarted = pipeline.Pipeline(self.port, repos_map={"p": self.prog}, runs_dir=self.runs)
        self.assertEqual(restarted.commit_ready(self.get(tid), rid, "Finish work"), sha)
        with self.assertRaisesRegex(pipeline.CommitRefused, "no changes"):
            restarted.commit_ready(self.get(tid), rid + 100, "Finish work")
        (wt / "work.txt").write_text("later edit")
        with self.assertRaisesRegex(pipeline.CommitRefused, "worktree changed"):
            restarted.commit_ready(self.get(tid), rid, "Finish work")

class CodexReview(Base):
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

class GateAndReview(Base):
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
        self.prog["gate"]["commands"]["full"] = [sys.executable, "-c", "import sys; print('boom: it broke'); sys.exit(1)"]
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

    def test_a_worker_sha_that_is_not_the_worktree_head_is_a_failure_not_a_gate(self):
        tid = self.task()
        self.d.tick()
        self.worker_commits(tid)
        self.run_worker(tid, "a" * 40)
        self.d.tick()
        self.assertEqual(self.fl.of("gate"), [])
        self.assertIn(("ready", "retry"), self.states(tid))
        self.assertEqual(ops.list_runs(self.conn, tid)[0]["outcome"], "gate-fail")

    def test_a_docs_only_diff_with_a_scope_map_needs_no_test_project(self):
        mp = Path(self.tmp.name) / "map.json"
        mp.write_text(json.dumps({"always": [], "fallback": ["Big"], "patterns": {"*.md": []}}))
        self.prog["gate"]["scope_map"] = str(mp)
        tid = self.task()
        self.d.tick()
        self.run_worker(tid, self.worker_commits(tid, "notes.md"))
        self.d.tick()
        self.assertEqual(self.status(tid), "gated")
        self.assertEqual(self.fl.of("gate"), [])

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

    def test_a_review_in_flight_is_adopted_after_a_restart_not_requeued(self):
        tid, sha = self.to_gated()
        d2 = self.make()                                                  # a new dispatcher: the hub restarted
        d2.scan_fn = lambda want: {self.fl.of("review")[0]["run_id"]: "windows"}
        d2.launcher = type("L", (), {"adopt": lambda s, spec, started: FakeHandle(), "launch": self.fl.launch,
                                     "scan": None})()
        d2.recover_orphans()
        self.assertEqual(self.status(tid), "gated")
        self.assertEqual(len(d2.live), 1)
        self.assertEqual(list(d2.live.values())[0][2]["role"], "review")


class Decisions(Base):
    def test_every_decision_is_in_the_ledger_and_gets_its_outcome_after_the_next_run(self):
        self.prog["gate"]["commands"]["full"] = [sys.executable, "-c", "import sys; sys.exit(1)"]
        tid = self.task()
        self.d.tick()
        self.run_worker(tid, self.worker_commits(tid))
        self.d.tick()
        self.d.tick()
        names = [r["question_name"] for r in self.conn.execute("SELECT question_name FROM decisions ORDER BY id")]
        self.assertEqual(names, ["retry_or_escalate", "plan_wrong", "gate_failure_class"])
        self.assertTrue(all(r["outcome"] is None for r in self.conn.execute("SELECT outcome FROM decisions")))
        self.prog["gate"]["commands"]["full"] = [sys.executable, "-c", "pass"]       # the retry's gate passes
        self.d.tick()
        self.run_worker(tid, self.worker_commits(tid, "fix.txt"))
        self.d.tick()
        self.d.tick()
        self.assertEqual(self.status(tid), "gated")
        out = {r["question_name"]: json.loads(r["outcome"]) for r in self.conn.execute(
            "SELECT question_name, outcome FROM decisions WHERE outcome IS NOT NULL")}
        self.assertEqual(out["retry_or_escalate"], {"label": "retry"})
        self.assertEqual(out["gate_failure_class"], {"correct": True})
        self.assertEqual(json.loads(ops.get_setting(self.conn, "_pending_outcomes")), {})

    def test_outcome_shapes_for_a_failed_next_run(self):
        a = self.task()
        for took, want in (("retry", {"label": "escalate", "audit": "missed"}), ("escalate", {"audit": "needless"})):
            did = self.port.record_decision(question_name="retry_or_escalate", criteria_version="1", model="m",
                                            state_hash="h", answer=took, shadow=True, rule_answer=took)
            self.d._pend(a, did, "retry_or_escalate", took=took)
            self.d._settle(a, passed=False)
            self.assertEqual(json.loads(ops.get_decision(self.conn, did)["outcome"]) if False else
                             ops.get_decision(self.conn, did)["outcome"], want)
            self.assertIsNone(contract.decision_hit(took, ops.get_decision(self.conn, did)["outcome"]) if took == "escalate"
                              else None)


class Landing(Base):
    def test_the_loop_lands_a_reviewed_batch_on_local_main_and_does_not_push(self):
        self.fix.git("config", "user.name", "t")
        tid = self.task()
        wt = repos.native(self.pipe.worktree(self.get(tid)))
        sha = self.fix.commit("feature.txt", "feature\n", cwd=wt)
        self.reviewed(tid, sha)
        pdir = Path(self.tmp.name) / "repos"
        pdir.mkdir()
        (pdir / "p.toml").write_text(
            f'name = "p"\nrepo = {json.dumps(str(self.fix.repo))}\nworktrees = {json.dumps(str(Path(self.tmp.name) / "wts"))}\n'
            f'[gate]\nplatform = "windows"\n[gate.commands]\nfull = [{json.dumps(sys.executable)}, "-c", "pass"]\n')
        store = Store(Path(self.tmp.name) / "l.db")
        self.addCleanup(store.close)
        svc = LandingService(store, Settings(token="t", runs_dir=self.runs), repos_dir=pdir, gate_kw={"available": False})
        remote_before = self.fix.remote_head()
        ops.set_setting(self.conn, "dispatch_enabled", "false")
        self.assertEqual(svc.tick(), {})                                  # paused: nothing lands
        self.assertEqual(self.status(tid), "reviewed")
        ops.set_setting(self.conn, "dispatch_enabled", "true")
        out = svc.tick()
        self.assertEqual(out["p"]["outcome"], "landed", out)
        self.assertEqual(self.status(tid), "landed")
        self.assertEqual(self.fix.remote_head(), remote_before)           # push = false
        self.assertEqual((Path(self.fix.repo) / "feature.txt").read_text(), "feature\n")     # local main moved
        self.assertTrue(self.fix.git("merge-base", "--is-ancestor", "origin/main", "main") == "")
        self.assertEqual(ops.get_setting(self.conn, "landing_batch"), "")
        g = json.loads(ops.get_setting(self.conn, "gate"))
        self.assertEqual(g["status"], "landed")
        self.assertIn("median", g)

    def test_a_dirty_local_main_checkout_is_never_written_under(self):
        tid = self.task()
        wt = repos.native(self.pipe.worktree(self.get(tid)))
        sha = self.fix.commit("feature.txt", "feature\n", cwd=wt)
        self.reviewed(tid, sha)
        (Path(self.fix.repo) / "owner-wip.txt").write_text("mine\n")
        store = Store(Path(self.tmp.name) / "l.db")
        self.addCleanup(store.close)
        pdir = Path(self.tmp.name) / "repos"
        pdir.mkdir()
        (pdir / "p.toml").write_text(
            f'name = "p"\nrepo = {json.dumps(str(self.fix.repo))}\nworktrees = {json.dumps(str(Path(self.tmp.name) / "wts"))}\n'
            f'[gate]\nplatform = "windows"\n[gate.commands]\nfull = [{json.dumps(sys.executable)}, "-c", "pass"]\n')
        svc = LandingService(store, Settings(token="t", runs_dir=self.runs), repos_dir=pdir, gate_kw={"available": False})
        out = svc.tick()
        self.assertEqual(out["p"]["outcome"], "waiting-clean")
        self.assertEqual(list((self.runs / "landing").glob("*")), [])      # no batch gate was spent on it
        self.assertEqual(self.status(tid), "reviewed")
        self.assertTrue((Path(self.fix.repo) / "owner-wip.txt").exists())
        self.assertFalse((Path(self.fix.repo) / "feature.txt").exists())
        self.assertEqual(len(ops.open_questions(self.conn)), 1)
        svc.tick()
        self.assertEqual(len(ops.open_questions(self.conn)), 1)           # asked once while open


if __name__ == "__main__":
    unittest.main()
