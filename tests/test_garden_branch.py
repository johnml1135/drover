"""cleanup.delete_branch: branch-only deletion with fresh checks, rescue refs and bundles, in temp repos."""
import unittest
from pathlib import Path
from unittest import mock

from drover import cleanup, config, git_client, process
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
        self.assertTrue(self.run_del("same")["ok"])
        self.assertFalse(self.exists("same"))

    def test_unmerged_is_rescued_then_deleted(self):
        tip = self.r.branch("feat/x", {"x.txt": "x\n"})
        real, seen = process.run, []
        with mock.patch.object(process, "run", lambda command: seen.append(command) or real(command)):
            res = self.run_del("feat/x")
        # Section 5.1: cleanup's git had no timeout, so every command runs at LONG, and every one but the worktree
        # listing (git_worktrees' own) scans without optional locks, the ref changes included.
        git = [c for c in seen if "git" in c.argv]
        self.assertEqual({c.timeout_s for c in git}, {git_client.LONG})
        self.assertIn("update-ref", {a for c in git for a in c.argv})
        self.assertEqual([c.argv[c.argv.index("-C") + 2:][:2] for c in git if (c.env or {}).get("GIT_OPTIONAL_LOCKS") != "0"],
                         [("worktree", "list")])
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
        real = git_client.ask

        def fake(cwd, *args, **kw):
            if "cherry" in args:
                return git_client.GitReply(git_client.Answer.UNKNOWN, "", "fatal", 128, "exit")
            return real(cwd, *args, **kw)
        with mock.patch.object(git_client, "ask", fake):
            self.refused(self.run_del("feat/x", approved=row("feat/x", "land-task")), "not approved")

    def test_merge_base_error_refuses(self):
        real = git_client.ask

        def fake(cwd, *args, **kw):
            if "merge-base" in args:
                return git_client.GitReply(git_client.Answer.UNKNOWN, "", "fatal", 128, "exit")
            return real(cwd, *args, **kw)
        with mock.patch.object(git_client, "ask", fake):
            self.refused(self.run_del("feat/x"), "git error")

    def test_a_read_that_does_not_end_refuses_as_a_git_error(self):
        """cleanup's reads had no timeout: at LONG, one that times out or cannot start is a GitError refusal, never a raw
        timeout escaping delete_branch, whichever read it is."""
        for word in ("check-ref-format", "symbolic-ref", "rev-parse", "merge-base", "cherry"):
            for why in ("timeout", "unavailable"):
                with self.subTest(word=word, why=why):
                    real = process.run

                    def stuck(command):
                        if word in command.argv:
                            return process.Result("timed_out" if why == "timeout" else "unavailable", None, b"", b"", 0.0, why)
                        return real(command)
                    with mock.patch.object(process, "run", stuck):
                        self.refused(self.run_del("feat/x"), "git error")
                    self.assertTrue(self.exists("feat/x"))

    def test_worktree_list_error_refuses(self):
        def fake(repo, *args, error, **kw):     # the listing goes through git_worktrees on git_client
            self.assertEqual(args[:2], ("worktree", "list"))
            raise error(git_client.GitFailed(args, "exit", 1, "fixture stderr", ""))
        with mock.patch.object(git_client, "out", fake):
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
        real = git_client.mutate
        moved = []

        def fake(cwd, *args, **kw):
            if "update-ref" in args and "-d" in args and not moved:
                moved.append(1)
                self.r.git("update-ref", "refs/heads/feat/x", self.r.git("rev-parse", "origin/main"))
            return real(cwd, *args, **kw)
        with mock.patch.object(git_client, "mutate", fake):
            res = self.run_del("feat/x")
        self.assertFalse(res["ok"])
        self.assertTrue(self.exists("feat/x"))


if __name__ == "__main__":
    unittest.main()
