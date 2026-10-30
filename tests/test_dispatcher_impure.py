import io
import json
import subprocess
import tempfile
import time
import unittest
from unittest import mock
from unittest.mock import patch
from pathlib import Path

from drover import brief, dispatcher, launch
from drover.ledger import ops

from tests.test_dispatcher import Base, BLOCKED, DONE, WORKTREE, res


class RetryEscalate(Base):
    def run_fail(self, i):
        self.gate_ok = False
        self.finish(i, res("done", DONE))
        self.d.tick()

    def test_retry_then_escalate_then_replan(self):
        self.allow["codex"] = False
        t = self.task(size="S")
        self.d.tick()
        for i in range(6):
            self.run_fail(i)
        models = [sp["model"] for sp in self.fl.specs if sp.get("role") == "worker"]
        self.assertEqual(models, ["haiku-5.5", "haiku-5.5", "sonnet-5.5", "sonnet-5.5", "opus-5.5", "opus-5.5"])
        self.assertEqual(self.status(t), "blocked")
        ev = self.events("replan-request")
        self.assertEqual([e["data"]["trigger"] for e in ev], ["retries-exhausted"])
        self.assertEqual(len(self.events("escalate")), 2)

    def test_retry_brief_has_excerpt_and_shadow_logged(self):
        self.task(size="S")
        self.d.tick()
        self.run_fail(0)
        self.assertIn("FAIL x", self.fl.specs[1]["user_prompt"])
        self.assertIn("retry_or_escalate", self.decisions)
        row = self.conn.execute("SELECT shadow, rule_answer FROM decisions WHERE question_name='retry_or_escalate'").fetchone()
        self.assertEqual((row[0], row[1]), (1, '"retry"'))

    def test_no_record_counts_as_failure(self):
        t = self.task(size="S")
        self.d.tick()
        self.finish(0, res("no-record", "just chatter"))
        self.d.tick()
        self.assertEqual(ops.list_runs(self.conn, t)[0]["outcome"], "no-record")
        self.assertEqual(len(self.fl.specs), 2)

    def test_gate_pass(self):
        t = self.task(priority="P4")
        with mock.patch.object(self.pipe.ci, "prepare", wraps=self.pipe.ci.prepare) as prepare:
            self.d.tick()
            self.finish(0, res("done", DONE))
            self.d.tick()
            self.d.tick()
        self.assertEqual(prepare.call_args.kwargs["priority"], "P4")
        self.assertEqual(self.status(t), "gated")
        self.assertEqual(ops.list_runs(self.conn, t)[0]["tokens_in"], 10)



class Replan(Base):
    def test_blocker_trigger_holds_dependents(self):
        a = self.task()
        b = self.task(deps=[a], ready=True) if False else self.task(deps=[a])
        self.d.tick()
        self.finish(0, res("blocked", BLOCKED))
        self.d.tick()
        self.assertEqual(self.status(a), "blocked")
        self.assertEqual(self.status(b), "blocked")
        e = self.events("replan-request")[0]
        self.assertEqual((e["data"]["trigger"], e["data"]["blocker"]["kind"]), ("blocker", "plan"))

    def test_plan_wrong_acted(self):
        def yes(name, state, rule_answer=None):
            r = self.decide(name, state, rule_answer)
            if name == "plan_wrong":
                r.update(acted=True, answer=True)
            return r
        self.d = self.make(decide=yes)
        t = self.task(size="S")
        self.d.tick()
        self.gate_ok = False
        self.finish(0, res("done", DONE))
        self.d.tick()
        self.assertEqual(self.events("replan-request")[0]["data"]["trigger"], "plan-wrong")
        self.assertEqual(self.status(t), "blocked")

    def test_stale_brief_trigger(self):
        a = self.task()
        b = self.task(deps=[a])
        self.d = self.make(decide=lambda n, s, rule_answer=None: {**self.decide(n, s, rule_answer), "acted": True, "answer": True})
        self.verdict = {"verdict": "queue", "slots": 0, "reasons": []}
        self.d.tick()                                   # sets the cursor
        ops.set_status(self.conn, a, "claimed", "x")
        ops.set_status(self.conn, a, "running", "x")
        for s in ("gated", "reviewed", "landed"):
            ops.set_status(self.conn, a, s, "x")
        self.d.tick()
        self.assertEqual(self.events("replan-request")[0]["data"]["trigger"], "stale-brief")
        self.assertEqual(self.status(b), "blocked")



class FailClosed(Base):


    def test_main_wires_the_pipeline_and_stays_paused(self):
        import contextlib
        import io
        lock = str(Path(self.tmp.name) / "d.lock")       # never the real C:\\drover lock
        orig = dispatcher.IS_WINDOWS
        dispatcher.IS_WINDOWS = True
        self.addCleanup(setattr, dispatcher, "IS_WINDOWS", orig)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch('drover.disk.tick', return_value={
                'drives': [], 'pause': False, 'critical_tokens': []}):
            rc = dispatcher.main(["--db", str(Path(self.tmp.name) / "m.db"), "--once", "--json", "--lock", lock])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out.getvalue())["paused"])        # dispatch_enabled defaults to false

    def test_main_refuses_off_windows(self):
        import io
        from contextlib import redirect_stderr
        orig = dispatcher.IS_WINDOWS
        dispatcher.IS_WINDOWS = False
        self.addCleanup(setattr, dispatcher, "IS_WINDOWS", orig)
        err = io.StringIO()
        with redirect_stderr(err):
            rc = dispatcher.main(["--db", str(Path(self.tmp.name) / "m.db"), "--once",
                                  "--lock", str(Path(self.tmp.name) / "d.lock")])
        self.assertEqual(rc, 2)
        self.assertIn("only inside the Windows hub", err.getvalue())
        self.assertFalse((Path(self.tmp.name) / "m.db").exists())

    def test_empty_sha_is_blocked(self):
        rec = launch.parse_action_record('{"status": "done", "sha": "", "note": "x"}')
        self.assertEqual((rec["status"], rec["blocker"]["kind"]), ("blocked", "no-sha"))
        t = self.task()
        self.d.tick()
        self.finish(0, res("blocked", '{"status": "done", "sha": ""}'))
        self.d.tick()
        self.assertEqual(self.events("replan-request")[0]["data"]["blocker"]["kind"], "no-sha")



class ProviderErrors(Base):
    def test_owner_reset_excludes_reviews_of_previous_workers(self):
        tid = self.task()
        worker = ops.start_run(self.conn, tid, 'worker', 'claude', 'haiku-5.5')
        ops.finish_run(self.conn, worker, 'gated')
        review = ops.start_run(self.conn, tid, 'review', 'claude', 'sonnet-5.5')
        ops.finish_run(self.conn, review, 'provider-error')
        ops.set_status(self.conn, tid, 'needs-owner')
        ops.set_status(self.conn, tid, 'ready', actor='owner')
        self.d.tick()
        self.finish(0, {**res('error'), 'provider_error': 'capacity'})
        self.d.tick()
        row, = self.port.waiting.pending(('task', tid))
        self.assertEqual(row['data']['minutes'], 1)
        self.assertEqual(self.status(tid), 'ready')

    def test_provider_error_is_not_a_task_failure(self):
        t = self.task(size="S")
        for i in range(2):
            self.d.tick()
            self.finish(i, {**res("error"), "provider_error": "Selected model is at capacity."})
            self.d.tick()
            runs = ops.list_runs(self.conn, t)
            self.assertEqual(runs[-1]["outcome"], "provider-error")
            self.assertEqual(dispatcher.failures(runs), 0)
            self.assertEqual(self.status(t), "ready")
            self.assertEqual(self.events("replan-request"), [])
            now = self.d.now() + 10 ** 6
            self.d.now = lambda now=now: now
            self.port.waiting.expire(now)
        self.assertEqual(len(self.events("provider-error")), 2)

    def test_persistent_provider_errors_alert_the_owner(self):
        t = self.task(size="S")
        for i in range(dispatcher.MAX_PROVIDER_ERRORS):
            self.d.now = lambda i=i: time.time() + 10 ** 6 * (i + 1)
            self.d.tick()
            self.finish(i, {**res("error"), "provider_error": "429 rate limit"})
            self.d.tick()
        self.assertEqual(self.status(t), "needs-owner")

    def test_usage_classifies_provider_errors_from_the_stream(self):
        u = launch.Usage()
        u.feed({"type": "error", "message": "Selected model is at capacity. Please try a different model."})
        self.assertIn("at capacity", u.provider_error)
        v = launch.Usage()
        v.feed({"type": "turn.failed", "error": {"message": "compile failed in tests"}})
        self.assertIsNone(v.provider_error)
        w = launch.Usage()
        w.feed({"type": "result", "is_error": True, "api_error_status": 529, "result": "Overloaded"})
        self.assertIn("529", w.provider_error)
        x = launch.Usage()
        x.feed({"type": "result", "is_error": True, "result": "API Error: 429 rate_limit_error"})
        self.assertIsNotNone(x.provider_error)

    def test_real_failures_are_not_provider_errors(self):
        for text in ("3 tests failed: test_authentication_flow", "assertion 500 != 200",
                     "unauthorized access test failed", "quota calculation is wrong"):
            for ev in ({"type": "result", "is_error": True, "result": text}, {"type": "error", "message": text},
                       {"type": "turn.failed", "error": {"message": text}}):
                self.assertIsNone(launch.provider_error_of(ev), (text, ev["type"]))

    def test_provider_error_is_not_sticky(self):
        u = launch.Usage()
        u.feed({"type": "error", "message": "Selected model is at capacity."})
        u.feed({"type": "assistant", "message": {"content": [{"type": "text", "text": "carrying on"}]}})
        self.assertIsNone(u.provider_error)

    def test_owner_requeue_resets_the_provider_streak(self):
        t = self.task(size="S")
        for i in range(dispatcher.MAX_PROVIDER_ERRORS):
            self.d.now = lambda i=i: time.time() + 10 ** 6 * (i + 1)
            self.d.tick()
            self.finish(i, {**res("error"), "provider_error": "429 rate limit"})
            self.d.tick()
        self.assertEqual(self.status(t), "needs-owner")
        time.sleep(1.1)
        ops.set_status(self.conn, t, "ready", "owner", reason="requeue")
        n = dispatcher.MAX_PROVIDER_ERRORS
        self.d.now = lambda: time.time() + 10 ** 7
        self.d.tick()
        self.finish(n, {**res("error"), "provider_error": "429 rate limit"})
        self.d.tick()
        self.assertEqual(self.status(t), "ready")



class ReviewClassification(Base):
    def test_replan_land_uses_sha_case_insensitively(self):
        self.check_audit('done', 'LAND@ABCDEF0123456789ABCDEF0123456789ABCDEF01', [], None, 'land')

    def test_replan_land_precedes_provider_warning(self):
        self.check_audit('done', 'LAND@abcdef0123456789abcdef0123456789abcdef01', [], 'warning', 'land')

    def test_replan_hold_precedes_provider_warning(self):
        self.check_audit('done', 'HOLD', ['Fix the race'], 'warning', 'hold')

    def test_replan_provider_error_classification(self):
        self.check_audit('error', '', [], 'capacity', 'provider-error')

    def test_replan_timeout_classification(self):
        self.check_audit('timeout', '', [], None, 'timeout')

    def test_replan_empty_hold_classification(self):
        self.check_audit('done', 'HOLD', [], None, 'no-record')

    def check_audit(self, status, verdict, findings, provider_error, expected):
        from drover import contract
        sha = 'abcdef0123456789abcdef0123456789abcdef01'
        self.pipe.current_head = lambda: sha
        tid = self.task()
        ops.update_task(self.conn, tid, worktree=WORKTREE)
        ops.set_status(self.conn, tid, 'claimed')
        worker = ops.start_run(self.conn, tid, 'worker', 'claude', 'sonnet-5.5')
        ops.event(self.conn, 'worker-done', 'dispatcher', {'run': worker, 'sha': sha}, task_id=tid)
        ops.finish_run(self.conn, worker, contract.GATED)
        ops.set_status(self.conn, tid, 'gated')
        self.d.tick()
        spec, = [s for s in self.fl.specs if s['task']['id'] == tid and s['role'] == 'review']
        self.port.waiting.request_replan(tid, 'owner-request')
        text = json.dumps({'verdict': verdict, 'findings': findings, 'note': 'review'})
        self.finish(self.fl.specs.index(spec), {**res(status, text), 'provider_error': provider_error})
        self.d.tick()
        review = next(r for r in ops.list_runs(self.conn, tid) if r['id'] == spec['run_id'])
        self.assertEqual(review['outcome'], expected)
        self.assertEqual(self.status(tid), 'blocked')
        self.assertFalse(any(e['task_id'] == tid for e in self.events('review-done')))
        audit, = [e for e in self.events('replan.stage-finished') if e['task_id'] == tid]
        self.assertEqual(audit['data']['run'], spec['run_id'])


class FrameworkDetection(unittest.TestCase):
    def test_wsl_framework_detection_finds_directories_with_only_files(self):
        # Exercise real Git pathspec semantics, with a fake WSL transport and no commits.
        with tempfile.TemporaryDirectory() as tmp:
            def git(*args, input=None, **kw):
                # Text-mode stdin becomes CRLF on Windows; mktree treats the CR as
                # part of the entry name. Send exact bytes to build the same tree everywhere.
                result = subprocess.run(['git', '-C', tmp, *args], capture_output=True,
                                        input=input.encode('utf-8') if input is not None else None,
                                        check=True, **kw)
                result.stdout = result.stdout.decode('utf-8')
                return result
            git('init', '-q')
            blob = git('hash-object', '-w', '--stdin', input='framework instructions\n').stdout.strip()
            leaf = git('mktree', input=f'100644 blob {blob}\tREADME.md\n').stdout.strip()
            docs = git('mktree', input=f'040000 tree {leaf}\tsuperpowers\n').stdout.strip()
            tree = git('mktree', input=(f'040000 tree {leaf}\t.specify\n'
                                       f'040000 tree {docs}\tdocs\n'
                                       f'040000 tree {leaf}\topenspec\n')).stdout.strip()
            self.assertEqual(git('ls-tree', '-d', '--name-only', tree).stdout.splitlines(),
                             ['.specify', 'docs', 'openspec'])
            def wsl_git(root, *args, **kw):
                return git(*(tree if arg == 'HEAD' else arg for arg in args), **kw)
            with patch('drover.placement.routes_through_wsl', return_value=True), \
                    patch('drover.placement.git', side_effect=wsl_git):
                text = brief.framework_text('/home/seat/work/task')
            for path in ('openspec/', '.specify/', 'docs/superpowers/'):
                self.assertIn(path, text)
