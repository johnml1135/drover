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


class Safety(PolicyBase):
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
        self.fix.push(sha)
        tid = self.task()
        wt = repos.native(self.pipe.worktree(self.get(tid)))
        self.assertEqual(self.fix.head("HEAD", cwd=wt), sha)
        self.assertEqual(self.fix.head("main"), sha)
        self.assertEqual((self.fix.repo / "upstream.txt").read_text(), "upstream")
        self.assertEqual(self.fix.status(), [])

    def test_divergence_prevents_dispatch_with_needs_owner_reason(self):
        self.fix.commit("local.txt")
        sha = self.fix.branch("upstream", {"upstream.txt": "upstream"})
        self.fix.push(sha)
        tid = self.task()
        self.assertEqual(self.d.tick()["launched"], [])
        self.assertEqual(self.status(tid), "needs-owner")
        self.assertEqual(self.states(tid)[-1][1], "main diverged from origin")
        self.assertIsNone(self.get(tid)["worktree"])

    def test_offline_pauses_only_affected_repo_and_alerts_once(self):
        self.fix.online = False
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
        self.fix.online = True
        question = ops.open_questions(self.conn)[0]
        ops.answer_question(self.conn, question['id'], choice='Retry origin sync')
        self.d.tick()
        self.assertTrue(self.fl.specs)
        self.assertNotEqual(self.status(other), 'needs-owner')
        self.assertFalse(ops.open_questions(self.conn))

    def test_offline_repo_does_not_pause_another_repo(self):
        other_repo = MemoryRepo(Path(self.tmp.name) / "healthy")
        self.repository.fixtures.append(other_repo)
        self.pipe.all_repos()['acme-app'] = prog_dict(
            other_repo.repo, Path(self.tmp.name) / 'other-wts', name='acme-app')
        self.fix.online = False
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
        self.assertEqual(self.fix.head("HEAD", cwd=wt), self.fix.head("main"))
        self.assertNotEqual(self.fix.head("main"), self.fix.head("origin/main"))
        sha = self.fix.commit("mine.txt", "mine\n", cwd=wt)
        plan = self.pipe.ci.prepare(self.prog, wt, sha, "inspect")
        self.assertEqual((self.pipe.base_ref(self.prog), plan["files"]), ("main", ["mine.txt"]))     # landed.txt is not in the diff

    def test_pushing_repos_also_branch_from_local_main(self):
        prog = prog_dict(self.fix.repo, Path(self.tmp.name) / "wts2", push=True)
        pipe = pipeline.Pipeline(self.port, repos_map={"p": prog}, runs_dir=self.runs)
        self.assertEqual(pipe.base_ref(prog), "main")

    def test_a_git_without_relative_paths_is_refused_for_cross_os_worktrees_not_made_absolute(self):
        fake = CheckoutGit({'base.txt': b'base\n'}, legacy=True)
        tid = self.task()
        dest = Path(self.tmp.name) / 'wts' / f'drover-{tid}'
        adapter = pipeline.Repository()
        with patch.object(pipeline, 'git', fake), patch.object(placement, 'is_linux_native', return_value=False) as _placement_patch_4, patch('drover.placement_exec.is_linux_native', _placement_patch_4), patch('drover.placement_host.is_linux_native', _placement_patch_4), patch('drover.placement_paths.is_linux_native', _placement_patch_4), \
                patch.object(placement, 'routes_through_wsl', return_value=False) as _placement_patch_5, patch('drover.placement_git.routes_through_wsl', _placement_patch_5), patch('drover.placement_paths.routes_through_wsl', _placement_patch_5):
            with self.assertRaises(pipeline.WorktreeRefused) as cm:
                adapter.add(self.fix.repo, dest, f'drover/{tid}', 'main')
        self.assertIn('relative-paths', str(cm.exception))
        self.assertFalse(dest.exists())
        with patch.object(pipeline, 'git', fake), patch.object(placement, 'is_linux_native', return_value=True) as _placement_patch_6, patch('drover.placement_exec.is_linux_native', _placement_patch_6), patch('drover.placement_host.is_linux_native', _placement_patch_6), patch('drover.placement_paths.is_linux_native', _placement_patch_6), \
                patch.object(placement, 'routes_through_wsl', return_value=False) as _placement_patch_7, patch('drover.placement_git.routes_through_wsl', _placement_patch_7), patch('drover.placement_paths.routes_through_wsl', _placement_patch_7):
            # A native filesystem may use absolute pointers with old Git.
            adapter.add(self.fix.repo, dest, f'drover/{tid}', 'main')
            self.assertTrue((dest / '.git').is_file())
            self.assertEqual((dest / 'base.txt').read_bytes(), b'base\n')

    def test_fetch_and_landing_git_calls_are_bounded(self):
        from unittest import mock
        self.assertEqual((pipeline.FETCH_TIMEOUT_S, landing.FETCH_TIMEOUT_S), (120, 120))
        tid = self.task()
        fetch_args = ("fetch", "origin", "main",
                      "--refmap=+refs/heads/main:refs/remotes/origin/main")
        timeout_error = subprocess.TimeoutExpired(["git", *fetch_args], timeout=pipeline.FETCH_TIMEOUT_S)
        self.repository.sync = pipeline.Repository().sync
        real_pipeline_git = pipeline.git
        results = []

        def run_pipeline_git(*args, **kwargs):
            result = real_pipeline_git(*args, **kwargs)
            results.append(result)
            return result
        # Exercise the production fetch caller and wrapper, while making its timeout deterministic.
        with mock.patch.object(placement, "git", side_effect=timeout_error) as placed_git, mock.patch('drover.placement_git.git', placed_git):
            with mock.patch.object(pipeline, "git", side_effect=run_pipeline_git) as run_git:
                self.assertFalse(self.pipe._create(self.get(tid), self.prog, self.fix.repo, None))
        run_git.assert_called_once_with(self.fix.repo, *fetch_args, check=False,
                                        timeout=pipeline.FETCH_TIMEOUT_S)
        placed_git.assert_called_once_with(self.fix.repo, *fetch_args,
                                           timeout=pipeline.FETCH_TIMEOUT_S)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].returncode, 124)

    def test_a_land_verdict_needs_the_whole_sha(self):
        tid, sha = self.to_gated()
        (spec,) = self.fl.of("review")
        self.fl.handles[spec["run_id"]].result = result("done", json.dumps(
            {"status": "done", "sha": sha, "verdict": f"LAND@{sha[:12]}", "findings": []}))
        self.d.tick()
        self.assertEqual(self.status(tid), "gated")
        self.assertFalse(landing.review_landed(self.conn, tid, sha))



class ReadyToCommit(PolicyBase):
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
        self.fix.stage("work.txt", cwd=wt)
        self.fix.save(wt, "work")
        self.blocked(tid, "missing_windows_worker")
        self.assertEqual(ops.list_runs(self.conn, tid)[0]["outcome"], "done")
        self.assertEqual(ops.events(self.conn, task_id=tid, kind="replan-request"), [])

    def test_committed_work_ahead_of_base_outside_scope_is_refused(self):
        tid = self.task(scope_files=["work.txt"])
        self.d.tick()
        wt = repos.native(self.get(tid)["worktree"])
        (Path(wt) / "other.txt").write_text("x\n")
        self.fix.stage("other.txt", cwd=wt)
        self.fix.save(wt, "work")
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
        sha = self.fix.head("HEAD", cwd=wt)
        self.assertEqual(self.fix.tree(cwd=wt)["work.txt"].strip(), "finished")
        message = self.fix.message(wt)
        self.assertEqual(message, f"Finish scoped work\n\nExplain the change.\n\nDrover-Task: {tid}\nDrover-Run: {rid}")
        self.assertEqual(self.fix.status(wt), [])
        event = ops.events(self.conn, task_id=tid, kind="worker-done")[-1]
        self.assertEqual(event["data"]["sha"], sha)
        self.assertEqual(ops.list_runs(self.conn, tid)[0]["outcome"], "done")
        self.d.tick()
        self.assertEqual(self.status(tid), "gated")

    def test_out_of_scope_untracked_and_staged_changes_need_owner_without_staging_more(self):
        tid = self.task(scope_files=["work.txt"])
        self.d.tick()
        wt = Path(repos.native(self.get(tid)["worktree"]))
        original = self.fix.head("HEAD", cwd=wt)
        (wt / "work.txt").write_text("allowed")
        (wt / "private.txt").write_text("outside")
        (wt / "base.txt").write_text("staged outside")
        self.fix.stage("base.txt", cwd=wt)
        before = self.fix.status(wt)
        self.ready(tid)
        self.assertEqual(self.status(tid), "needs-owner")
        self.assertEqual(self.fix.head("HEAD", cwd=wt), original)
        self.assertEqual(self.fix.status(wt), before)
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
        replies = [subprocess.CompletedProcess([], 0, 'base.txt\0work.txt\0', ''),
                   subprocess.CompletedProcess([], 0, '', '')]
        with patch.object(pipeline, 'git', side_effect=replies) as run:
            self.assertEqual(pipeline.Repository().changes('checkout'), ['base.txt', 'work.txt'])
        self.assertEqual([call.args for call in run.call_args_list], [
            ('checkout', 'diff', '--name-only', '--no-renames', '-z', 'HEAD'),
            ('checkout', 'ls-files', '--others', '--exclude-standard', '-z')])
        tid = self.task(scope_files=["work.txt"])
        self.d.tick()
        wt = Path(repos.native(self.get(tid)["worktree"]))
        (Path(wt) / "base.txt").rename(Path(wt) / "work.txt")
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
        self.fix.stage("notes.md", cwd=wt)
        self.ready(tid)
        self.assertEqual('\n'.join(sorted(self.fix.tree(cwd=wt))), ".drover/scripts/drover/test.ps1\nnotes.md\nsrc/new file.txt")
        self.assertEqual(self.fix.status(wt), [])

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
        sha = self.fix.head("HEAD", cwd=wt)
        self.d.tick()
        self.assertEqual(self.fix.head("HEAD", cwd=wt), sha)
        self.assertEqual(self.status(tid), "running")
        self.assertEqual(ops.list_runs(self.conn, tid)[0]["outcome"], "done")
        self.assertEqual(ops.events(self.conn, task_id=tid, kind="worker-done")[-1]["data"]["sha"], sha)

    def test_commit_recovery_reads_keep_pipeline_git_semantics(self):
        task = dict(id='T', worktree=str(self.fix.repo), scope_files=['work.txt'])
        adapter = pipeline.Repository()
        pipe = pipeline.Pipeline(self.port, runs_dir=self.runs, repository=adapter)
        for recovered in (True, False):
            with self.subTest(recovered=recovered):
                def placed(repo, *args, **kw):
                    if args == ('rev-parse', 'HEAD'):
                        return subprocess.CompletedProcess(args, 0, 'commit-sha\n', '')
                    self.assertEqual(args, ('diff', '--name-only', '--no-renames', '-z', 'main...HEAD'))
                    return subprocess.CompletedProcess(args, 0, 'work.txt\0', '')
                with patch.object(adapter, 'changes', return_value=[]), \
                        patch.object(adapter, 'trailers', return_value=['T', '1'] if recovered else ['', '']), \
                        patch.object(adapter, 'ahead', return_value=True), \
                        patch.object(pipe, 'repo', return_value=self.prog), \
                        patch.object(ci, 'git', side_effect=AssertionError('commit recovery used CI Git')), \
                        patch.object(placement, 'git', side_effect=placed) as calls, patch('drover.placement_git.git', calls):
                    self.assertEqual(pipe.commit_ready(task, 1, 'Recover'), 'commit-sha')
                self.assertEqual(calls.call_count, 1 if recovered else 2)
                for call in calls.call_args_list:
                    self.assertEqual(call.kwargs, {'timeout': None})

    def test_repository_read_errors_during_commit_are_owner_refusals(self):
        for operation in ('head', 'changed_paths'):
            with self.subTest(operation=operation):
                tid = self.task(scope_files=['work.txt'])
                self.d.tick()
                wt = self.get(tid)['worktree']
                rid = self.fl.of('worker')[-1]['run_id']
                if operation == 'head':
                    (Path(wt) / 'work.txt').write_text('finished')
                    self.pipe.commit_ready(self.get(tid), rid, 'Finish work')
                else:
                    self.fix.commit('work.txt', 'finished', cwd=wt)
                with patch.object(self.repository, operation, side_effect=ci.CIError('git read failed')):
                    self.ready(tid)
                self.assertEqual(self.status(tid), 'needs-owner')
                self.assertEqual(ops.list_runs(self.conn, tid)[0]['outcome'], 'blocked')
                self.assertIn('git read failed', ops.list_runs(self.conn, tid)[0]['note'])
                self.assertEqual(self.fl.of('gate'), [])

    def test_fresh_pipeline_recovers_only_the_same_task_and_run_commit(self):
        tid = self.task(scope_files=["work.txt"])
        self.d.tick()
        wt = Path(repos.native(self.get(tid)["worktree"]))
        (wt / "work.txt").write_text("finished")
        rid = self.fl.of("worker")[-1]["run_id"]
        sha = self.pipe.commit_ready(self.get(tid), rid, "Finish work")
        restarted = pipeline.Pipeline(self.port, repos_map={"p": self.prog}, runs_dir=self.runs,
                                      repository=self.repository)
        self.assertEqual(restarted.commit_ready(self.get(tid), rid, "Finish work"), sha)
        with self.assertRaisesRegex(pipeline.CommitRefused, "no changes"):
            restarted.commit_ready(self.get(tid), rid + 100, "Finish work")
        (wt / "work.txt").write_text("later edit")
        with self.assertRaisesRegex(pipeline.CommitRefused, "worktree changed"):
            restarted.commit_ready(self.get(tid), rid, "Finish work")
