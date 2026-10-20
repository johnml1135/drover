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
from tests.gitfix import Repo
from tests.dispatch_fixture import Observations, make_dispatcher

GO = {"verdict": "go", "slots": 10, "reasons": []}


def prog_dict(repo, worktrees, gate_cmd=None, unknown=False, push=False, **kw):
    script = Path(repo) / ".drover/scripts/drover/test.ps1"
    if (Path(repo) / ".git").exists() and not script.exists():
        script.parent.mkdir(parents=True, exist_ok=True)
        exe = sys.executable.replace("'", "''")
        script.write_text(f"if (-not $env:DROVER_FIXTURE_CODE) {{ $env:DROVER_FIXTURE_CODE = 'pass' }}\n& '{exe}' -c $env:DROVER_FIXTURE_CODE\nexit $LASTEXITCODE\n")
        subprocess.run(["git", "-C", str(repo), "add", str(script)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "add fixture action"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "push", "origin", "HEAD:main"], check=True, capture_output=True)
    raw = {"name": "p", "repo": str(repo), "worktrees": str(worktrees),
           "scripts": {} if unknown else {"test": ".drover/scripts/drover/test.ps1"},
           "run": {"env": {"DROVER_FIXTURE_CODE": gate_cmd[-1] if gate_cmd else "pass"}},
           "gate": {"platform": "windows", "timeout_s": 60, "idle_s": 60},
           "land": {"push": push}, **kw}
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
    """Workers and reviews wait for the test to finish them; CI runs execute through the real runner."""

    def __init__(self, runs_dir):
        self.specs, self.handles, self.runs_dir, self.gate_script = [], {}, runs_dir, []

    def launch(self, spec):
        self.specs.append(spec)
        if spec.get("role") == "gate" and self.gate_script:        # an infrastructure outcome: (status, returncode, output)
            status, code, text = self.gate_script.pop(0)
            (runner.run_dir(self.runs_dir, spec["run_id"]) / runner.OUT_FILE).write_text(text)
            h = FakeHandle({"status": status, "record": None, "text": "", "handoff": None, "returncode": code,
                            "timeout": "no output for 600s" if status == "timeout" else None,
                            "usage": {"tokens_in": 0, "tokens_out": 0, "tokens_cached": 0, "context": 0}})
        elif spec.get("role") == "gate":
            r = subprocess.run([sys.executable, '-m', 'drover.runner', '--run-id', str(spec['run_id']),
                                '--runs-dir', str(self.runs_dir), '--cwd', str(spec['cwd']),
                                '--run-config', json.dumps(spec.get('run_config', {})), '--', *spec['argv']],
                               capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1])
            out = runner.run_dir(self.runs_dir, spec["run_id"]) / runner.OUT_FILE
            h = FakeHandle({"status": "done" if r.returncode == 0 else "error", "record": None, "text": "", "handoff": None,
                            "usage": {"tokens_in": 0, "tokens_out": 0, "tokens_cached": 0, "context": 0},
                            "returncode": r.returncode, "timeout": None})
        else:
            h = FakeHandle()
        self.handles[spec["run_id"]] = h
        return h

    def scan(self, want_wsl=False):
        handles = self.handles.items() if isinstance(self.handles, dict) else [(s["run_id"], h) for s, h in zip(self.specs, self.handles)]
        return {rid: next((s.get("placement", "windows") for s in self.specs if s["run_id"] == rid), "windows")
                for rid, h in handles if h.poll() is None}

    def wsl_up(self):
        return True

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
        now = kw.pop("now", time.time)
        allow = kw.pop("allow", lambda backend: True)
        observed = Observations(self.conn, now, lambda backend: {"remaining_pct": 90.0}, allow)
        args = dict(launcher=self.fl, pipeline=self.pipe, resources=lambda **kw: GO,
                    decide=self.decide, now=now, usage_poller=observed)
        args.update(kw)
        return make_dispatcher(self.port, **args)

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


class Repos(unittest.TestCase):
    def test_fictional_examples_load(self):
        progs = repos.load_all(repos.REPOS_DIR)
        self.assertEqual(set(progs), {"acme-lib", "acme-app"})
        self.assertTrue(all(p["gate"]["unknown"] for p in progs.values()))
        self.assertTrue(all(not p["land"]["push"] for p in progs.values()))


class UnknownGate(Base):
    def test_missing_task_checkout_keeps_versioned_gate_config_before_recovery(self):
        self.fix.commit('.drover/config.toml', 'schema_version=1\n[scripts]\ntest=".drover/scripts/drover/test.ps1"\n')
        self.fix.git('push', 'origin', 'HEAD:main')
        machine = {'name': 'p', 'repo': str(self.fix.repo), 'worktrees': str(Path(self.tmp.name) / 'wts')}
        pipe = pipeline.Pipeline(self.port, repos_map={'p': repos.load(machine)})
        tid = self.task()
        ops.update_task(self.conn, tid, worktree=str(Path(self.tmp.name) / 'missing'))
        self.assertTrue(pipe.prepare_dispatch(self.get(tid)))
        self.assertEqual(ops.open_questions(self.conn), [])
        recovered = pipe.worktree(self.get(tid))
        self.assertTrue(placement.reachable(recovered))
        self.assertEqual(pipe.repo(self.get(tid))['scripts']['test'], '.drover/scripts/drover/test.ps1')

    def test_missing_repo_path_goes_to_owner_without_crashing(self):
        self.pipe._repos = {'p': {'name': 'p', 'gate': {'unknown': True}}}
        tid = self.task(repo='p')
        self.assertFalse(self.pipe.prepare_dispatch(self.get(tid)))
        self.assertEqual(self.get(tid)['status'], 'needs-owner')
        self.assertIsNone(self.pipe.repo(self.get(tid)))


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
        self.assertEqual([q["title"] for q in qs], ["What is the test script path for p?"])
        self.d.tick()
        self.assertEqual(len(ops.open_questions(self.conn)), 1)           # not again
        ops.answer_question(self.conn, qs[0]["id"], text=".drover/scripts/drover/test.ps1")
        self.d.tick()
        self.assertEqual(self.pipe.repo(self.get(a))["scripts"]["test"], ".drover/scripts/drover/test.ps1")
        self.assertEqual(len(self.fl.of("worker")), 3)                    # b and c came back too, and launched
        self.assertEqual(len(ops.open_questions(self.conn)), 0)
        self.assertFalse(self.pipe.repo({"repo": "p"})["gate"]["unknown"])

    def test_a_gate_cmd_written_without_an_owner_answer_is_ignored(self):
        t = self.task()
        self.d.tick()
        qid = ops.open_questions(self.conn)[0]["id"]
        for forged in (".drover/scripts/drover/test.ps1",                                     # a bare string, as a token would PUT
                       json.dumps({"cmd": "evil", "question": qid, "actor": "owner"}),   # the question is not answered
                       json.dumps({"cmd": "evil", "question": 999, "actor": "owner"})):  # not this repo's question
            ops.set_setting(self.conn, "gate_cmd_p", forged)
            self.assertTrue(self.pipe.repo({"repo": "p"})["gate"]["unknown"], forged)
            self.d.tick()
            self.assertEqual(self.status(t), "needs-owner")
        ops.answer_question(self.conn, qid, text=".drover/scripts/drover/other.ps1")
        ops.set_setting(self.conn, "gate_cmd_p", json.dumps({"cmd": "evil", "question": qid, "actor": "owner"}))
        self.assertEqual(self.pipe.repo({"repo": "p"})["scripts"]["test"], ".drover/scripts/drover/other.ps1")
        self.d.tick()
        self.assertEqual(self.pipe.repo({"repo": "p"})["scripts"]["test"], ".drover/scripts/drover/other.ps1")

    def test_an_answer_recorded_by_anyone_but_the_owner_is_ignored(self):
        self.task()
        self.d.tick()
        qid = ops.open_questions(self.conn)[0]["id"]
        with self.assertRaises(PermissionError):
            ops.answer_question(self.conn, qid, text=".drover/scripts/test.ps1", actor="dispatcher")
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
                with patch.object(pipeline, "git", fake), patch.object(placement, "is_linux_native", return_value=True), patch.object(placement, 'routes_through_wsl', return_value=False):
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
            self.assertEqual(cmd[cmd.index('--cwd') + 1], placement.to_wsl_path(expected))

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

    def test_a_dirty_foreign_worktree_is_untouched_and_the_task_branches_from_the_committed_tip(self):
        self.fix.branch("feat", {"f.txt": "f\n"})
        other = Path(self.tmp.name) / "other"
        self.fix.git("worktree", "add", "-q", other, "feat")
        (other / "scratch.txt").write_text("owner's work\n")
        tid = self.task(branch="feat")
        wt = repos.native(self.pipe.worktree(self.get(tid)))
        self.assertNotEqual(Path(wt).resolve(), other.resolve())
        self.assertEqual((other / "scratch.txt").read_text(), "owner's work\n")
        self.assertEqual(self.get(tid)["branch"], f"drover/{tid}")
        self.assertEqual(self.fix.git("rev-parse", "HEAD", cwd=wt), self.fix.git("rev-parse", "feat"))
        ev = [e for e in ops.events(self.conn, task_id=tid, kind="worktree-fallback")]
        self.assertEqual(ev[-1]["data"]["source_branch"], "feat")
        self.assertIn("uncommitted", ev[-1]["data"]["reason"])
        mine = self.task()
        wt = repos.native(self.pipe.worktree(self.get(mine)))
        (Path(wt) / "wip.txt").write_text("worker in progress\n")
        self.assertEqual(self.pipe.worktree(self.get(mine)), self.get(mine)["worktree"])   # owned: fine

    def test_a_missing_registered_worktree_gets_a_fresh_task_branch_and_nothing_is_pruned(self):
        self.fix.branch("feat", {"f.txt": "f\n"})
        other = Path(self.tmp.name) / "other"
        self.fix.git("worktree", "add", "-q", other, "feat")
        shutil.rmtree(other)                                   # registered in git, gone from disk
        tid = self.task(branch="feat")
        wt = repos.native(self.pipe.worktree(self.get(tid)))
        self.assertEqual(self.get(tid)["branch"], f"drover/{tid}")
        self.assertEqual(self.fix.git("rev-parse", "HEAD", cwd=wt), self.fix.git("rev-parse", "feat"))
        self.assertIn(str(other.name), self.fix.git("worktree", "list", "--porcelain"))   # registration kept
        self.assertIn("unreachable", ops.events(self.conn, task_id=tid, kind="worktree-fallback")[-1]["data"]["reason"])

    def test_a_wsl_only_worktree_of_a_windows_hub_is_probed_through_wsl_and_a_stale_one_falls_back(self):
        self.fix.branch("feat", {"f.txt": "f\n"})
        calls = []
        real = subprocess.run

        def fake_wsl(argv, *a, **kw):
            if argv[0] == "wsl.exe":
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 128, "", "fatal: cannot change to '/home/johnm/work/x'")
            return real(argv, *a, **kw)
        tid = self.task(branch="feat")
        ops.update_task(self.conn, tid, worktree="/home/acme/work/acme-lib.worktrees/x")
        with patch("subprocess.run", fake_wsl), \
                patch.object(placement, "routes_through_wsl", lambda p, nt=None: str(p).startswith("/home/")):
            wt = repos.native(self.pipe.worktree(self.get(tid)))
        self.assertTrue(calls and calls[0][:5] == ["wsl.exe", "-d", "Ubuntu", "--exec", "git"], calls)
        self.assertEqual(self.get(tid)["branch"], f"drover/{tid}")
        self.assertTrue(Path(wt).exists())

    def test_placement_routes_only_wsl_only_paths_through_wsl_from_windows(self):
        self.assertEqual(placement.git_argv("/home/johnm/w", ["status"], nt=True, distro="Ubuntu"),
                         ["wsl.exe", "-d", "Ubuntu", "--exec", "git", "-C", "/home/johnm/w", "status"])
        self.assertEqual(placement.git_argv("/home/johnm/w", ["status"], nt=False)[0], "git")
        self.assertEqual(placement.git_argv("/mnt/h/r", ["status"], nt=True)[0], "git")
        self.assertEqual(placement.git_argv("/home/j/w", ["worktree", "add", "H:\\x"], nt=True, distro="U")[-1], "/mnt/h/x")

    def _absolute_pointer(self, wt):
        admin = Path(self.fix.git("-C", wt, "rev-parse", "--absolute-git-dir"))
        (Path(wt) / ".git").unlink()
        (Path(wt) / ".git").write_text(f"gitdir: {admin}\n")

    def test_an_owned_worktree_gets_a_relative_pointer_without_a_repo_wide_extension(self):
        tid = self.task()
        wt = repos.native(self.pipe.worktree(self.get(tid)))
        self._absolute_pointer(wt)
        config_before = self.fix.git("config", "--local", "-l")        # worktree add itself may have set the extension
        self.pipe.worktree(self.get(tid))
        pointer = (Path(wt) / ".git").read_text().strip().removeprefix("gitdir: ")
        self.assertFalse(Path(pointer).is_absolute(), pointer)
        self.assertEqual(self.fix.git("rev-parse", "--is-inside-work-tree", cwd=wt), "true")
        self.assertEqual(self.fix.git("config", "--local", "-l"), config_before)

    def test_a_foreign_clean_worktree_pointer_is_not_edited(self):
        self.fix.branch("feat", {"f.txt": "f\n"})
        other = Path(self.tmp.name) / "other"
        self.fix.git("worktree", "add", "-q", other, "feat")
        self._absolute_pointer(other)
        before = (other / ".git").read_text()
        self.pipe.worktree(self.get(self.task(branch="feat")))
        self.assertEqual((other / ".git").read_text(), before)

    def test_a_failed_pointer_fix_logs_and_falls_back_to_a_fresh_branch(self):
        tid = self.task()
        wt = repos.native(self.pipe.worktree(self.get(tid)))
        self._absolute_pointer(wt)
        with patch.object(placement, "relativize_gitdir", return_value=False):
            self.pipe.worktree(self.get(tid))
        self.assertTrue(ops.events(self.conn, task_id=tid, kind="worktree-repair-failed"))
        self.assertTrue(ops.events(self.conn, task_id=tid, kind="worktree-fallback"))

    def test_placement_translates_path_arguments_only(self):
        a = placement.git_argv("/home/j/w", ["commit", "-m", "fix C:\\x", "--", "R:\\f"], nt=True, distro="U")
        self.assertEqual(a[-4:], ["-m", "fix C:\\x", "--", "R:\\f"])
        a = placement.git_argv("/home/j/w", ["worktree", "add", "-b", "C:\\br", "D:\\wt", "E:\\ref"], nt=True, distro="U")
        self.assertEqual(a[-5:], ["add", "-b", "C:\\br", "/mnt/d/wt", "E:\\ref"])

    def test_the_dispatcher_launches_on_a_fresh_branch_when_the_registered_worktree_is_dirty(self):
        self.fix.branch("feat", {"f.txt": "f\n"})
        other = Path(self.tmp.name) / "other"
        self.fix.git("worktree", "add", "-q", other, "feat")
        (other / "scratch.txt").write_text("x\n")
        tid = self.task(branch="feat")
        self.assertEqual(self.d.tick()["launched"], [tid])
        self.assertTrue((other / "scratch.txt").exists())

    def test_wsl_placement_gets_the_worktree_in_its_config_form_and_the_launcher_maps_it(self):
        self.assertEqual(placement.to_wsl_path("R:\\repos\\acme-lib.worktrees\\drover-T1"),
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

    def test_branch_syncs_remote_main_and_keeps_owner_checkout_consistent(self):
        sha = self.fix.branch("upstream", {"upstream.txt": "upstream"})
        self.fix.git("push", "-q", "origin", f"{sha}:main")
        tid = self.task()
        wt = repos.native(self.pipe.worktree(self.get(tid)))
        self.assertEqual(self.fix.git("rev-parse", "HEAD", cwd=wt), sha)
        self.assertEqual(self.fix.git("rev-parse", "main"), sha)
        self.assertEqual((self.fix.repo / "upstream.txt").read_text(), "upstream")
        self.assertEqual(self.fix.git("status", "--porcelain"), "")

    def test_divergence_prevents_dispatch_with_needs_owner_reason(self):
        self.fix.commit("local.txt")
        sha = self.fix.branch("upstream", {"upstream.txt": "upstream"})
        self.fix.git("push", "-q", "origin", f"{sha}:main")
        tid = self.task()
        self.assertEqual(self.d.tick()["launched"], [])
        self.assertEqual(self.status(tid), "needs-owner")
        self.assertEqual(self.states(tid)[-1][1], "main diverged from origin")
        self.assertIsNone(self.get(tid)["worktree"])

    def test_offline_pauses_only_affected_repo_and_alerts_once(self):
        self.fix.git("remote", "set-url", "origin", str(self.fix.root / "offline.git"))
        tid = self.task()
        self.d.tick()
        self.assertEqual(self.status(tid), "needs-owner")
        self.assertEqual(len(ops.open_questions(self.conn)), 1)
        other = self.task()
        self.d.tick()
        self.assertEqual(self.status(other), "blocked")
        self.assertEqual(len(ops.open_questions(self.conn)), 1)
        self.assertIsNone(self.get(other)["worktree"])
        self.assertEqual(self.fl.specs, [])
        self.fix.git("remote", "set-url", "origin", str(self.fix.remote))
        question = ops.open_questions(self.conn)[0]
        ops.answer_question(self.conn, question['id'], choice='Retry origin sync')
        self.d.tick()
        self.assertTrue(self.fl.specs)
        self.assertNotEqual(self.status(other), 'needs-owner')
        self.assertFalse(ops.open_questions(self.conn))

    def test_offline_repo_does_not_pause_another_repo(self):
        other_repo = Repo()
        self.addCleanup(other_repo.cleanup)
        self.pipe.all_repos()['acme-app'] = prog_dict(
            other_repo.repo, Path(self.tmp.name) / 'other-wts', name='acme-app')
        self.fix.git('remote', 'set-url', 'origin', str(self.fix.root / 'offline.git'))
        offline = self.task()
        healthy = self.task(repo='acme-app')
        out = self.d.tick()
        self.assertEqual(out['launched'], [healthy])
        self.assertEqual(self.status(offline), 'needs-owner')
        self.assertEqual(self.status(healthy), 'running')
        self.assertEqual(len(ops.open_questions(self.conn)), 1)

    def test_work_is_branched_from_the_land_base_local_main_when_landing_is_local(self):
        self.fix.commit("landed.txt", "landed locally\n")                        # local main runs ahead of origin/main
        tid = self.task()
        wt = repos.native(self.pipe.worktree(self.get(tid)))
        self.assertEqual(self.fix.git("rev-parse", "HEAD", cwd=wt), self.fix.git("rev-parse", "main"))
        self.assertNotEqual(self.fix.git("rev-parse", "main"), self.fix.git("rev-parse", "origin/main"))
        sha = self.fix.commit("mine.txt", "mine\n", cwd=wt)
        plan = self.pipe.ci.prepare(self.prog, wt, sha, "inspect")
        self.assertEqual((self.pipe.base_ref(self.prog), plan["files"]), ("main", ["mine.txt"]))     # landed.txt is not in the diff

    def test_pushing_repos_also_branch_from_local_main(self):
        prog = prog_dict(self.fix.repo, Path(self.tmp.name) / "wts2", push=True)
        pipe = pipeline.Pipeline(self.port, repos_map={"p": prog}, runs_dir=self.runs)
        self.assertEqual(pipe.base_ref(prog), "main")

    def test_a_git_without_relative_paths_is_refused_for_cross_os_worktrees_not_made_absolute(self):
        real = pipeline.git

        def fake(repo, *a, **kw):
            if "--relative-paths" in a:
                return subprocess.CompletedProcess(a, 129, "", "error: unknown option `relative-paths'")
            return real(repo, *a, **kw)
        tid = self.task()
        from unittest import mock
        with mock.patch.object(pipeline, "git", fake), mock.patch.object(placement, "is_linux_native", lambda d: False), patch.object(placement, 'routes_through_wsl', return_value=False):
            with self.assertRaises(pipeline.WorktreeRefused) as cm:
                self.pipe.worktree(self.get(tid))
        self.assertIn("relative-paths", str(cm.exception))
        self.assertFalse((Path(self.tmp.name) / "wts" / f"drover-{tid}").exists())
        with mock.patch.object(pipeline, "git", fake), mock.patch.object(placement, "is_linux_native", lambda d: True), patch.object(placement, 'routes_through_wsl', return_value=False):
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


class ReadyToCommit(Base):
    def ready(self, tid, message="Finish scoped work\n\nExplain the change."):
        spec = self.fl.of("worker")[-1]
        text = json.dumps({"status": "ready_to_commit", "commit_message": message, "note": "verified"})
        self.fl.handles[spec["run_id"]].result = result("ready_to_commit", text)
        self.d.tick()
        return spec["run_id"]

    def blocked(self, tid, kind="verification-environment"):
        spec = self.fl.of("worker")[-1]
        text = json.dumps({"status": "blocked", "blocker": {"kind": kind, "detail": "cannot run suites"}, "note": "done"})
        self.fl.handles[spec["run_id"]].result = result("blocked", text)
        self.d.tick()

    def test_environment_block_with_changes_goes_to_the_gate_without_replan(self):
        tid = self.task(scope_files=["work.txt"])
        self.d.tick()
        wt = repos.native(self.get(tid)["worktree"])
        (Path(wt) / "work.txt").write_text("finished\n")
        self.blocked(tid)
        self.assertEqual(ops.list_runs(self.conn, tid)[0]["outcome"], "done")
        self.assertEqual(ops.events(self.conn, task_id=tid, kind="replan-request"), [])
        self.assertEqual(len(ops.events(self.conn, task_id=tid, kind="env-block-ready")), 1)
        self.d.tick()
        self.assertEqual(self.status(tid), "gated")

    def test_environment_block_with_committed_work_ahead_of_base_goes_to_the_gate(self):
        tid = self.task(scope_files=["work.txt"])
        self.d.tick()
        wt = repos.native(self.get(tid)["worktree"])
        (Path(wt) / "work.txt").write_text("finished\n")
        self.fix.git("add", "work.txt", cwd=wt)
        self.fix.git("commit", "-m", "work", cwd=wt)
        self.blocked(tid, "missing_windows_worker")
        self.assertEqual(ops.list_runs(self.conn, tid)[0]["outcome"], "done")
        self.assertEqual(ops.events(self.conn, task_id=tid, kind="replan-request"), [])

    def test_committed_work_ahead_of_base_outside_scope_is_refused(self):
        tid = self.task(scope_files=["work.txt"])
        self.d.tick()
        wt = repos.native(self.get(tid)["worktree"])
        (Path(wt) / "other.txt").write_text("x\n")
        self.fix.git("add", "other.txt", cwd=wt)
        self.fix.git("commit", "-m", "work", cwd=wt)
        with self.assertRaisesRegex(pipeline.CommitRefused, "other.txt"):
            self.pipe.commit_ready(self.get(tid), 1, "m")

    def test_environment_block_without_changes_still_replans(self):
        tid = self.task(scope_files=["work.txt"])
        self.d.tick()
        self.blocked(tid)
        self.assertEqual(ops.list_runs(self.conn, tid)[0]["outcome"], "blocked")
        self.assertEqual(len(ops.events(self.conn, task_id=tid, kind="replan-request")), 1)

    def test_other_blocker_kinds_with_changes_still_replan(self):
        tid = self.task(scope_files=["work.txt"])
        self.d.tick()
        wt = repos.native(self.get(tid)["worktree"])
        (Path(wt) / "work.txt").write_text("x\n")
        self.blocked(tid, "plan-wrong")
        self.assertEqual(len(ops.events(self.conn, task_id=tid, kind="replan-request")), 1)

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
        self.assertEqual(self.fix.git("ls-tree", "-r", "--name-only", "HEAD", cwd=wt), ".drover/scripts/drover/test.ps1\nnotes.md\nsrc/new file.txt")
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

    def test_wsl_repo_runs_worker_wsl_gate_and_codex_review_without_windows(self):
        self.prog["run"] = {"platforms": ["wsl"], "default": "wsl"}
        self.prog["gate"]["platform"] = "wsl"
        runs = self.runs

        class HostRunsWslGate(FakeLauncher):
            """Checks the WSL gate argv and plan, then runs the same plan with host paths: wsl.exe and python3 are
            not on every host (this suite also runs on Windows)."""
            def launch(self, spec):
                if spec.get("role") == "gate":
                    assert spec["placement"] == "wsl" and spec["argv"][0] == "python3", spec["argv"]
                    d = runner.run_dir(runs, spec["run_id"])
                    body = json.loads((d / "plan.json").read_text())
                    assert body["repo"].startswith("/"), body["repo"]
                    body.update(repo=spec["cwd"], out=str(d / "receipt.json"), log=str(d / runner.OUT_FILE))
                    (d / "host-plan.json").write_text(json.dumps(body))
                    spec = {**spec, "argv": [sys.executable, str(Path(ci.__file__)),
                                             "--plan", str(d / "host-plan.json")]}
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
        self.prog["run"]["env"]["DROVER_FIXTURE_CODE"] = [sys.executable, "-c", "import sys; print('boom: it broke'); sys.exit(1)"][-1]
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
        self.prog["run"]["env"]["DROVER_FIXTURE_CODE"] = [sys.executable, "-c", "import sys; print('boom: it broke'); sys.exit(1)"][-1]
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


class DispatcherContract(Base):
    """The mandatory pipeline writes the contract consumed by the tuner."""
    def setUp(self):
        super().setUp()
        self.used = [10.0, 40.0]
        self.prog['review'].update(backend='codex', model='gpt-6.1-sol', high_risk_model='gpt-6.1-sol', seat=None)
        self.d.usage_poller = Observations(
            self.conn, self.d.now,
            lambda backend: {'windows': {
                'seven_day': {'remaining_pct': 100 - self.used[0]},
                'five_hour': {'remaining_pct': 100 - self.used[1]}}},
            lambda backend: backend == 'claude')

    def finish_worker(self, tid):
        self.run_worker(tid, self.worker_commits(tid))
        self.d.tick()                    # derive the gate from the finished worker
        self.d.tick()                    # ingest the real gate result

    def test_allowance_used_is_the_delta_and_the_tuner_reads_it(self):
        from drover import tuner
        tid = self.task()
        self.d.tick()
        self.used = [10.75, 41.5]
        self.finish_worker(tid)
        run = self.port.list_runs(tid)[0]
        self.assertEqual(run['allowance_used'], {'seven_day_pct': 0.75, 'five_hour_pct': 1.5})
        self.assertEqual(list(tuner._runs(self.conn))[0]['pct'], 0.75)
        self.assertEqual(contract.format_allowance(run['allowance_used']), '0.75% week, 1.5% 5h')

    def test_gate_receipt_event_and_first_pass_are_what_the_tuner_wants(self):
        from drover import tuner
        tid = self.task()
        self.d.tick()
        self.finish_worker(tid)
        row = list(tuner._runs(self.conn))[0]
        receipt = contract.parse_gate_receipt(row['gate_receipt'])
        self.assertTrue(receipt['ok'])
        self.assertGreater(tuner._diff_lines(row), 0)
        event = ops.events(self.conn, task_id=tid, kind=contract.GATE_EVENT)[0]['data']
        self.assertEqual(event['run'], row['id'])
        self.assertFalse(event['red'])
        self.assertGreaterEqual(event['duration_s'], 0)
        routes = tuner.route_stats([row], {})
        self.assertEqual(routes['worker:M'][(row['backend'], row['model'])]['first_pass_ok'], 1)

    def test_failed_gate_keeps_excerpt_for_retry_and_diff_lines_for_audit(self):
        from drover import tuner
        self.prog["run"]["env"]["DROVER_FIXTURE_CODE"] = [sys.executable, '-c', "print('FAIL a\\nFAIL b'); raise SystemExit(1)"][-1]
        tid = self.task()
        self.d.tick()
        self.finish_worker(tid)
        run = self.port.list_runs(tid)[0]
        self.assertEqual(run['outcome'], 'gate-fail')
        self.assertIn('FAIL b', contract.receipt_excerpt(run['gate_receipt']))
        self.assertIn('FAIL b', self.fl.of('worker')[-1]['user_prompt'])
        self.assertTrue(ops.events(self.conn, task_id=tid, kind=contract.GATE_EVENT)[0]['data']['red'])
        self.assertGreater(tuner._diff_lines(run), 0)

    def test_tune_batch_reads_dispatcher_gate_events(self):
        from drover import tuner
        for _ in range(5):
            tid = self.task()
            self.d.tick()
            self.finish_worker(tid)
        self.assertEqual(len(ops.events(self.conn, kind=contract.GATE_EVENT)), 5)
        self.assertEqual(tuner.tune_batch(self.conn, tuner.BATCH_DEFAULT), tuner.BATCH_RANGE[0])

    def test_pipeline_outcomes_are_in_the_contract_vocabulary(self):
        tid = self.task()
        self.d.tick()
        self.finish_worker(tid)
        outcomes = {r['outcome'] for r in self.port.list_runs(tid) if r['ended']}
        self.assertEqual(outcomes, {'gated', 'passed'})
        self.assertLessEqual(outcomes | set(dispatcher.FAIL_OUTCOMES), set(contract.OUTCOMES))


class Decisions(Base):
    def test_every_decision_is_in_the_ledger_and_gets_its_outcome_after_the_next_run(self):
        self.prog["run"]["env"]["DROVER_FIXTURE_CODE"] = [sys.executable, "-c", "import sys; sys.exit(1)"][-1]
        tid = self.task()
        self.d.tick()
        self.run_worker(tid, self.worker_commits(tid))
        self.d.tick()
        self.d.tick()
        names = [r["question_name"] for r in self.conn.execute("SELECT question_name FROM decisions ORDER BY id")]
        self.assertEqual(names, ["retry_or_escalate", "plan_wrong", "gate_failure_class"])
        self.assertTrue(all(r["outcome"] is None for r in self.conn.execute("SELECT outcome FROM decisions")))
        self.prog["run"]["env"]["DROVER_FIXTURE_CODE"] = [sys.executable, "-c", "pass"][-1]       # the retry's gate passes
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
    def test_restart_finishes_pending_ci_before_changed_queue_and_base_rebuild(self):
        import threading
        tid = self.task()
        wt = repos.native(self.pipe.worktree(self.get(tid)))
        self.reviewed(tid, self.fix.commit('first.txt', 'first', cwd=wt))
        count, release = Path(self.tmp.name) / 'ci-started', Path(self.tmp.name) / 'ci-release'
        self.addCleanup(release.touch)
        code = ('import pathlib,subprocess,time; '
                'head=lambda: subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip(); '
                'before=head(); pathlib.Path(' + repr(str(count)) + ').write_text(before); '
                '\nwhile not pathlib.Path(' + repr(str(release)) + ').exists(): time.sleep(.05)'
                '\nassert head()==before, "checkout reset while CI was active"')
        pdir = Path(self.tmp.name) / 'resume-repos'
        pdir.mkdir()
        (pdir / 'p.toml').write_text(
            f'name="p"\nrepo={json.dumps(str(self.fix.repo))}\n'
            f'worktrees={json.dumps(str(Path(self.tmp.name) / "wts"))}\n'
            '[scripts]\ntest=".drover/scripts/drover/test.ps1"\n[gate]\nplatform="windows"\ndocs_only=false\n'
            '[land]\npolicy="single"\n[run.env]\nDROVER_FIXTURE_CODE=' + json.dumps(code) + '\n')
        db_path = Path(self.tmp.name) / 'l.db'
        child_code = ('from hub.dispatch import LandingService; from hub.store import Store; '
                      'from hub.settings import Settings; '
                      f'LandingService(Store({str(db_path)!r}), Settings(runs_dir={str(self.runs)!r}), '
                      f'repos_dir={str(pdir)!r}).tick()')
        with open(Path(self.tmp.name) / 'landing-caller.log', 'w') as output:
            caller = subprocess.Popen([sys.executable, '-c', child_code], stdout=output, stderr=output)
            self.addCleanup(lambda: caller.poll() is None and caller.kill())
            deadline = time.monotonic() + 30
            while not count.exists() and caller.poll() is None and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(count.exists(), (Path(self.tmp.name) / 'landing-caller.log').read_text())
            original_status = next(self.runs.glob('ci-*/status.json'))
            caller.terminate()
            caller.wait(timeout=10)
        urgent = self.task(priority='P0')
        urgent_wt = repos.native(self.pipe.worktree(self.get(urgent)))
        self.reviewed(urgent, self.fix.commit('urgent.txt', 'urgent', cwd=urgent_wt))
        self.fix.commit('owner.txt', 'new main')
        timer = threading.Timer(.5, release.touch)
        timer.start()
        self.addCleanup(timer.join)
        store = Store(db_path)
        self.addCleanup(store.close)
        out = LandingService(store, Settings(runs_dir=self.runs), repos_dir=pdir).tick()
        self.assertEqual(out['p']['outcome'], 'landed')
        self.assertEqual(self.status(urgent), 'landed')
        self.assertEqual(self.status(tid), 'reviewed')
        original_result = json.loads(original_status.with_name('receipt.json').read_text())
        self.assertTrue(original_result['passed'])
        self.assertNotEqual(original_result['sha'], self.fix.git('rev-parse', 'main'))

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
            f'[gate]\nplatform = "windows"\n[scripts]\ntest = ".drover/scripts/drover/test.ps1"\n[run.env]\nDROVER_FIXTURE_CODE = "pass"\n')
        store = Store(Path(self.tmp.name) / "l.db")
        self.addCleanup(store.close)
        svc = LandingService(store, Settings(token="t", runs_dir=self.runs), repos_dir=pdir)
        remote_before = self.fix.remote_head()
        ops.set_setting(self.conn, "dispatch_enabled", "false")
        self.assertEqual(svc.tick(), {})                                  # paused: nothing lands
        self.assertEqual(self.status(tid), "reviewed")
        ops.set_setting(self.conn, "dispatch_enabled", "true")
        out = svc.tick()
        self.assertEqual(out["p"]["outcome"], "landed", out)
        self.assertEqual(self.status(tid), "landed")
        self.assertEqual(self.fix.remote_head(), remote_before)           # push = false
        self.assertEqual((Path(self.fix.repo) / "feature.txt").read_text(), "feature\n")
        self.assertEqual(self.fix.git("status", "--porcelain"), "")
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
            f'[gate]\nplatform = "windows"\n[scripts]\ntest = ".drover/scripts/drover/test.ps1"\n[run.env]\nDROVER_FIXTURE_CODE = "pass"\n')
        svc = LandingService(store, Settings(token="t", runs_dir=self.runs), repos_dir=pdir)
        out = svc.tick()
        self.assertEqual(out["p"]["outcome"], "refused")
        self.assertIn("main checkout dirty", out["p"]["error"])
        self.assertEqual(self.status(tid), "reviewed")
        self.assertEqual((Path(self.fix.repo) / "owner-wip.txt").read_text(), "mine\n")
        self.assertFalse((Path(self.fix.repo) / "feature.txt").exists())
        question, = ops.open_questions(self.conn)
        self.assertEqual(svc.tick()["p"]["outcome"], "held")
        ops.answer_question(self.conn, question['id'], choice='Retry landing')
        self.assertEqual(svc.tick()["p"]["outcome"], "refused")
        self.assertEqual([q['id'] for q in ops.open_questions(self.conn)], [question['id']])


if __name__ == "__main__":
    unittest.main()


class AnsweredScriptGate(Base):
    def test_answer_releases_worker_and_the_script_survives_commit_reload(self):
        self.pipe.all_repos()['p'] = prog_dict(self.fix.repo, Path(self.tmp.name) / 'wts', unknown=True)
        tid = self.task()
        self.d.tick()
        question = ops.open_questions(self.conn)[0]
        ops.answer_question(self.conn, question['id'], text='.drover/scripts/drover/test.ps1')
        self.assertEqual(self.d.tick()['launched'], [tid])
        self.run_worker(tid, self.worker_commits(tid))
        self.d.tick()
        self.d.tick()
        self.assertEqual(self.status(tid), 'gated')
        self.assertEqual([r['outcome'] for r in ops.list_runs(self.conn, tid) if r['role'] == 'gate'], ['passed'])
