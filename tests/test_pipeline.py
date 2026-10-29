"""The pipeline: pause switch, repo config, worktrees, gate runs, reviews, decisions and the landing loop."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from drover import config, contract, dispatcher, ci, placement, landing, launch, pipeline, repos, runner
from drover.ledger import connect, ops
from drover.port import DirectPort
from hub.dispatch import DispatcherService, LandingService
from tests.tiers import integration
from tests.fakeci import execute_gate, resources_go
from tests.gitfix import Repo
from tests.dispatch_fixture import Observations, make_dispatcher
from tests.pipeline_fixture import MemoryRepo, MemoryRepository, CheckoutGit, finish_gate, read_checkouts

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
    """Workers and reviews wait for the test to finish them; CI gates run their plan in-process (tests/fakeci.py)."""

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
            h = FakeHandle(execute_gate(spec, self.runs_dir))      # the gate script runs for real, in-process
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
    def new_fixture(self):
        return Repo()

    def setUp(self):
        self.fix = self.new_fixture()
        self.addCleanup(self.fix.cleanup)
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        from drover import config, disk
        import copy
        import os
        environment = patch.dict(os.environ, {'DROVER_HOME': str(root / 'state')})
        environment.start()
        self.addCleanup(environment.stop)
        original_config = config.get()
        self.addCleanup(config.use, original_config)
        cfg = copy.deepcopy(original_config)
        cfg['paths']['state_dir'] = str(root / 'state')
        cfg['paths']['wsl_worktrees'] = str(root / 'ext4')          # never the real ~/work
        cfg['repos_dir'] = str(root / 'state' / 'repos')
        config.use(cfg)
        # The detached landing caller needs the same admission as the parent mock.
        state = root / 'state'
        state.mkdir()
        (state / 'config.toml').write_text(config.dumps({'trackers': [
            {'kind': 'disk', 'name': tracker['name'], 'enabled': False}
            for tracker in cfg['trackers'] if tracker['kind'] == 'disk']}), encoding='utf-8')
        for context in (patch.object(disk, 'read', return_value=[dict(name='fixture', path=str(root), free_gb=1000., level='ok')]),):
            context.start()
            self.addCleanup(context.stop)
        self.conn = connect(root / "l.db")
        self.addCleanup(self.conn.close)
        self.runs = root / "runs"
        self.runs.mkdir()
        self.port = DirectPort(self.conn)
        self.fl = FakeLauncher(self.runs)
        self.prog = prog_dict(self.fix.repo, root / "wts")
        self.pipe = self.new_pipeline()
        self.pipe.ci.resource_check = resources_go
        self.decisions = []
        ops.set_setting(self.conn, "dispatch_enabled", "true")
        self.d = self.make()

    def new_pipeline(self):
        return pipeline.Pipeline(self.port, repos_map={"p": self.prog}, runs_dir=self.runs)

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
        worktree = self.get(tid)["worktree"]
        self.assertIsNotNone(worktree, f"{tid} has no worktree: it was never launched")
        return self.fix.commit(name, "x\n", cwd=repos.native(worktree))

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


class PolicyLauncher(FakeLauncher):
    """Script gate outcomes directly; receipt parsing and retry policy stay real."""
    def __init__(self, runs_dir):
        super().__init__(runs_dir)
        self.gate_code, self.gate_output = 0, ''

    def launch(self, spec):
        if spec.get('role') != 'gate' or self.gate_script:
            return super().launch(spec)
        self.specs.append(spec)
        handle = FakeHandle(finish_gate(spec, self.runs_dir, code=self.gate_code, output=self.gate_output))
        self.handles[spec['run_id']] = handle
        return handle


class PolicyBase(Base):
    def new_fixture(self):
        self.memory_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.memory_tmp.cleanup)
        fixture = MemoryRepo(self.memory_tmp.name)
        self.repository = MemoryRepository(fixture)
        return fixture

    def new_pipeline(self):
        return pipeline.Pipeline(self.port, repos_map={"p": self.prog}, runs_dir=self.runs,
                                 repository=self.repository)

    def make(self, **kw):
        dispatcher = super().make(**kw)
        # Replanning's source inspection is an independent Git consumer.
        dispatcher.replanning._inputs = self.planner_inputs
        return dispatcher

    def planner_inputs(self, task):
        checkout = task['worktree']
        reads = read_checkouts(self.repository, task['read_access'], self.pipe.all_repos(),
                               Path(self.tmp.name) / 'read-checkouts')
        return dict(repo=task['repo'], branch='refs/heads/' + task['branch'],
                    head=self.repository.head(checkout), dirty_digest='fixture',
                    checkout=checkout, read_checkouts=reads)

    def setUp(self):
        # Fail immediately if a policy case accidentally crosses a process/sleep boundary.
        for name in ('subprocess.Popen', 'time.sleep'):
            guard = patch(name, side_effect=AssertionError('policy test crossed ' + name))
            guard.start()
            self.addCleanup(guard.stop)
        # prog_dict remains the shared real-repository helper for importing modules.
        helper = patch(__name__ + '.prog_dict', side_effect=policy_prog)
        helper.start()
        self.addCleanup(helper.stop)
        super().setUp()
        drive = placement.drive
        shared = patch.object(placement, 'drive', side_effect=lambda p: 'T' if str(p).startswith((self.tmp.name, self.memory_tmp.name)) else drive(p))
        shared.start(); self.addCleanup(shared.stop)
        for context in (
            patch('drover.task_access.prepare', side_effect=lambda entries, configured=None, root=None:
                  read_checkouts(self.repository, entries, configured or self.pipe.all_repos(),
                                 root or Path(self.tmp.name) / 'read-checkouts')),
            patch('drover.task_access.verify'),
        ):
            context.start()
            self.addCleanup(context.stop)
        self.fl = PolicyLauncher(self.runs)
        self.d = self.make()


def policy_prog(repo, worktrees, gate_cmd=None, unknown=False, push=False, **kw):
    return repos.normalize({'name': 'p', 'repo': str(repo), 'worktrees': str(worktrees),
                            'scripts': {} if unknown else {'test': '.drover/scripts/drover/test.ps1'},
                            'run': {'env': {}},
                            'gate': {'platform': 'windows', 'timeout_s': 60, 'idle_s': 60},
                            'land': {'push': push}, **kw})
