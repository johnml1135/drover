"""Ledger operations: pure functions over a connection; every mutation is one transaction.

Claim order (ready_tasks): priority, then epic rank, then task rank, then pressure (age_hours x (1 + dependents)),
then size S<M<L. Rank sits above pressure on purpose: the owner reprioritises by dragging, so owner rank must beat
pressure; pressure and size only break rank ties.

Backlog gate: a task reaches `ready` only if `approved_at` is set, and only an owner transition out of
proposed/draft sets it. Holds (urgent, replan, manual block) are a list of {source, reason} on the blocker; a held
task returns to its previous state only when the list is empty.
"""
import hashlib
import json
from contextvars import ContextVar
from collections import deque
from datetime import datetime, timezone

from .. import task_context
from . import scope as scopes
from . import states
from .db import tx
from .states import IllegalTransition  # noqa: F401  (re-exported)

SIZE_ORDER = {"S": 0, "M": 1, "L": 2}
HANDOFF_MAX = 1500
NOTE_MAX = 300
TASK_JSON = ("acceptance", "scope_files", "model_pin", "read_access")
TASK_EDITABLE = ("title", "goal", "acceptance", "scope_files", "priority", "size", "risk", "repo_path", "repo",
                 "epic_id", "handoff", "branch", "worktree", "kind", "write", "read_access", "model_pin")
EPIC_EDITABLE = ("repo", "title", "plan_md", "status")
HOLD_REASONS = ("replan", "urgent")


class NotFound(LookupError):
    pass


def clock():
    return datetime.now(timezone.utc)


def stamp(dt=None):
    return (dt or clock()).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _j(v):
    return json.dumps(v, separators=(",", ":"), sort_keys=True)


def _b36(n):
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = digits[r] + out
        if not n:
            return out


def _next_id(conn, prefix):
    key = f"seq_{prefix}"
    conn.execute("INSERT INTO settings(key,value) VALUES(?, '0') ON CONFLICT(key) DO NOTHING", (key,))
    n = conn.execute("UPDATE settings SET value = CAST(value AS INTEGER)+1 WHERE key=? RETURNING value", (key,)).fetchone()[0]
    return f"{prefix}-{_b36(int(n))}"


def _task(row):
    if row is None:
        return None
    d = dict(row)
    for k in TASK_JSON:
        d[k] = json.loads(d[k]) if d.get(k) else (None if k == "model_pin" else [])
    d["blocker"] = None
    d["write"] = bool(d["write"])
    d["description"] = d["goal"]
    return d


def _jrow(row, *cols):
    if row is None:
        return None
    d = dict(row)
    for k in cols:
        if d.get(k) is not None:
            d[k] = json.loads(d[k])
    return d


_answer_context = ContextVar("waiting_answer_context", default=None)
_event_queues = ContextVar("waiting_event_queues", default=None)


def event(conn, kind, actor, data=None, task_id=None, epic_id=None, at=None):
    with tx(conn):
        from .waiting import Waiting
        Waiting(conn, actor)._validate_event(kind, data or {})
        when = at or stamp()
        if kind == "status" and (data or {}).get("to") == states.LANDED and (data or {}).get("from") != states.LANDED:
            from .. import stats
            stats.landing(conn, _parse(at).timestamp() if at else None)
        cur = conn.execute("INSERT INTO task_events(task_id,epic_id,kind,actor,data,at) VALUES(?,?,?,?,?,?)",
                           (task_id, epic_id, kind, actor, _j(data or {}), when))
        ident = cur.lastrowid
        row = {"id": ident, "kind": kind, "actor": actor, "task_id": task_id,
               "epic_id": epic_id, "data": data or {}, "at": when}
        from ..notifications.core import capture_event
        capture_event(conn, row)
        if task_id is not None:
            from ..notifications.groups import changed
            changed(conn, task_id)
        contexts = _event_queues.get() or {}
        if conn in contexts:
            contexts[conn].append(row)
            return ident
        queue = deque([row])
        token = _event_queues.set({**contexts, conn: queue})
        try:
            while queue:
                current = queue.popleft()
                Waiting(conn, current['actor']).resolve(current)
        finally:
            _event_queues.reset(token)
        return ident


def events(conn, task_id=None, epic_id=None, kind=None, after=0, limit=None, newest=False):
    if not isinstance(after, int) or after < 0:
        raise ValueError("after must be a nonnegative event position")
    if limit is not None and (not isinstance(limit, int) or limit < 0):
        raise ValueError("limit must be nonnegative")
    q, a = "SELECT * FROM task_events WHERE id>?", [after]
    for col, v in (("task_id", task_id), ("epic_id", epic_id), ("kind", kind)):
        if v is not None:
            q += f" AND {col}=?"
            a.append(v)
    q += " ORDER BY id DESC" if newest else " ORDER BY id"
    if limit is not None:
        q += " LIMIT ?"
        a.append(limit)
    rows = [_jrow(r, "data") for r in conn.execute(q, a)]
    return rows[::-1] if newest else rows


# ---- settings and policy -------------------------------------------------------------------------------

def get_setting(conn, key, default=None):
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def set_setting(conn, key, value):
    with tx(conn):
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, str(value)))


def add_policy_version(conn, version, data, note="", actor="owner"):
    with tx(conn):
        conn.execute("INSERT INTO policy_versions(version,created,data,note) VALUES(?,?,?,?)",
                     (version, stamp(), _j(data), note))
        conn.execute("INSERT INTO settings(key,value) VALUES('policy_version',?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (version,))
        event(conn, "policy", actor, {"version": version})


def current_policy(conn):
    return get_setting(conn, "policy_version")


# ---- epics ---------------------------------------------------------------------------------------------

def _next_rank(conn, table, where="1=1", args=()):
    return conn.execute(f"SELECT COALESCE(MAX(rank),0)+1 FROM {table} WHERE {where}", args).fetchone()[0]


def get_epic(conn, epic_id):
    r = conn.execute("SELECT * FROM epics WHERE id=?", (epic_id,)).fetchone()
    if r is None:
        raise NotFound(f"epic {epic_id}")
    return dict(r)


def create_epic(conn, repo, title, plan_md="", actor="owner", rank=None, status="open"):
    with tx(conn):
        eid, now = _next_id(conn, "E"), stamp()
        rank = _next_rank(conn, "epics") if rank is None else rank
        conn.execute("INSERT INTO epics(id,repo,title,plan_md,rank,status,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                     (eid, repo, title, plan_md, rank, status, now, now))
        event(conn, "epic-created", actor, {"title": title}, epic_id=eid)
        return get_epic(conn, eid)


def update_epic(conn, epic_id, actor="owner", **fields):
    bad = set(fields) - set(EPIC_EDITABLE)
    if bad:
        raise ValueError(f"cannot update epic fields {sorted(bad)}")
    with tx(conn):
        old = get_epic(conn, epic_id)
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE epics SET {sets}, updated=? WHERE id=?", (*fields.values(), stamp(), epic_id))
            event(conn, "epic-updated", actor, {k: [old[k], v] for k, v in fields.items() if k != "plan_md"},
                  epic_id=epic_id)
        return get_epic(conn, epic_id)


def list_epics(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM epics ORDER BY rank, id")]
