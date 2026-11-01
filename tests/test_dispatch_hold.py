import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from drover import config, dispatch_hold
from drover.ledger import connect, ops
from drover.port import DirectPort

from tests.test_dispatcher import Base

CFG = {"magnets": config.DEFAULTS["dispatch"]["magnets"], "conflict_hold": True}


def task(tid, scope, repo="p", branch=None, repo_path="/r"):
    return {"id": tid, "scope_files": scope, "repo": repo, "branch": branch, "repo_path": repo_path}


class FakeGit:
    """Answers rev-parse with a per-ref tip and diff with the files that tip changed."""

    def __init__(self, files, tips=None):
        self.files, self.tips, self.calls = files, tips or {}, []

    def __call__(self, repo, *args):
        self.calls.append(args)
        if args[0] == "rev-parse":
            ref = args[-1][:-len("^{commit}")]
            return None if ref == "ghost" else self.tips.get(ref, ref + "-tip") + "\n"
        if args[0] == "diff":
            tip = args[-1].split("...")[1]
            return "\n".join(self.files.get(tip, [])) + "\n"
        return None

    def diffs(self):
        return [c for c in self.calls if c[0] == "diff"]


def run(ready, active, files):
    return dispatch_hold.holds(ready, active, CFG, git=FakeGit(files), cache={}, base_of=lambda t: "main")


class Config(unittest.TestCase):
    def test_cap_default_is_40(self):
        self.assertEqual(config.validate({})["capacity"]["parallel_cap"], 40)
        self.assertEqual(config.validate({"capacity": {"parallel_cap": 64}})["capacity"]["parallel_cap"], 64)
        with self.assertRaises(config.ConfigError):
            config.validate({"capacity": {"parallel_cap": 65}})

    def test_dispatch_section_validates(self):
        d = config.validate({})["dispatch"]
        self.assertTrue(d["conflict_hold"])
        self.assertIn("*golden*", d["magnets"])
        self.assertEqual(config.validate({"dispatch": {"magnets": ["*.x"], "conflict_hold": False}})["dispatch"],
                         {"magnets": ["*.x"], "conflict_hold": False})
        with self.assertRaises(config.ConfigError):
            config.validate({"dispatch": {"nope": 1}})

    def test_stored_policy_cap_6_is_ignored(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            conn = connect(Path(tmp) / "l.db")
            self.addCleanup(conn.close)
            ops.set_setting(conn, "parallel_cap", "7")
            ops.add_policy_version(conn, "v1", {"parallel_cap": 6})
            self.assertEqual(DirectPort(conn).get_policy()["parallel_cap"], 40)


class Reasons(unittest.TestCase):
    def test_shared_file_holds_behind_active_branch(self):
        out = run([task("T2", ["src/a.py"])], [task("T1", ["x"], branch="b1")], {"b1-tip": ["src/a.py"]})
        self.assertEqual(list(out), ["T2"])
        self.assertIn("T1", out["T2"])
        self.assertIn("src/a.py", out["T2"])

    def test_same_magnet_pattern_holds_on_different_files(self):
        out = run([task("T2", ["tests/b.golden.json"])], [task("T1", ["x"], branch="b1")],
                  {"b1-tip": ["tests/a.golden.json"]})
        self.assertIn("T2", out)
        self.assertIn("a.golden.json", out["T2"])

    def test_no_overlap_starts(self):
        self.assertEqual(run([task("T2", ["src/b.py"])], [task("T1", ["x"], branch="b1")],
                             {"b1-tip": ["src/a.py", "docs/x.md"]}), {})

    def test_empty_scope_is_not_held(self):
        self.assertEqual(run([task("T2", [])], [task("T1", ["x"], branch="b1")], {"b1-tip": ["src/a.py"]}), {})

    def test_other_repo_never_holds(self):
        self.assertEqual(run([task("T2", ["src/a.py"], repo="q")], [task("T1", ["x"], branch="b1")],
                             {"b1-tip": ["src/a.py"]}), {})

    def test_unknown_branch_holds_nothing(self):
        files = {"b1-tip": ["src/a.py"]}
        self.assertEqual(run([task("T2", ["src/a.py"])], [task("T1", ["x"], branch=None)], files), {})
        self.assertEqual(run([task("T2", ["src/a.py"])], [task("T1", ["x"], branch="ghost")], files), {})
        self.assertEqual(dispatch_hold.holds([task("T2", ["src/a.py"])], [task("T1", ["x"], branch="b1")], CFG,
                                             git=lambda *a: None, cache={}), {})

    def test_hold_clears_when_the_branch_lands(self):
        ready, active = [task("T2", ["src/a.py"])], [task("T1", ["x"], branch="b1")]
        self.assertIn("T2", run(ready, active, {"b1-tip": ["src/a.py"]}))
        self.assertEqual(run(ready, active, {"b1-tip": []}), {})     # landed: nothing past base
        self.assertEqual(run(ready, [], {"b1-tip": ["src/a.py"]}), {})   # and the task is no longer active

    def test_conflict_hold_off_holds_nothing(self):
        out = dispatch_hold.holds([task("T2", ["src/a.py"])], [task("T1", ["x"], branch="b1")],
                                  {**CFG, "conflict_hold": False}, git=FakeGit({"b1-tip": ["src/a.py"]}), cache={})
        self.assertEqual(out, {})

    def test_git_runs_once_per_branch_tip_across_ticks(self):
        git, cache = FakeGit({"b1-tip": ["src/a.py"]}), {}
        for _ in range(3):
            dispatch_hold.holds([task("T2", ["src/a.py"]), task("T3", ["src/z.py"])],
                                [task("T1", ["x"], branch="b1")], CFG, git=git, cache=cache, base_of=lambda t: "main")
        self.assertEqual(len(git.diffs()), 1)
        git.tips["b1"] = "new"                     # the branch moved: read again
        dispatch_hold.holds([task("T2", ["src/a.py"])], [task("T1", ["x"], branch="b1")], CFG, git=git, cache=cache,
                            base_of=lambda t: "main")
        self.assertEqual(len(git.diffs()), 2)


class InTick(Base):
    def setUp(self):
        super().setUp()
        self.git = FakeGit({"b1-tip": ["src/a.py"]})
        patch = mock.patch.object(dispatch_hold, "_git", self.git)
        patch.start()
        self.addCleanup(patch.stop)
        dispatch_hold._CACHE.clear()
        self.active = self.task(scope_files=["other/x"])
        self.conn.execute("UPDATE tasks SET status='running', branch='b1', repo_path='/r' WHERE id=?", (self.active,))

    def beat(self):
        return json.loads(ops.get_setting(self.conn, "dispatcher_heartbeat")).get("not_dispatched")

    def test_held_task_stays_ready_and_is_reported_as_conflict_hold(self):
        t = self.task(scope_files=["src/a.py"])
        out = self.d.tick()
        self.assertEqual((out["launched"], self.status(t)), ([], "ready"))
        self.assertEqual(self.beat(), {"conflict-hold": 1})

    def test_picked_task_is_held_too(self):
        held, free = self.task(scope_files=["src/a.py"]), self.task(scope_files=["src/free.py"])
        self.assertEqual(self.d.tick()["launched"], [free])      # the best-ranked pick waits, the next one starts
        self.assertEqual(self.status(held), "ready")

    def test_git_failure_in_tick_holds_nothing(self):
        t = self.task(scope_files=["src/a.py"])
        with mock.patch.object(dispatch_hold, "_git", side_effect=RuntimeError("boom")):
            self.assertEqual(self.d.tick()["launched"], [t])


if __name__ == "__main__":
    unittest.main()
