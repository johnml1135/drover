"""cleanup.delete_branch: branch-only deletion with fresh checks, rescue refs and bundles, in temp repos."""
import unittest
from pathlib import Path
from unittest import mock

from drover import cleanup, config
from tests.gitfix import Repo, sh

NOW = 1_792_000_000  # a fixed day


def row(branch, disposition="propose-delete", **kw):
    return {"branch": branch, "disposition": disposition, "reason": "r", "worktree": "", **kw}


class Base(unittest.TestCase):
    def setUp(self):
        self.r = Repo()
        self.addCleanup(self.r.cleanup)
        self.addCleanup(config.reset)
        config.use(config.validate({"paths": {"state_dir": str(self.r.root / "state")}}))
        self.r.git("remote", "set-head", "origin", "main")
        self.events = []
        self.out = self.r.root / "rescue"

    def run_del(self, branch, approved=None, **kw):
        return cleanup.delete_branch(self.r.repo, branch, approved if approved is not None else row(branch),
                                    log=lambda step, **d: self.events.append((step, d)), now=NOW,
                                    rescue_root=self.out, **kw)

    def exists(self, branch):
        return sh("git", "-C", self.r.repo, "rev-parse", "--verify", "-q", "refs/heads/" + branch, check=False).returncode == 0

    def steps(self):
        return [s for s, _ in self.events]


class TestDeleteBranch(Base):
    def test_merged_branch_is_deleted_without_a_rescue(self):
        self.r.branch("done", {"d.txt": "d\n"})
        self.r.git("push", "-q", "origin", "done:main")
        self.r.git("fetch", "-q", "origin")
        res = self.run_del("done")
        self.assertTrue(res["ok"], res)
        self.assertFalse(self.exists("done"))
        self.assertIsNone(res["rescue_ref"])
        self.assertEqual(self.steps(), ["checked", "deleted"])

    def test_patch_equivalent_branch_counts_as_merged(self):
        self.r.branch("same", {"s.txt": "s\n"})
        self.r.git("branch", "-q", "other", "origin/main")
        wt = self.r.root / "wt"
        self.r.git("worktree", "add", "-q", wt, "other")
        self.r.commit("s.txt", "s\n", cwd=wt, msg="same: s.txt")  # same patch, different commit
        self.r.git("push", "-q", "origin", "other:main")
        self.r.git("worktree", "remove", "--force", wt)
        self.r.git("fetch", "-q", "origin")
        self.assertTrue(self.run_del("same")["ok"])
        self.assertFalse(self.exists("same"))

    def test_unmerged_is_rescued_then_deleted(self):
        tip = self.r.branch("feat/x", {"x.txt": "x\n"})
        res = self.run_del("feat/x")
        self.assertTrue(res["ok"], res)
        self.assertFalse(self.exists("feat/x"))
        self.assertTrue(res["rescue_ref"].startswith("refs/drover-rescue/feat/x-"))
        self.assertEqual(self.r.git("rev-parse", res["rescue_ref"]), tip)
        self.assertEqual(self.steps(), ["checked", "rescue-ref", "deleted"])
        self.assertIsNone(res["bundle"])

    def test_snapshot_batches_also_write_a_bundle(self):
        tip = self.r.branch("archive/old", {"o.txt": "o\n"})
        res = self.run_del("archive/old", approved=row("archive/old", "owner-review"), bundle=True)
        self.assertTrue(res["ok"], res)
        bundle = Path(res["bundle"])
        self.assertEqual(bundle.parent.parent, self.out)
        self.assertTrue(bundle.is_file())
        self.assertIn(tip, sh("git", "-C", self.r.repo, "bundle", "list-heads", bundle).stdout)
        self.assertEqual(self.steps(), ["checked", "rescue-ref", "bundle", "deleted"])

    def test_checked_out_branch_is_refused(self):
        self.r.branch("busy", {"b.txt": "b\n"})
        wt = self.r.root / "busy-wt"
        self.r.git("worktree", "add", "-q", wt, "busy")
        res = self.run_del("busy")
        self.assertFalse(res["ok"])
        self.assertIn("checked out", res["reason"])
        self.assertTrue(self.exists("busy"))
        self.assertEqual(self.steps(), ["refused"])

    def test_default_branch_is_refused(self):
        res = self.run_del("main", approved=row("main"))
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "default branch")
        self.assertTrue(self.exists("main"))

    def test_unapproved_rows_are_never_touched(self):
        self.r.branch("keep", {"k.txt": "k\n"})
        self.r.branch("land", {"l.txt": "l\n"})
        self.assertFalse(self.run_del("keep", approved=row("other"))["ok"])         # row names another branch
        self.assertFalse(self.run_del("keep", approved={})["ok"])
        self.assertFalse(self.run_del("land", approved=row("land", "land-task"))["ok"])  # not a delete approval
        self.assertTrue(self.exists("keep") and self.exists("land"))
        self.assertEqual(self.r.git("for-each-ref", "refs/drover-rescue"), "")

    def test_missing_branch_and_remote_untouched(self):
        self.assertEqual(self.run_del("ghost")["reason"], "no such local branch")
        self.r.branch("gone", {"g.txt": "g\n"})
        self.r.git("push", "-q", "origin", "gone")
        self.assertTrue(self.run_del("gone")["ok"])
        self.assertIn("refs/heads/gone", self.r.git("ls-remote", self.r.remote))  # remote branch survives


class TestFailClosed(Base):
    def setUp(self):
        super().setUp()
        self.tip = self.r.branch("feat/x", {"x.txt": "x\n"})

    def refused(self, res, why=None):
        self.assertFalse(res["ok"], res)
        if why:
            self.assertIn(why, res["reason"])
        self.assertTrue(self.exists("feat/x"))
        self.assertEqual(self.r.git("for-each-ref", "refs/drover-rescue"), "")

    def test_dangling_origin_head_refuses(self):
        self.r.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/gone")
        self.refused(self.run_del("feat/x", approved=row("feat/x", "land-task")), "does not resolve")
        self.refused(self.run_del("feat/x"), "does not resolve")

    def test_cherry_error_is_not_merged(self):
        real = cleanup.git

        def fake(cwd, *args, **kw):
            if "cherry" in args:
                r = real(cwd, *args, **kw)
                r.returncode, r.stdout = 128, ""
                return r
            return real(cwd, *args, **kw)
        with mock.patch.object(cleanup, "git", fake):
            self.refused(self.run_del("feat/x", approved=row("feat/x", "land-task")), "not approved")

    def test_merge_base_error_refuses(self):
        real = cleanup.git

        def fake(cwd, *args, **kw):
            if "merge-base" in args:
                r = real(cwd, *args, **kw)
                r.returncode = 128
                return r
            return real(cwd, *args, **kw)
        with mock.patch.object(cleanup, "git", fake):
            self.refused(self.run_del("feat/x"), "git error")

    def test_worktree_list_error_refuses(self):
        real = cleanup.git

        def fake(cwd, *args, **kw):
            r = real(cwd, *args, **kw)
            if "worktree" in args:
                r.returncode = 1
            return r
        with mock.patch.object(cleanup, "git", fake):
            self.refused(self.run_del("feat/x"), "git error")

    def test_hostile_branch_names(self):
        for bad in ("-D", "--force", "a..b", "x@{1}", "refs/heads/feat/x", "/abs", "a b", "a~1", "feat/x\n", "", "-"):
            res = self.run_del(bad, approved=row(bad))
            self.assertFalse(res["ok"], bad)
            self.assertEqual(res["reason"], "invalid branch name", bad)
        self.assertTrue(self.exists("feat/x"))

    def test_rescue_name_never_overwrites(self):
        sha = self.r.git("rev-parse", "feat/x")
        taken = "refs/drover-rescue/feat/x-%s-%s" % (__import__("time").strftime("%Y%m%d", __import__("time").gmtime(NOW)), sha[:8])
        self.r.git("update-ref", taken, self.r.git("rev-parse", "origin/main"))
        res = self.run_del("feat/x")
        self.assertFalse(res["ok"])
        self.assertIn("rescue ref", res["reason"])
        self.assertTrue(self.exists("feat/x"))
        self.assertEqual(self.r.git("rev-parse", taken), self.r.git("rev-parse", "origin/main"))

    def test_moved_branch_is_not_deleted(self):
        real = cleanup.git
        moved = []

        def fake(cwd, *args, **kw):
            if "update-ref" in args and "-d" in args and not moved:
                moved.append(1)
                real(cwd, "update-ref", "refs/heads/feat/x", real(cwd, "rev-parse", "origin/main").stdout.strip())
            return real(cwd, *args, **kw)
        with mock.patch.object(cleanup, "git", fake):
            res = self.run_del("feat/x")
        self.assertFalse(res["ok"])
        self.assertTrue(self.exists("feat/x"))


if __name__ == "__main__":
    unittest.main()
