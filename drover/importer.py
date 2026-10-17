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

ACTOR = "importer"
PRIORITIES = ("P0", "P1", "P2", "P3", "P4")
SIZES = ("S", "M", "L")
IMPORT_EPIC = "Import"
HISTORY = {"done": "import-done", "superseded": "import-superseded"}
APPROVE_OPTIONS = (("Approve", "Approve this batch as a deletion/landing proposal; the garden step still runs it later."),
                   ("Reject", "Keep every branch in this batch."),
                   ("Review individually", "Break the batch up and decide branch by branch."))


class ImportFailed(ValueError):
    pass


def sha(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:24]


def _s(v, default=""):
    return default if v is None else str(v)


def _list(v):
    return [str(x) for x in v] if isinstance(v, (list, tuple)) else ([] if v in (None, "") else [str(v)])


def _dicts(v):
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


# ---- keyed records -------------------------------------------------------------------------------------

def _index(conn, kind):
    """{source_hash: latest event} for one import event kind."""
    return {e["data"]["source_hash"]: e for e in ops.events(conn, kind=kind) if "source_hash" in e["data"]}


def _record(conn, idx, kind, key, payload, task_id=None, epic_id=None):
    """Append an import event unless the latest one for `key` already holds this payload."""
    old = idx.get(key)
    if old and {k: v for k, v in old["data"].items() if k != "source_hash"} == payload:
        return "unchanged"
    ops.event(conn, kind, ACTOR, {"source_hash": key, **payload}, task_id=task_id, epic_id=epic_id)
    return "updated" if old else "created"


class Tally:
    def __init__(self):
        self.counts, self.warnings = {}, []

    def add(self, kind, action):
        self.counts.setdefault(kind, {})
        self.counts[kind][action] = self.counts[kind].get(action, 0) + 1

    def warn(self, msg):
        self.warnings.append(msg)


# ---- epics and tasks -----------------------------------------------------------------------------------

def _prio(task):
    p = _s(task.get("priority"))
    return p if p in PRIORITIES else "P3"


def _epic_order(data):
    """Epic ids by their best task priority, then by where that task sits in the input."""
    best = {}
    for i, t in enumerate(_dicts(data.get("tasks"))):
        key = (PRIORITIES.index(_prio(t)), i)
        e = _s(t.get("epic_id"))
        best[e] = min(best.get(e, key), key)
    return best


def owner_touched(conn, task_id=None, epic_id=None):
    """True when an owner-actor event (edit, approve, comment, reorder, answer) names this task or epic."""
    if task_id is not None:
        q, a = "SELECT 1 FROM task_events WHERE actor='owner' AND task_id=? LIMIT 1", (task_id,)
    else:
        q, a = "SELECT 1 FROM task_events WHERE actor='owner' AND epic_id=? AND task_id IS NULL LIMIT 1", (epic_id,)
    return conn.execute(q, a).fetchone() is not None


def _conflict(conn, cidx, key, diff, why, task_id=None, epic_id=None):
    """Record the harvest's new values without applying them (owner edits win)."""
    payload = {"why": why, "harvest": diff}
    return _record(conn, cidx, "import-conflict", key, payload, task_id=task_id, epic_id=epic_id)


def _ensure_epic(conn, tally, idx, cidx, repo, title, plan_md, key):
    old = idx.get(key)
    if old:
        eid = old["epic_id"]
        cur = ops.get_epic(conn, eid)
        want = {"repo": repo, "title": title, "plan_md": plan_md}
        diff = {k: v for k, v in want.items() if cur[k] != v}
        if diff and owner_touched(conn, epic_id=eid):
            _conflict(conn, cidx, key, diff, "owner touched this epic", epic_id=eid)
            tally.add("epic", "conflict")
        elif diff:
            ops.update_epic(conn, eid, ACTOR, **diff)
            tally.add("epic", "updated")
        else:
            tally.add("epic", "unchanged")
        return eid
    eid = ops.create_epic(conn, repo, title, plan_md, ACTOR)["id"]  # created in priority order, so rank follows it
    ops.event(conn, "import-epic", ACTOR, {"source_hash": key, "title": title}, epic_id=eid)
    tally.add("epic", "created")
    return eid


def import_epics(conn, data, tally):
    idx, cidx = _index(conn, "import-epic"), _index(conn, "import-conflict")
    order = _epic_order(data)
    ids = {}
    ranked = sorted(_dicts(data.get("epics")), key=lambda e: order.get(_s(e.get("id")), (99, 0)))
    for e in ranked:
        title = _s(e.get("title")).strip()
        if not title:
            tally.warn(f"epic {_s(e.get('id'))!r} has no title; skipped")
            tally.add("epic", "invalid")
            continue
        repo = _s(e.get("repo"), "unknown") or "unknown"
        key = sha("epic", repo, title)
        ids[_s(e.get("id"))] = _ensure_epic(conn, tally, idx, cidx, repo, title, _s(e.get("goal")), key)
    return ids


def _norm(title):
    return " ".join(re.sub(r"[^\w\s]", " ", _s(title).lower()).split())


def _src_file(t):
    refs = _list(t.get("source_refs"))
    return re.sub(r":\d+(-\d+)?$", "", refs[0]) if refs else ""


def task_keys(tasks):
    """Stable key per task: repo + (branch, else source file without line numbers) + normalised title.

    Identical keys within one import are told apart by their order of appearance."""
    seen, out = {}, []
    for t in tasks:
        base = ("task", _s(t.get("repo"), "unknown") or "unknown", _s(t.get("branch")) or _src_file(t),
                _norm(t.get("title")))
        n = seen[base] = seen.get(base, 0) + 1
        out.append(sha(*base, n) if n > 1 else sha(*base))
    return out


def _task_event(t):
    return {"native_id": t.get("native_id"), "import_id": _s(t.get("id")), "source_refs": _list(t.get("source_refs")),
            "source_file": _src_file(t), "norm_title": _norm(t.get("title")), "repo": _s(t.get("repo")),
            "confidence": _s(t.get("confidence")), "sources": _list(t.get("sources")),
            "branches": _list(t.get("branches")), "unverified_branches": _list(t.get("unverified_branches")),
            "merged_from": t.get("merged_from"), "date": t.get("date"), "rank_status": t.get("rank_status") or {},
            "tags": ["low-confidence"] if _s(t.get("confidence")) == "low" else []}


def _task_fields(t, epic_id):
    from . import task_context
    if "kind" in t:
        task_context.check_kind(t["kind"])
    if "write" in t and not isinstance(t["write"], bool):
        raise ValueError("write must be a boolean")
    if "read_access" in t:
        t = {**t, "read_access": task_context.read_access(t["read_access"], t.get("repo"))}
    return {"title": _s(t.get("title")), "goal": _s(t.get("description", t.get("goal"))), "acceptance": _list(t.get("acceptance")),
            "scope_files": _list(t.get("scope_files")), "priority": _prio(t),
            "size": _s(t.get("size")) if _s(t.get("size")) in SIZES else "M",
            "repo": _s(t.get("repo")) or None, "branch": _s(t.get("branch")) or None, "epic_id": epic_id,
            **{k: t[k] for k in ("kind", "write", "read_access") if k in t}}


def assert_proposed(task):
    """The backlog gate: the importer must never leave a task it created ready or beyond."""
    if task["status"] != "proposed" or task.get("approved_at"):
        raise ImportFailed(f"{task['id']} would be {task['status']}; imported tasks must be proposed")


SIMILAR = 0.85


def _match(tasks, keys, idx):
    """Per input task, the existing import-task event it is the same task as (exact key, then same repo and source
    file with title similarity >= SIMILAR), or None."""
    exact = {i: idx[k] for i, k in enumerate(keys) if k in idx}
    taken = {e["task_id"] for e in exact.values()}
    pool = [e for e in idx.values() if e["task_id"] not in taken]
    found = dict(exact)
    for i, t in enumerate(tasks):
        if i in found:
            continue
        repo, src, nt = _s(t.get("repo")), _src_file(t), _norm(t.get("title"))
        best, score = None, SIMILAR
        for e in pool:
            d = e["data"]
            if d.get("repo") != repo or d.get("source_file") != src or e["task_id"] in taken:
                continue
            r = difflib.SequenceMatcher(None, nt, d.get("norm_title", "")).ratio()
            if r >= score:
                best, score = e, r
        if best:
            found[i] = best
            taken.add(best["task_id"])
    return found


def import_tasks(conn, data, epic_ids, tally):
    idx, cidx = _index(conn, "import-task"), _index(conn, "import-conflict")
    raw = _dicts(data.get("tasks"))
    items = [t for t in raw if _s(t.get("title")).strip()]
    tally.counts.setdefault("task", {})
    for _ in range(len(raw) - len(items)):
        tally.add("task", "invalid")
    keys = task_keys(items)
    matched = _match(items, keys, idx)
    order = sorted(range(len(items)), key=lambda i: (PRIORITIES.index(_prio(items[i])), i))
    ledger_ids = {}
    for i in order:
        t, key = items[i], keys[i]
        eid = epic_ids.get(_s(t.get("epic_id")))
        fields = _task_fields(t, eid)
        old = matched.get(i)
        if old:
            tid = old["task_id"]
            cur = ops.get_task(conn, tid)
            diff = {k: v for k, v in fields.items() if cur[k] != v}
            if diff and (cur["status"] != "proposed" or owner_touched(conn, task_id=tid)):
                why = "task is " + cur["status"] if cur["status"] != "proposed" else "owner touched this task"
                _conflict(conn, cidx, key, diff, why, task_id=tid, epic_id=cur["epic_id"])
                action = "conflict"
            else:
                if diff:
                    ops.update_task(conn, tid, ACTOR, **diff)
                ev = _record(conn, idx, "import-task", key, _task_event(t), task_id=tid, epic_id=eid)
                action = "updated" if diff or ev != "unchanged" else "unchanged"
                if ev == "created":
                    action = "updated"  # re-keyed after a reword or shifted line
        else:
            made = ops.create_task(conn, fields["title"], _s(t.get("repo"), "unknown") or "unknown", epic_id=eid,
                                   repo_path=fields["repo"], goal=fields["goal"], acceptance=fields["acceptance"],
                                   scope_files=fields["scope_files"], priority=fields["priority"],
                                   size=fields["size"], status="proposed", actor=ACTOR,
                                   **{k: fields[k] for k in ("kind", "write", "read_access") if k in fields})
            tid = made["id"]
            if fields["branch"]:
                ops.update_task(conn, tid, ACTOR, branch=fields["branch"])
            assert_proposed(ops.get_task(conn, tid))
            _record(conn, idx, "import-task", key, _task_event(t), task_id=tid, epic_id=eid)
            action = "created"
        tally.add("task", action)
        ledger_ids[_s(t.get("id"))] = tid
    return ledger_ids


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


# ---- questions -----------------------------------------------------------------------------------------

def parse_questions(text):
    """Parse the grilling format: '## Qn. title', '**Body.**', '**Options.**' bullets, '**Recommended.**'."""
    out = []
    for block in re.split(r"(?m)^## ", text)[1:]:
        head, _, rest = block.partition("\n")
        title = re.sub(r"^Q\d+\.\s*", "", head).strip()
        body = re.search(r"\*\*Body\.\*\*\s*(.*?)(?=\n\*\*Options\.\*\*|\Z)", rest, re.S)
        opts = re.search(r"\*\*Options\.\*\*\s*(.*?)(?=\n\*\*Recommended\.\*\*|\Z)", rest, re.S)
        rec = re.search(r"(?:\*\*Recommended\.\*\*|(?m:^)Recommended:)[ \t]*(.*)", rest, re.S)
        options = [{"letter": m.group(1), "label": f"{m.group(1)}. {m.group(2).strip()}"}
                   for m in re.finditer(r"(?m)^- ([A-Z])\.[ \t]+(.*)$", opts.group(1) if opts else "")]
        letters = {o["letter"] for o in options}
        first = re.match(r"\(?([A-Z])(?![A-Za-z])", rec.group(1)) if rec else None  # explicit, first token only
        pick = first.group(1) if first and first.group(1) in letters else None
        for o in options:
            o["recommended"] = o.pop("letter") == pick
        out.append({"title": title, "body": body.group(1).strip() if body else "", "options": options,
                    "recommended_text": rec.group(1).strip() if rec else ""})
    return out


def _upsert_question(conn, idx, tally, key, epic_id, title, body, options, allow_text=True):
    old = idx.get(key)
    if old:
        qid = old["data"]["question"]
        q = ops.get_question(conn, qid)
        opts = [{"label": o["label"], "description": o.get("description", ""), "recommended": bool(o.get("recommended"))}
                for o in options]
        if q["status"] == "open" and (q["title"], q["body"], q["options"]) != (title, body, opts):
            conn.execute("UPDATE questions SET title=?, body=?, options=? WHERE id=?",
                         (title, body, json.dumps(opts, separators=(",", ":"), sort_keys=True), qid))
            ops.event(conn, "import-question", ACTOR, {"source_hash": key, "question": qid, "title": title}, epic_id=epic_id)
            tally.add("question", "updated")
        else:
            tally.add("question", "unchanged")
        return qid
    qid = ops.ask_question(conn, "epic", epic_id, title, body, options, allow_text, ACTOR)
    ops.event(conn, "import-question", ACTOR, {"source_hash": key, "question": qid, "title": title}, epic_id=epic_id)
    tally.add("question", "created")
    return qid


def import_questions(conn, text, import_epic, tally):
    idx = _index(conn, "import-question")
    for q in parse_questions(text):
        if not q["title"]:
            tally.warn("a question has no title; skipped")
            continue
        body = q["body"] + (f"\n\nRecommended: {q['recommended_text']}" if q["recommended_text"] else "")
        _upsert_question(conn, idx, tally, sha("question", q["title"]), import_epic, q["title"], body, q["options"])


# ---- branch dispositions: proposals only ---------------------------------------------------------------

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
