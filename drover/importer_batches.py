"""Build and manage proposed branch batches."""
import json

from .ledger import ops
from .importer_common import ACTOR, APPROVE_OPTIONS, _dicts, _index, _s, sha
from .importer_questions import _upsert_question

def _is_snapshot(b):
    return "snapshot" in _s(b.get("reason")) or _s(b.get("branch")).split("/")[0] in ("archive", "backup", "checkpoint")


def classify(b):
    """(group, label, recommendation) for a branch row; None when the row is not a candidate (default branch)."""
    disp, reason = _s(b.get("disposition")), _s(b.get("reason"))
    if disp == "owner-review" and reason.startswith("default branch"):
        return None
    if disp == "script-delete" and b.get("clean_verified") is False:
        return ("owner-review", "owner-review", "review individually")
    if disp == "script-delete":
        return ("script-delete", "script-delete", "approve")
    if disp == "propose-delete":
        conf = _s(b.get("conf"), "low")
        return (f"propose-delete-{conf}", f"propose-delete ({conf} confidence)",
                "approve" if conf == "high" else "review individually")
    if disp == "owner-review" and _is_snapshot(b):
        return ("owner-review-snapshot", "owner-review: archive/backup/snapshot refs", "bundle then delete")
    if disp == "owner-review":
        return ("owner-review", "owner-review", "review individually")
    if disp == "land-task":
        return ("land-task", "land-task", "approve")
    return ("other", f"other ({disp})", "review individually")


def _rows(branches, task_ids):
    """{(repo, group): {"label", "rec", "rows": {branch: row}}} for every candidate branch row."""
    groups = {}
    for b in _dicts(branches):
        c = classify(b)
        if c is None or not _s(b.get("branch")):
            continue
        g = groups.setdefault((_s(b.get("repo"), "unknown"), c[0]), {"label": c[1], "rec": c[2], "rows": {}})
        row = {"branch": _s(b.get("branch")), "reason": _s(b.get("reason")), "disposition": _s(b.get("disposition")),
               "confidence": _s(b.get("conf")), "task": task_ids.get(_s(b.get("task_id")), None),
               "worktree": _s(b.get("worktree")), "clean_verified": b.get("clean_verified")}
        if c[0] == "owner-review" and b.get("clean_verified") is False:
            row["reason"] = "clean state not verified; " + row["reason"]
        g["rows"].setdefault(row["branch"], row)
    return groups


def batch_id(repo, group, rows):
    """Content identity: the same group with different rows is a different batch."""
    return "G-" + sha("batch", repo, group, sorted((r["branch"], r["disposition"], r["task"]) for r in rows))[:10]


def _item_line(r):
    return f"{r['branch']} ({r['reason']})" if not r["task"] else f"{r['branch']} -> {r['task']}"


def build_batches(branches, task_ids, skip=None):
    """Proposal batches per repo and group. `skip` maps (repo, group) to branch names already decided."""
    out = []
    for (repo, group), g in sorted(_rows(branches, task_ids).items()):
        done = (skip or {}).get((repo, group), set())
        rows = sorted((r for r in g["rows"].values() if r["branch"] not in done), key=lambda r: r["branch"])
        if not rows:
            continue
        out.append({"id": batch_id(repo, group, rows), "title": f"{repo}: {g['label']} ({len(rows)} branches)",
                    "repo": repo, "group": group, "recommendation": g["rec"], "count": len(rows),
                    "items": [_item_line(r) for r in rows],
                    "entries": rows, "executes": False, "source": "import"})
    return out


def _approvals(conn):
    """garden.approve events written by the owner. The token API cannot write them, and anything else is ignored."""
    return [e for e in ops.events(conn, kind="garden.approve") if e["actor"] == "owner"]


def failed_rows(conn, batch):
    """Branch names the garden step could not remove (error or kept-repropose) since the batch was last approved."""
    last = max((e["id"] for e in _approvals(conn) if e["data"].get("id") == batch), default=0)
    return {e["data"].get("branch") for k in ("garden.row.error", "garden.row.kept-repropose")
            for e in ops.events(conn, kind=k) if e["data"].get("batch") == batch and e["id"] > last}


def decided_rows(conn):
    """{(repo, group): branch names} already decided: every row of a rejected batch, and the rows of an approved
    batch that were approved and not left failed by the garden step. Unticked and failed rows stay open."""
    rejected = {e["data"]["id"] for e in ops.events(conn, kind="garden.reject") if "id" in e["data"]}
    approved = {e["data"]["id"] for e in _approvals(conn) if "id" in e["data"]}
    out = {}
    for e in ops.events(conn, kind="import-batch"):
        d = e["data"]
        if d.get("id") in rejected:
            names = {r["branch"] for r in d["entries"]}
        elif d.get("id") in approved:
            names = {r["branch"] for r in approved_rows(conn, d["id"])} - failed_rows(conn, d["id"])
        else:
            continue
        out.setdefault((d["repo"], d["group"]), set()).update(names)
    return out


def approved_rows(conn, batch):
    """The exact rows the owner approved for a batch id (empty when it was not approved). The garden step runs
    only these. A subset approval (record_approval with branches) is honoured: the latest approval's rows win."""
    approvals = [e["data"] for e in _approvals(conn) if e["data"].get("id") == batch]
    if not approvals:
        return []
    if "rows" in approvals[-1]:
        return approvals[-1]["rows"]
    rows = [e["data"]["entries"] for e in ops.events(conn, kind="import-batch") if e["data"].get("id") == batch]
    return rows[-1] if rows else []


def record_approval(conn, batch, actor="owner", branches=None):
    """Write the garden.approve event for a proposed batch, capturing the exact rows approved. `branches` approves
    only those rows (a per-row review); names that are not in the batch are refused."""
    rows = next((e["data"]["entries"] for e in reversed(ops.events(conn, kind="import-batch"))
                 if e["data"].get("id") == batch), None)
    if rows is None:
        raise ops.NotFound(f"batch {batch}")
    if branches is not None:
        want = set(branches)
        unknown = want - {r["branch"] for r in rows}
        if unknown:
            raise ValueError(f"not in batch {batch}: {sorted(unknown)}")
        rows = [r for r in rows if r["branch"] in want]
    ops.event(conn, "garden.approve", actor, {"id": batch, "rows": rows})
    return rows


def repropose(conn, batch, branches):
    """Open a remainder batch holding `branches` of an earlier batch (rows the owner left unticked, or that the
    garden step could not remove), with its own proposal and owner question. Returns the new batch dict, or None."""
    src = next((e for e in reversed(ops.events(conn, kind="import-batch")) if e["data"].get("id") == batch), None)
    if src is None:
        raise ops.NotFound(f"batch {batch}")
    want = set(branches)
    rows = sorted((r for r in src["data"]["entries"] if r["branch"] in want), key=lambda r: r["branch"])
    if not rows:
        return None
    repo, group = src["data"]["repo"], src["data"]["group"]
    bid = batch_id(repo, group, rows)
    cur = [b for b in json.loads(ops.get_setting(conn, "garden_batches") or "[]") if isinstance(b, dict)]
    if any(b.get("id") == bid for b in cur):
        return next(b for b in cur if b.get("id") == bid)
    new = {"id": bid, "title": f"{repo}: {group} ({len(rows)} branches, left over)", "repo": repo, "group": group,
           "recommendation": "review individually", "count": len(rows), "items": [_item_line(r) for r in rows],
           "entries": rows, "executes": False, "source": "import"}
    ops.event(conn, "import-batch", ACTOR, {"id": bid, "repo": repo, "group": group, "entries": rows},
              epic_id=src["epic_id"])
    ops.set_setting(conn, "garden_batches", json.dumps(cur + [new]))
    body = (f"{new['title']}. These rows were left out of, or could not be removed by, batch {batch}. Nothing is deleted "
            f"until you approve. Batch id {bid}.\n\n" + "\n".join(new["items"][:15]))
    options = [{"label": lab, "description": desc, "recommended": lab == "Review individually"}
               for lab, desc in APPROVE_OPTIONS]
    qid = ops.ask_question(conn, "epic", src["epic_id"], f"Approve batch? {new['title']}", body, options, True, ACTOR)
    ops.event(conn, "import-question", ACTOR, {"source_hash": sha("batch-question", bid), "question": qid,
                                               "title": f"Approve batch? {new['title']}"}, epic_id=src["epic_id"])
    return new


def import_batches(conn, data, task_ids, import_epic, tally):
    batches = build_batches(data.get("branch_dispositions"), task_ids, decided_rows(conn))
    cur = [b for b in json.loads(ops.get_setting(conn, "garden_batches") or "[]") if isinstance(b, dict)]
    mine = {(b["repo"], b["group"]): b["id"] for b in batches}
    new_ids = {b["id"] for b in batches}
    old = {b.get("id"): b for b in cur}
    qidx = _index(conn, "import-question")
    keep = []
    for b in cur:  # drop undecided proposals this import replaces (same group, different rows) or no longer makes
        ours = b.get("source") == "import"
        if ours and b["id"] not in new_ids:
            q = qidx.get(sha("batch-question", b["id"]))
            if q and ops.get_question(conn, q["data"]["question"])["status"] == "open":
                ops.answer_question(conn, q["data"]["question"], text="Superseded by a re-import with different rows.",
                                    actor=ACTOR)
            tally.add("garden_batch", "superseded")
        elif not ours:
            keep.append(b)
    for b in batches:
        tally.add("garden_batch", "unchanged" if old.get(b["id"]) == b else "created")
        if b["id"] not in old:
            ops.event(conn, "import-batch", ACTOR, {"id": b["id"], "repo": b["repo"], "group": b["group"],
                                                    "entries": b["entries"]}, epic_id=import_epic)
    ops.set_setting(conn, "garden_batches", json.dumps(keep + batches))
    idx = qidx
    for b in batches:
        options = [{"label": lab, "description": desc, "recommended": _recommend(b["recommendation"], lab)}
                   for lab, desc in APPROVE_OPTIONS]
        body = (f"{b['title']}. Recommendation: {b['recommendation']}. Importing deleted nothing; this stays a "
                f"proposal until the garden step runs. Batch id {b['id']}.\n\n" + "\n".join(b["items"][:15]) +
                (f"\n... and {b['count'] - 15} more" if b["count"] > 15 else ""))
        _upsert_question(conn, idx, tally, sha("batch-question", b["id"]), import_epic, f"Approve batch? {b['title']}",
                         body, options, allow_text=True)


def _recommend(rec, label):
    if rec == "review individually":
        return label == "Review individually"
    return label == "Approve"  # approve, and 'bundle then delete' (the bundling step is named in the body)
