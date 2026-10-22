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
