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

EVIL = "| `x` <script>alert(1)</script> [a](http://e)\n# Heading\n'); DROP TABLE tasks;--"

QUESTIONS = """# Open questions

## Q1. What about snapshots?

**Body.** 200 refs.

**Options.**
- A. Delete all.
- B. Bundle then delete.
- C. Review one at a time.

**Recommended.** B. It is reversible.

## Q2. Second?

**Body.** Body two.

**Options.**
- A. Yes.
- B. No.

**Recommended.** Your call; A if protected.

## Q3. Third?

**Body.** Body three.

**Options.**
- A.
- B. Real option.

Recommended: B
"""


def task(i, **kw):
    t = {"native_id": None, "title": f"task {i}", "repo": "acme-lib", "branch": None, "branches": [],
         "source_refs": [f"f.md:{i}"], "confidence": "high", "merged_from": 1, "sources": ["A"], "date": "2026-10-16",
         "priority": "P3", "goal": f"goal {i}", "acceptance": ["a1", "a2"], "size": "S", "id": f"IMP-{i:04d}",
         "epic_id": "EP-01"}
    t.update(kw)
    return t


def branch(name, disp, repo="acme-lib", conf="high", reason="r", task_id=None, **kw):
    return {"repo": repo, "branch": name, "disposition": disp, "reason": reason, "task_id": task_id, "conf": conf,
            "worktree": "", "on_disk": "no", "locked": "no", **kw}


def sample():
    return {
        "epics": [{"id": "EP-01", "title": "acme-lib: one", "repo": "acme-lib", "goal": "g1", "task_ids": []},
                  {"id": "EP-02", "title": "acme-app: two", "repo": "acme-app", "goal": "g2", "task_ids": []}],
        "tasks": [task(1, priority="P3"), task(2, priority="P0", branch="feat/x", confidence="low"),
                  task(3, epic_id="EP-02", repo="acme-app", priority="P1", scope_files=["a/b.py"], size="L"),
                  task(4, title=EVIL, goal=EVIL, acceptance=[EVIL], confidence="low")],
        "done": [{"native_id": "T1", "title": "done one", "repo": "acme-lib", "branch": "b", "branches": [],
                  "source_refs": ["x:1"], "confidence": "high", "evidence": "landed abc", "date": "2026-10-16"}],
        "superseded": [{"native_id": "T9", "title": EVIL, "repo": "acme-lib", "branch": None, "branches": [],
                        "source_refs": ["x:9"], "confidence": "high", "evidence": EVIL, "date": "2026-10-16"}],
        "branch_dispositions": [
            branch("a1", "script-delete", clean_verified=True), branch("a2", "script-delete", clean_verified=True),
            branch("a3", "script-delete", clean_verified=False),
            branch("p1", "propose-delete", conf="high"), branch("p2", "propose-delete", conf="low"),
            branch("p3", "propose-delete", conf="medium"),
            branch("archive/x", "owner-review", reason="archive snapshot with 2 commits"),
            branch("dirty", "owner-review", reason="worktree has 1 dirty line"),
            branch("main", "owner-review", reason="default branch: keep, not a deletion candidate"),
            branch("land1", "land-task", task_id="IMP-0002", repo="acme-lib"),
            branch(EVIL, "script-delete", repo="acme-app", clean_verified=True),
        ],
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.conn = connect(Path(self.tmp.name) / "l.db")
        self.addCleanup(self.conn.close)
        self.data = sample()

    def run_it(self, dry=False, data=None, q=QUESTIONS):
        return importer.run_import(self.conn, data or self.data, q, dry)

    def snapshot(self):
        tabs = ("epics", "tasks", "task_events", "questions", "settings", "task_deps")
        return {t: [tuple(r) for r in self.conn.execute(f"SELECT * FROM {t} ORDER BY 1")] for t in tabs}

    def batches(self):
        return json.loads(ops.get_setting(self.conn, "garden_batches"))


class TestImport(Base):
    def test_every_kind_lands_in_the_right_place(self):
        r = self.run_it()
        c = r["counts"]
        self.assertEqual(c["task"], {"created": 4})
        self.assertEqual(c["epic"], {"created": 2})
        self.assertEqual(c["done"], {"created": 1})
        self.assertEqual(c["superseded"], {"created": 1})
        self.assertEqual(c["question"]["created"], 3 + len(self.batches()))
        tasks = ops.list_tasks(self.conn)
        self.assertEqual(len(tasks), 4)  # done and superseded are NOT tasks
        self.assertEqual({t["status"] for t in tasks}, {"proposed"})
        self.assertEqual(len(ops.events(self.conn, kind="import-done")), 1)
        self.assertEqual(len(ops.events(self.conn, kind="import-superseded")), 1)

    def test_task_fields_and_confidence(self):
        self.run_it()
        by = {t["title"]: t for t in ops.list_tasks(self.conn)}
        t = by["task 2"]
        self.assertEqual((t["goal"], t["acceptance"], t["repo"], t["branch"], t["priority"], t["size"]),
                         ("goal 2", ["a1", "a2"], "acme-lib", "feat/x", "P0", "S"))
        self.assertEqual(by["task 3"]["scope_files"], ["a/b.py"])
        self.assertEqual(by["task 3"]["size"], "L")
        ev = ops.events(self.conn, task_id=t["id"], kind="import-task")[0]["data"]
        self.assertEqual(ev["source_refs"], ["f.md:2"])
        self.assertEqual(ev["confidence"], "low")
        self.assertEqual(ev["tags"], ["low-confidence"])
        hi = ops.events(self.conn, task_id=by["task 1"]["id"], kind="import-task")[0]["data"]
        self.assertEqual(hi["tags"], [])

    def test_epic_rank_follows_task_priority(self):
        self.run_it()
        order = [e["title"] for e in ops.list_epics(self.conn)]
        self.assertEqual(order, ["Import", "acme-lib: one", "acme-app: two"])  # EP-01 holds the P0 task
        d = sample()
        d["tasks"][1]["priority"] = "P4"  # now EP-02 (P1) outranks EP-01 (P3)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as t2:
            c2 = connect(Path(t2) / "l.db")
            importer.run_import(c2, d)
            self.assertEqual([e["title"] for e in ops.list_epics(c2)], ["Import", "acme-app: two", "acme-lib: one"])
            c2.close()
        by_epic = {}
        for t in ops.list_tasks(self.conn, epic_id=ops.list_epics(self.conn)[1]["id"]):
            by_epic[t["title"]] = t["rank"]
        self.assertLess(by_epic["task 2"], by_epic["task 1"])  # P0 before P3

    def test_idempotent_reimport(self):
        self.run_it()
        before = self.snapshot()
        r = self.run_it()
        self.assertEqual(self.snapshot(), before)
        for kind, c in r["counts"].items():
            self.assertEqual(set(c) - {"unchanged"}, set(), kind)

    def test_reimport_updates_instead_of_duplicating(self):
        self.run_it()
        tid = next(t["id"] for t in ops.list_tasks(self.conn) if t["title"] == "task 1")
        d = copy.deepcopy(self.data)
        d["tasks"][0]["goal"] = "new goal"
        d["tasks"][0]["priority"] = "P2"
        d["tasks"].append(task(5))
        r = self.run_it(data=d)
        self.assertEqual(r["counts"]["task"], {"updated": 1, "unchanged": 3, "created": 1})
        self.assertEqual(len(ops.list_tasks(self.conn)), 5)
        t = ops.get_task(self.conn, tid)
        self.assertEqual((t["goal"], t["priority"], t["status"]), ("new goal", "P2", "proposed"))

    def test_reimport_keeps_owner_decisions(self):
        self.run_it()
        tid = next(t["id"] for t in ops.list_tasks(self.conn) if t["title"] == "task 1")
        ops.approve(self.conn, tid, "owner")
        qid = ops.open_questions(self.conn)[0]["id"]
        ops.answer_question(self.conn, qid, choice="B. Bundle then delete.")
        gid = self.batches()[0]["id"]
        ops.set_setting(self.conn, "garden_batches", json.dumps(self.batches()[1:]))
        ops.event(self.conn, "garden.approve", "owner", {"id": gid})
        self.run_it()
        self.assertEqual(ops.get_task(self.conn, tid)["status"], "ready")
        self.assertEqual(ops.get_question(self.conn, qid)["status"], "answered")
        self.assertNotIn(gid, [b["id"] for b in self.batches()])

    def test_reimport_skips_owner_deleted_tasks_and_continues_other_imports(self):
        self.run_it()
        tid = next(t['id'] for t in ops.list_tasks(self.conn) if t['title'] == 'task 2')
        ops.delete_task(self.conn, tid, confirm=True)
        for reworded in (False, True, True):
            with self.subTest(reworded=reworded):
                data = copy.deepcopy(self.data)
                if reworded:
                    data['tasks'][1]['title'] = 'task 2a'
                    data['tasks'][1]['source_refs'] = ['f.md:200']
                data['tasks'][0]['goal'] = 'Updated acme-lib goal'
                data['tasks'].append(task(5))
                result = self.run_it(data=data)
                self.assertEqual(result['counts']['task']['skipped'], 1)
                tasks = ops.list_tasks(self.conn)
                self.assertEqual(len(tasks), 4)
                self.assertNotIn(tid, [t['id'] for t in tasks])
                self.assertFalse(any(t['title'].startswith('task 2') for t in tasks))
                self.assertEqual(next(t['goal'] for t in tasks if t['title'] == 'task 1'), 'Updated acme-lib goal')
                self.assertTrue(ops.events(self.conn, kind='import-task', task_id=tid))
                self.assertTrue(all(r['task'] != tid for b in self.batches() for r in b['entries']))

    def test_never_ready_assertion(self):
        self.run_it()
        self.assertEqual({t["status"] for t in ops.list_tasks(self.conn)}, {"proposed"})
        self.assertTrue(all(t["approved_at"] is None for t in ops.list_tasks(self.conn)))
        real = ops.create_task

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as t2:
            c2 = connect(Path(t2) / "l.db")

            def sneaky(*a, **k):
                made = real(*a, **k)
                c2.execute("UPDATE tasks SET status='ready', approved_at='x' WHERE id=?", (made["id"],))
                return ops.get_task(c2, made["id"])

            with mock.patch.object(ops, "create_task", sneaky):
                with self.assertRaises(importer.ImportFailed):
                    importer.run_import(c2, self.data)
            self.assertEqual(c2.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)  # rolled back whole
            c2.close()

    def test_ledger_itself_refuses_ready_without_owner(self):
        self.run_it()
        tid = ops.list_tasks(self.conn)[0]["id"]
        with self.assertRaises(PermissionError):
            ops.set_status(self.conn, tid, "ready", actor=importer.ACTOR)

    def test_dry_run_changes_nothing(self):
        before = self.snapshot()
        r = self.run_it(dry=True)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(r["dry_run"])
        self.assertEqual(r["counts"]["task"], {"created": 4})
        self.assertEqual(r["ledger"]["tasks"], {"proposed": 4})
        self.run_it()
        after = self.snapshot()
        self.run_it(dry=True)
        self.assertEqual(self.snapshot(), after)
        self.assertFalse(self.conn.in_transaction)

    def test_questions_parsed_with_options_and_recommended(self):
        self.run_it()
        qs = {q["title"]: q for q in ops.open_questions(self.conn) if q["target_kind"] == "epic"}
        q = qs["What about snapshots?"]
        epic = ops.get_epic(self.conn, q["target_id"])
        self.assertEqual(epic["title"], "Import")
        self.assertEqual([o["label"] for o in q["options"]], ["A. Delete all.", "B. Bundle then delete.", "C. Review one at a time."])
        self.assertEqual([o["recommended"] for o in q["options"]], [False, True, False])
        self.assertEqual([o["recommended"] for o in qs["Second?"]["options"]], [False, False])  # only an explicit first token counts
        self.assertEqual([o["label"] for o in qs["Third?"]["options"]], ["B. Real option."])  # empty bullet ignored
        self.assertEqual([o["recommended"] for o in qs["Third?"]["options"]], [True])

    def test_batches(self):
        self.run_it()
        b = {x["title"]: x for x in self.batches()}
        self.assertIn("acme-lib: script-delete (2 branches)", b)  # a3 unverified moved out
        self.assertEqual(b["acme-lib: script-delete (2 branches)"]["entries"][0]["branch"], "a1")
        self.assertIn("acme-lib: propose-delete (high confidence) (1 branches)", b)
        self.assertIn("acme-lib: propose-delete (low confidence) (1 branches)", b)
        snap = b["acme-lib: owner-review: archive/backup/snapshot refs (1 branches)"]
        self.assertEqual(snap["recommendation"], "bundle then delete")
        rev = b["acme-lib: owner-review (2 branches)"]  # dirty + unverified a3; main excluded
        self.assertEqual(sorted(e["branch"] for e in rev["entries"]), ["a3", "dirty"])
        self.assertTrue(any("not verified" in e["reason"] for e in rev["entries"]))
        self.assertFalse(any(e["branch"] == "main" for x in self.batches() for e in x["entries"]))
        land = b["acme-lib: land-task (1 branches)"]
        lt = next(t for t in ops.list_tasks(self.conn) if t["title"] == "task 2")
        self.assertEqual(land["entries"][0]["task"], lt["id"])
        self.assertTrue(all(x["executes"] is False for x in self.batches()))
        # one 'Approve batch?' question per batch with the three options
        qs = [q for q in ops.open_questions(self.conn) if q["title"].startswith("Approve batch?")]
        self.assertEqual(len(qs), len(self.batches()))
        self.assertEqual([o["label"] for o in qs[0]["options"]], ["Approve", "Reject", "Review individually"])

    def test_hostile_strings_verbatim_then_escaped_by_board(self):
        self.run_it()
        t = next(t for t in ops.list_tasks(self.conn) if t["goal"] == EVIL)
        self.assertEqual((t["title"], t["goal"], t["acceptance"]), (EVIL, EVIL, [EVIL]))
        ev = next(e for e in ops.events(self.conn, kind="import-superseded"))
        self.assertEqual((ev["data"]["title"], ev["data"]["evidence"]), (EVIL, EVIL))
        self.assertIn(EVIL, [i["branch"] for b in self.batches() for i in b["entries"]])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 4)  # no injection
        ops.approve(self.conn, t["id"], "owner")
        ops.set_status(self.conn, t["id"], "needs-owner", "system", reason="x")  # the board lists titles here
        text = board.render(self.conn)
        self.assertNotIn("<script>", text)
        self.assertNotIn("\n# Heading", text)
        self.assertIn("&lt;script>", text)

    def test_hub_templates_escape_garden_titles(self):
        try:
            from jinja2 import Environment
        except ImportError:
            self.skipTest("jinja2 not installed")
        self.run_it()
        evil = next(b for b in self.batches() if b["repo"] == "acme-app")
        out = Environment(autoescape=True).from_string("<h3>{{ b.title }}</h3>{% for x in b['items'] %}<li>{{ x }}</li>{% endfor %}").render(b=evil)
        self.assertNotIn("<script>", out)

    def test_no_deletion_side_effects(self):
        src = ast.parse(Path(importer.__file__).read_text())
        mods = {n.names[0].name.split(".")[0] for n in ast.walk(src) if isinstance(n, ast.Import)}
        mods |= {n.module.split(".")[0] for n in ast.walk(src) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertFalse(mods & {"subprocess", "shutil", "os"}, mods)
        boom = mock.Mock(side_effect=AssertionError("importer touched the filesystem or a process"))
        victim = Path(self.tmp.name) / "keep.txt"
        victim.write_text("x")
        with mock.patch.object(subprocess, "run", boom), mock.patch.object(subprocess, "Popen", boom), \
                mock.patch.object(shutil, "rmtree", boom), mock.patch.object(os, "remove", boom), \
                mock.patch.object(os, "unlink", boom), mock.patch.object(Path, "unlink", boom):
            self.run_it()
        self.assertTrue(victim.exists())
        self.assertTrue(all(b["executes"] is False for b in self.batches()))

    def test_bad_input_is_tolerated_or_rejected_cleanly(self):
        d = {"tasks": [{"title": "", "id": "x"}, {"title": "ok", "priority": "ZZ", "size": "XL", "acceptance": "one"},
                       "junk"], "epics": None}
        r = self.run_it(data=d, q=None)
        self.assertEqual(r["counts"]["task"], {"created": 1, "invalid": 1})
        t = ops.list_tasks(self.conn)[0]
        self.assertEqual((t["priority"], t["size"], t["acceptance"]), ("P3", "M", ["one"]))
        with self.assertRaises(importer.ImportFailed):
            importer.run_import(self.conn, [])










if __name__ == "__main__":
    unittest.main()
