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
        from drover.ledger.waiting import Waiting
        rows = Waiting(self.conn).pending(("task", t["id"]))
        self.assertEqual(rows[0]["data"]["note"], "waiting on owner")
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

    def test_ci_migration_is_idempotent_with_existing_rows(self):
        from drover.ledger import ci as slots, landing as storage
        conn = connect(Path(self.tmp.name) / 'pre-ci.db', migrate_db=False)
        self.conns.append(conn)
        migrate(conn, MIGRATIONS[:5])
        self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='gate_tickets'").fetchone())
        migrate(conn)
        slots.lease_request(conn, 'task', 'P1', holder='runner', at=100)
        storage.record_base(conn, 'p', 'a' * 40, 'green')
        storage.record_merged(conn, {'id': 'task', 'last_sha': 'abc'}, 'tip', 'result')
        before = {name: [tuple(row) for row in conn.execute(f'PRAGMA table_info({name})')]
                  for name in ('gate_tickets', 'landing_lock', 'landing_batches', 'base_gate', 'task_landed')}
        migrate(conn)
        for name, columns in before.items():
            self.assertEqual([tuple(row) for row in conn.execute(f'PRAGMA table_info({name})')], columns)
        self.assertEqual(slots.lease_queue(conn)[0]['holder'], 'runner')
        self.assertEqual(storage.base_status(conn, 'p', 'a' * 40), 'green')
        self.assertEqual(storage.land_sha(conn, 'task'), 'abc')
        self.assertEqual(migrate(conn), MIGRATIONS[-1][0])

    def test_forward_only_extra_migration_applied_once_and_data_kept(self):
        t = self.task("keep")
        extra = MIGRATIONS + [(MIGRATIONS[-1][0] + 1, "ALTER TABLE tasks ADD COLUMN extra TEXT;")]
        self.assertEqual(migrate(self.conn, extra), MIGRATIONS[-1][0] + 1)
        self.assertEqual(migrate(self.conn, extra), MIGRATIONS[-1][0] + 1)
        self.assertEqual(self.conn.execute("SELECT title, extra FROM tasks WHERE id=?", (t["id"],)).fetchone()[0], "keep")

    def test_failed_migration_rolls_back(self):
        bad = MIGRATIONS + [(MIGRATIONS[-1][0] + 1, "ALTER TABLE tasks ADD COLUMN rollback_probe TEXT; SELECT * FROM no_such_table;")]
        with self.assertRaises(Exception):
            migrate(self.conn, bad)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(schema_version(self.conn), MIGRATIONS[-1][0])
        self.assertNotIn("rollback_probe", {row[1] for row in self.conn.execute("PRAGMA table_info(tasks)")})

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
