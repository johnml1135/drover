import json
import threading
import time

from drover import config, decisions, stats, usage
from drover.ledger import board as ledger_board
from drover.ledger import connect, ops, tx
from drover.ledger.ops import NotFound as LedgerNotFound

from .store_common import BadRequest, NotFound, _Rollback, _translate


class Store:
    def __init__(self, db_path, publish=None):
        self.path = str(db_path)
        self.publish = publish or (lambda kind, data=None: None)
        self._local = threading.local()
        self._conns = []
        self._lock = threading.Lock()
        self.conn  # open + migrate on the constructing thread

    @property
    def conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = connect(self.path)
            self._local.conn = c
            with self._lock:
                self._conns.append(c)
        return c

    def close(self):
        with self._lock:
            conns, self._conns = self._conns, []
        for c in conns:
            try:
                c.close()
            except Exception:
                pass  # already closed or otherwise unavailable
        self._local = threading.local()

    def _changed(self, name, **data):
        self.publish(name, data)

    # -- epics and tasks -------------------------------------------------
    @_translate
    def create_epic(self, repo, title, plan_md="", actor="dispatcher"):
        e = ops.create_epic(self.conn, repo, title, plan_md, actor=actor)
        self._changed("epic.created", id=e["id"])
        return e

    def list_epics(self):
        return ops.list_epics(self.conn)
    @_translate
    def create_task(self, title, repo="default", actor="dispatcher", **f):
        if not title or not title.strip():
            raise BadRequest("title required")
        t = ops.create_task(self.conn, title.strip(), repo, actor=actor, **f)
        self._changed("task.created", id=t["id"])
        return self.get_task(t["id"])
    @_translate
    def get_task(self, tid):
        t = ops.get_task(self.conn, tid)
        t["deps"] = ops.deps_of(self.conn, tid)
        t["dependents"] = ops.dependents_of(self.conn, tid)
        t["related"] = ops.related_of(self.conn, tid)
        return t

    def list_tasks(self, status=None, epic_id=None, repo=None, q=None, limit=500, include_archived=False):
        tasks = ops.list_tasks(self.conn, epic_id=epic_id, repo=repo, include_archived=include_archived)
        if status:
            want = set(status.split(","))
            tasks = [t for t in tasks if t["status"] in want]
        if q:
            ql = q.lower()
            tasks = [t for t in tasks if ql in t["title"].lower() or ql in (t["goal"] or "").lower()]
        return tasks[:limit]
    @_translate
    def update_task(self, tid, actor="dispatcher", **fields):
        fields = {k: v for k, v in fields.items() if v is not None}
        t = ops.update_task(self.conn, tid, actor=actor, **fields)
        self._changed("task.updated", id=tid)
        return self.get_task(tid)
    @_translate
    def set_status(self, tid, status, actor, reason=None):
        t = ops.set_status(self.conn, tid, status, actor=actor, reason=reason)
        self._changed("task.status", id=tid, status=status)
        return t
    @_translate
    def approve(self, tid):
        """Owner approval. The caller must have authenticated the owner."""
        t = ops.approve(self.conn, tid, actor="owner")
        self._changed("task.status", id=tid, status=t["status"])
        return t
    @_translate
    def accept_read_only(self, tid):
        """The authenticated owner accepts the latest successful findings."""
        t = ops.accept_read_only(self.conn, tid, actor='owner')
        self._changed('task.status', id=tid, status=t['status'])
        return t
    @_translate
    def record_external_landing(self, tid, sha, base_ref, gate, review):
        with tx(self.conn):
            old = ops.get_task(self.conn, tid)['status']
            t = ops.record_external_landing(self.conn, tid, sha, base_ref, gate, review, actor='owner')
        if old != t['status']:
            self._changed('task.status', id=tid, status=t['status'])
        return t
    @_translate
    def claim_next(self, worker):
        t = ops.claim_next(self.conn, dict(worker))
        if t:
            self._changed("task.claimed", id=t["id"])
        return t
    @_translate
    def reorder(self, kind, item_id, before_id=None, after_id=None, actor="owner"):
        r = ops.reorder(self.conn, kind, item_id, before_id, after_id, actor=actor)
        self._changed("reordered", kind=kind, id=item_id)
        return r

    @_translate
    def archive_task(self, tid, archived=True, actor="owner"):
        result = ops.archive_task(self.conn, tid, archived, actor)
        self._changed("task.archived", id=tid, archived=archived)
        return result

    @_translate
    def delete_task(self, tid, confirm=False, actor="owner"):
        result = ops.delete_task(self.conn, tid, confirm, actor)
        self._changed("task.deleted", id=tid)
        return result

    @_translate
    def stats(self, since=None, task=None):
        return stats.query(self.conn, since, task)

    def seat_order(self):
        return usage.order(self.conn, seat_load=usage.active_load(self.conn))

    @_translate
    def set_usage(self, seat, window, pct, resets):
        if not config.seat_by_name(seat):
            raise BadRequest("unknown seat")
        result = usage.manual(self.conn, seat, window, pct, resets)
        self._changed("usage.updated", seat=seat)
        return result

    def usage_alerts(self):
        alerts = []
        for seat in config.get()["seats"]:
            row = self.conn.execute("SELECT ok FROM allowance_readings WHERE seat=? ORDER BY ts DESC,id DESC LIMIT 1",
                                    (seat["name"],)).fetchone()
            if row and not row["ok"]:
                alerts.append({"seat": seat["name"], "message": "Allowance reading failed; enter a manual reading."})
        return alerts

    # -- runs ------------------------------------------------------------
    @_translate
    def start_run(self, task_id, role, backend, model=None, effort=None, actor=None):
        rid = ops.start_run(self.conn, task_id, role, backend, model, effort, actor=actor)
        self._changed("run.started", id=rid, task_id=task_id)
        return {"id": rid, "task_id": task_id}

    @_translate
    def finish_run(self, run_id, outcome, actor="dispatcher", **fields):
        r = ops.finish_run(self.conn, run_id, outcome, actor=actor, **fields)
        self._changed("run.finished", id=run_id, task_id=r["task_id"], outcome=outcome)
        return r

    @_translate
    def run_role(self, run_id):
        row = self.conn.execute("SELECT role FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise NotFound(f"run {run_id}")
        return row[0]

    def list_runs(self, task_id):
        return ops.list_runs(self.conn, task_id)

    # -- events ----------------------------------------------------------
    @_translate
    def add_event(self, kind, actor, data=None, task_id=None, epic_id=None):
        ops.event(self.conn, kind, actor, data, task_id=task_id, epic_id=epic_id)
        self._changed("event", kind=kind, task_id=task_id)
        return self.list_events(kind=kind, limit=1)[-1]

    def list_events(self, task_id=None, epic_id=None, kind=None, since_id=0, limit=200):
        evs = [e for e in ops.events(self.conn, task_id, epic_id, kind) if e["id"] > since_id]
        return evs[-limit:]

    # -- comments, questions, decisions ----------------------------------
    @_translate
    def add_comment(self, target_kind, target_id, author, body, urgent=False, replan=False):
        r = ops.add_comment(self.conn, target_kind, target_id, author, body, urgent, replan)
        self._changed("comment", target_kind=target_kind, target_id=target_id)
        return r

    @_translate
    def list_comments(self, target_kind, target_id):
        return ops.list_comments(self.conn, target_kind, target_id)

    @_translate
    def ask_question(self, target_kind, target_id, title, body="", options=(), allow_text=True,
                     needs_owner=False, actor="dispatcher"):
        qid = ops.ask_question(self.conn, target_kind, target_id, title, body, options,
                               allow_text, actor, needs_owner)
        self._changed("question.asked", id=qid)
        return ops.get_question(self.conn, qid)

    @_translate
    def answer_question(self, qid, choice=None, text=None):
        q = ops.answer_question(self.conn, qid, choice, text, actor="owner")
        if q["target_kind"] == "epic" and q["target_id"] == ops.get_setting(self.conn, "tuner_epic"):
            cfg = config.get()
            decisions.record(config.state_dir(cfg), q["title"], text or f"Owner chose {choice}.",
                             {"choice": choice, "question": qid, "policy_version": ops.current_policy(self.conn)},
                             "owner", sync_settings=cfg["config_sync"])
        self._changed("question.answered", id=qid)
        return q

    def list_questions(self, open_only=False):
        if open_only:
            return ops.open_questions(self.conn)
        rows = self.conn.execute("select id from questions order by id").fetchall()
        return [ops.get_question(self.conn, r[0]) for r in rows]

    @_translate
    def record_decision(self, **f):
        did = ops.record_decision(self.conn, **f)
        self._changed("decision.recorded", id=did)
        return ops.get_decision(self.conn, did)

    @_translate
    def decision_outcome(self, did, outcome):
        ops.record_outcome(self.conn, did, outcome)
        self._changed("decision.outcome", id=did)
        return ops.get_decision(self.conn, did)

    def list_decisions(self):
        rows = self.conn.execute("select id from decisions order by id").fetchall()
        return [ops.get_decision(self.conn, r[0]) for r in rows]

    # -- settings, heartbeat ---------------------------------------------
    def get_settings(self):
        rows = self.conn.execute(
            "select key, value from settings where key not like 'seq\\_%' escape '\\' "
            "and key not like '\\_%' escape '\\'").fetchall()
        return {r[0]: r[1] for r in rows}

    def update_settings(self, patch, author="owner"):
        for k, v in patch.items():
            if not k or k.startswith(("seq_", "_")):
                raise BadRequest(f"reserved setting {k!r}")
            ops.set_setting(self.conn, k, v)
        if patch:
            cfg = config.get()
            state = config.state_dir(cfg)
            state.mkdir(parents=True, exist_ok=True)
            (state / "settings.json").write_text(json.dumps(self.get_settings(), indent=2) + "\n", encoding="utf-8")
            reason = ("Setup detection refreshed tool locations." if author == "wizard"
                      else f"{author.capitalize()} changed hub settings.")
            decisions.record(state, "Settings", reason,
                             {"settings": patch, "policy_version": ops.current_policy(self.conn)}, author,
                             sync_settings=cfg["config_sync"])
        self._changed("settings", keys=list(patch))
        return self.get_settings()

    def _json_setting(self, key, default=None):
        v = ops.get_setting(self.conn, key)
        return json.loads(v) if v else default

    def _put_json_setting(self, key, value):
        ops.set_setting(self.conn, key, json.dumps(value))

    def put_heartbeat(self, payload, ts=None):
        hb = {"ts": ts if ts is not None else time.time(), **payload}
        self._put_json_setting("_heartbeat", hb)
        self._changed("heartbeat", ts=hb["ts"])
        return hb

    def get_heartbeat(self):
        return self._json_setting("_heartbeat")

    # -- dispatch pause toggle (the contract with the pipeline slice) -----
    def dispatch_enabled(self):
        """False unless the owner turned dispatch on: a fresh ledger dispatches nothing."""
        v = ops.get_setting(self.conn, "dispatch_enabled")
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    def set_dispatch_enabled(self, enabled, actor="owner"):
        """Owner action. The caller must have authenticated the owner."""
        enabled = bool(enabled)
        ops.set_setting(self.conn, "dispatch_enabled", "true" if enabled else "false")
        ops.event(self.conn, "dispatch.enabled" if enabled else "dispatch.paused", actor, {"enabled": enabled})
        self._changed("dispatch", enabled=enabled)
        return enabled

    @_translate
    def approve_epic(self, epic_id):
        """Owner approval of every proposed task in one epic ('' or None: tasks with no epic).
        Each goes through approve(), so nothing becomes ready without the owner path."""
        if epic_id:
            ops.get_epic(self.conn, epic_id)
        want = epic_id or None
        ids = [t["id"] for t in ops.list_tasks(self.conn) if t["status"] == "proposed" and t["epic_id"] == want]
        for tid in ids:
            self.approve(tid)
        return ids

    # -- board and import ------------------------------------------------
    def board_text(self):
        return ledger_board.render(self.conn)

    def import_tasks(self, items, dry_run=True, actor="dispatcher"):
        """Bulk-create `proposed` tasks atomically. Duplicates (repo+epic+title) are skipped.

        An item may name an epic by title (created on demand) and deps by existing task id.
        """
        conn = self.conn
        table, created = [], []
        try:
            with tx(conn):
                epics = {(e["repo"], e["title"].lower()): e["id"] for e in ops.list_epics(conn)}
                seen = {(t["repo"], t["epic_id"], t["title"].lower())
                        for t in ops.list_tasks(conn, include_archived=True)}
                for i, it in enumerate(items):
                    row = self._import_one(conn, i, it, epics, seen, dry_run, actor)
                    table.append(row)
                    if row.get("id"):
                        created.append(row["id"])
                if dry_run:
                    raise _Rollback
        except _Rollback:
            pass
        if created:
            self._changed("import", ids=created)
        return {"dry_run": dry_run, "table": table, "created": created,
                "counts": {a: sum(1 for r in table if r["action"] == a)
                           for a in ("create", "duplicate", "invalid")}}

    def _import_one(self, conn, i, it, epics, seen, dry_run, actor):
        title = (it.get("title") or "").strip()
        repo = it.get("repo") or "default"
        epic_name = it.get("epic") or ""
        row = {"index": i, "title": title, "epic": epic_name, "repo": repo,
               "action": "create", "reason": "", "priority": it.get("priority", "P2")}
        if not title:
            return {**row, "action": "invalid", "reason": "title required"}
        epic_id = epics.get((repo, epic_name.lower())) if epic_name else None
        if (repo, epic_id, title.lower()) in seen:
            return {**row, "action": "duplicate", "reason": "already in ledger or batch"}
        previous_epics = dict(epics)
        conn.execute("SAVEPOINT import_row")
        try:
            if epic_name and epic_id is None:
                epic_id = ops.create_epic(conn, repo, epic_name, actor=actor)["id"]
                epics[(repo, epic_name.lower())] = epic_id
            fields = {k: it[k] for k in ("repo_path", "goal", "acceptance", "scope_files", "priority",
                                         "size", "risk", "deps", "related", "kind", "write", "read_access", "branch", "description") if k in it}
            t = ops.create_task(conn, title, repo, epic_id=epic_id, status="proposed",
                                actor=actor, **fields)
        except BaseException as e:
            conn.execute("ROLLBACK TO import_row")
            epics.clear()
            epics.update(previous_epics)
            if isinstance(e, (ValueError, LedgerNotFound)):
                return {**row, "action": "invalid", "reason": str(e)}
            raise
        finally:
            conn.execute("RELEASE import_row")
        seen.add((repo, epic_id, title.lower()))
        row["id"] = t["id"]
        return row
