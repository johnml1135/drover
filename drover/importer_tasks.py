"""Import epics and proposed tasks."""
import difflib
import re

from .ledger import ops
from .importer_common import ACTOR, PRIORITIES, SIZES, ImportFailed, _dicts, _index, _list, _record, _s, sha

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
            try:
                cur = ops.get_task(conn, tid)
            except ops.NotFound:
                # Retained import events are tombstones for owner-deleted tasks, including fuzzy matches.
                tally.add("task", "skipped")
                continue
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
