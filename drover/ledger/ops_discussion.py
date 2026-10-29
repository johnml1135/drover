"""Ledger operations."""
from .ops_common import (
    NotFound,
    _answer_context,
    _j,
    _jrow,
    event,
    get_epic,
    stamp,
    states,
    tx,
)
from .ops_tasks import (
    get_task,
    set_status,
)



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
