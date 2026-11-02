"""The scope check judges the worker's own changes, not main files its branch took in during the run."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from drover import pipeline, repos
from drover.dispatcher_launch import LaunchSteps
from tests.gitfix import Repo
from tests.test_pipeline import policy_prog
from tests.tiers import integration


@integration
class ScopeBase(unittest.TestCase):
    def fresh(self):
        """A new repository, worktree and task, so no scenario sees another's commits."""
        self.fix = Repo()
        self.addCleanup(self.fix.cleanup)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = self.fix.git("rev-parse", "main")
        self.wt = Path(self.tmp.name) / "wt"
        self.fix.git("worktree", "add", "-q", "-b", "work", self.wt, "main")
        self.addCleanup(self.fix.git, "worktree", "remove", "--force", self.wt)
        self.task = {"id": 1, "repo": "p", "worktree": str(self.wt), "scope_files": ["work.txt"]}

    def pipe(self, **kw):
        prog = policy_prog(self.fix.repo, Path(self.tmp.name) / "wts", **kw)
        return pipeline.Pipeline(None, repos_map={"p": prog}, runs_dir=self.tmp.name,
                                 repository=pipeline.Repository())

    def newer_main(self):
        self.fix.commit("newmain.txt", cwd=self.fix.repo)

    def work(self, name="work.txt"):
        self.fix.commit(name, cwd=self.wt)

    def land(self, **kw):
        return self.pipe(**kw).commit_ready(self.task, 1, "m", base=self.base)

    def scenario_merged_newer_main_is_not_out_of_scope(self):
        self.work()
        self.newer_main()
        self.fix.git("merge", "-q", "--no-edit", "main", cwd=self.wt)
        self.assertEqual(self.land(), self.fix.git("rev-parse", "HEAD", cwd=self.wt))

    def scenario_rebase_onto_newer_main_is_not_out_of_scope(self):
        self.work()
        self.newer_main()
        self.fix.git("rebase", "-q", "main", cwd=self.wt)
        self.assertEqual(self.land(), self.fix.git("rev-parse", "HEAD", cwd=self.wt))

    def scenario_real_out_of_scope_file_on_a_merged_branch_is_refused(self):
        self.work()
        self.work("other.txt")
        self.newer_main()
        self.fix.git("merge", "-q", "--no-edit", "main", cwd=self.wt)
        with self.assertRaises(pipeline.CommitRefused) as cm:
            self.land()
        self.assertEqual(cm.exception.paths, ["other.txt"])

    def scenario_real_out_of_scope_file_on_top_of_a_rebase_is_refused(self):
        self.work()
        self.newer_main()
        self.fix.git("rebase", "-q", "main", cwd=self.wt)
        self.work("other.txt")
        with self.assertRaises(pipeline.CommitRefused) as cm:
            self.land()
        self.assertEqual(cm.exception.paths, ["other.txt"])

    def scenario_unresolved_base_branch_falls_back_to_the_launch_snapshot(self):
        self.work()
        self.newer_main()
        self.fix.git("merge", "-q", "--no-edit", "main", cwd=self.wt)
        with self.assertRaises(pipeline.CommitRefused) as cm:
            self.land(default_branch="no-such-branch")
        self.assertEqual(cm.exception.paths, ["newmain.txt"])

    def scenario_the_configured_base_branch_is_used_not_main(self):
        self.fix.git("branch", "trunk", "main")
        self.work()
        self.fix.git("checkout", "-q", "trunk")
        self.fix.commit("newtrunk.txt")
        self.fix.git("checkout", "-q", "main")
        self.fix.git("merge", "-q", "--no-edit", "trunk", cwd=self.wt)
        self.assertEqual(self.land(default_branch="trunk"), self.fix.git("rev-parse", "HEAD", cwd=self.wt))

    def start(self):
        """The snapshot the dispatcher records as the run begins in this worktree."""
        port = SimpleNamespace(get_task=lambda tid: self.task, set_status=lambda *a, **k: self.fail("sent to owner"))
        steps = SimpleNamespace(port=port, pipeline=self.pipe())
        return LaunchSteps._output_snapshot(steps, self.task, str(self.wt))["base"]

    def land_from_start(self, base):
        return self.pipe().commit_ready(self.task, 1, "m", base=base)

    def scenario_reused_worktree_behind_main_accepts_the_workers_commit(self):
        self.newer_main()
        base = self.start()
        self.assertEqual(base, self.fix.git("rev-parse", "HEAD", cwd=self.wt))
        self.work()
        self.assertEqual(self.land_from_start(base), self.fix.git("rev-parse", "HEAD", cwd=self.wt))

    def scenario_commits_of_earlier_runs_stay_in_scope_checking(self):
        self.work("other.txt")
        base = self.start()
        self.work()
        with self.assertRaises(pipeline.CommitRefused) as cm:
            self.land_from_start(base)
        self.assertEqual(cm.exception.paths, ["other.txt"])

    def scenario_history_below_the_run_start_cannot_be_rewritten(self):
        self.work()
        base = self.start()
        self.fix.git("reset", "-q", "--hard", "main", cwd=self.wt)
        self.task["scope_files"] = ["rewritten.txt"]
        self.work("rewritten.txt")
        with self.assertRaises(pipeline.CommitRefused) as cm:
            self.land_from_start(base)
        self.assertIn("does not descend", str(cm.exception))

    def test_scope_check_judges_only_the_workers_own_changes(self):
        # One test, nine scenarios: each is a real-git check with its own repository (see fresh), run as a subtest.
        scenarios = [name for name in dir(self) if name.startswith("scenario_")]
        self.assertEqual(len(scenarios), 9)
        for name in scenarios:
            with self.subTest(scenario=name):
                self.fresh()
                getattr(self, name)()


if __name__ == "__main__":
    unittest.main()
