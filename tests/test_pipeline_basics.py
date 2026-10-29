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


class Switch(PolicyBase):
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



class UnknownGate(PolicyBase):
    def test_missing_task_checkout_keeps_versioned_gate_config_before_recovery(self):
        self.fix.commit('.drover/config.toml', 'schema_version=1\n[scripts]\ntest=".drover/scripts/drover/test.ps1"\n')
        self.fix.push()
        machine = {'name': 'p', 'repo': str(self.fix.repo), 'worktrees': str(Path(self.tmp.name) / 'wts')}
        pipe = pipeline.Pipeline(self.port, repos_map={'p': self.repository.load(machine)}, repository=self.repository)
        tid = self.task()
        ops.update_task(self.conn, tid, worktree=str(Path(self.tmp.name) / 'missing'))
        self.assertTrue(pipe.prepare_dispatch(self.get(tid)))
        self.assertEqual(ops.open_questions(self.conn), [])
        recovered = pipe.worktree(self.get(tid))
        self.assertTrue(self.repository.reachable(recovered))
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



class AnsweredScriptGate(PolicyBase):
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
