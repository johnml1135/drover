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
from tests.test_ledger import Base


class ReorderTests(Base):
    def titles(self, epic):
        return [t["title"] for t in sorted(ops.list_tasks(self.conn, epic_id=epic), key=lambda t: t["rank"])]

    def setUp(self):
        super().setUp()
        self.e = ops.create_epic(self.conn, "p", "E")["id"]
        self.ts = [ops.create_task(self.conn, n, "p", self.e, scope_files=['fixture-24'])["id"] for n in "abcd"]

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
        other = ops.create_task(self.conn, "x", "p", e2, scope_files=['fixture-49'])["id"]
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
        from drover.ledger.waiting import Waiting, cause_key
        a = self.task("a")
        b = self.task("b", deps=[a["id"]])
        c = self.task("c", deps=[b["id"]], status="draft")
        free = self.task("free")
        r = ops.add_comment(self.conn, "task", a["id"], "owner", "stop", urgent=True)
        self.assertEqual(r["routed"], ["held"])
        self.assertEqual((self.status(a["id"]), self.status(b["id"]), self.status(c["id"]), self.status(free["id"])),
                         ("ready", "blocked", "blocked", "ready"))
        waiting = Waiting(self.conn, "owner")
        cause = cause_key("urgent", "task", a["id"])
        self.assertEqual({row["cause"] for row in waiting.pending(("task", b["id"]))}, {cause})
        ops.set_status(self.conn, a["id"], "cancelled", "owner")
        self.assertEqual((self.status(b["id"]), self.status(c["id"])), ("ready", "draft"))

    def test_urgent_on_epic_holds_its_pending_tasks(self):
        from drover.ledger.waiting import Waiting, cause_key
        e = ops.create_epic(self.conn, "p", "E")
        a = self.task("a", epic_id=e["id"])
        ops.add_comment(self.conn, "epic", e["id"], "owner", "pause", urgent=True)
        self.assertEqual(self.status(a["id"]), "blocked")
        self.assertIsNone(ops.claim_next(self.conn, self.worker()))
        waiting = Waiting(self.conn, "owner")
        qid = waiting.pending(("task", a["id"]))[0]["question_id"]
        ops.answer_question(self.conn, qid, choice="Resume work")
        self.assertEqual(self.status(a["id"]), "ready")

    def test_replan_flag_creates_request_event(self):
        t = self.task()
        r = ops.add_comment(self.conn, "task", t["id"], "owner", "wrong plan", replan=True)
        self.assertEqual(r["routed"], ["replan-request"])
        evs = ops.events(self.conn, task_id=t["id"], kind="replan-request")
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["data"]["comment"], r["id"])
        self.assertEqual(self.status(t["id"]), "blocked")
        from drover.ledger.waiting import Waiting
        request, = Waiting(self.conn, "owner").replans()
        self.assertEqual(request["task_id"], t["id"])

    def test_bad_target(self):
        with self.assertRaises(ops.NotFound):
            ops.add_comment(self.conn, "task", "T-zzz", "owner", "x")
        with self.assertRaises(ValueError):
            ops.add_comment(self.conn, "run", "1", "owner", "x")


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
        ops.unblock(self.conn, t["id"], "owner")
        ops.set_status(self.conn, t["id"], "draft", "owner")
        self.assertEqual(ops.approve(self.conn, t["id"])["status"], "ready")

    def test_claim_and_hold_release_never_make_unapproved_ready(self):
        t = self.task(status="proposed")
        ops.block(self.conn, t["id"], "Manual pause")
        ops.unblock(self.conn, t["id"])
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


    def test_replan_release_leaves_urgent_holds(self):
        from drover.ledger.waiting import Waiting, cause_key
        a = self.task("a")
        b = self.task("b", deps=[a["id"]])
        waiting = Waiting(self.conn, "owner")
        ops.add_comment(self.conn, "task", a["id"], "owner", "stop", urgent=True)
        ops.add_comment(self.conn, "task", a["id"], "owner", "revise", replan=True)
        request = waiting.replans()[0]
        ops.set_status(self.conn, a["id"], "ready", "owner")
        waiting.release(cause_key("replan", request["id"]))
        self.assertEqual(self.status(b["id"]), "blocked")
        rows = waiting.pending(("task", b["id"]))
        self.assertEqual([row["cause"] for row in rows], [cause_key("urgent", "task", a["id"])])
        ops.answer_question(self.conn, rows[0]["question_id"], choice="Resume work")
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
        ops.start_run(self.conn, a["id"], "worker", "local")
        done = self.task("done-one")
        ops.claim_next(self.conn, self.worker())
        from tests.ledger_fixture import stage
        stage(self.conn, done["id"], 'landed')
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
        self.assertIn("Waiting on you: why", self.run_cli("show", tid)[1])
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
