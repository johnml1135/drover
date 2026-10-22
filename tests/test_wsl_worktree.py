"""ADR 0007: an ext4 (WSL) worktree is created by the WSL git against the one Windows repository."""
import subprocess
import unittest
from unittest.mock import patch

from drover import placement

EXT4 = "/home/dev/work/acme.worktrees/drover-T1"


class AddOnExt4(unittest.TestCase):
    def run_add(self, dest, nt):
        calls = []

        def run(repo, *args, check=False, **kw):
            calls.append((repo, args, kw))
            return subprocess.CompletedProcess(args, 0, "", "")
        with patch.object(placement, "IS_WINDOWS", nt):
            placement.add_worktree("H:\\repos\\acme", dest, "-b", "drover/T1", dest, "main", run=run)
        return calls

    def test_an_ext4_destination_runs_every_git_call_on_the_wsl_side(self):
        calls = self.run_add(EXT4, True)
        self.assertEqual([c[1][c[1].index("worktree") + 1] for c in calls], ["add", "lock"])
        self.assertTrue(all(c[2] == {"side": "wsl"} for c in calls), calls)
        self.assertIn("--relative-paths", calls[0][1])
        self.assertEqual(calls[1][1][:4], ("worktree", "lock", "--reason", "wsl"))      # the existing lock reason

    def test_a_windows_destination_keeps_windows_git(self):
        calls = self.run_add("H:\\repos\\acme.worktrees\\drover-T1", True)
        self.assertTrue(all(c[2] == {} for c in calls), calls)
        self.assertEqual(calls[1][1][3], "windows")

    def test_wsl_side_argv_reaches_the_repository_through_mnt(self):
        argv = placement.git_argv("H:\\repos\\acme", ["worktree", "add", "-b", "b", EXT4, "main"], nt=True, side="wsl")
        self.assertEqual(argv[0], "wsl.exe")
        i = argv.index("-C")
        self.assertEqual(argv[i + 1], "/mnt/h/repos/acme")
        self.assertIn(EXT4, argv)
        plain = placement.git_argv("H:\\repos\\acme", ["status"], nt=True)
        self.assertEqual(plain[0], "git")                                                # no side: Windows git as before

    def test_side_is_ignored_when_this_host_is_not_windows(self):
        self.assertEqual(placement.git_argv("/mnt/h/repos/acme", ["status"], nt=False, side="wsl")[0], "git")


if __name__ == "__main__":
    unittest.main()


class ListingSide(unittest.TestCase):
    def test_branch_lookup_and_unlock_list_worktrees_with_the_wsl_git(self):
        from drover import pipeline
        seen = []

        def fake(repo, *args, **kw):
            seen.append((args[:2], kw.get("side")))
            out = "worktree /home/dev/work/a.worktrees/drover-T\nbranch refs/heads/drover/T\n"
            if "-z" in args:
                out = "worktree /home/dev/work/a.worktrees/drover-T\0locked wsl\0\0"
            return subprocess.CompletedProcess(args, 0, out, "")
        with patch.object(placement, "git", fake), patch.object(pipeline.placement, "git", fake):
            found = pipeline.Pipeline.__new__(pipeline.Pipeline)._worktree_of_branch("H:\\repos\\a", "drover/T")
            placement.unlock_worktree("H:\\repos\\a", "/home/dev/work/a.worktrees/drover-T")
        self.assertEqual(found, "/home/dev/work/a.worktrees/drover-T")
        self.assertEqual([side for _, side in seen if _[0] == "worktree"], ["wsl", "wsl", "wsl"])   # list, list, unlock


class CleanupCoversBothRoots(unittest.TestCase):
    def test_both_roots_are_listed_and_protected_and_only_this_sides_are_scanned(self):
        from pathlib import Path
        from drover import disk_paths
        machine = {"name": "a", "worktrees": "H:\\repos\\a.worktrees"}
        self.assertEqual(len(disk_paths.worktree_roots(machine)), 2)
        with patch.object(placement, "IS_WINDOWS", False), \
                patch.object(placement, "wsl_worktrees_root", return_value="/home/dev/work"):
            mine = disk_paths.worktree_roots(machine, this_side=True)
        self.assertEqual(mine[-1], Path("/home/dev/work/a.worktrees"))         # WSL scans ext4 as well
        with patch.object(placement, "IS_WINDOWS", True):
            self.assertEqual(disk_paths.worktree_roots({"name": "a"}, this_side=True), [])    # Windows leaves ext4 to WSL
        self.assertEqual(disk_paths.worktree_roots({"worktrees": "H:\\x"}), [Path(placement.native("H:\\x"))])


from tests.test_disk_worktrees import WorktreeCase


class CleanupScansWslRoot(WorktreeCase):
    def test_an_unregistered_folder_under_the_wsl_root_is_reported(self):
        wsl_root = self.r.root / 'ext4'
        stray = wsl_root / 'alpha.worktrees' / 'stray'
        stray.mkdir(parents=True)
        (stray / 'file.txt').write_text('x')
        with patch.object(placement, 'wsl_worktrees_root', return_value=str(wsl_root)):
            result = self.cleanup()
        self.assertTrue(any(i['path'] == str(stray) for i in result['items']), result['items'])
