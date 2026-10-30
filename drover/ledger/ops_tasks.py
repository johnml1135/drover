"""Ledger operations."""
from .ops_common import (
    HANDOFF_MAX,
    IllegalTransition,
    NotFound,
    TASK_EDITABLE,
    _j,
    _next_id,
    _next_rank,
    _task,
    event,
    get_epic,
    hashlib,
    stamp,
    states,
    task_context,
    tx,
)



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


def set_status(conn, task_id, new, actor="system", reason=None, data=None):
    """Move a task along a legal transition and write one event. Illegal moves raise IllegalTransition.

    Entering `ready` needs approved_at; only the owner's proposed/draft -> ready sets it.
    """
    # Self-run attestations will select their execution mode here when that mode exists.
    if new in (states.CLAIMED, states.RUNNING, states.GATED, states.REVIEWED, states.LANDED):
        raise IllegalTransition('engine stages require evidence-checked operations')
    return _set_status(conn, task_id, new, actor, reason, data)


def _set_status(conn, task_id, new, actor="system", reason=None, data=None):
    with tx(conn):
        t = get_task(conn, task_id)
        if t.get('internal'):
            raise PermissionError('internal director accounting task is immutable')
        old = t["status"]
        if new == states.READY and old in states.CREATION_STATES and actor != 'owner':
            states.check(old, new, actor)
        if new == states.READY and old in states.CREATION_STATES and t['write'] and not t['scope_files']:
            raise ValueError('writable tasks require scope_files before approval')
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
            from .ops_stages import read_only_result
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
