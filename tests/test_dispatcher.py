import json
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

from drover import launch
from tests.dispatch_fixture import FakePipeline, Observations, make_dispatcher
from drover.ledger import connect, ops
from drover.port import DirectPort

WORKTREE = r"R:\repos\wt"      # a Windows path, so placement does not depend on the host running the tests
GO = {"verdict": "go", "slots": 10, "reasons": []}
DONE = '{"status": "done", "sha": "abc1234", "blocker": null, "note": "ok"}'
BLOCKED = '{"status": "blocked", "sha": "", "blocker": {"kind": "plan", "detail": "api differs"}, "note": ""}'


SPEC = {"run_id": 3, "backend": "claude", "model": "sonnet-5.5", "cwd": ".", "system_append": "SYS",
        "user_prompt": "BRIEF", "task": {"id": "T", "title": "t", "goal": "g"}}



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

    def scan(self, want_wsl=False):
        handles = self.handles.items() if isinstance(self.handles, dict) else [(s["run_id"], h) for s, h in zip(self.specs, self.handles)]
        return {rid: next((s.get("placement", "windows") for s in self.specs if s["run_id"] == rid), "windows")
                for rid, h in handles if h.poll() is None}

    def wsl_up(self):
        return True

    def launch(self, spec):
        if spec.get('role') == 'gate':
            from drover import ci
            out, sha = Path(spec['argv'][1]), spec['argv'][2]
            out.write_text(json.dumps({'version': ci.VERSION, 'sha': sha, 'passed': True, 'status': 'passed',
                                      'exit_code': 0, 'tree_unchanged': True, 'kind': 'full', 'log_tail': []}))
            return FakeHandle({'status': 'done', 'returncode': 0, 'record': None, 'text': '',
                               'usage': {'tokens_in': 0, 'tokens_out': 0, 'tokens_cached': 0, 'context': 0}})
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
        self.pipe = FakePipeline(self.port, Path(self.tmp.name) / "runs")
        self.pipe.current_head = lambda: "abc1234" if self.gate_ok else "FAIL x"
        self.d = self.make()

    def make(self, **kw):
        now = kw.pop("now", time.time)
        headroom = kw.pop("headroom", lambda backend: {"remaining_pct": 90.0})
        observed = Observations(self.conn, now, headroom, lambda backend: self.allow[backend])
        args = dict(launcher=self.fl, pipeline=self.pipe, resources=lambda **kw: self.verdict,
                    decide=self.decide, now=now, usage_poller=observed)
        args.update(kw)
        return make_dispatcher(self.port, **args)

    def decide(self, name, state, rule_answer=None):
        self.decisions.append(name)
        return {"answer": None, "shadow_answer": rule_answer, "acted": False, "confidence": 0.9,
                "criteria_version": "1", "model": "nimble-v3", "state_hash": "h", "probabilities": None}

    def task(self, ready=True, **kw):
        t = ops.create_task(self.conn, kw.pop("title", "t"), "p", read_access=[], repo_path=kw.pop("repo_path", None), **kw)
        if ready:
            ops.approve(self.conn, t["id"])
        return t["id"]

    def status(self, tid):
        return ops.get_task(self.conn, tid)["status"]

    def finish(self, i, result):
        self.fl.handles[i].result = result

    def events(self, kind):
        return ops.events(self.conn, kind=kind)



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



class HardeningFixtures(Base):
    def setUp(self):
        super().setUp()
        self.allow["codex"] = False
        self.t = 1_700_000_000.0


class CompletionWake(Base):
    def test_post_commit_hint_is_once_per_reaped_run_and_does_not_bypass_stop(self):
        task = self.task()
        self.d.tick()
        run_id = self.fl.specs[0]["run_id"]
        observed = []

        def committed(run_ids):
            rows = ops.events(self.conn, kind="worker-done")
            outcomes = {r["id"]: r["outcome"] for r in ops.list_runs(self.conn, task)}
            observed.append((run_ids, bool(rows), outcomes.get(run_id)))

        self.d.completion_wake = committed
        self.finish(0, res("done", DONE))
        ops.set_setting(self.conn, "dispatch_enabled", "false")
        out = self.d.tick()
        self.assertEqual(out["reaped"], [run_id])
        self.assertEqual(observed, [((run_id,), True, "done")])
        self.assertEqual(self.status(task), "running")  # wake did not admit a gate while dispatch was stopped

        self.d.tick()
        self.assertEqual(len(observed), 1)                 # duplicate ticks cannot reapply the completion
