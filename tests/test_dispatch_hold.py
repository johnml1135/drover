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
    """for-each-ref lists refs/heads/<branch> and the base refs; diff answers by (base sha, tip sha)."""

    def __init__(self, files, tips=None, base="m1"):
        self.files, self.calls = files, []        # files: tip -> changed files, or (base, tip) -> files
        self.refs = {"refs/heads/main": base, **{f"refs/heads/{k}": v for k, v in (tips or {}).items()}}

    def branch(self, name, tip=None):
        self.refs[f"refs/heads/{name}"] = tip or name + "-tip"
        return self

    def __call__(self, repo, *args):
        self.calls.append((repo, *args))
        if args[0] == "for-each-ref":
            return "".join(f"{sha} {name}\n" for name, sha in self.refs.items())
        if args[0] == "diff":
            base, tip = args[-1].split("...")
            return "\n".join(self.files.get((base, tip), self.files.get(tip, []))) + "\n"
        return None

    def diffs(self):
        return [c for c in self.calls if c[1] == "diff"]


def fake(files, *branches):
    git = FakeGit(files)
    for b in branches:
        git.branch(b)
    return git


def run(ready, active, files):
    git = fake(files, "b1")
    return dispatch_hold.holds(ready, active, CFG, git=git, cache={}, base_of=lambda t: "refs/heads/main")


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
        git = fake(files, "b1")
        self.assertEqual(dispatch_hold.holds([task("T2", ["src/a.py"])], [task("T1", ["x"], branch="ghost")], CFG,
                                             git=git, cache={}), {})
        self.assertEqual([c[1] for c in git.calls], ["for-each-ref"])     # a missing ref costs no further git call
        self.assertEqual(dispatch_hold.holds([task("T2", ["src/a.py"])], [task("T1", ["x"], branch="b1")], CFG,
                                             git=lambda *a: None, cache={}), {})

    def test_hold_clears_when_the_branch_lands(self):
        ready, active = [task("T2", ["src/a.py"])], [task("T1", ["x"], branch="b1")]
        self.assertIn("T2", run(ready, active, {"b1-tip": ["src/a.py"]}))
        self.assertEqual(run(ready, active, {"b1-tip": []}), {})     # landed: nothing past base
        self.assertEqual(run(ready, [], {"b1-tip": ["src/a.py"]}), {})   # and the task is no longer active

    def test_conflict_hold_off_holds_nothing(self):
        git = fake({"b1-tip": ["src/a.py"]}, "b1")
        out = dispatch_hold.holds([task("T2", ["src/a.py"])], [task("T1", ["x"], branch="b1")],
                                  {**CFG, "conflict_hold": False}, git=git, cache={})
        self.assertEqual((out, git.calls), ({}, []))

    def test_no_scoped_ready_task_spawns_no_git(self):
        git = fake({"b1-tip": ["src/a.py"]}, "b1")
        dispatch_hold.holds([task("T2", [])], [task("T1", ["x"], branch="b1")], CFG, git=git, cache={})
        dispatch_hold.holds([], [task("T1", ["x"], branch="b1")], CFG, git=git, cache={})
        self.assertEqual(git.calls, [])

    def test_git_runs_once_per_branch_tip_across_ticks(self):
        git, cache = fake({"b1-tip": ["src/a.py"]}, "b1"), {}
        ready, active = [task("T2", ["src/a.py"]), task("T3", ["src/z.py"])], [task("T1", ["x"], branch="b1")]
        for _ in range(3):
            dispatch_hold.holds(ready, active, CFG, git=git, cache=cache)
        self.assertEqual(len(git.diffs()), 1)
        git.branch("b1", "new")                    # the branch moved: read again, and the old key is pruned
        dispatch_hold.holds(ready, active, CFG, git=git, cache=cache)
        self.assertEqual(len(git.diffs()), 2)
        self.assertEqual(len(cache), 1)

    def test_forty_active_branches_spawn_one_process_per_repo_when_cached(self):
        git, cache = FakeGit({}), {}
        for i in range(40):
            git.branch(f"b{i}")
        active = [task(f"A{i}", ["x"], branch=f"b{i}") for i in range(40)]
        ready = [task("T2", ["src/a.py"])]
        dispatch_hold.holds(ready, active, CFG, git=git, cache=cache)
        self.assertEqual(len(git.calls), 41)                 # one listing, one diff per uncached tip
        git.calls.clear()
        dispatch_hold.holds(ready, active, CFG, git=git, cache=cache)
        self.assertEqual(len(git.calls), 1)
        git.calls.clear()
        other = [task(f"B{i}", ["x"], branch=f"b{i}", repo_path="/other") for i in range(40)]
        dispatch_hold.holds(ready, active[:20] + other[:20], CFG, git=git, cache=cache)
        self.assertEqual(len({c[0] for c in git.calls if c[1] == "for-each-ref"}), 2)

    def test_pushed_repo_releases_when_the_branch_is_on_the_remote_ref(self):
        git = FakeGit({("m1", "b1-tip"): ["src/a.py"], ("r1", "b1-tip"): []})   # local main is behind the remote
        git.branch("b1").refs["refs/remotes/origin/main"] = "r1"
        ready, active = [task("T2", ["src/a.py"])], [task("T1", ["x"], branch="b1")]
        local = dispatch_hold.holds(ready, active, CFG, git=git, cache={}, base_of=lambda t: "refs/heads/main")
        pushed = dispatch_hold.holds(ready, active, CFG, git=git, cache={},
                                     base_of=lambda t: "refs/remotes/origin/main")
        self.assertEqual((list(local), pushed), (["T2"], {}))


class InTick(Base):
    def setUp(self):
        super().setUp()
        self.git = fake({"b1-tip": ["src/a.py"]}, "b1")
        patch = mock.patch.object(dispatch_hold, "_git", self.git)
        patch.start()
        self.addCleanup(patch.stop)
        dispatch_hold._CACHE.clear()
        self.active = self.task(scope_files=["other/x"])
        self.conn.execute("UPDATE tasks SET status='gated', branch='b1', repo_path='/r' WHERE id=?", (self.active,))

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

    def test_pushing_repo_compares_against_the_remote_ref(self):
        self.pipe.prog["landing"]["push"] = True
        self.git.files = {("m1", "b1-tip"): ["src/a.py"], ("r1", "b1-tip"): []}
        self.git.refs["refs/remotes/origin/main"] = "r1"
        t = self.task(scope_files=["src/a.py"])
        self.assertEqual(self.d.tick()["launched"], [t])

    def test_no_git_when_there_are_no_slots(self):
        self.task(scope_files=["src/a.py"])
        self.verdict = {"verdict": "go", "slots": 0, "reasons": []}
        self.d.tick()
        self.assertEqual(self.git.calls, [])

    def test_git_failure_in_tick_holds_nothing(self):
        t = self.task(scope_files=["src/a.py"])
        with mock.patch.object(dispatch_hold, "_git", side_effect=RuntimeError("boom")):
            self.assertEqual(self.d.tick()["launched"], [t])


if __name__ == "__main__":
    unittest.main()
