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
from tests.tiers import integration
from tests.fakeci import execute_gate, resources_go
from tests.gitfix import Repo
from tests.dispatch_fixture import Observations, make_dispatcher
from tests.pipeline_fixture import MemoryRepo, MemoryRepository, CheckoutGit, finish_gate, read_checkouts

from tests.test_pipeline import Base, PolicyBase, FakeHandle, result, policy_prog as prog_dict


class DispatcherContract(PolicyBase):
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
        self.fl.gate_code, self.fl.gate_output = 1, 'FAIL a\nFAIL b'
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



class Decisions(PolicyBase):
    def test_every_decision_is_in_the_ledger_and_gets_its_outcome_after_the_next_run(self):
        self.fl.gate_code = 1
        tid = self.task()
        self.d.tick()
        self.run_worker(tid, self.worker_commits(tid))
        self.d.tick()
        self.d.tick()
        names = [r["question_name"] for r in self.conn.execute("SELECT question_name FROM decisions ORDER BY id")]
        self.assertEqual(names, ["retry_or_escalate", "plan_wrong", "gate_failure_class"])
        self.assertTrue(all(r["outcome"] is None for r in self.conn.execute("SELECT outcome FROM decisions")))
        self.fl.gate_code = 0       # the retry's gate passes
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



@integration
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
                      'from tests.fakeci import resources_go; '
                      'from hub.settings import Settings; '
                      f'service=LandingService(Store({str(db_path)!r}), Settings(runs_dir={str(self.runs)!r}), '
                      f'repos_dir={str(pdir)!r}); service.ci.resource_check=resources_go; service.tick()')
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
        service = LandingService(store, Settings(runs_dir=self.runs), repos_dir=pdir)
        service.ci.resource_check = resources_go
        out = service.tick()
        self.assertEqual(out['p']['outcome'], 'landed')
        self.assertEqual(self.status(urgent), 'landed')
        self.assertEqual(self.status(tid), 'reviewed')
        original_result = json.loads(original_status.with_name('receipt.json').read_text())
        self.assertTrue(original_result['passed'])
        self.assertNotEqual(original_result['sha'], self.fix.git('rev-parse', 'main'))

    def test_the_loop_lands_a_reviewed_batch_on_local_main_and_does_not_push(self):
        self.fix.git("config", "user.name", "t")
        self.fix.git('config', 'core.autocrlf', 'true')
        committed = subprocess.run(['git', '-C', str(self.fix.repo), 'show', 'main:base.txt'],
                                   capture_output=True, check=True).stdout
        tid = self.task(scope_files=["feature.txt"])
        wt = repos.native(self.pipe.worktree(self.get(tid)))
        self.assertEqual((Path(wt) / 'base.txt').read_bytes(), committed)
        admin = self.fix.git('rev-parse', '--absolute-git-dir', cwd=wt)
        # Git for Windows may mark the pointer read-only; replace it as fixture setup.
        dotgit = Path(wt) / '.git'
        dotgit.chmod(0o600)
        dotgit.unlink()
        dotgit.write_text(f'gitdir: {admin}\n')
        self.pipe.worktree(self.get(tid))
        self.assertEqual(self.fix.git('rev-parse', '--is-inside-work-tree', cwd=wt), 'true')
        (Path(wt) / "feature.txt").write_text("feature\n")
        sha = self.pipe.commit_ready(self.get(tid), 1, "Finish feature")
        self.assertIn("Drover-Task: " + tid, self.fix.git("log", "-1", "--format=%B", cwd=wt))
        self.reviewed(tid, sha)
        pdir = Path(self.tmp.name) / "repos"
        pdir.mkdir()
        (pdir / "p.toml").write_text(
            f'name = "p"\nrepo = {json.dumps(str(self.fix.repo))}\nworktrees = {json.dumps(str(Path(self.tmp.name) / "wts"))}\n'
            f'[gate]\nplatform = "windows"\n[scripts]\ntest = ".drover/scripts/drover/test.ps1"\n[run.env]\nDROVER_FIXTURE_CODE = "pass"\n')
        store = Store(Path(self.tmp.name) / "l.db")
        self.addCleanup(store.close)
        svc = LandingService(store, Settings(token="t", runs_dir=self.runs), repos_dir=pdir)
        svc.ci.resource_check = resources_go
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
        svc.ci.resource_check = resources_go
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
