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


class Worktrees(PolicyBase):
    def test_worktree_add_disables_autocrlf_including_the_legacy_fallback(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                committed = {'base.txt': b'base\nsecond line\n', 'binary.dat': b'\x00\xff\n'}
                fake = CheckoutGit(committed, legacy=legacy)
                tid = self.task()
                wt = Path(self.tmp.name) / 'wts' / f'drover-{tid}'
                with patch.object(pipeline, 'git', fake), patch.object(placement, 'is_linux_native', return_value=True) as _placement_patch_9, patch('drover.placement_exec.is_linux_native', _placement_patch_9), patch('drover.placement_host.is_linux_native', _placement_patch_9), patch('drover.placement_paths.is_linux_native', _placement_patch_9), \
                        patch.object(placement, 'routes_through_wsl', return_value=False) as _placement_patch_10, patch('drover.placement_git.routes_through_wsl', _placement_patch_10), patch('drover.placement_paths.routes_through_wsl', _placement_patch_10):
                    pipeline.Repository().add(self.fix.repo, wt, f'drover/{tid}', 'main')
                self.assertEqual(len(fake.adds), 2 if legacy else 1)
                for name, blob in committed.items():
                    self.assertEqual((wt / name).read_bytes(), blob)
                for args in fake.adds:
                    self.assertEqual(args[:4], ('-c', 'core.autocrlf=false', 'worktree', 'add'))

    def test_a_wsl_placement_creates_its_worktree_under_the_wsl_root_and_windows_keeps_the_h_root(self):
        ext4 = Path(self.tmp.name) / "ext4"
        with patch.object(placement, "wsl_worktrees_root", return_value=str(ext4)):
            wsl_tid, win_tid = self.task(), self.task()
            wsl = self.pipe.worktree(self.get(wsl_tid), "wsl")
            win = self.pipe.worktree(self.get(win_tid), "windows")
            default = self.pipe.worktree(self.get(self.task()))
        self.assertEqual(wsl, str(ext4 / "p.worktrees" / f"drover-{wsl_tid}"))
        self.assertEqual(win, str(Path(self.tmp.name) / "wts" / f"drover-{win_tid}"))
        self.assertTrue(default.startswith(str(Path(self.tmp.name) / "wts")))
        self.assertTrue((Path(wsl) / "base.txt").exists())
        again = self.pipe.worktree(self.get(wsl_tid), "windows")            # a persisted worktree keeps its place
        self.assertEqual(again, wsl)

    def test_example_home_paths_are_persisted_and_launched_as_absolute_paths(self):
        root = Path(self.tmp.name)
        repo = root / 'repos' / 'acme-lib'
        repo.parent.mkdir()
        self.fix.checkout(repo, "example", "main")
        with patch('os.path.expanduser', side_effect=lambda p: str(root / p[2:]) if p.startswith('~/') else p):
            with patch.object(repos, 'load', self.repository.load):
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
        self.assertEqual(self.fix.head("HEAD", cwd=wt), self.fix.head("origin/main"))
        tag = self.fix.tags.get(str(wt))
        self.assertEqual((tag["owner"], tag["repo"]), (pipeline.OWNER_PREFIX + tid, "p"))
        self.assertEqual(self.pipe.worktree(self.get(tid)), wt)             # idempotent

    def test_uses_an_existing_branch_and_a_clean_worktree_that_has_it(self):
        self.fix.branch("feat", {"f.txt": "f\n"})
        other = Path(self.tmp.name) / "other"
        self.fix.checkout(other, "feat")
        tid = self.task(branch="feat")
        self.assertEqual(Path(self.pipe.worktree(self.get(tid))), other)
        self.assertEqual(self.get(tid)["worktree"], str(other))

    def test_a_dirty_foreign_worktree_is_untouched_and_the_task_branches_from_the_committed_tip(self):
        self.fix.branch("feat", {"f.txt": "f\n"})
        other = Path(self.tmp.name) / "other"
        self.fix.checkout(other, "feat")
        (other / "scratch.txt").write_text("owner's work\n")
        tid = self.task(branch="feat")
        wt = repos.native(self.pipe.worktree(self.get(tid)))
        self.assertNotEqual(Path(wt).resolve(), other.resolve())
        self.assertEqual((other / "scratch.txt").read_text(), "owner's work\n")
        self.assertEqual(self.get(tid)["branch"], f"drover/{tid}")
        self.assertEqual(self.fix.head("HEAD", cwd=wt), self.fix.head("feat"))
        ev = [e for e in ops.events(self.conn, task_id=tid, kind="worktree-fallback")]
        self.assertEqual(ev[-1]["data"]["source_branch"], "feat")
        self.assertIn("uncommitted", ev[-1]["data"]["reason"])
        mine = self.task()
        wt = repos.native(self.pipe.worktree(self.get(mine)))
        (Path(wt) / "wip.txt").write_text("worker in progress\n")
        self.assertEqual(self.pipe.worktree(self.get(mine)), self.get(mine)["worktree"])   # owned: fine

    def test_unavailable_wsl_does_not_adopt_a_reachable_foreign_inventory_path(self):
        self.fix.branch('feat', {'f.txt': 'f\n'})
        phantom = Path(self.tmp.name) / 'other'
        self.fix.checkout(phantom, 'unrelated', 'main')
        before = (phantom / '.git').read_bytes()
        tid = self.task(branch='feat')
        def inventory(repo, side=None):
            if side == 'wsl':
                return []  # owning inventory unavailable
            return [dict(worktree=str(phantom), branch='refs/heads/feat', locked='wsl')]
        with patch.object(placement, 'IS_WINDOWS', True) as _placement_patch_1, patch('drover.placement_exec.IS_WINDOWS', _placement_patch_1), patch('drover.placement_git.IS_WINDOWS', _placement_patch_1), patch('drover.placement_host.IS_WINDOWS', _placement_patch_1), patch('drover.placement_paths.IS_WINDOWS', _placement_patch_1), patch('drover.placement_process.IS_WINDOWS', _placement_patch_1), patch('drover.placement_proof.IS_WINDOWS', _placement_patch_1), patch('drover.placement_tree.IS_WINDOWS', _placement_patch_1), patch('drover.placement_worker.IS_WINDOWS', _placement_patch_1), \
                patch.object(placement, 'routes_through_wsl', return_value=False) as _placement_patch_2, patch('drover.placement_git.routes_through_wsl', _placement_patch_2), patch('drover.placement_paths.routes_through_wsl', _placement_patch_2), \
                patch.object(self.repository, 'inventory', side_effect=inventory):
            wt = repos.native(self.pipe.worktree(self.get(tid)))
        self.assertNotEqual(Path(wt).resolve(), phantom.resolve())
        self.assertEqual(self.get(tid)['branch'], f'drover/{tid}')
        self.assertEqual(self.fix.head('HEAD', cwd=wt), self.fix.head('feat'))
        self.assertEqual(self.repository.branch(phantom), 'unrelated')
        self.assertEqual((phantom / '.git').read_bytes(), before)
        self.assertIsNone(self.fix.tags.get(str(phantom)))
        event = ops.events(self.conn, task_id=tid, kind='worktree-fallback')[-1]
        self.assertEqual(event['data']['source_branch'], 'feat')
        self.assertIn('unreachable', event['data']['reason'])


    def test_a_missing_registered_worktree_gets_a_fresh_task_branch_and_nothing_is_pruned(self):
        self.fix.branch("feat", {"f.txt": "f\n"})
        other = Path(self.tmp.name) / "other"
        self.fix.checkout(other, "feat")
        shutil.rmtree(other)                                   # registered in git, gone from disk
        tid = self.task(branch="feat")
        wt = repos.native(self.pipe.worktree(self.get(tid)))
        self.assertEqual(self.get(tid)["branch"], f"drover/{tid}")
        self.assertEqual(self.fix.head("HEAD", cwd=wt), self.fix.head("feat"))
        self.assertIn(str(other.name), '\n'.join(self.fix.checkouts))   # registration kept
        self.assertIn("unreachable", ops.events(self.conn, task_id=tid, kind="worktree-fallback")[-1]["data"]["reason"])

    def test_a_wsl_only_worktree_of_a_windows_hub_is_probed_through_wsl_and_a_stale_one_falls_back(self):
        self.fix.branch("feat", {"f.txt": "f\n"})
        calls = []
        def fake_wsl(argv, *a, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 128, '', "fatal: cannot change to '/home/johnm/work/x'")
        tid = self.task(branch='feat')
        stale = '/home/acme/work/acme-lib.worktrees/x'
        ops.update_task(self.conn, tid, worktree=stale)
        reachable = self.repository.reachable
        def probe(path):
            return placement.reachable(path) if str(path) == stale else reachable(path)
        with patch('subprocess.run', fake_wsl), \
                patch.object(placement, 'routes_through_wsl', lambda p, nt=None: str(p) == stale) as _placement_patch_3, patch('drover.placement_git.routes_through_wsl', _placement_patch_3), patch('drover.placement_paths.routes_through_wsl', _placement_patch_3), \
                patch.object(self.repository, 'reachable', side_effect=probe):
            wt = repos.native(self.pipe.worktree(self.get(tid)))
        self.assertTrue(calls and calls[0][:5] == ['wsl.exe', '-d', 'Ubuntu', '--exec', 'git'], calls)
        self.assertEqual(self.get(tid)["branch"], f"drover/{tid}")
        self.assertTrue(Path(wt).exists())

    def test_placement_routes_only_wsl_only_paths_through_wsl_from_windows(self):
        self.assertEqual(placement.git_argv("/home/johnm/w", ["status"], nt=True, distro="Ubuntu"),
                         ["wsl.exe", "-d", "Ubuntu", "--exec", "git", "-C", "/home/johnm/w", "status"])
        self.assertEqual(placement.git_argv("/home/johnm/w", ["status"], nt=False)[0], "git")
        self.assertEqual(placement.git_argv("/mnt/h/r", ["status"], nt=True)[0], "git")
        self.assertEqual(placement.git_argv("/home/j/w", ["worktree", "add", "H:\\x"], nt=True, distro="U")[-1], "/mnt/h/x")

    def _absolute_pointer(self, wt):
        admin = Path(self.fix.admin(wt))
        (Path(wt) / ".git").unlink()
        (Path(wt) / ".git").write_text(f"gitdir: {admin}\n")

    def test_an_owned_worktree_gets_a_relative_pointer_without_a_repo_wide_extension(self):
        tid = self.task()
        wt = repos.native(self.pipe.worktree(self.get(tid)))
        self._absolute_pointer(wt)
        config_file = self.fix.repo / '.git' / 'config'
        config_before = config_file.read_bytes()
        self.pipe.worktree(self.get(tid))
        pointer = (Path(wt) / ".git").read_text().strip().removeprefix("gitdir: ")
        self.assertFalse(Path(pointer).is_absolute(), pointer)
        self.assertTrue((Path(wt) / pointer).is_dir())
        self.assertEqual(config_file.read_bytes(), config_before)

    def test_a_foreign_clean_worktree_pointer_is_not_edited(self):
        self.fix.branch("feat", {"f.txt": "f\n"})
        other = Path(self.tmp.name) / "other"
        self.fix.checkout(other, "feat")
        self._absolute_pointer(other)
        before = (other / ".git").read_text()
        self.pipe.worktree(self.get(self.task(branch="feat")))
        self.assertEqual((other / ".git").read_text(), before)

    def test_a_failed_pointer_fix_logs_and_falls_back_to_a_fresh_branch(self):
        tid = self.task()
        wt = repos.native(self.pipe.worktree(self.get(tid)))
        self._absolute_pointer(wt)
        with patch.object(self.repository, "repair", return_value=False):
            self.pipe.worktree(self.get(tid))
        self.assertTrue(ops.events(self.conn, task_id=tid, kind="worktree-repair-failed"))
        self.assertTrue(ops.events(self.conn, task_id=tid, kind="worktree-fallback"))

    def test_placement_translates_path_arguments_only(self):
        a = placement.git_argv("/home/j/w", ["commit", "-m", "fix C:\\x", "--", "R:\\f"], nt=True, distro="U")
        self.assertEqual(a[-4:], ["-m", "fix C:\\x", "--", "R:\\f"])
        a = placement.git_argv("/home/j/w", ["worktree", "add", "-b", "C:\\br", "D:\\wt", "E:\\ref"], nt=True, distro="U")
        self.assertEqual(a[-5:], ["add", "-b", "C:\\br", "/mnt/d/wt", "E:\\ref"])

    def test_the_dispatcher_decides_placement_before_it_makes_the_worktree(self):
        seen = []
        real = self.pipe.worktree

        def spy(task, where=None):
            seen.append(where)
            return real(task, where)
        tid = self.task()
        with patch.object(self.pipe, "worktree", spy):
            self.assertEqual(self.d.tick()["launched"], [tid])
        self.assertEqual(seen, ["windows"])                                 # prog_dict is a windows-only repo

    def test_a_linux_task_gets_a_wsl_worktree_only_where_the_repo_allows_wsl(self):
        seen = []
        real = self.pipe.worktree

        def spy(task, where=None):
            seen.append(where)
            return real(task, where)
        both = prog_dict(self.fix.repo, Path(self.tmp.name) / "wts", run={"env": {}, "platforms": ["windows", "wsl"],
                                                                         "default": "windows"})
        ext4 = Path(self.tmp.name) / "ext4"
        tid = self.task(goal="Port it. platform: linux")
        with patch.object(self.pipe, "worktree", spy), patch.object(placement, "wsl_worktrees_root", return_value=str(ext4)):
            self.pipe._repos = {"p": both}
            self.d.tick()
            self.assertEqual(seen, ["wsl"])
            self.assertTrue((ext4 / "p.worktrees" / f"drover-{tid}").is_dir())
            seen.clear()
            win_only = self.task(goal="Port it. platform: linux")
            self.pipe._repos = {"p": prog_dict(self.fix.repo, Path(self.tmp.name) / "wts",
                                               run={"env": {}, "platforms": ["windows"], "default": "windows"})}
            self.d.tick()
        self.assertEqual(seen, [])                # refused before any worktree is made
        self.assertEqual(self.states(win_only)[-1][0], "needs-owner")
        self.assertFalse((ext4 / "p.worktrees" / f"drover-{win_only}").exists())

    def test_the_dispatcher_launches_on_a_fresh_branch_when_the_registered_worktree_is_dirty(self):
        self.fix.branch("feat", {"f.txt": "f\n"})
        other = Path(self.tmp.name) / "other"
        self.fix.checkout(other, "feat")
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
