"""Ledger operations."""
from .ops_common import (
    NOTE_MAX,
    NotFound,
    SIZE_ORDER,
    _j,
    _jrow,
    _parse,
    _task,
    clock,
    current_policy,
    event,
    get_epic,
    get_setting,
    json,
    scopes,
    stamp,
    tx,
)
from .ops_tasks import (
    get_task,
)



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
        if t["status"] == "claimed" and role == "worker":
            from .ops_stages import start_running
            start_running(conn, task_id, cur.lastrowid, actor)
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
