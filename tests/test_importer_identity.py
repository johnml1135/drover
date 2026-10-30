"""Importer tests: synthetic import.json covering every kind; idempotence, backlog gate, dry run, hostile strings."""
import ast
import copy
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from drover import importer
from drover.ledger import board, connect, ops

from tests.test_importer import Base, EVIL, QUESTIONS, task, branch, sample


class TestOwnerWins(Base):
    def tid(self, title):
        return next(t["id"] for t in ops.list_tasks(self.conn) if t["title"] == title)

    def test_owner_edited_title_and_priority_survive(self):
        self.run_it()
        tid = self.tid("task 1")
        ops.update_task(self.conn, tid, "owner", title="My title", priority="P0")
        d = copy.deepcopy(self.data)
        d["tasks"][0]["goal"] = "harvest goal"
        r = self.run_it(data=d)
        t = ops.get_task(self.conn, tid)
        self.assertEqual((t["title"], t["priority"], t["goal"]), ("My title", "P0", "goal 1"))
        self.assertEqual(r["counts"]["task"].get("conflict"), 1)
        c = ops.events(self.conn, task_id=tid, kind="import-conflict")
        self.assertEqual(c[0]["data"]["harvest"]["goal"], "harvest goal")
        self.assertEqual(len(ops.events(self.conn, task_id=tid, kind="import-conflict")), 1)
        self.run_it(data=d)  # a second identical run records no second conflict
        self.assertEqual(len(ops.events(self.conn, task_id=tid, kind="import-conflict")), 1)

    def test_owner_comment_or_reorder_also_locks(self):
        self.run_it()
        a, b = self.tid("task 1"), self.tid("task 2")
        ops.add_comment(self.conn, "task", a, "owner", "hold on")
        d = copy.deepcopy(self.data)
        d["tasks"][0]["goal"] = "x"
        self.assertEqual(self.run_it(data=d)["counts"]["task"]["conflict"], 1)
        self.assertEqual(ops.get_task(self.conn, a)["goal"], "goal 1")
        self.assertEqual(ops.get_task(self.conn, b)["status"], "proposed")

    def test_ready_and_running_tasks_untouched(self):
        self.run_it()
        r1, r2 = self.tid("task 1"), self.tid("task 3")
        ops.approve(self.conn, r1, "owner")
        ops.approve(self.conn, r2, "owner")
        from tests.ledger_fixture import stage
        stage(self.conn, r2, 'running')
        d = copy.deepcopy(self.data)
        for t in d["tasks"]:
            t["goal"] = "changed " + t["id"]
        self.run_it(data=d)
        self.assertEqual(ops.get_task(self.conn, r1)["goal"], "goal 1")
        self.assertEqual((ops.get_task(self.conn, r2)["goal"], ops.get_task(self.conn, r2)["status"]), ("goal 3", "running"))

    def test_epic_owner_edit_survives(self):
        self.run_it()
        eid = next(e["id"] for e in ops.list_epics(self.conn) if e["title"] == "acme-lib: one")
        ops.update_epic(self.conn, eid, "owner", plan_md="my plan")
        d = copy.deepcopy(self.data)
        d["epics"][0]["goal"] = "harvest plan"
        r = self.run_it(data=d)
        self.assertEqual(ops.get_epic(self.conn, eid)["plan_md"], "my plan")
        self.assertEqual(r["counts"]["epic"].get("conflict"), 1)
        self.assertEqual(ops.events(self.conn, epic_id=eid, kind="import-conflict")[0]["data"]["harvest"],
                         {"plan_md": "harvest plan"})

    def test_untouched_proposed_task_still_updates(self):
        self.run_it()
        d = copy.deepcopy(self.data)
        d["tasks"][0]["goal"] = "better"
        self.assertEqual(self.run_it(data=d)["counts"]["task"].get("updated"), 1)



class TestStableKeys(Base):
    def test_reworded_title_same_task(self):
        self.run_it()
        d = copy.deepcopy(self.data)
        d["tasks"][0]["title"] = "Task 1!!"  # normalises to the same key
        r = self.run_it(data=d)
        self.assertEqual(len(ops.list_tasks(self.conn)), 4)
        self.assertNotIn("created", r["counts"]["task"])

    def test_similar_title_matches_by_similarity(self):
        d = copy.deepcopy(self.data)
        d["tasks"][0]["title"] = "Implement the frobnicator composer for Cebuano words"
        d["tasks"][0]["source_refs"] = ["notes/a.md:10"]
        self.run_it(data=d)
        d2 = copy.deepcopy(d)
        d2["tasks"][0]["title"] = "Implement the frobnicator composer for Cebuano word"
        d2["tasks"][0]["source_refs"] = ["notes/a.md:10"]
        r = self.run_it(data=d2)
        self.assertEqual(len(ops.list_tasks(self.conn)), 4)
        self.assertEqual(r["counts"]["task"].get("created"), None)
        self.run_it(data=d2)  # re-keyed: now an exact match
        self.assertEqual(self.run_it(data=d2)["counts"]["task"], {"unchanged": 4})

    def test_shifted_line_same_task(self):
        self.run_it()
        d = copy.deepcopy(self.data)
        for t in d["tasks"]:
            t["source_refs"] = [x.replace(":", ":9") for x in t["source_refs"]]  # f.md:1 -> f.md:91
        r = self.run_it(data=d)
        self.assertEqual(len(ops.list_tasks(self.conn)), 4)
        self.assertNotIn("created", r["counts"]["task"])

    def test_different_file_is_a_different_task(self):
        self.run_it()
        d = copy.deepcopy(self.data)
        d["tasks"][0]["source_refs"] = ["other.md:1"]
        self.run_it(data=d)
        self.assertEqual(len(ops.list_tasks(self.conn)), 5)

    def test_duplicate_keys_in_one_import_stay_separate(self):
        d = copy.deepcopy(self.data)
        d["tasks"].append(task(9, title="task 1", source_refs=["f.md:77"]))
        self.run_it(data=d)
        self.assertEqual(len(ops.list_tasks(self.conn)), 5)
        self.assertEqual(self.run_it(data=d)["counts"]["task"], {"unchanged": 5})



class TestBatchIdentity(Base):
    def test_approval_captures_rows_and_new_rows_make_a_new_batch(self):
        self.run_it()
        g = next(b for b in self.batches() if b["title"].startswith("acme-lib: script-delete"))
        self.assertEqual(importer.record_approval(self.conn, g["id"]), g["entries"])
        self.assertEqual([r["branch"] for r in importer.approved_rows(self.conn, g["id"])], ["a1", "a2"])
        ev = ops.events(self.conn, kind="garden.approve")[0]["data"]
        self.assertEqual(ev["rows"], g["entries"])
        ops.set_setting(self.conn, "garden_batches", json.dumps([b for b in self.batches() if b["id"] != g["id"]]))
        d = copy.deepcopy(self.data)
        d["branch_dispositions"].append(branch("a9", "script-delete", clean_verified=True))
        self.run_it(data=d)
        new = [b for b in self.batches() if b["title"].startswith("acme-lib: script-delete")]
        self.assertEqual(len(new), 1)
        self.assertNotEqual(new[0]["id"], g["id"])
        self.assertEqual([r["branch"] for r in new[0]["entries"]], ["a9"])  # approved rows are not re-proposed
        self.assertEqual(importer.approved_rows(self.conn, new[0]["id"]), [])
        qs = [q["title"] for q in ops.open_questions(self.conn) if "script-delete" in q["title"] and "acme-lib" in q["title"]]
        self.assertIn(f"Approve batch? {new[0]['title']}", qs)
        self.run_it(data=d)  # stable
        self.assertEqual(len([b for b in self.batches() if b["title"].startswith("acme-lib: script-delete")]), 1)

    def test_undecided_batch_with_changed_rows_is_replaced(self):
        self.run_it()
        old = next(b for b in self.batches() if b["title"].startswith("acme-lib: script-delete"))
        d = copy.deepcopy(self.data)
        d["branch_dispositions"].append(branch("a9", "script-delete", clean_verified=True))
        r = self.run_it(data=d)
        sd = [b for b in self.batches() if b["title"].startswith("acme-lib: script-delete")]
        self.assertEqual(len(sd), 1)
        self.assertNotEqual(sd[0]["id"], old["id"])
        self.assertEqual(len(sd[0]["entries"]), 3)
        self.assertEqual(r["counts"]["garden_batch"]["superseded"], 1)
        self.assertFalse(any(old["id"] in q["body"] for q in ops.open_questions(self.conn)))



class TestCli(Base):
    def test_cli_dry_run_then_real_run(self):
        d = Path(self.tmp.name)
        (d / "i.json").write_text(json.dumps(self.data))
        (d / "q.md").write_text(QUESTIONS)
        db = str(d / "cli.db")
        out = []
        with mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a)))):
            self.assertEqual(importer.main([str(d / "i.json"), "--questions", str(d / "q.md"), "--db", db, "--dry-run"]), 0)
            self.assertIn("DRY RUN", out[-1])
            self.assertIn("proposed=4", out[-1])
            c = connect(db)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
            c.close()
            self.assertEqual(importer.main([str(d / "i.json"), "--questions", str(d / "q.md"), "--db", db]), 0)
            self.assertIn("IMPORTED", out[-1])
            self.assertEqual(importer.main([str(d / "missing.json"), "--db", db]), 2)
        c = connect(db)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 4)
        c.close()
