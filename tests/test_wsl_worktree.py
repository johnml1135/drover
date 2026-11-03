"""ADR 0007: an ext4 (WSL) worktree is created by the WSL git against the one Windows repository."""
import unittest
from unittest.mock import patch

from drover import git_client, placement, repos
from drover.placement_paths import cleanup_parts, cleanup_path

EXT4 = "/home/dev/work/acme.worktrees/drover-T1"


def listed(answer, seen):
    """`git_client.out` for `listing`: `answer(side)` is git's -z porcelain, or None where git could not run."""
    def out(repo, *args, timeout_s, side=None, error=None, **_):
        seen.append(side)
        text = answer(side)
        if text is None:
            raise error(git_client.GitFailed(args, 'unavailable', None, 'git unavailable: fixture', ''))
        return text
    return patch.object(git_client, 'out', out)


def spelled(path):
    """A listed path as `inventory` gives it, made native: `listing` spells each row with `cleanup_path`."""
    return repos.native(cleanup_path(path))


def mutations(calls, answer=lambda args: git_client.GitDone(True, "", "", 0, None)):
    """`git_client.mutate` keeping (repo, args, side, timeout_s) for each call."""
    def mutate(repo, *args, timeout_s, side=None, env=None):
        calls.append((repo, args, side, timeout_s))
        return answer(args)
    return patch.object(git_client, "mutate", mutate)


class AddOnExt4(unittest.TestCase):
    def run_add(self, dest, nt):
        calls = []
        with mutations(calls), patch.object(placement, "IS_WINDOWS", nt) as _placement_patch_1, patch('drover.placement_exec.IS_WINDOWS', _placement_patch_1), patch('drover.placement_git.IS_WINDOWS', _placement_patch_1), patch('drover.placement_host.IS_WINDOWS', _placement_patch_1), patch('drover.placement_paths.IS_WINDOWS', _placement_patch_1), patch('drover.placement_process.IS_WINDOWS', _placement_patch_1), patch('drover.placement_proof.IS_WINDOWS', _placement_patch_1), patch('drover.placement_tree.IS_WINDOWS', _placement_patch_1), patch('drover.placement_worker.IS_WINDOWS', _placement_patch_1):
            placement.add_worktree("H:\\repos\\acme", dest, "-b", "drover/T1", dest, "main")
        return calls

    def test_an_ext4_destination_runs_every_git_call_on_the_wsl_side(self):
        calls = self.run_add(EXT4, True)
        self.assertEqual([c[1][c[1].index("worktree") + 1] for c in calls if "worktree" in c[1]], ["add"])
        self.assertTrue(all(c[2] == "wsl" for c in calls), calls)
        self.assertEqual(calls[-1][1], ("config", "gc.worktreePruneExpire", "never"))      # neither side's gc expires it
        self.assertIn("--relative-paths", calls[0][1])
        i = calls[0][1].index("--lock")
        self.assertEqual(calls[0][1][i:i + 3], ("--lock", "--reason", "wsl"))      # locked at birth

    def test_a_windows_destination_keeps_windows_git(self):
        calls = self.run_add("H:\\repos\\acme.worktrees\\drover-T1", True)
        self.assertTrue(all(c[2] is None for c in calls), calls)
        i = calls[0][1].index("--lock")
        self.assertEqual(calls[0][1][i + 2], "windows")

    def test_add_worktree_keeps_no_60_second_limit(self):
        from drover import ci, landing_batch, pipeline
        self.assertEqual({c[3] for c in self.run_add("H:\\repos\\acme.worktrees\\drover-T1", True)}, {git_client.LONG})
        for name, add, refused in (
                ("pipeline.Repository.add", lambda: pipeline.Repository().add("H:\\repos\\acme", "H:\\w\\T1", "drover/T1",
                                                                              "main"), pipeline.WorktreeRefused),
                ("landing_batch.build_tip", lambda: landing_batch.build_tip("H:\\repos\\acme", "H:\\w\\group", "a" * 40, []),
                 ci.CIError)):
            for why in ("timeout", "exit"):
                with self.subTest(name, why=why):
                    calls = []
                    failed = lambda args: git_client.GitDone(False, "", "fatal: no", None if why == "timeout" else 128, why)
                    with mutations(calls, failed), patch.object(landing_batch.ci, "read_result", return_value=None):
                        with self.assertRaisesRegex(refused, "git worktree add failed: " + (
                                "timed out after 600s" if why == "timeout" else "fatal: no")):
                            add()
                    self.assertEqual([(c[1][2:4], c[3]) for c in calls], [(("worktree", "add"), git_client.LONG)])

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
        with patch.object(placement, 'IS_WINDOWS', True) as _placement_patch_2, patch('drover.placement_exec.IS_WINDOWS', _placement_patch_2), patch('drover.placement_git.IS_WINDOWS', _placement_patch_2), patch('drover.placement_host.IS_WINDOWS', _placement_patch_2), patch('drover.placement_paths.IS_WINDOWS', _placement_patch_2), patch('drover.placement_process.IS_WINDOWS', _placement_patch_2), patch('drover.placement_proof.IS_WINDOWS', _placement_patch_2), patch('drover.placement_tree.IS_WINDOWS', _placement_patch_2), patch('drover.placement_worker.IS_WINDOWS', _placement_patch_2), listed(lambda side: wsl if side == 'wsl' else native, seen), \
                patch.object(placement, 'reachable', side_effect=reachable):
            pipe = pipeline.Pipeline.__new__(pipeline.Pipeline)
            pipe.repository = pipeline.Repository()
            found = pipe._worktree_of_branch('H:\\repos\\a', 'drover/T')
        return found, seen

    def record(self, path, lock=''):
        return f'worktree {path}\0branch refs/heads/drover/T\0' + (f'locked {lock}\0' if lock else '') + '\0'

    def test_native_worktree_remains_available_without_wsl(self):
        path = 'H:/work/a'
        found, seen = self.lookup(self.record(path), None, lambda p: cleanup_parts(p) == cleanup_parts(path))
        self.assertEqual(found, (spelled(path), True))
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
        self.assertEqual(found, (spelled(path), False))
        self.assertEqual(seen, [None, 'wsl'])

    def test_unavailable_wsl_retains_foreign_registration_without_permitting_reuse(self):
        phantom = 'H:/home/dev/work/a'
        found, seen = self.lookup(self.record(phantom, 'wsl'), None, lambda p: True)
        self.assertEqual(found, (spelled(phantom), False))
        self.assertEqual(seen, [None, 'wsl'])

    def test_unlock_lists_worktrees_with_the_wsl_git(self):
        seen = []
        with listed(lambda side: 'worktree /home/dev/work/a\0locked wsl\0\0', seen), \
                patch.object(placement, 'git', lambda repo, *args, **kw: seen.append(kw.get('side'))):
            placement.unlock_worktree('H:\\repos\\a', '/home/dev/work/a')
        self.assertEqual(seen, ['wsl', 'wsl'])     # the listing, then the unlock


class CleanupCoversBothRoots(unittest.TestCase):
    def test_both_roots_are_listed_and_protected_and_only_this_sides_are_scanned(self):
        from pathlib import Path
        from drover import disk_paths
        machine = {"name": "a", "worktrees": "H:\\repos\\a.worktrees"}
        self.assertEqual(len(disk_paths.worktree_roots(machine)), 2)
        with patch.object(placement, "IS_WINDOWS", False) as _placement_patch_4, patch('drover.placement_exec.IS_WINDOWS', _placement_patch_4), patch('drover.placement_git.IS_WINDOWS', _placement_patch_4), patch('drover.placement_host.IS_WINDOWS', _placement_patch_4), patch('drover.placement_paths.IS_WINDOWS', _placement_patch_4), patch('drover.placement_process.IS_WINDOWS', _placement_patch_4), patch('drover.placement_proof.IS_WINDOWS', _placement_patch_4), patch('drover.placement_tree.IS_WINDOWS', _placement_patch_4), patch('drover.placement_worker.IS_WINDOWS', _placement_patch_4), \
                patch.object(repos, "wsl_worktrees_root", return_value="/home/dev/work"):
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
        with patch.object(repos, 'wsl_worktrees_root', return_value=str(wsl_root)):
            result = self.cleanup()
        self.assertTrue(any(i['path'] == str(stray) for i in result['items']), result['items'])
        self.assertTrue(any(side == 'wsl' and request['stage'] == 3 for side, request in self.calls), self.calls)
