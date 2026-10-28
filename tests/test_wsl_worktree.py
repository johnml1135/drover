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
        with patch.object(placement, "IS_WINDOWS", nt) as _placement_patch_1, patch('drover.placement_exec.IS_WINDOWS', _placement_patch_1), patch('drover.placement_git.IS_WINDOWS', _placement_patch_1), patch('drover.placement_host.IS_WINDOWS', _placement_patch_1), patch('drover.placement_paths.IS_WINDOWS', _placement_patch_1), patch('drover.placement_process.IS_WINDOWS', _placement_patch_1), patch('drover.placement_proof.IS_WINDOWS', _placement_patch_1), patch('drover.placement_tree.IS_WINDOWS', _placement_patch_1), patch('drover.placement_worker.IS_WINDOWS', _placement_patch_1):
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
    def lookup(self, native, wsl, reachable):
        from drover import pipeline
        seen = []
        def fake(repo, *args, **kw):
            seen.append(kw.get('side'))
            out = wsl if kw.get('side') == 'wsl' else native
            return subprocess.CompletedProcess(args, 127 if out is None else 0, out or '', '')
        with patch.object(placement, 'IS_WINDOWS', True) as _placement_patch_2, patch('drover.placement_exec.IS_WINDOWS', _placement_patch_2), patch('drover.placement_git.IS_WINDOWS', _placement_patch_2), patch('drover.placement_host.IS_WINDOWS', _placement_patch_2), patch('drover.placement_paths.IS_WINDOWS', _placement_patch_2), patch('drover.placement_process.IS_WINDOWS', _placement_patch_2), patch('drover.placement_proof.IS_WINDOWS', _placement_patch_2), patch('drover.placement_tree.IS_WINDOWS', _placement_patch_2), patch('drover.placement_worker.IS_WINDOWS', _placement_patch_2), patch.object(pipeline, 'git', fake), \
                patch.object(placement, 'reachable', side_effect=reachable):
            pipe = pipeline.Pipeline.__new__(pipeline.Pipeline)
            pipe.repository = pipeline.Repository()
            found = pipe._worktree_of_branch('H:\\repos\\a', 'drover/T')
        return found, seen

    def record(self, path, lock=''):
        return f'worktree {path}\nbranch refs/heads/drover/T\n' + (f'locked {lock}\n' if lock else '') + '\n'

    def test_native_worktree_remains_available_without_wsl(self):
        path = 'H:/work/a'
        found, seen = self.lookup(self.record(path), None, lambda p: p == placement.native(path))
        self.assertEqual(found, (path, True))
        self.assertEqual(seen, [None])

    def test_wsl_lock_prevents_a_reachable_windows_misinterpretation_from_masking_ext4(self):
        phantom = 'H:/home/dev/work/a'
        path = '/home/dev/work/a'
        found, seen = self.lookup(self.record(phantom, 'wsl'), self.record(path, 'wsl'), lambda p: True)
        self.assertEqual(found, (path, True))
        self.assertEqual(seen, [None, 'wsl'])

    def test_unreachable_native_interpretation_falls_back_to_ext4(self):
        path = '/home/dev/work/a'
        found, seen = self.lookup(self.record('H:/home/dev/work/a'), self.record(path), lambda p: p == path)
        self.assertEqual(found, (path, True))
        self.assertEqual(seen, [None, 'wsl'])

    def test_missing_registration_is_retained_when_neither_side_can_reach_it(self):
        path = 'H:/work/missing'
        found, seen = self.lookup(self.record(path), None, lambda p: False)
        self.assertEqual(found, (path, False))
        self.assertEqual(seen, [None, 'wsl'])

    def test_unavailable_wsl_retains_foreign_registration_without_permitting_reuse(self):
        phantom = 'H:/home/dev/work/a'
        found, seen = self.lookup(self.record(phantom, 'wsl'), None, lambda p: True)
        self.assertEqual(found, (phantom, False))
        self.assertEqual(seen, [None, 'wsl'])

    def test_unlock_lists_worktrees_with_the_wsl_git(self):
        seen = []
        def fake(repo, *args, **kw):
            seen.append(kw.get('side'))
            out = 'worktree /home/dev/work/a\0locked wsl\0\0'
            return subprocess.CompletedProcess(args, 0, out, '')
        with patch.object(placement, 'git', fake) as _placement_patch_3, patch('drover.placement_git.git', _placement_patch_3):
            placement.unlock_worktree('H:\\repos\\a', '/home/dev/work/a')
        self.assertEqual(seen, ['wsl', 'wsl'])


class CleanupCoversBothRoots(unittest.TestCase):
    def test_both_roots_are_listed_and_protected_and_only_this_sides_are_scanned(self):
        from pathlib import Path
        from drover import disk_paths
        machine = {"name": "a", "worktrees": "H:\\repos\\a.worktrees"}
        self.assertEqual(len(disk_paths.worktree_roots(machine)), 2)
        with patch.object(placement, "IS_WINDOWS", False) as _placement_patch_4, patch('drover.placement_exec.IS_WINDOWS', _placement_patch_4), patch('drover.placement_git.IS_WINDOWS', _placement_patch_4), patch('drover.placement_host.IS_WINDOWS', _placement_patch_4), patch('drover.placement_paths.IS_WINDOWS', _placement_patch_4), patch('drover.placement_process.IS_WINDOWS', _placement_patch_4), patch('drover.placement_proof.IS_WINDOWS', _placement_patch_4), patch('drover.placement_tree.IS_WINDOWS', _placement_patch_4), patch('drover.placement_worker.IS_WINDOWS', _placement_patch_4), \
                patch.object(placement, "wsl_worktrees_root", return_value="/home/dev/work"):
            mine = disk_paths.worktree_roots(machine, this_side=True)
        self.assertEqual(mine[-1], Path("/home/dev/work/a.worktrees"))         # WSL scans ext4 as well
        with patch.object(placement, "IS_WINDOWS", True) as _placement_patch_5, patch('drover.placement_exec.IS_WINDOWS', _placement_patch_5), patch('drover.placement_git.IS_WINDOWS', _placement_patch_5), patch('drover.placement_host.IS_WINDOWS', _placement_patch_5), patch('drover.placement_paths.IS_WINDOWS', _placement_patch_5), patch('drover.placement_process.IS_WINDOWS', _placement_patch_5), patch('drover.placement_proof.IS_WINDOWS', _placement_patch_5), patch('drover.placement_tree.IS_WINDOWS', _placement_patch_5), patch('drover.placement_worker.IS_WINDOWS', _placement_patch_5):
            self.assertEqual(disk_paths.worktree_roots({"name": "a"}, this_side=True), [])    # Windows leaves ext4 to WSL
        self.assertEqual(disk_paths.worktree_roots({"worktrees": "H:\\x"}), [Path(placement.native("H:\\x"))])


from tests.test_disk_worktrees import WorktreeCase


class CleanupScansWslRoot(WorktreeCase):
    def test_an_unregistered_folder_under_the_owning_root_is_reported(self):
        wsl_root = self.r.root / 'ext4'
        owning_root = self.trees if self.side == 'windows' else wsl_root / 'alpha.worktrees'
        stray = owning_root / 'stray'
        stray.mkdir(parents=True)
        (stray / 'file.txt').write_text('x')
        with patch.object(placement, 'wsl_worktrees_root', return_value=str(wsl_root)):
            result = self.cleanup()
        self.assertTrue(any(i['path'] == str(stray) for i in result['items']), result['items'])
        self.assertTrue(any(side == 'wsl' and request['stage'] == 3 for side, request in self.calls), self.calls)
