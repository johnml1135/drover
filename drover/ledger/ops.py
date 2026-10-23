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


def events(conn, task_id=None, epic_id=None, kind=None, after=0, limit=None):
    if not isinstance(after, int) or after < 0:
        raise ValueError("after must be a nonnegative event position")
    if limit is not None and (not isinstance(limit, int) or limit < 0):
        raise ValueError("limit must be nonnegative")
    q, a = "SELECT * FROM task_events WHERE id>?", [after]
    for col, v in (("task_id", task_id), ("epic_id", epic_id), ("kind", kind)):
        if v is not None:
            q += f" AND {col}=?"
            a.append(v)
    q += " ORDER BY id"
    if limit is not None:
        q += " LIMIT ?"
        a.append(limit)
    return [_jrow(r, "data") for r in conn.execute(q, a)]


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


def list_tasks(conn, status=None, epic_id=None, repo=None, include_archived=False):
    q, a = "SELECT * FROM tasks WHERE " + ("1=1" if include_archived else "archived=0"), []
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
                kind="implementation", write=None, read_access=None, branch=None, related=(), description=None,
                initiating_role=None, group_id=None, parent_id=None):
    if status not in states.CREATION_STATES:
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
        if parent_id is not None:
            get_task(conn, parent_id)
        if initiating_role is None and parent_id is not None:
            inherited = conn.execute("SELECT role_id FROM task_ownership WHERE task_id=? AND event_class='*'", (parent_id,)).fetchone()
            initiating_role = inherited['role_id'] if inherited else None
        if group_id is not None:
            from ..notifications import Notifications
            try:
                group = Notifications(conn).get_group(group_id)
            except NotFound as error:
                raise ValueError(f'unknown task group: {group_id}') from error
            if initiating_role is None:
                initiating_role = group['role_id']
        tid, now = _next_id(conn, "T"), stamp()
        if rank is None:
            rank = _next_rank(conn, "tasks", "epic_id IS ?", (epic_id,))
        conn.execute(
            "INSERT INTO tasks(id,epic_id,repo,repo_path,title,goal,acceptance,scope_files,priority,size,risk,rank,"
            "status,created,updated,kind,\"write\",read_access,branch) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, epic_id, repo, repo_path, title, goal, _j(list(acceptance)), _j(list(scope_files)), priority, size,
             risk, rank, status, now, now, kind, write, _j(read_access), branch))
        if parent_id is not None:
            conn.execute('UPDATE tasks SET parent_id=? WHERE id=?', (parent_id, tid))
        if initiating_role is not None:
            from ..notifications import Notifications
            Notifications(conn).assign(tid, initiating_role)
        if group_id is not None:
            Notifications(conn).enroll(group_id, tid, group['revision'], f'create:{tid}')
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
    if "model_pin" in fields:
        from .. import config
        if actor != "owner":
            raise PermissionError('only owner may edit a model pin')
        pin = fields['model_pin']
        if pin is not None and pin != {}:
            ladders = config.get()['models']['ladders']
            if not isinstance(pin, dict) or set(pin) != {'backend', 'model'} or not isinstance(pin['backend'], str) or not isinstance(pin['model'], str) or pin['backend'] not in ladders or pin['model'] not in ladders[pin['backend']]:
                raise ValueError('invalid model pin')
        fields['model_pin'] = pin or None
    with tx(conn):
        old = get_task(conn, task_id)
        if old.get('internal'):
            raise PermissionError('internal director accounting task is immutable')
        if "kind" in fields:
            task_context.check_kind(fields["kind"])
        if "write" in fields and not isinstance(fields["write"], bool):
            raise ValueError('write must be a boolean')
        if "read_access" in fields:
            fields["read_access"] = task_context.read_access(fields["read_access"], fields.get("repo", old["repo"]))
        _replace_connections(conn, task_id, deps, related, actor)
        if "epic_id" in fields and fields["epic_id"] is not None:
            get_epic(conn, fields["epic_id"])
        vals = {k: (_j(list(v)) if k in ("acceptance", "scope_files", "read_access") else (_j(v) if k == "model_pin" and v is not None else v)) for k, v in fields.items()}
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
        from ..notifications.groups import reaches
        if task_id == depends_on or reaches(conn, depends_on, task_id):
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


def set_status(conn, task_id, new, actor="system", reason=None, data=None):
    """Move a task along a legal transition and write one event. Illegal moves raise IllegalTransition.

    Entering `ready` needs approved_at; only the owner's proposed/draft -> ready sets it.
    """
    with tx(conn):
        t = get_task(conn, task_id)
        if t.get('internal'):
            raise PermissionError('internal director accounting task is immutable')
        old = t["status"]
        from .waiting import Waiting
        if new == states.READY:
            if actor not in ('dispatcher', 'system'):
                from .. import contract
                marker = conn.execute('SELECT COALESCE(MAX(id),0) FROM runs WHERE task_id=? AND role=? AND ended IS NOT NULL AND outcome<>?',
                                      (task_id, 'worker', contract.DONE)).fetchone()[0]
                conn.execute('UPDATE tasks SET retry_after_run=?,replans_used=0 WHERE id=?', (marker, task_id))
            if not t["approved_at"] and old not in states.CREATION_STATES:
                raise NotApproved(f"{task_id} was never approved by the owner; it cannot become ready")
            waiting = Waiting(conn, actor)
            pending = waiting.pending(("task", task_id))
            if pending and actor in ("owner", "director"):
                from .waiting import REPLAN
                if any(row["kind"] != REPLAN for row in pending):
                    raise ValueError('task has independent waits')
                for request in waiting.replans():
                    if request["task_id"] == task_id:
                        waiting._request_done(request["id"], "owner-resolved")
                t = get_task(conn, task_id)
                old = t["status"]
                if old == states.READY:
                    return t
            if waiting.pending(("task", task_id)):
                raise ValueError('task has outstanding waits')
        states.check(old, new, actor)
        if (old, new) == (states.NEEDS_OWNER, states.LANDED):
            run = read_only_result(conn, t)
            if run is None:
                raise IllegalTransition('only completed read-only findings may be accepted without landing')
            receipt = dict(schema_version=1, evidence=run,
                           result_hash=hashlib.sha256(_j(run).encode()).hexdigest())
            data = {**(data or {}), 'read_only': True, 'run': run['id'], 'read_only_receipt': receipt}
        approved = None if new in (states.DRAFT, states.PROPOSED) else t["approved_at"]  # demotion needs re-approval
        if (old, new) in ((states.PROPOSED, states.READY), (states.DRAFT, states.READY)):
            approved = approved or stamp()
        if new == states.READY and not approved:
            raise NotApproved(f"{task_id} was never approved by the owner; it cannot become ready")
        if new == states.BLOCKED:
            if not reason:
                raise ValueError("blocked needs a reason")
            return block(conn, task_id, reason, actor)
        extra = ""
        if new in (states.READY, states.DRAFT, states.PROPOSED):
            extra = ", owner_kind=NULL, model=NULL"
        conn.execute(f"UPDATE tasks SET status=?, blocker=NULL, approved_at=?, updated=?{extra} WHERE id=?",
                     (new, approved, stamp(), task_id))
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


def block(conn, task_id, reason, actor="system"):
    from .waiting import Waiting, MANUAL, cause_key
    with tx(conn):
        Waiting(conn, actor).wait(("task", task_id), MANUAL, cause_key(task_id),
                                  cause=cause_key("manual", task_id),
                                  data={"note": reason, "mode": "blocked"})
        return get_task(conn, task_id)


def unblock(conn, task_id, actor="system"):
    """Release only the manual cause, retaining independent waits."""
    from .waiting import Waiting, MANUAL, cause_key
    with tx(conn):
        task = get_task(conn, task_id)
        waiting = Waiting(conn, actor)
        manual = any(row["kind"] == MANUAL for row in waiting.pending(("task", task_id)))
        if task["status"] != "blocked" and not manual:
            raise IllegalTransition(f"{task_id} is {task['status']}, not blocked")
        waiting.release(cause_key("manual", task_id))
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
         "FROM tasks t LEFT JOIN epics e ON e.id=t.epic_id WHERE t.status='ready' AND t.archived=0 "
         "AND NOT EXISTS (SELECT 1 FROM waits w WHERE w.subject_kind='task' AND w.subject_key=t.id AND w.released_event IS NULL) "
         "AND NOT EXISTS (SELECT 1 FROM task_groups g WHERE g.parent_id=t.id AND g.ready=0) "
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

    worker keys: owner_kind, model, branch, worktree, actor, repo, repo_path (filters), policy_version. The ledger's `dispatch_repos` setting always applies.
    """
    worker = worker or {}
    actor = worker.get("actor") or worker.get("owner_kind") or "worker"
    with tx(conn):
        skip = set(exclude or ())
        from ..notifications.controls import admission, task_admission
        if not admission(conn)['allowed']:
            return None
        from .waiting import Waiting
        waiting, runs = Waiting(conn, actor), None
        from .. import dispatch_repos      # the owner's repo filter binds every claimer: dispatcher, hub API, CLI
        allow = dispatch_repos.allowed(lambda k, d=None: get_setting(conn, k, d))
        for cand in ready_tasks(conn, worker.get("repo"), worker.get("repo_path"), now):
            if cand["id"] in skip or not dispatch_repos.permits(allow, cand["repo"]):
                continue
            if not task_admission(conn, cand['id'])['allowed']:
                continue
            runs = waiting.replan_runs() if runs is None else runs     # one snapshot per call (inside tx)
            if waiting.replan_busy(cand["id"], runs):
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


# ---- runs ----------------------------------------------------------------------------------------------

def start_run(conn, task_id, role, backend, model=None, effort=None, policy_version=None, actor=None):
    actor = actor or role
    with tx(conn):
        t = get_task(conn, task_id)
        if role == 'director' or role.startswith('director:') or t.get('internal'):
            raise PermissionError('director runs require the supervised runtime')
        from ..notifications.controls import task_admission
        control = task_admission(conn, task_id)
        if not control['allowed']:
            raise PermissionError(control['reason'])
        cur = conn.execute(
            "INSERT INTO runs(task_id,role,backend,model,effort,policy_version,started) VALUES(?,?,?,?,?,?,?)",
            (task_id, role, backend, model, effort, policy_version or t["policy_version"] or current_policy(conn),
             stamp()))
        conn.execute("UPDATE tasks SET attempts=attempts+1, updated=? WHERE id=?", (stamp(), task_id))
        event(conn, "run-started", actor, {"run": cur.lastrowid, "role": role, "backend": backend}, task_id=task_id)
        if t["status"] == "claimed":
            set_status(conn, task_id, "running", actor, reason="run started")
        from .. import stats
        stats.initial_run(conn, cur.lastrowid)
        return cur.lastrowid


def finish_run(conn, run_id, outcome, actor="system", tokens_in=None, tokens_out=None, tokens_cached=None,
               allowance_used=None, gate_receipt=None, review_verdict=None, note=None, stats=None):
    if note is not None and len(note) > NOTE_MAX:
        raise ValueError(f"note is {len(note)} chars; the limit is {NOTE_MAX}")
    with tx(conn):
        r = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if r is None:
            raise NotFound(f"run {run_id}")
        if r['role'] == 'director' or r['role'].startswith('director:') or conn.execute('SELECT 1 FROM director_runs WHERE run_id=?', (run_id,)).fetchone():
            raise PermissionError('director runs require the supervised runtime')
        if r["ended"]:
            raise ValueError(f"run {run_id} already finished")
        now = stamp()
        wall = (_parse(now) - _parse(r["started"])).total_seconds()
        conn.execute(
            "UPDATE runs SET ended=?, wall_s=?, outcome=?, tokens_in=?, tokens_out=?, tokens_cached=?, "
            "allowance_used=?, gate_receipt=?, review_verdict=?, note=? WHERE id=?",
            (now, wall, outcome, tokens_in, tokens_out, tokens_cached,
             _j(allowance_used) if allowance_used is not None else None, gate_receipt, review_verdict, note, run_id))
        from .. import stats as stats_mod
        if stats:
            stats_mod.update_run(conn, run_id, **stats)
        stats_mod.finish_run(conn, run_id)
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
        if r['role'] == 'director' or r['role'].startswith('director:') or conn.execute('SELECT 1 FROM director_runs WHERE run_id=?', (run_id,)).fetchone():
            raise PermissionError('director runs require the supervised runtime')
        if not r["ended"]:
            raise ValueError(f"run {run_id} is still open; finish it first")
        conn.execute("UPDATE runs SET outcome=COALESCE(?,outcome), gate_receipt=COALESCE(?,gate_receipt), "
                     "note=COALESCE(?,note) WHERE id=?", (outcome, gate_receipt, note, run_id))
        from .. import stats
        stats.update_run(conn, run_id, gate_result=gate_receipt or r["gate_receipt"])
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


# ---- comments ------------------------------------------------------------------------------------------

def _target(conn, kind, tid):
    if kind == "task":
        return get_task(conn, tid)
    if kind == "epic":
        return get_epic(conn, tid)
    if kind == "director":
        from ..notifications import Notifications
        return Notifications(conn).role_state(tid)
    if kind == "group":
        from ..notifications import Notifications
        return Notifications(conn).get_group(tid)
    raise ValueError(f"comment target kind {kind!r}")


def _target_event(conn, target_kind, target_id, kind, actor, data):
    if target_kind == 'task':
        return event(conn, kind, actor, data, task_id=target_id)
    if target_kind == 'epic':
        return event(conn, kind, actor, data, epic_id=target_id)
    # Owner bookkeeping must not become unassigned ordinary-task notifications.
    return event(conn, 'notification.owner-' + kind, actor,
                 {**data, 'target_kind': target_kind, 'target_id': target_id})


def add_comment(conn, target_kind, target_id, author, body, urgent=False, replan=False):
    """Record a comment and route it: an owner comment on a needs-owner task requeues it; urgent holds dependents
    (of a task) or the pending tasks (of an epic); replan writes a replan-request event."""
    with tx(conn):
        tgt = _target(conn, target_kind, target_id)
        if target_kind in ('director', 'group') and (urgent or replan):
            raise ValueError('urgent/replan comments require a task or epic target')
        cur = conn.execute("INSERT INTO comments(target_kind,target_id,author,body,urgent,replan,at) "
                           "VALUES(?,?,?,?,?,?,?)", (target_kind, target_id, author, body, int(urgent), int(replan), stamp()))
        cid = cur.lastrowid
        _target_event(conn, target_kind, target_id, "comment", author,
                      {"comment": cid, "urgent": urgent, "replan": replan})
        routed = []
        if (target_kind == "task" and tgt["status"] == "needs-owner" and author == "owner"
                and tgt["approved_at"]):
            from .waiting import Waiting
            if (not Waiting(conn, author).pending(("task", target_id))
                    and not conn.execute("SELECT 1 FROM questions WHERE target_kind='task' AND target_id=? AND status='open'", (target_id,)).fetchone()):
                set_status(conn, target_id, "ready", author, reason="owner comment", data={"comment": cid})
                routed.append("requeued")
        if urgent:
            routed.append("held")
        if replan:
            _target_event(conn, target_kind, target_id, "replan-request", author, {"comment": cid})
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
        _target_event(conn, target_kind, target_id, "question-asked", actor,
                      {"question": cur.lastrowid, "title": title})
        if needs_owner and target_kind == "task":
            from .waiting import Waiting, OWNER_ANSWER, cause_key
            waiting = Waiting(conn, actor)
            cause = cause_key("question", cur.lastrowid)
            waiting.wait(("task", target_id), OWNER_ANSWER, cause_key(cause), cause=cause,
                         data={"mode": "needs-owner"})
            waiting.ask(cause, target_kind, target_id, title, body, options, allow_text)
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
        from .waiting import Waiting
        Waiting(conn, actor)._owner_answer(qid, choice, text, actor)
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
        token = _answer_context.set((conn, qid))
        try:
            _target_event(conn, q["target_kind"], q["target_id"], "question-answered", actor,
                          {"question": qid, "choice": choice})
        finally:
            _answer_context.reset(token)
        if q["target_kind"] == "task":
            t = get_task(conn, q["target_id"])
            left = conn.execute("SELECT COUNT(*) FROM questions WHERE status='open' AND target_kind='task' "
                                "AND target_id=?", (t["id"],)).fetchone()[0]
            if (t["status"] == states.NEEDS_OWNER and not left and t["approved_at"]
                    and not Waiting(conn, actor).pending(("task", t["id"]))):
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


# ---- task lifecycle ------------------------------------------------------------------------------------

def archive_task(conn, task_id, archived=True, actor="owner"):
    if actor != "owner":
        raise PermissionError("only the owner may archive tasks")
    with tx(conn):
        t = get_task(conn, task_id)
        if t.get('internal'):
            raise PermissionError('internal director accounting task is immutable')
        if t["status"] in ("claimed", "running", "gated", "reviewed") or conn.execute(
                "SELECT 1 FROM runs WHERE task_id=? AND ended IS NULL AND role<>'planner'", (task_id,)).fetchone():
            raise ValueError("finish or cancel active work before archiving")
        conn.execute("UPDATE tasks SET archived=?,updated=? WHERE id=?", (int(archived), stamp(), task_id))
        event(conn, "task.archived", actor, {"archived": bool(archived)}, task_id=task_id)
        return get_task(conn, task_id)


def delete_task(conn, task_id, confirm=False, actor="owner"):
    if actor != "owner":
        raise PermissionError("only the owner may delete tasks")
    if confirm is not True:
        raise ValueError("confirm deletion explicitly")
    with tx(conn):
        t = get_task(conn, task_id)
        if t.get('internal'):
            raise PermissionError('internal director accounting task is immutable')
        if t["status"] in ("claimed", "running", "gated", "reviewed") or conn.execute(
                "SELECT 1 FROM runs WHERE task_id=? AND ended IS NULL", (task_id,)).fetchone():
            raise ValueError("finish or cancel active work before deleting")
        from .waiting import Waiting
        if Waiting(conn, actor).replan_busy(task_id):
            raise ValueError("finish or clear uncertain planner before deleting")
        Waiting(conn, actor).forget_task(task_id)
        from ..notifications.core import forget_task
        forget_task(conn, task_id)
        # Landing metadata may reference a task; retain the audit batch but detach its membership.
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_landed'").fetchone():
            conn.execute("DELETE FROM task_landed WHERE task_id=?", (task_id,))
        conn.execute("DELETE FROM task_deps WHERE task_id=? OR depends_on=?", (task_id, task_id))
        conn.execute("DELETE FROM task_links WHERE task_id=? OR related_id=?", (task_id, task_id))
        conn.execute("DELETE FROM runs WHERE task_id=?", (task_id,))
        for table in ("comments", "questions"):
            conn.execute(f"DELETE FROM {table} WHERE target_kind='task' AND target_id=?", (task_id,))
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        from ..notifications.groups import changed
        changed(conn, task_id)
        # Append-only events and anonymous hourly/daily aggregates are retained.
        return {"deleted": task_id}
