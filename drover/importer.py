"""Import the backlog harvest into the ledger: proposed tasks, history events, questions, garden batches.

Pure ledger operations in one transaction (a dry run rolls it back). Idempotent: every record is keyed on a stable
source hash kept in an `import-*` task_event, so a re-run updates instead of duplicating. Nothing is ever approved,
made ready or deleted here: tasks stay `proposed`, branch dispositions stay proposals in the `garden_batches`
setting until the owner approves them and the garden step runs. All input strings are untrusted and stored verbatim.
"""
import argparse
import difflib
import hashlib
import json
import re
import sys
from pathlib import Path

from .ledger import connect, default_path, ops
from .ledger.db import HubRequired

from .importer_common import (ACTOR, PRIORITIES, SIZES, IMPORT_EPIC, APPROVE_OPTIONS, ImportFailed, Tally,
                              sha, _s, _list, _dicts, _index, _record)
from .importer_tasks import (_prio, _epic_order, owner_touched, _conflict, _ensure_epic, import_epics, _norm,
                             _src_file, task_keys, _task_event, _task_fields, assert_proposed, SIMILAR, _match,
                             import_tasks)
from .importer_questions import parse_questions, _upsert_question, import_questions
from .importer_batches import (_is_snapshot, classify, _rows, batch_id, _item_line, build_batches, _approvals,
                               failed_rows, decided_rows, approved_rows, record_approval, repropose, import_batches,
                               _recommend)

HISTORY = {"done": "import-done", "superseded": "import-superseded"}


# ---- done and superseded: history events, never tasks --------------------------------------------------

def import_history(conn, data, import_epic, tally):
    for field, kind in HISTORY.items():
        idx = _index(conn, kind)
        for t in _dicts(data.get(field)):
            title = _s(t.get("title")).strip()
            if not title:
                tally.add(field, "invalid")
                continue
            repo = _s(t.get("repo"), "unknown")
            key = sha(field, repo, title, sorted(_list(t.get("source_refs"))))
            payload = {"title": title, "repo": repo, "native_id": t.get("native_id"), "branch": t.get("branch"),
                       "evidence": _s(t.get("evidence")), "source_refs": _list(t.get("source_refs")),
                       "confidence": _s(t.get("confidence")), "date": t.get("date")}
            tally.add(field, _record(conn, idx, kind, key, payload, epic_id=import_epic))


# ---- entry point ---------------------------------------------------------------------------------------

def status_table(conn):
    return {"tasks": {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status")},
            "epics": conn.execute("SELECT COUNT(*) FROM epics").fetchone()[0],
            "open_questions": conn.execute("SELECT COUNT(*) FROM questions WHERE status='open'").fetchone()[0],
            "garden_batches": len(json.loads(ops.get_setting(conn, "garden_batches") or "[]"))}


def run_import(conn, data, questions_text=None, dry_run=False):
    """Apply the import in one transaction; with dry_run the transaction is rolled back. Returns a report dict."""
    if not isinstance(data, dict):
        raise ImportFailed("import.json must be an object")
    if conn.in_transaction:
        raise ImportFailed("run_import needs a connection with no open transaction")
    tally = Tally()
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = [e for e in ops.list_epics(conn) if e["title"] == IMPORT_EPIC and e["repo"] == "import"]
        import_epic = existing[0]["id"] if existing else ops.create_epic(conn, "import", IMPORT_EPIC, "", ACTOR)["id"]
        epic_ids = import_epics(conn, data, tally)
        task_ids = import_tasks(conn, data, epic_ids, tally)
        import_history(conn, data, import_epic, tally)
        if questions_text:
            import_questions(conn, questions_text, import_epic, tally)
        import_batches(conn, data, task_ids, import_epic, tally)
        report = {"dry_run": dry_run, "counts": tally.counts, "warnings": tally.warnings, "ledger": status_table(conn)}
        if dry_run:
            conn.execute("ROLLBACK")
        else:
            changed = any(a != "unchanged" for c in tally.counts.values() for a in c)
            if changed:
                ops.event(conn, "import-run", ACTOR, {"counts": tally.counts}, epic_id=import_epic)
            conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return report


def format_report(r):
    kinds = ("created", "updated", "unchanged", "conflict", "superseded", "invalid")
    lines = [("DRY RUN (nothing written)" if r["dry_run"] else "IMPORTED"), "",
             f"{'kind':<16}" + "".join(f"{k:>11}" for k in kinds)]
    for kind, c in sorted(r["counts"].items()):
        lines.append(f"{kind:<16}" + "".join(f"{c.get(k, 0):>11}" for k in kinds))
    led = r["ledger"]
    lines += ["", "ledger after this import" + (" (rolled back)" if r["dry_run"] else "") + ":",
              "  tasks by status: " + (", ".join(f"{k}={v}" for k, v in sorted(led["tasks"].items())) or "none"),
              f"  epics={led['epics']} open_questions={led['open_questions']} garden_batches={led['garden_batches']}"]
    lines += [f"warning: {w}" for w in r["warnings"][:20]]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="drover.importer", description=__doc__.splitlines()[0])
    ap.add_argument("import_json")
    ap.add_argument("--questions", help="open-questions.md in the grilling format")
    ap.add_argument("--db", help="ledger path (default: the ledger default path)")
    ap.add_argument("--dry-run", action="store_true", help="count what would change, then roll back")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        data = json.loads(Path(a.import_json).read_text(encoding="utf-8"))
        text = Path(a.questions).read_text(encoding="utf-8") if a.questions else None
        conn = connect(a.db or default_path())
        try:
            report = run_import(conn, data, text, a.dry_run)
        finally:
            conn.close()
    except (OSError, ValueError, HubRequired) as e:
        print(f"drover.importer: {e}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=1) if a.json else format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
