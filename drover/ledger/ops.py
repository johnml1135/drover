"""Ledger operations: pure functions over a connection; every mutation is one transaction.

Claim order (ready_tasks): priority, then epic rank, then task rank, then pressure (age_hours x (1 + dependents)),
then size S<M<L. Rank sits above pressure on purpose: the owner reprioritises by dragging, so owner rank must beat
pressure; pressure and size only break rank ties.

Backlog gate: a task reaches `ready` only if `approved_at` is set, and only an owner transition out of
proposed/draft sets it. Holds (urgent, replan, manual block) are a list of {source, reason} on the blocker; a held
task returns to its previous state only when the list is empty.
"""
import json
from datetime import datetime, timezone

from .. import task_context
from . import scope as scopes
from . import states
from .db import tx
from .states import IllegalTransition  # noqa: F401  (re-exported)

SIZE_ORDER = {"S": 0, "M": 1, "L": 2}
HANDOFF_MAX = 1500
NOTE_MAX = 300
TASK_JSON = ("acceptance", "scope_files", "blocker", "read_access")
TASK_EDITABLE = ("title", "goal", "acceptance", "scope_files", "priority", "size", "risk", "repo_path", "repo",
                 "epic_id", "handoff", "branch", "worktree", "kind", "write", "read_access")
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
        d[k] = json.loads(d[k]) if d.get(k) else (None if k == "blocker" else [])
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


def event(conn, kind, actor, data=None, task_id=None, epic_id=None, at=None):
    conn.execute("INSERT INTO task_events(task_id,epic_id,kind,actor,data,at) VALUES(?,?,?,?,?,?)",
                 (task_id, epic_id, kind, actor, _j(data or {}), at or stamp()))


def events(conn, task_id=None, epic_id=None, kind=None):
    q, a = "SELECT * FROM task_events WHERE 1=1", []
    for col, v in (("task_id", task_id), ("epic_id", epic_id), ("kind", kind)):
        if v is not None:
            q += f" AND {col}=?"
            a.append(v)
    return [_jrow(r, "data") for r in conn.execute(q + " ORDER BY id", a)]


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


# ---- tasks ---------------------------------------------------------------------------------------------

def get_task(conn, task_id):
    r = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if r is None:
        raise NotFound(f"task {task_id}")
    return _task(r)


def list_tasks(conn, status=None, epic_id=None, repo=None):
    q, a = "SELECT * FROM tasks WHERE 1=1", []
    for col, v in (("status", status), ("epic_id", epic_id), ("repo", repo)):
        if v is not None:
            q += f" AND {col}=?"
            a.append(v)
    return [_task(r) for r in conn.execute(q + " ORDER BY epic_id, rank, id", a)]


def _check_handoff(text):
    if len(text) > HANDOFF_MAX:
        raise ValueError(f"handoff is {len(text)} chars; the limit is {HANDOFF_MAX}")


def create_task(conn, title, repo, epic_id=None, repo_path=None, goal="", acceptance=(), scope_files=(),
                priority="P2", size="M", risk="low", status="proposed", actor="owner", deps=(), rank=None,
                kind="implementation", write=None, read_access=None, branch=None, related=(), description=None):
    if status not in ("proposed", "draft"):
        raise ValueError("a new task starts as proposed or draft")
    task_context.check_kind(kind)
    if write is None:
        write = kind not in task_context.READ_ONLY_KINDS
    if not isinstance(write, bool):
        raise ValueError('write must be a boolean')
    read_access = task_context.read_access(read_access, repo)
    if description is not None:
        goal = description
    with tx(conn):
        if epic_id is not None:
            get_epic(conn, epic_id)
        tid, now = _next_id(conn, "T"), stamp()
        if rank is None:
            rank = _next_rank(conn, "tasks", "epic_id IS ?", (epic_id,))
        conn.execute(
            "INSERT INTO tasks(id,epic_id,repo,repo_path,title,goal,acceptance,scope_files,priority,size,risk,rank,"
            "status,created,updated,kind,\"write\",read_access,branch) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, epic_id, repo, repo_path, title, goal, _j(list(acceptance)), _j(list(scope_files)), priority, size,
             risk, rank, status, now, now, kind, write, _j(read_access), branch))
        event(conn, "task-created", actor, {"title": title, "status": status}, task_id=tid, epic_id=epic_id)
        _replace_connections(conn, tid, deps, related, actor)
        return get_task(conn, tid)


def update_task(conn, task_id, actor="owner", **fields):
    if "description" in fields:
        fields["goal"] = fields.pop("description")
    deps, related = fields.pop("deps", None), fields.pop("related", None)
    bad = set(fields) - set(TASK_EDITABLE)
    if bad:
        raise ValueError(f"cannot update task fields {sorted(bad)} (use set_status/reorder/add_dep)")
    if "handoff" in fields:
        _check_handoff(fields["handoff"])
    with tx(conn):
        old = get_task(conn, task_id)
        if "kind" in fields:
            task_context.check_kind(fields["kind"])
        if "write" in fields and not isinstance(fields["write"], bool):
            raise ValueError('write must be a boolean')
        if "read_access" in fields:
            fields["read_access"] = task_context.read_access(fields["read_access"], fields.get("repo", old["repo"]))
        _replace_connections(conn, task_id, deps, related, actor)
        if "epic_id" in fields and fields["epic_id"] is not None:
            get_epic(conn, fields["epic_id"])
        vals = {k: (_j(list(v)) if k in ("acceptance", "scope_files", "read_access") else v) for k, v in fields.items()}
        if vals:
            sets = ", ".join(f'"{k}"=?' for k in vals)
            conn.execute(f"UPDATE tasks SET {sets}, updated=? WHERE id=?", (*vals.values(), stamp(), task_id))
            event(conn, "task-updated", actor,
                  {k: ([old[k], v] if k != "handoff" else "changed") for k, v in fields.items()}, task_id=task_id)
        return get_task(conn, task_id)


def _replace_connections(conn, tid, deps, related, actor):
    # Caller owns the transaction: a bad link rolls back scalar fields and all previous links.
    if deps is not None:
        if not isinstance(deps, (list, tuple)):
            raise ValueError("deps must be a list")
        conn.execute("DELETE FROM task_deps WHERE task_id=?", (tid,))
        for d in deps:
            if isinstance(d, str):
                target, kind = d, "blocks"
            elif isinstance(d, dict):
                target, kind = d.get("depends_on"), d.get("kind", "blocks")
            elif isinstance(d, (tuple, list)) and len(d) == 2:
                target, kind = d
            else:
                raise ValueError('deps entries must be ids or {depends_on, kind}')
            add_dep(conn, tid, target, kind, actor)
        event(conn, "deps-set", actor, {"deps": deps_of(conn, tid)}, task_id=tid)
    if related is not None:
        if not isinstance(related, (list, tuple)):
            raise ValueError('related must be a list of task ids')
        conn.execute("DELETE FROM task_links WHERE task_id=? OR related_id=?", (tid, tid))
        for other in related:
            if not isinstance(other, str):
                raise ValueError('related must be a list of task ids')
            get_task(conn, other)
            if tid == other:
                raise ValueError('a task cannot relate to itself')
            a, b = sorted((tid, other))
            conn.execute("INSERT OR IGNORE INTO task_links(task_id,related_id) VALUES(?,?)", (a, b))
        event(conn, "related-set", actor, {"related": list(related)}, task_id=tid)


def related_of(conn, task_id):
    return [r[0] for r in conn.execute(
        "SELECT related_id FROM task_links WHERE task_id=? UNION SELECT task_id FROM task_links "
        "WHERE related_id=? ORDER BY 1", (task_id, task_id))]


def _reaches(conn, start, target):
    """True when `target` is reachable from `start` by following depends_on."""
    seen, stack = set(), [start]
    while stack:
        n = stack.pop()
        if n == target:
            return True
        if n in seen:
            continue
        seen.add(n)
        stack += [r[0] for r in conn.execute("SELECT depends_on FROM task_deps WHERE task_id=?", (n,))]
    return False


def add_dep(conn, task_id, depends_on, kind="blocks", actor="owner"):
    if kind not in ("blocks", "waits-for"):
        raise ValueError(f"dependency kind {kind!r}")
    with tx(conn):
        get_task(conn, task_id)
        get_task(conn, depends_on)
        if task_id == depends_on or _reaches(conn, depends_on, task_id):
            raise ValueError(f"dependency {task_id} -> {depends_on} would make a cycle")
        conn.execute("INSERT INTO task_deps(task_id,depends_on,kind) VALUES(?,?,?) "
                     "ON CONFLICT(task_id,depends_on) DO UPDATE SET kind=excluded.kind", (task_id, depends_on, kind))
        event(conn, "dep-added", actor, {"depends_on": depends_on, "kind": kind}, task_id=task_id)


def deps_of(conn, task_id):
    return [dict(r) for r in conn.execute("SELECT depends_on, kind FROM task_deps WHERE task_id=? ORDER BY depends_on",
                                          (task_id,))]


def dependents_of(conn, task_id, transitive=False):
    out, stack = [], [task_id]
    while stack:
        n = stack.pop()
        for (d,) in conn.execute("SELECT task_id FROM task_deps WHERE depends_on=?", (n,)):
            if d not in out:
                out.append(d)
                if transitive:
                    stack.append(d)
    return out


# ---- status --------------------------------------------------------------------------------------------

def _blocker(holds, prev):
    return {"reason": "; ".join(h["reason"] for h in holds), "holds": holds, "prev": prev}


class NotApproved(PermissionError):
    pass


def read_only_result(conn, task):
    """The latest run's findings, when they are eligible for owner acceptance."""
    if task['write'] or task['status'] != 'needs-owner':
        return None
    run = conn.execute("SELECT * FROM runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (task['id'],)).fetchone()
    if run and run['role'] == 'worker' and run['ended'] and run['outcome'] == 'read-only-done':
        return dict(run)
    return None


def accept_read_only(conn, task_id, actor='owner'):
    """Complete accepted findings without a commit, gate or landing queue entry."""
    if actor != 'owner':
        raise PermissionError('only the owner may accept read-only findings')
    with tx(conn):
        task = get_task(conn, task_id)
        run = read_only_result(conn, task)
        if run is None:
            raise ValueError('task has no completed read-only findings to accept')
        return set_status(conn, task_id, 'landed', actor, reason='Owner accepted read-only findings',
                          data={'read_only': True, 'run': run['id']})


def set_status(conn, task_id, new, actor="system", reason=None, blocker=None, data=None):
    """Move a task along a legal transition and write one event. Illegal moves raise IllegalTransition.

    Entering `ready` needs approved_at; only the owner's proposed/draft -> ready sets it.
    """
    with tx(conn):
        t = get_task(conn, task_id)
        old = t["status"]
        states.check(old, new, actor)
        if (old, new) == ('needs-owner', 'landed') and read_only_result(conn, t) is None:
            raise IllegalTransition('only completed read-only findings may be accepted without landing')
        approved = None if new in ("draft", "proposed") else t["approved_at"]  # demotion needs re-approval
        if (old, new) in (("proposed", "ready"), ("draft", "ready")):
            approved = approved or stamp()
        if new == "ready" and not approved:
            raise NotApproved(f"{task_id} was never approved by the owner; it cannot become ready")
        if new == "blocked":
            if blocker is None:
                if not reason:
                    raise ValueError("blocked needs a reason")
                blocker = _blocker([{"source": "manual", "reason": reason}], old)
            else:
                holds = blocker.get("holds") or [{"source": blocker.get("source", "manual"),
                                                  "reason": blocker.get("reason", "blocked")}]
                blocker = _blocker(holds, blocker.get("prev", old))
        else:
            blocker = None
        extra = ""
        if new in ("ready", "draft", "proposed"):
            extra = ", owner_kind=NULL, model=NULL"
        conn.execute(f"UPDATE tasks SET status=?, blocker=?, approved_at=?, updated=?{extra} WHERE id=?",
                     (new, _j(blocker) if blocker else None, approved, stamp(), task_id))
        event(conn, "status", actor, {"from": old, "to": new, "reason": reason, **(data or {})},
              task_id=task_id, epic_id=t["epic_id"])
        return get_task(conn, task_id)


def approve(conn, task_id, actor="owner"):
    """proposed/draft -> ready, owner only; the only way to set approved_at."""
    if actor != "owner":
        raise PermissionError("only the owner may approve")
    with tx(conn):
        t = get_task(conn, task_id)
        if t["status"] not in ("proposed", "draft"):
            raise IllegalTransition(f"cannot approve a {t['status']} task")
        return set_status(conn, task_id, "ready", actor, reason="approved")


def _restore(conn, t, actor, reason):
    prev = (t["blocker"] or {}).get("prev", "ready")
    if prev not in states.PENDING or (prev == "ready" and not t["approved_at"]):
        prev = "ready" if t["approved_at"] else "proposed"
    return set_status(conn, t["id"], prev, actor, reason=reason)


def _holds_of(t):
    """The blocker's hold list; converts an old-format blocker (reason only) in place of a missing list."""
    b = t["blocker"] or {}
    if "holds" in b:
        return b["holds"], b.get("prev", "ready")
    return ([{"source": b.get("source", "manual"), "reason": b.get("reason", "blocked")}] if b else []), \
        b.get("prev", "ready")


def add_hold(conn, task_id, source, reason, actor="system"):
    """Hold a task for `source`. Joins an existing hold; otherwise blocks the task, remembering its state."""
    with tx(conn):
        t = get_task(conn, task_id)
        if t["status"] != "blocked":
            return set_status(conn, task_id, "blocked", actor,
                              blocker=_blocker([{"source": source, "reason": reason}], t["status"]), reason=reason)
        old, prev = _holds_of(t)
        holds = [h for h in old if not (h["source"] == source and h["reason"] == reason)]
        holds.append({"source": source, "reason": reason})
        conn.execute("UPDATE tasks SET blocker=?, updated=? WHERE id=?",
                     (_j(_blocker(holds, prev)), stamp(), task_id))
        event(conn, "hold-added", actor, {"source": source, "reason": reason}, task_id=task_id, epic_id=t["epic_id"])
        return get_task(conn, task_id)


def remove_hold(conn, task_id, source, actor="system", reason=None):
    """Drop `source`'s holds (optionally only those with `reason`); restore the prior state when none remain.
    Returns True when the task was released."""
    with tx(conn):
        t = get_task(conn, task_id)
        if t["status"] != "blocked":
            return False
        old, prev = _holds_of(t)
        left = [h for h in old if not (h["source"] == source and (reason is None or h["reason"] == reason))]
        if len(left) == len(old):
            return False
        if left:
            conn.execute("UPDATE tasks SET blocker=?, updated=? WHERE id=?",
                         (_j(_blocker(left, prev)), stamp(), task_id))
            event(conn, "hold-removed", actor, {"source": source}, task_id=task_id, epic_id=t["epic_id"])
            return False
        _restore(conn, t, actor, f"{source} released")
        return True


def block(conn, task_id, reason, actor="system"):
    return add_hold(conn, task_id, "manual", reason, actor)


def unblock(conn, task_id, actor="system"):
    """Remove the manual block; the task stays blocked while other holds remain."""
    t = get_task(conn, task_id)
    if t["status"] != "blocked":
        raise IllegalTransition(f"{task_id} is {t['status']}, not blocked")
    remove_hold(conn, task_id, "manual", actor)
    return get_task(conn, task_id)


# ---- claiming ------------------------------------------------------------------------------------------

def _active_scopes(conn, repo):
    out = []
    for r in conn.execute("SELECT scope_files FROM tasks WHERE status IN ('claimed','running','gated','reviewed') AND repo IS ?", (repo,)):
        out += json.loads(r[0])
    return out


def _candidates(conn, repo=None, repo_path=None):
    q = ("SELECT t.*, COALESCE(e.rank, 1e18) AS epic_rank, "
         "(SELECT COUNT(*) FROM task_deps d JOIN tasks x ON x.id=d.task_id WHERE d.depends_on=t.id "
         " AND x.status NOT IN ('landed','cancelled')) AS dependents "
         "FROM tasks t LEFT JOIN epics e ON e.id=t.epic_id WHERE t.status='ready' "
         "AND NOT EXISTS (SELECT 1 FROM task_deps d JOIN tasks p ON p.id=d.depends_on "
         "WHERE d.task_id=t.id AND d.kind='blocks' AND p.status<>'landed')")
    a = []
    for col, v in (("t.repo", repo), ("t.repo_path", repo_path)):
        if v is not None:
            q += f" AND {col}=?"
            a.append(v)
    return [dict(r) for r in conn.execute(q, a)]


def ready_tasks(conn, repo=None, repo_path=None, now=None):
    """Claimable tasks, best first: priority, epic rank, task rank, pressure, size."""
    now = now or clock()
    rows = _candidates(conn, repo, repo_path)

    def pressure(r):
        return max((now - _parse(r["created"])).total_seconds() / 3600, 0) * (1 + r["dependents"])

    rows.sort(key=lambda r: (r["priority"], r["epic_rank"], r["rank"], -pressure(r), SIZE_ORDER[r["size"]], r["id"]))
    active = {}
    out = []
    for r in rows:
        if r["repo"] not in active:
            active[r["repo"]] = _active_scopes(conn, r["repo"])
        if not scopes.any_overlap(json.loads(r["scope_files"]), active[r["repo"]]):
            out.append(_task(r))
    return out


def claim_next(conn, worker=None, now=None, exclude=()):
    """Atomically claim the best ready task for `worker`; None when nothing is claimable.

    `exclude` is a collection of task ids to pass over (the dispatcher's skip list for the tick).

    worker keys: owner_kind, model, branch, worktree, actor, repo, repo_path (filters), policy_version.
    """
    worker = worker or {}
    actor = worker.get("actor") or worker.get("owner_kind") or "worker"
    with tx(conn):
        skip = set(exclude or ())
        for cand in ready_tasks(conn, worker.get("repo"), worker.get("repo_path"), now):
            if cand["id"] in skip:
                continue
            row = conn.execute(
                "UPDATE tasks SET status='claimed', owner_kind=?, model=?, branch=COALESCE(?,branch), "
                "worktree=COALESCE(?,worktree), policy_version=?, updated=? WHERE id=? AND status='ready' RETURNING *",
                (worker.get("owner_kind"), worker.get("model"), worker.get("branch"), worker.get("worktree"),
                 worker.get("policy_version") or current_policy(conn), stamp(now), cand["id"])).fetchone()
            if row:
                event(conn, "status", actor, {"from": "ready", "to": "claimed", "reason": "claim",
                                              "model": worker.get("model")}, task_id=cand["id"],
                      epic_id=cand["epic_id"])
                return _task(row)
    return None


# ---- dispatch backoff ----------------------------------------------------------------------------------
# Per-task launch backoff lives in one hidden setting (no task metadata column): {task_id: {"until": epoch, "min": n}}.

BACKOFF_KEY = "_dispatch_backoff"


def get_backoffs(conn):
    raw = get_setting(conn, BACKOFF_KEY)
    return json.loads(raw) if raw else {}


def set_backoff(conn, task_id, until, minutes):
    """Record that `task_id` should not be claimed before epoch `until`; None clears it."""
    with tx(conn):
        b = get_backoffs(conn)
        if until is None:
            b.pop(task_id, None)
        else:
            b[task_id] = {"until": until, "min": minutes}
        set_setting(conn, BACKOFF_KEY, json.dumps(b))


# ---- runs ----------------------------------------------------------------------------------------------

def start_run(conn, task_id, role, backend, model=None, effort=None, policy_version=None, actor=None):
    actor = actor or role
    with tx(conn):
        t = get_task(conn, task_id)
        cur = conn.execute(
            "INSERT INTO runs(task_id,role,backend,model,effort,policy_version,started) VALUES(?,?,?,?,?,?,?)",
            (task_id, role, backend, model, effort, policy_version or t["policy_version"] or current_policy(conn),
             stamp()))
        conn.execute("UPDATE tasks SET attempts=attempts+1, updated=? WHERE id=?", (stamp(), task_id))
        event(conn, "run-started", actor, {"run": cur.lastrowid, "role": role, "backend": backend}, task_id=task_id)
        if t["status"] == "claimed":
            set_status(conn, task_id, "running", actor, reason="run started")
        return cur.lastrowid


def finish_run(conn, run_id, outcome, actor="system", tokens_in=None, tokens_out=None, tokens_cached=None,
               allowance_used=None, gate_receipt=None, review_verdict=None, note=None):
    if note is not None and len(note) > NOTE_MAX:
        raise ValueError(f"note is {len(note)} chars; the limit is {NOTE_MAX}")
    with tx(conn):
        r = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if r is None:
            raise NotFound(f"run {run_id}")
        if r["ended"]:
            raise ValueError(f"run {run_id} already finished")
        now = stamp()
        wall = (_parse(now) - _parse(r["started"])).total_seconds()
        conn.execute(
            "UPDATE runs SET ended=?, wall_s=?, outcome=?, tokens_in=?, tokens_out=?, tokens_cached=?, "
            "allowance_used=?, gate_receipt=?, review_verdict=?, note=? WHERE id=?",
            (now, wall, outcome, tokens_in, tokens_out, tokens_cached,
             _j(allowance_used) if allowance_used is not None else None, gate_receipt, review_verdict, note, run_id))
        event(conn, "run-finished", actor, {"run": run_id, "outcome": outcome}, task_id=r["task_id"])
        return _jrow(conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone(), "allowance_used")


def amend_run(conn, run_id, outcome=None, gate_receipt=None, note=None):
    """Settle a finished run's judgement afterwards: the dispatcher finishes a worker run as `done` and records
    `gated` or `gate-fail` once the gate run is in. Only an ended run can be amended, and only these three fields."""
    if note is not None and len(note) > NOTE_MAX:
        raise ValueError(f"note is {len(note)} chars; the limit is {NOTE_MAX}")
    with tx(conn):
        r = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if r is None:
            raise NotFound(f"run {run_id}")
        if not r["ended"]:
            raise ValueError(f"run {run_id} is still open; finish it first")
        conn.execute("UPDATE runs SET outcome=COALESCE(?,outcome), gate_receipt=COALESCE(?,gate_receipt), "
                     "note=COALESCE(?,note) WHERE id=?", (outcome, gate_receipt, note, run_id))
        event(conn, "run-amended", "system", {"run": run_id, "outcome": outcome}, task_id=r["task_id"])
        return _jrow(conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone(), "allowance_used")


def list_runs(conn, task_id):
    return [_jrow(r, "allowance_used") for r in conn.execute("SELECT * FROM runs WHERE task_id=? ORDER BY id", (task_id,))]


# ---- reorder -------------------------------------------------------------------------------------------

def _siblings(conn, kind, row):
    if kind == "epic":
        return [dict(r) for r in conn.execute("SELECT id, rank FROM epics ORDER BY rank, id")]
    return [dict(r) for r in conn.execute("SELECT id, rank FROM tasks WHERE epic_id IS ? ORDER BY rank, id",
                                          (row["epic_id"],))]


def _renormalise(conn, kind, sibs):
    table = "epics" if kind == "epic" else "tasks"
    for i, s in enumerate(sibs, 1):
        conn.execute(f"UPDATE {table} SET rank=? WHERE id=?", (float(i), s["id"]))


def reorder(conn, kind, item_id, before_id=None, after_id=None, actor="owner"):
    """Place an epic (among epics) or a task (within its epic) before or after a sibling, by fractional rank."""
    if kind not in ("epic", "task") or (before_id is None) == (after_id is None):
        raise ValueError("reorder needs kind epic|task and exactly one of before_id/after_id")
    anchor_id = before_id or after_id
    if anchor_id == item_id:
        raise ValueError("cannot place an item relative to itself")
    with tx(conn):
        row = get_epic(conn, item_id) if kind == "epic" else get_task(conn, item_id)
        anchor = get_epic(conn, anchor_id) if kind == "epic" else get_task(conn, anchor_id)
        if kind == "task" and anchor["epic_id"] != row["epic_id"]:
            raise ValueError("tasks are reordered within one epic")
        for attempt in range(2):
            sibs = [s for s in _siblings(conn, kind, row) if s["id"] != item_id]
            i = next(i for i, s in enumerate(sibs) if s["id"] == anchor_id)
            lo_i, hi_i = (i - 1, i) if before_id else (i, i + 1)
            hi = sibs[hi_i]["rank"] if hi_i < len(sibs) else sibs[i]["rank"] + 1.0
            lo = sibs[lo_i]["rank"] if lo_i >= 0 else sibs[i]["rank"] - 1.0
            new = (lo + hi) / 2
            if lo < new < hi:
                break
            _renormalise(conn, kind, [*sibs])  # gap exhausted: spread ranks 1..n, then retry
        else:
            raise RuntimeError("could not find a rank")
        table = "epics" if kind == "epic" else "tasks"
        conn.execute(f"UPDATE {table} SET rank=?, updated=? WHERE id=?", (new, stamp(), item_id))
        event(conn, "reordered", actor, {"before": before_id, "after": after_id, "rank": new},
              task_id=item_id if kind == "task" else None, epic_id=item_id if kind == "epic" else row["epic_id"])
        return new


# ---- holds and replan ----------------------------------------------------------------------------------

def _hold(conn, task_id, source, reason, actor):
    t = get_task(conn, task_id)
    if t["status"] in states.PENDING or t["status"] == "blocked":
        add_hold(conn, task_id, source, reason, actor)
        return True
    return False


def _overlapping_in_epic(conn, t):
    if t["epic_id"] is None:
        return []
    mine = t["scope_files"]
    return [o["id"] for o in list_tasks(conn, epic_id=t["epic_id"])
            if o["id"] != t["id"] and o["status"] in (*states.PENDING, "blocked")
            and scopes.any_overlap(mine, o["scope_files"])]


def replan_hold(conn, task_id, actor="system", reason="replan"):
    """Block the task (reason replan) and hold its dependents plus same-epic scope-overlapping pending tasks."""
    with tx(conn):
        t = get_task(conn, task_id)
        if t["status"] in states.TERMINAL:
            raise IllegalTransition(f"cannot hold a {t['status']} task")
        add_hold(conn, task_id, task_id, reason, actor)
        held = []
        for tid in [*dependents_of(conn, task_id, transitive=True), *_overlapping_in_epic(conn, t)]:
            if tid not in held and _hold(conn, tid, task_id, reason, actor):
                held.append(tid)
        event(conn, "hold", actor, {"reason": reason, "held": held}, task_id=task_id, epic_id=t["epic_id"])
        return held


def replan_release(conn, task_id, actor="system", reason="replan"):
    """Remove this source's replan holds everywhere (pass reason="urgent" for urgent ones, None for all). Returns the ids that actually left blocked."""
    with tx(conn):
        released = []
        for r in conn.execute("SELECT id FROM tasks WHERE status='blocked' ORDER BY id").fetchall():
            if remove_hold(conn, r[0], task_id, actor, reason):
                released.append(r[0])
        event(conn, "release", actor, {"released": released}, task_id=task_id)
        return released


# ---- comments ------------------------------------------------------------------------------------------

def _target(conn, kind, tid):
    if kind == "task":
        return get_task(conn, tid)
    if kind == "epic":
        return get_epic(conn, tid)
    raise ValueError(f"comment target kind {kind!r}")


def add_comment(conn, target_kind, target_id, author, body, urgent=False, replan=False):
    """Record a comment and route it: an owner comment on a needs-owner task requeues it; urgent holds dependents
    (of a task) or the pending tasks (of an epic); replan writes a replan-request event."""
    with tx(conn):
        tgt = _target(conn, target_kind, target_id)
        cur = conn.execute("INSERT INTO comments(target_kind,target_id,author,body,urgent,replan,at) "
                           "VALUES(?,?,?,?,?,?,?)", (target_kind, target_id, author, body, int(urgent), int(replan), stamp()))
        cid = cur.lastrowid
        ids = {"task_id": target_id} if target_kind == "task" else {"epic_id": target_id}
        event(conn, "comment", author, {"comment": cid, "urgent": urgent, "replan": replan}, **ids)
        routed = []
        if (target_kind == "task" and tgt["status"] == "needs-owner" and author == "owner"
                and tgt["approved_at"]):
            set_status(conn, target_id, "ready", author, reason="owner comment", data={"comment": cid})
            routed.append("requeued")
        if urgent:
            if target_kind == "task":
                victims = dependents_of(conn, target_id, transitive=True)
            else:
                victims = [t["id"] for t in list_tasks(conn, epic_id=target_id)]
            held = [v for v in victims if _hold(conn, v, target_id, "urgent", author)]
            event(conn, "hold", author, {"reason": "urgent", "held": held}, **ids)
            routed.append("held")
        if replan:
            event(conn, "replan-request", author, {"comment": cid}, **ids)
            routed.append("replan-request")
        return {"id": cid, "routed": routed}


def list_comments(conn, target_kind, target_id):
    return [dict(r) for r in conn.execute("SELECT * FROM comments WHERE target_kind=? AND target_id=? ORDER BY id",
                                          (target_kind, target_id))]


# ---- questions -----------------------------------------------------------------------------------------

def ask_question(conn, target_kind, target_id, title, body="", options=(), allow_text=True, actor="system",
                 needs_owner=False):
    """Open a question. needs_owner=True also moves the (task) target to needs-owner."""
    with tx(conn):
        tgt = _target(conn, target_kind, target_id)
        opts = [{"label": o["label"], "description": o.get("description", ""), "recommended": bool(o.get("recommended"))}
                for o in options]
        cur = conn.execute("INSERT INTO questions(target_kind,target_id,title,body,options,allow_text,asked) "
                           "VALUES(?,?,?,?,?,?,?)", (target_kind, target_id, title, body, _j(opts), int(allow_text), stamp()))
        ids = {"task_id": target_id} if target_kind == "task" else {"epic_id": target_id}
        event(conn, "question-asked", actor, {"question": cur.lastrowid, "title": title}, **ids)
        if needs_owner and target_kind == "task" and tgt["status"] != "needs-owner":
            set_status(conn, target_id, "needs-owner", actor, reason=f"question: {title}")
        return cur.lastrowid


def get_question(conn, qid):
    r = conn.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
    if r is None:
        raise NotFound(f"question {qid}")
    return _jrow(r, "options")


def open_questions(conn):
    return [_jrow(r, "options") for r in conn.execute("SELECT * FROM questions WHERE status='open' ORDER BY id")]


def answer_question(conn, qid, choice=None, text=None, actor="owner"):
    """Answer with an option label and/or free text. Requeues a needs-owner task once none of its questions is open."""
    with tx(conn):
        q = get_question(conn, qid)
        if q["status"] != "open":
            raise ValueError(f"question {qid} is already answered")
        if choice is None and not text:
            raise ValueError("an answer needs a choice or text")
        if choice is not None and choice not in [o["label"] for o in q["options"]]:
            raise ValueError(f"{choice!r} is not one of the options")
        if text and not q["allow_text"]:
            raise ValueError("this question does not take free text")
        conn.execute("UPDATE questions SET status='answered', answer_choice=?, answer_text=?, answered_at=? WHERE id=?",
                     (choice, text, stamp(), qid))
        ids = {"task_id": q["target_id"]} if q["target_kind"] == "task" else {"epic_id": q["target_id"]}
        event(conn, "question-answered", actor, {"question": qid, "choice": choice}, **ids)
        if q["target_kind"] == "task":
            t = get_task(conn, q["target_id"])
            left = conn.execute("SELECT COUNT(*) FROM questions WHERE status='open' AND target_kind='task' "
                                "AND target_id=?", (t["id"],)).fetchone()[0]
            if t["status"] == "needs-owner" and not left and t["approved_at"]:
                set_status(conn, t["id"], "ready", actor, reason="question answered", data={"question": qid})
        return get_question(conn, qid)


# ---- decisions -----------------------------------------------------------------------------------------

def record_decision(conn, question_name, criteria_version, model, state_hash, answer, probabilities=None,
                    confidence=None, shadow=False, rule_answer=None):
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO decisions(question_name,criteria_version,model,state_hash,answer,probabilities,confidence,"
            "shadow,rule_answer,outcome,at) VALUES(?,?,?,?,?,?,?,?,?,NULL,?)",
            (question_name, criteria_version, model, state_hash, _j(answer),
             _j(probabilities) if probabilities is not None else None, confidence, int(shadow),
             _j(rule_answer) if rule_answer is not None else None, stamp()))
        return cur.lastrowid


def record_outcome(conn, decision_id, outcome):
    with tx(conn):
        n = conn.execute("UPDATE decisions SET outcome=? WHERE id=?", (_j(outcome), decision_id)).rowcount
        if not n:
            raise NotFound(f"decision {decision_id}")


def get_decision(conn, decision_id):
    r = conn.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
    if r is None:
        raise NotFound(f"decision {decision_id}")
    return _jrow(r, "answer", "probabilities", "rule_answer", "outcome")
