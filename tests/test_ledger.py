import io
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from drover.ledger import IllegalTransition, board, connect, migrate, ops, schema_version
from drover.ledger import cli, db, scope, states
from drover.ledger.schema import MIGRATIONS


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self.tmp.name) / "ledger.db"
        self.conn = connect(self.path)
        self.conns = [self.conn]
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: [c.close() for c in self.conns])

    def task(self, title="t", status="ready", **kw):
        t = ops.create_task(self.conn, title, kw.pop("repo", "p"), **kw)
        if status in ("ready", "draft"):
            if status == "ready":
                ops.approve(self.conn, t["id"])
            else:
                ops.set_status(self.conn, t["id"], "draft")
        return ops.get_task(self.conn, t["id"])

    def status(self, tid):
        return ops.get_task(self.conn, tid)["status"]

    def worker(self, **kw):
        return {"owner_kind": "claude", "model": "sonnet", **kw}


class TransitionTests(Base):
    def test_every_table_row_is_known_and_terminals_are_closed(self):
        for old, news in states.TRANSITIONS.items():
            self.assertIn(old, states.STATES)
            self.assertTrue(news <= set(states.STATES))
        for s in states.TERMINAL:
            self.assertEqual(states.TRANSITIONS[s], set())

    def test_happy_path(self):
        t = self.task()
        ops.claim_next(self.conn, self.worker())
        for s in ("running", "gated", "reviewed", "landed"):
            ops.set_status(self.conn, t["id"], s, "w")
        self.assertEqual(self.status(t["id"]), "landed")

    def test_illegal_transitions_raise_and_change_nothing(self):
        t = self.task(status="draft")
        n = len(ops.events(self.conn, task_id=t["id"]))
        for bad in ("running", "landed", "gated", "claimed"):
            with self.assertRaises(IllegalTransition):
                ops.set_status(self.conn, t["id"], bad, "owner")
        self.assertEqual(len(ops.events(self.conn, task_id=t["id"])), n)
        self.assertEqual(self.status(t["id"]), "draft")

    def test_landed_and_cancelled_are_terminal(self):
        t = self.task()
        ops.set_status(self.conn, t["id"], "cancelled")
        with self.assertRaises(IllegalTransition):
            ops.set_status(self.conn, t["id"], "ready", "owner")

    def test_every_status_change_writes_one_event(self):
        t = self.task(status="draft")
        before = len(ops.events(self.conn, task_id=t["id"], kind="status"))
        ops.approve(self.conn, t["id"])
        ops.claim_next(self.conn, self.worker())
        ops.set_status(self.conn, t["id"], "running")
        evs = ops.events(self.conn, task_id=t["id"], kind="status")
        self.assertEqual(len(evs) - before, 3)
        self.assertEqual(evs[-1]["data"]["from"], "claimed")

    def test_approve_is_owner_only(self):
        t = self.task(status="proposed")
        with self.assertRaises(PermissionError):
            ops.approve(self.conn, t["id"], actor="worker")
        with self.assertRaises(PermissionError):
            ops.set_status(self.conn, t["id"], "ready", "worker")
        self.assertEqual(ops.approve(self.conn, t["id"])["status"], "ready")

    def test_approve_rejects_wrong_state(self):
        t = self.task()
        with self.assertRaises(IllegalTransition):
            ops.approve(self.conn, t["id"])

    def test_blocked_needs_reason_and_unblock_restores(self):
        t = self.task(status="draft")
        with self.assertRaises(ValueError):
            ops.set_status(self.conn, t["id"], "blocked")
        ops.block(self.conn, t["id"], "waiting on owner")
        self.assertEqual(ops.get_task(self.conn, t["id"])["blocker"]["reason"], "waiting on owner")
        self.assertEqual(ops.unblock(self.conn, t["id"])["status"], "draft")
        self.assertIsNone(ops.get_task(self.conn, t["id"])["blocker"])

    def test_requeue_clears_owner(self):
        t = self.task()
        ops.claim_next(self.conn, self.worker())
        self.assertEqual(ops.get_task(self.conn, t["id"])["owner_kind"], "claude")
        ops.set_status(self.conn, t["id"], "ready")
        self.assertIsNone(ops.get_task(self.conn, t["id"])["owner_kind"])

    def test_events_are_append_only(self):
        self.task()
        import sqlite3
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE task_events SET kind='x'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("DELETE FROM task_events")

    def test_failed_mutation_rolls_back_whole_transaction(self):
        t = self.task()
        with self.assertRaises(ValueError):
            ops.update_task(self.conn, t["id"], title="new", handoff="x" * 1501)
        self.assertEqual(ops.get_task(self.conn, t["id"])["title"], "t")


class EntityTests(Base):
    def test_ids_are_base36(self):
        ids = [ops.create_task(self.conn, "t", "p")["id"] for _ in range(40)]
        self.assertEqual(ids[0], "T-1")
        self.assertEqual(ids[9], "T-a")
        self.assertEqual(ids[34], "T-z")
        self.assertEqual(ids[35], "T-10")
        self.assertEqual(len(set(ids)), 40)

    def test_task_json_fields_roundtrip_and_enums_enforced(self):
        t = ops.create_task(self.conn, "t", "p", acceptance=["a", "b"], scope_files=["src/**"], priority="P1",
                            size="S", risk="high")
        self.assertEqual((t["acceptance"], t["scope_files"], t["status"]), (["a", "b"], ["src/**"], "proposed"))
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            ops.create_task(self.conn, "t", "p", priority="P9")

    def test_update_limits(self):
        t = self.task()
        ops.update_task(self.conn, t["id"], handoff="x" * 1500, goal="g")
        with self.assertRaises(ValueError):
            ops.update_task(self.conn, t["id"], handoff="x" * 1501)
        with self.assertRaises(ValueError):
            ops.update_task(self.conn, t["id"], status="landed")

    def test_epic_crud_and_task_epic_link(self):
        e = ops.create_epic(self.conn, "p", "E", "plan")
        ops.update_epic(self.conn, e["id"], title="E2")
        self.assertEqual(ops.get_epic(self.conn, e["id"])["title"], "E2")
        with self.assertRaises(ops.NotFound):
            ops.create_task(self.conn, "t", "p", epic_id="E-zz")

    def test_dep_cycle_and_self_rejected(self):
        a, b, c = (self.task(str(i)) for i in range(3))
        ops.add_dep(self.conn, b["id"], a["id"])
        ops.add_dep(self.conn, c["id"], b["id"], "waits-for")
        for x, y in ((a["id"], c["id"]), (a["id"], a["id"])):
            with self.assertRaises(ValueError):
                ops.add_dep(self.conn, x, y)
        with self.assertRaises(ValueError):
            ops.add_dep(self.conn, c["id"], a["id"], "nonsense")

    def test_runs(self):
        t = self.task()
        ops.claim_next(self.conn, self.worker())
        rid = ops.start_run(self.conn, t["id"], "worker", "claude", "sonnet", "high")
        self.assertEqual(self.status(t["id"]), "running")
        self.assertEqual(ops.get_task(self.conn, t["id"])["attempts"], 1)
        with self.assertRaises(ValueError):
            ops.finish_run(self.conn, rid, "ok", note="n" * 301)
        r = ops.finish_run(self.conn, rid, "ok", tokens_in=10, tokens_out=5, allowance_used={"five_hour": 0.5},
                           gate_receipt="r1")
        self.assertEqual((r["outcome"], r["tokens_in"], r["allowance_used"]), ("ok", 10, {"five_hour": 0.5}))
        self.assertIsNotNone(r["wall_s"])
        with self.assertRaises(ValueError):
            ops.finish_run(self.conn, rid, "ok")
        with self.assertRaises(Exception):
            ops.start_run(self.conn, t["id"], "worker", "gpt", "x")

    def test_policy_settings_stamp_claim(self):
        ops.add_policy_version(self.conn, "v7", {"a": 1}, "note")
        self.task()
        t = ops.claim_next(self.conn, self.worker())
        self.assertEqual(t["policy_version"], "v7")
        ops.set_setting(self.conn, "k", 3)
        self.assertEqual(ops.get_setting(self.conn, "k"), "3")

    def test_decisions(self):
        d = ops.record_decision(self.conn, "gate-class", "c1", "nimble-v3", "abc", "syntax", {"syntax": 0.9}, 0.9,
                                shadow=True, rule_answer="type")
        self.assertIsNone(ops.get_decision(self.conn, d)["outcome"])
        ops.record_outcome(self.conn, d, {"correct": False})
        got = ops.get_decision(self.conn, d)
        self.assertEqual((got["answer"], got["probabilities"], got["rule_answer"], got["outcome"], got["shadow"]),
                         ("syntax", {"syntax": 0.9}, "type", {"correct": False}, 1))
        with self.assertRaises(ops.NotFound):
            ops.record_outcome(self.conn, 999, {})


class ClaimTests(Base):
    def test_priority_epic_task_rank_order(self):
        e1 = ops.create_epic(self.conn, "p", "E1")
        e2 = ops.create_epic(self.conn, "p", "E2")
        low = self.task("low", epic_id=e1["id"], priority="P3")
        e2b = self.task("e2-b", epic_id=e2["id"])
        e2a = self.task("e2-a", epic_id=e2["id"])
        e1t = self.task("e1", epic_id=e1["id"])
        p0 = self.task("p0-e2", epic_id=e2["id"], priority="P0")
        got = [ops.claim_next(self.conn, self.worker())["title"] for _ in range(5)]
        self.assertEqual(got, ["p0-e2", "e1", "e2-b", "e2-a", "low"])
        self.assertIsNone(ops.claim_next(self.conn, self.worker()))

    def test_task_rank_then_size_then_pressure(self):
        now = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
        e = ops.create_epic(self.conn, "p", "E")
        # same rank forced equal so the later keys decide
        a = ops.create_task(self.conn, "L-old", "p", e["id"], size="L", rank=1.0)
        b = ops.create_task(self.conn, "S", "p", e["id"], size="S", rank=1.0)
        c = ops.create_task(self.conn, "M-blocking", "p", e["id"], size="M", rank=1.0)
        d = ops.create_task(self.conn, "dependent", "p", e["id"], rank=5.0, deps=[c["id"]])
        for t in (a, b, c):
            ops.approve(self.conn, t["id"])
        # pressure: c has a dependent so its age counts double; make all the same age
        self.conn.execute("UPDATE tasks SET created='2026-10-16T12:00:00Z'")
        order = [t["title"] for t in ops.ready_tasks(self.conn, now=now)]
        self.assertEqual(order, ["M-blocking", "S", "L-old"])
        # equal pressure (no dependents) falls to size
        self.conn.execute("DELETE FROM task_deps")
        order = [t["title"] for t in ops.ready_tasks(self.conn, now=now)]
        self.assertEqual(order, ["S", "M-blocking", "L-old"])

    def test_older_wins_on_pressure(self):
        now = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
        a = self.task("new", rank=1.0)
        b = self.task("old", rank=1.0)
        self.conn.execute("UPDATE tasks SET created='2026-10-01T00:00:00Z' WHERE id=?", (b["id"],))
        self.assertEqual(ops.ready_tasks(self.conn, now=now)[0]["title"], "old")

    def test_dependency_gating(self):
        a = self.task("a")
        b = self.task("b", deps=[a["id"]])
        w = self.task("w", deps=[(a["id"], "waits-for")])
        self.assertEqual({t["title"] for t in ops.ready_tasks(self.conn)}, {"a", "w"})  # waits-for does not gate
        ops.claim_next(self.conn, self.worker(), now=None)
        got = ops.claim_next(self.conn, self.worker())
        self.assertEqual(got["title"], "w")
        self.assertIsNone(ops.claim_next(self.conn, self.worker()))
        for s in ("running", "gated", "reviewed"):
            ops.set_status(self.conn, a["id"], s, "w")
        self.assertIsNone(ops.claim_next(self.conn, self.worker()))  # reviewed is not landed
        ops.set_status(self.conn, a["id"], "landed", "w")
        self.assertEqual(ops.claim_next(self.conn, self.worker())["title"], "b")

    def test_unapproved_tasks_never_claimed(self):
        self.task("p", status="proposed")
        self.task("d", status="draft")
        self.assertIsNone(ops.claim_next(self.conn, self.worker()))

    def test_scope_overlap_excluded_while_claimed_or_running(self):
        a = self.task("a", repo="r", scope_files=["src/ledger/*.py"])
        b = self.task("b", repo="r", scope_files=["src/ledger/ops.py"])
        c = self.task("c", repo="r", scope_files=["docs/**"])
        d = self.task("d", repo="other", scope_files=["src/ledger/ops.py"])
        self.assertEqual(ops.claim_next(self.conn, self.worker())["title"], "a")
        self.assertEqual(ops.claim_next(self.conn, self.worker())["title"], "c")
        self.assertEqual(ops.claim_next(self.conn, self.worker())["title"], "d")  # other repo is free
        self.assertIsNone(ops.claim_next(self.conn, self.worker()))  # b still conflicts with claimed a
        ops.set_status(self.conn, a["id"], "running", "w")
        self.assertIsNone(ops.claim_next(self.conn, self.worker()))
        for st in ("gated", "reviewed"):  # unlanded changes still own the scope
            ops.set_status(self.conn, a["id"], st, "w")
            self.assertIsNone(ops.claim_next(self.conn, self.worker()))
        ops.set_status(self.conn, a["id"], "landed", "w")
        self.assertEqual(ops.claim_next(self.conn, self.worker())["title"], "b")

    def test_glob_overlap_rules(self):
        yes = [("a/b.py", "a/b.py"), ("a/*.py", "a/b.py"), ("a/**", "a/b/c.py"), ("src", "src/x.py"),
               ("src/*", "src/deep/*.md"), ("**/*.py", "x/y.py"), ("a/*.py", "a/b*.py")]
        no = [("a/*.py", "b/*.py"), ("a/x.py", "a/y.py"), ("docs/**", "src/**"), ("a/*.py", "a/b.md")]
        for x, y in yes:
            self.assertTrue(scope.overlap(x, y) and scope.overlap(y, x), (x, y))
        for x, y in no:
            self.assertFalse(scope.overlap(x, y) or scope.overlap(y, x), (x, y))

    def test_claim_filters_and_worker_fields(self):
        self.task("a", repo="r1")
        self.task("b", repo="r2")
        t = ops.claim_next(self.conn, self.worker(repo="r2", branch="br", worktree="/w"))
        self.assertEqual((t["title"], t["status"], t["branch"], t["worktree"], t["owner_kind"], t["model"]),
                         ("b", "claimed", "br", "/w", "claude", "sonnet"))

    def _racer(self, n_conns, results, barrier):
        c = connect(self.path)
        try:
            barrier.wait()
            while True:
                t = ops.claim_next(c, {"owner_kind": "w"})
                if t is None:
                    return
                results.append(t["id"])
        finally:
            c.close()

    def test_claim_race_two_connections_no_double_claim(self):
        for i in range(12):
            self.task(f"t{i}", scope_files=[f"d{i}/*"])
        results, barrier = [], threading.Barrier(2)
        ths = [threading.Thread(target=self._racer, args=(2, results, barrier)) for _ in range(2)]
        [t.start() for t in ths]
        [t.join(30) for t in ths]
        self.assertEqual(len(results), 12)
        self.assertEqual(len(set(results)), 12)
        self.assertEqual(len(ops.list_tasks(self.conn, status="claimed")), 12)

    def test_claim_race_single_task_one_winner(self):
        self.task("only")
        results, barrier = [], threading.Barrier(2)
        ths = [threading.Thread(target=self._racer, args=(2, results, barrier)) for _ in range(2)]
        [t.start() for t in ths]
        [t.join(30) for t in ths]
        self.assertEqual(len(results), 1)

    def test_claim_race_overlapping_scope_never_both(self):
        for i in range(6):
            self.task(f"t{i}", repo="r", scope_files=["shared/*"])
        results, barrier = [], threading.Barrier(2)
        ths = [threading.Thread(target=self._racer, args=(2, results, barrier)) for _ in range(2)]
        [t.start() for t in ths]
        [t.join(30) for t in ths]
        self.assertEqual(len(results), 1)


class ReorderTests(Base):
    def titles(self, epic):
        return [t["title"] for t in sorted(ops.list_tasks(self.conn, epic_id=epic), key=lambda t: t["rank"])]

    def setUp(self):
        super().setUp()
        self.e = ops.create_epic(self.conn, "p", "E")["id"]
        self.ts = [ops.create_task(self.conn, n, "p", self.e)["id"] for n in "abcd"]

    def test_before_after(self):
        ops.reorder(self.conn, "task", self.ts[3], before_id=self.ts[0])
        self.assertEqual(self.titles(self.e), list("dabc"))
        ops.reorder(self.conn, "task", self.ts[3], after_id=self.ts[2])
        self.assertEqual(self.titles(self.e), list("abcd"))
        ops.reorder(self.conn, "task", self.ts[0], after_id=self.ts[1])
        self.assertEqual(self.titles(self.e), list("bacd"))

    def test_repeated_squeeze_renormalises(self):
        for _ in range(80):  # bisecting the same gap exhausts float precision
            ops.reorder(self.conn, "task", self.ts[2], before_id=self.ts[1])
            ops.reorder(self.conn, "task", self.ts[3], before_id=self.ts[2])
            ops.reorder(self.conn, "task", self.ts[1], before_id=self.ts[3])
        ranks = [t["rank"] for t in ops.list_tasks(self.conn, epic_id=self.e)]
        self.assertEqual(len(set(ranks)), 4)
        ops.reorder(self.conn, "task", self.ts[0], after_id=self.ts[1])
        self.assertEqual(len(set(t["rank"] for t in ops.list_tasks(self.conn, epic_id=self.e))), 4)

    def test_epics_and_errors(self):
        e2 = ops.create_epic(self.conn, "p", "E2")["id"]
        e3 = ops.create_epic(self.conn, "p", "E3")["id"]
        ops.reorder(self.conn, "epic", e3, before_id=self.e)
        self.assertEqual([e["id"] for e in ops.list_epics(self.conn)], [e3, self.e, e2])
        other = ops.create_task(self.conn, "x", "p", e2)["id"]
        with self.assertRaises(ValueError):
            ops.reorder(self.conn, "task", self.ts[0], before_id=other)
        with self.assertRaises(ValueError):
            ops.reorder(self.conn, "task", self.ts[0])
        with self.assertRaises(ValueError):
            ops.reorder(self.conn, "task", self.ts[0], before_id=self.ts[0])

    def test_reorder_changes_claim_order(self):
        for t in self.ts:
            ops.approve(self.conn, t)
        ops.reorder(self.conn, "task", self.ts[3], before_id=self.ts[0])
        self.assertEqual(ops.claim_next(self.conn, self.worker())["title"], "d")


class CommentTests(Base):
    def test_owner_comment_requeues_needs_owner(self):
        t = self.task()
        ops.set_status(self.conn, t["id"], "needs-owner", "w", reason="stuck")
        r = ops.add_comment(self.conn, "task", t["id"], "someone", "fyi")
        self.assertEqual((r["routed"], self.status(t["id"])), ([], "needs-owner"))
        r = ops.add_comment(self.conn, "task", t["id"], "owner", "do X")
        self.assertEqual((r["routed"], self.status(t["id"])), (["requeued"], "ready"))
        self.assertEqual(len(ops.list_comments(self.conn, "task", t["id"])), 2)

    def test_plain_comment_changes_nothing_but_logs(self):
        t = self.task()
        ops.add_comment(self.conn, "task", t["id"], "owner", "hi")
        self.assertEqual(self.status(t["id"]), "ready")
        self.assertEqual(len(ops.events(self.conn, task_id=t["id"], kind="comment")), 1)

    def test_urgent_holds_dependents_and_release_restores(self):
        a = self.task("a")
        b = self.task("b", deps=[a["id"]])
        c = self.task("c", deps=[b["id"]], status="draft")
        free = self.task("free")
        r = ops.add_comment(self.conn, "task", a["id"], "owner", "stop", urgent=True)
        self.assertEqual(r["routed"], ["held"])
        self.assertEqual((self.status(a["id"]), self.status(b["id"]), self.status(c["id"]), self.status(free["id"])),
                         ("ready", "blocked", "blocked", "ready"))
        self.assertEqual(ops.get_task(self.conn, b["id"])["blocker"]["reason"], "urgent")
        self.assertEqual(sorted(ops.replan_release(self.conn, a["id"], reason="urgent")), sorted([b["id"], c["id"]]))
        self.assertEqual((self.status(b["id"]), self.status(c["id"])), ("ready", "draft"))

    def test_urgent_on_epic_holds_its_pending_tasks(self):
        e = ops.create_epic(self.conn, "p", "E")
        a = self.task("a", epic_id=e["id"])
        ops.add_comment(self.conn, "epic", e["id"], "owner", "pause", urgent=True)
        self.assertEqual(self.status(a["id"]), "blocked")
        self.assertIsNone(ops.claim_next(self.conn, self.worker()))
        ops.replan_release(self.conn, e["id"], reason="urgent")
        self.assertEqual(self.status(a["id"]), "ready")

    def test_replan_flag_creates_request_event(self):
        t = self.task()
        r = ops.add_comment(self.conn, "task", t["id"], "owner", "wrong plan", replan=True)
        self.assertEqual(r["routed"], ["replan-request"])
        evs = ops.events(self.conn, task_id=t["id"], kind="replan-request")
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["data"]["comment"], r["id"])
        self.assertEqual(self.status(t["id"]), "ready")

    def test_bad_target(self):
        with self.assertRaises(ops.NotFound):
            ops.add_comment(self.conn, "task", "T-zzz", "owner", "x")
        with self.assertRaises(ValueError):
            ops.add_comment(self.conn, "run", "1", "owner", "x")


class ReplanTests(Base):
    def test_hold_covers_dependents_and_epic_scope_overlap_only(self):
        e, e2 = ops.create_epic(self.conn, "p", "E"), ops.create_epic(self.conn, "p", "E2")
        target = self.task("target", epic_id=e["id"], scope_files=["src/a/*"])
        dep = self.task("dep", epic_id=e["id"], deps=[target["id"]])
        dep2 = self.task("dep-of-dep", deps=[dep["id"]])  # other epic, still transitive dependent
        overlap = self.task("overlap", epic_id=e["id"], scope_files=["src/a/x.py"])
        other_epic_overlap = self.task("oe", epic_id=e2["id"], scope_files=["src/a/x.py"])
        disjoint = self.task("disjoint", epic_id=e["id"], scope_files=["docs/*"])
        held = ops.replan_hold(self.conn, target["id"], "system")
        self.assertEqual(set(held), {dep["id"], dep2["id"], overlap["id"]})
        st = {t["title"]: t["status"] for t in ops.list_tasks(self.conn)}
        self.assertEqual(st, {"target": "blocked", "dep": "blocked", "dep-of-dep": "blocked", "overlap": "blocked",
                              "oe": "ready", "disjoint": "ready"})
        self.assertEqual(ops.get_task(self.conn, target["id"])["blocker"]["reason"], "replan")
        self.assertEqual({ops.claim_next(self.conn, self.worker())["title"] for _ in range(2)}, {"oe", "disjoint"})
        self.assertIsNone(ops.claim_next(self.conn, self.worker()))
        self.assertEqual(len(ops.events(self.conn, task_id=target["id"], kind="hold")), 1)

    def test_release_restores_prior_states(self):
        e = ops.create_epic(self.conn, "p", "E")
        target = self.task("t", epic_id=e["id"], scope_files=["a/*"])
        draft = self.task("d", epic_id=e["id"], scope_files=["a/*"], status="draft")
        ops.replan_hold(self.conn, target["id"])
        released = ops.replan_release(self.conn, target["id"], "director")
        self.assertEqual(set(released), {target["id"], draft["id"]})
        self.assertEqual((self.status(target["id"]), self.status(draft["id"])), ("ready", "draft"))
        self.assertEqual(ops.replan_release(self.conn, target["id"]), [])

    def test_running_task_can_be_held_and_other_blockers_untouched(self):
        t = self.task("t")
        dep = self.task("dep", deps=[t["id"]])
        ops.block(self.conn, dep["id"], "own reason")
        ops.claim_next(self.conn, self.worker())
        ops.replan_hold(self.conn, t["id"])
        self.assertEqual([h["source"] for h in ops.get_task(self.conn, dep["id"])["blocker"]["holds"]],
                         ["manual", t["id"]])
        ops.replan_release(self.conn, t["id"])
        self.assertEqual((self.status(t["id"]), self.status(dep["id"])), ("ready", "blocked"))

    def test_cannot_hold_landed(self):
        t = self.task("t")
        ops.claim_next(self.conn, self.worker())
        for s in ("running", "gated", "reviewed", "landed"):
            ops.set_status(self.conn, t["id"], s, "w")
        with self.assertRaises(IllegalTransition):
            ops.replan_hold(self.conn, t["id"])


class BacklogGateTests(Base):
    def test_blocked_route_cannot_reach_ready_unapproved(self):
        for first in ("proposed", "draft"):
            t = self.task(status=first)
            ops.block(self.conn, t["id"], "x")
            with self.assertRaises(PermissionError):
                ops.set_status(self.conn, t["id"], "ready", "system")
            with self.assertRaises(PermissionError):
                ops.set_status(self.conn, t["id"], "ready", "owner")
            self.assertEqual(ops.unblock(self.conn, t["id"])["status"], first)
            self.assertIsNone(ops.get_task(self.conn, t["id"])["approved_at"])

    def test_needs_owner_routes_cannot_reach_ready_unapproved(self):
        t = self.task(status="proposed")
        ops.block(self.conn, t["id"], "x")
        ops.set_status(self.conn, t["id"], "needs-owner", "w", reason="q")
        with self.assertRaises(PermissionError):
            ops.set_status(self.conn, t["id"], "ready", "owner")
        r = ops.add_comment(self.conn, "task", t["id"], "owner", "go")
        self.assertEqual((r["routed"], self.status(t["id"])), ([], "needs-owner"))
        q = ops.ask_question(self.conn, "task", t["id"], "?")
        ops.answer_question(self.conn, q, text="yes")
        self.assertEqual(self.status(t["id"]), "needs-owner")
        ops.set_status(self.conn, t["id"], "draft", "owner")
        self.assertEqual(ops.approve(self.conn, t["id"])["status"], "ready")

    def test_claim_and_hold_release_never_make_unapproved_ready(self):
        t = self.task(status="proposed")
        ops.replan_hold(self.conn, t["id"])
        ops.replan_release(self.conn, t["id"])
        self.assertEqual(self.status(t["id"]), "proposed")
        self.assertIsNone(ops.claim_next(self.conn, self.worker()))

    def test_approved_survives_requeue(self):
        t = self.task()
        ops.claim_next(self.conn, self.worker())
        ops.set_status(self.conn, t["id"], "ready", "w")
        self.assertIsNotNone(ops.get_task(self.conn, t["id"])["approved_at"])

    def test_migration_backfills_approved(self):
        c = connect(Path(self.tmp.name) / "old.db", migrate_db=False)
        self.conns.append(c)
        migrate(c, MIGRATIONS[:1])
        now = "2026-01-01T00:00:00Z"
        for i, st in enumerate(("proposed", "ready", "landed")):
            c.execute("INSERT INTO tasks(id,program,title,rank,status,created,updated) VALUES(?,?,?,?,?,?,?)",
                      (f"T-{i}", "p", "t", i, st, now, now))
        migrate(c)
        got = {r[0]: r[1] for r in c.execute("SELECT status, approved_at FROM tasks")}
        self.assertIsNone(got["proposed"])
        self.assertIsNotNone(got["ready"])
        self.assertIsNotNone(got["landed"])


class NestedHoldTests(Base):
    def test_urgent_then_replan_needs_both_released(self):
        a = self.task("a")
        b = self.task("b", deps=[a["id"]])
        ops.add_comment(self.conn, "task", a["id"], "owner", "stop", urgent=True)
        ops.replan_hold(self.conn, a["id"])  # a itself is not blocked by urgent; b is held by both reasons
        self.assertEqual(self.status(b["id"]), "blocked")
        ops.replan_release(self.conn, a["id"], reason="urgent")
        self.assertEqual(self.status(b["id"]), "blocked")  # replan still holds it
        self.assertEqual(ops.replan_release(self.conn, a["id"], reason="replan"), [a["id"], b["id"]])
        self.assertEqual((self.status(a["id"]), self.status(b["id"])), ("ready", "ready"))

    def test_replan_hold_on_already_blocked_task_adds(self):
        t = self.task("t")
        ops.block(self.conn, t["id"], "manual reason")
        ops.replan_hold(self.conn, t["id"])
        self.assertEqual(len(ops.get_task(self.conn, t["id"])["blocker"]["holds"]), 2)
        ops.unblock(self.conn, t["id"])  # removes only its own
        self.assertEqual(self.status(t["id"]), "blocked")
        self.assertEqual(ops.get_task(self.conn, t["id"])["blocker"]["holds"][0]["reason"], "replan")
        ops.replan_release(self.conn, t["id"])
        self.assertEqual(self.status(t["id"]), "ready")

    def test_two_sources_each_release_own(self):
        a, b = self.task("a"), self.task("b")
        c = self.task("c", deps=[a["id"], b["id"]])
        ops.replan_hold(self.conn, a["id"])
        ops.replan_hold(self.conn, b["id"])
        ops.replan_release(self.conn, a["id"])
        self.assertEqual(self.status(c["id"]), "blocked")
        ops.replan_release(self.conn, b["id"])
        self.assertEqual(self.status(c["id"]), "ready")


class FollowUpTests(Base):
    def test_demotion_clears_approval(self):
        t = self.task()
        ops.set_status(self.conn, t["id"], "draft", "owner")
        self.assertIsNone(ops.get_task(self.conn, t["id"])["approved_at"])
        with self.assertRaises(PermissionError):
            ops.set_status(self.conn, t["id"], "ready", "system")
        ops.approve(self.conn, t["id"])
        self.assertIsNotNone(ops.get_task(self.conn, t["id"])["approved_at"])

    def test_cli_actor_defaults_to_system(self):
        for sub in cli.parser()._subparsers._group_actions[0].choices.values():
            for act in sub._actions:
                if "--actor" in act.option_strings:
                    self.assertEqual(act.default, "system")

    def test_old_format_blocker_converted(self):
        t = self.task()
        ops.block(self.conn, t["id"], "old")
        old = '{"prev":"ready","reason":"old"}'
        self.conn.execute("UPDATE tasks SET blocker=? WHERE id=?", (old, t["id"]))
        ops.add_hold(self.conn, t["id"], "T-9", "replan")
        holds = ops.get_task(self.conn, t["id"])["blocker"]["holds"]
        self.assertEqual([h["reason"] for h in holds], ["old", "replan"])
        self.conn.execute("UPDATE tasks SET blocker=? WHERE id=?", (old, t["id"]))
        self.assertTrue(ops.remove_hold(self.conn, t["id"], "manual"))
        self.assertEqual(self.status(t["id"]), "ready")

    def test_replan_release_leaves_urgent_holds(self):
        a = self.task("a")
        b = self.task("b", deps=[a["id"]])
        ops.add_comment(self.conn, "task", a["id"], "owner", "stop", urgent=True)
        ops.replan_hold(self.conn, a["id"])
        ops.replan_release(self.conn, a["id"])
        self.assertEqual(self.status(b["id"]), "blocked")
        self.assertEqual([h["reason"] for h in ops.get_task(self.conn, b["id"])["blocker"]["holds"]], ["urgent"])
        ops.replan_release(self.conn, a["id"], reason="urgent")
        self.assertEqual(self.status(b["id"]), "ready")

    def test_board_escapes_links(self):
        t = self.task("see [click](http://evil.example)")
        ops.block(self.conn, t["id"], "r")
        text = board.render(self.conn)
        self.assertNotIn("[click](", text)
        self.assertIn("\\[click\\]", text)


class QuestionTests(Base):
    OPTS = [{"label": "A", "description": "a", "recommended": True}, {"label": "B"}]

    def test_ask_answer_choice_and_requeue(self):
        t = self.task()
        q = ops.ask_question(self.conn, "task", t["id"], "Which?", "body", self.OPTS, needs_owner=True)
        self.assertEqual(self.status(t["id"]), "needs-owner")
        self.assertEqual(ops.get_question(self.conn, q)["options"][1], {"label": "B", "description": "", "recommended": False})
        self.assertEqual(len(ops.open_questions(self.conn)), 1)
        ans = ops.answer_question(self.conn, q, choice="A", text="because")
        self.assertEqual((ans["status"], ans["answer_choice"], ans["answer_text"]), ("answered", "A", "because"))
        self.assertEqual(self.status(t["id"]), "ready")
        self.assertEqual(ops.open_questions(self.conn), [])

    def test_two_questions_requeue_only_after_both(self):
        t = self.task()
        q1 = ops.ask_question(self.conn, "task", t["id"], "1", options=self.OPTS, needs_owner=True)
        q2 = ops.ask_question(self.conn, "task", t["id"], "2", options=self.OPTS, needs_owner=True)
        ops.answer_question(self.conn, q1, choice="B")
        self.assertEqual(self.status(t["id"]), "needs-owner")
        ops.answer_question(self.conn, q2, choice="B")
        self.assertEqual(self.status(t["id"]), "ready")

    def test_validation(self):
        t = self.task()
        q = ops.ask_question(self.conn, "task", t["id"], "?", options=self.OPTS, allow_text=False)
        for kw in ({}, {"choice": "Z"}, {"text": "free"}):
            with self.assertRaises(ValueError):
                ops.answer_question(self.conn, q, **kw)
        ops.answer_question(self.conn, q, choice="A")
        with self.assertRaises(ValueError):
            ops.answer_question(self.conn, q, choice="A")
        self.assertEqual(self.status(t["id"]), "ready")  # question without needs_owner leaves state alone

    def test_free_text_only_question_on_epic(self):
        e = ops.create_epic(self.conn, "p", "E")
        q = ops.ask_question(self.conn, "epic", e["id"], "Name?")
        self.assertEqual(ops.answer_question(self.conn, q, text="Drover")["answer_text"], "Drover")


class BoardAndCliTests(Base):
    def test_board_sections(self):
        e = ops.create_epic(self.conn, "p", "E")
        a = self.task("running-one", epic_id=e["id"])
        b = self.task("blocked-one")
        ops.block(self.conn, b["id"], "needs a key")
        n = self.task("owner-one")
        ops.set_status(self.conn, n["id"], "needs-owner", "w", reason="x")
        ops.claim_next(self.conn, self.worker())
        ops.set_status(self.conn, a["id"], "running", "w")
        done = self.task("done-one")
        ops.claim_next(self.conn, self.worker())
        for s in ("running", "gated", "reviewed", "landed"):
            ops.set_status(self.conn, done["id"], s, "w")
        ops.ask_question(self.conn, "task", a["id"], "Pick one")
        text = board.render(self.conn)
        for needle in ("| ready | 0 |", "| landed | 1 |", "running-one", "needs a key", "owner-one", "Pick one",
                       "## Landed today\n\n- " + done["id"]):
            self.assertIn(needle, text)
        tomorrow = datetime.now(timezone.utc) + timedelta(days=2)
        self.assertIn("## Landed today\n\n- none", board.render(self.conn, now=tomorrow))

    def test_board_escapes_untrusted_text(self):
        t = self.task("b\n## Injected\n| x | y |")
        ops.block(self.conn, t["id"], "r\n# Forged")
        ops.ask_question(self.conn, "task", t["id"], "q\n> quote")
        evil = self.task("# lead", status="proposed")
        text = board.render(self.conn)
        self.assertNotIn("\n## Injected", text)
        self.assertNotIn("\n# Forged", text)
        self.assertNotIn("\n> quote", text)
        self.assertEqual([l for l in text.splitlines() if l.startswith("#")],
                         ["# Drover board", "## Counts", "## Running", "## Blocked", "## Needs owner",
                          "## Open questions", "## Landed today"])
        self.assertIn("b ## Injected", text)  # inline after the bullet, not a heading

    def run_cli(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env or {}, clear=False), redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["--db", str(self.path), *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_cli_flow(self):
        self.assertEqual(self.run_cli("init")[0], 0)
        rc, out, _ = self.run_cli("add", "task", "Do it", "--scope", "a/*", "--priority", "P1", "--json")
        import json
        tid = json.loads(out)["id"]
        self.assertEqual(self.run_cli("ready")[1].strip(), "nothing ready")
        self.assertEqual(self.run_cli("approve", tid, "--actor", "worker")[0], 2)
        self.assertEqual(self.run_cli("approve", tid)[0], 2)  # default actor is system
        self.assertEqual(self.run_cli("approve", tid, "--actor", "owner")[0], 0)
        self.assertIn(tid, self.run_cli("ready")[1])
        rc, out, _ = self.run_cli("claim", "--owner", "claude", "--model", "sonnet")
        self.assertIn(f"{tid} claimed", out)
        self.assertEqual(self.run_cli("claim")[0], 3)
        self.assertEqual(self.run_cli("block", tid, "why")[0], 0)
        self.assertIn("blocker: why", self.run_cli("show", tid)[1])
        self.assertIn("| blocked | 1 |", self.run_cli("board")[1])
        self.assertEqual(self.run_cli("show", "T-nope")[0], 2)

    def test_default_path_rules(self):
        self.assertEqual(db.default_path({"DROVER_LEDGER": "/x/y.db"}), Path("/x/y.db"))
        if os.name != "nt":
            with self.assertRaises(db.HubRequired):
                db.default_path({})
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("DROVER_LEDGER", None)
                err = io.StringIO()
                with redirect_stderr(err):
                    self.assertEqual(cli.main(["board"]), 2)
                self.assertIn("Hub", err.getvalue())


class MigrationTests(Base):
    def test_idempotent_and_versioned(self):
        v = schema_version(self.conn)
        self.assertEqual(v, MIGRATIONS[-1][0])
        self.assertEqual(migrate(self.conn), v)
        self.assertEqual(migrate(self.conn), v)
        self.conn.close()
        c2 = connect(self.path)
        self.conns.append(c2)
        self.assertEqual(c2.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0], len(MIGRATIONS))

    def test_forward_only_extra_migration_applied_once_and_data_kept(self):
        t = self.task("keep")
        extra = MIGRATIONS + [(MIGRATIONS[-1][0] + 1, "ALTER TABLE tasks ADD COLUMN extra TEXT;")]
        self.assertEqual(migrate(self.conn, extra), MIGRATIONS[-1][0] + 1)
        self.assertEqual(migrate(self.conn, extra), MIGRATIONS[-1][0] + 1)
        self.assertEqual(self.conn.execute("SELECT title, extra FROM tasks WHERE id=?", (t["id"],)).fetchone()[0], "keep")

    def test_failed_migration_rolls_back(self):
        bad = MIGRATIONS + [(MIGRATIONS[-1][0] + 1, "CREATE TABLE half(x); SELECT * FROM no_such_table;")]
        with self.assertRaises(Exception):
            migrate(self.conn, bad)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(schema_version(self.conn), MIGRATIONS[-1][0])
        self.assertIsNone(self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='half'").fetchone())

    def test_pragmas_and_tables(self):
        self.assertEqual(self.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertGreaterEqual(self.conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
        names = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for n in ("epics", "tasks", "task_deps", "runs", "task_events", "comments", "questions", "decisions",
                  "policy_versions", "settings", "schema_version"):
            self.assertIn(n, names)

    def test_wal_required_and_mnt_refused(self):
        if os.name != "nt":
            with self.assertRaises(db.HubRequired):
                connect("/mnt/c/Users/x/ledger.db")
        fake = mock.MagicMock()
        fake.execute.return_value.fetchone.return_value = ["delete"]
        with mock.patch("sqlite3.connect", return_value=fake):
            with self.assertRaises(RuntimeError):
                connect(Path(self.tmp.name) / "w.db")

    def test_commit_failure_rolls_back(self):
        calls = []

        class C:
            in_transaction = False

            def execute(self, sql, *a):
                calls.append(sql)
                if sql == "BEGIN IMMEDIATE":
                    self.in_transaction = True
                if sql == "COMMIT":
                    raise RuntimeError("busy")
                if sql == "ROLLBACK":
                    self.in_transaction = False

        with self.assertRaises(RuntimeError):
            with db.tx(C()):
                pass
        self.assertEqual(calls[-1], "ROLLBACK")

    def test_foreign_keys_enforced(self):
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO task_deps(task_id,depends_on) VALUES('x','y')")


if __name__ == "__main__":
    unittest.main()
