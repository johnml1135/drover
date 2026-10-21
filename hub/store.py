"""The only module that talks to the ledger (drover.ledger).

`Store` gives the API a small, stable surface and publishes a bus event after
every mutation. `UiView` is the synchronous contract hub.web expects.
SQLite connections are per thread (the ledger's connect() is thread-bound);
the ledger's own BEGIN IMMEDIATE transactions make writes and claims atomic.
"""
import json
import threading
import time

from drover import config, contract, decisions, dispatch_repos, importer, landing, origin, repos, stats, usage
from drover.ledger import board as ledger_board
from drover.ledger import connect, ops, tx
from drover.ledger.ops import NotFound as LedgerNotFound
from drover.ledger.states import IllegalTransition


class NotFound(LookupError):
    pass


class BadRequest(ValueError):
    pass


class Forbidden(PermissionError):
    pass


def _translate(fn):
    """Map ledger exceptions onto the hub's three error types."""
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except LedgerNotFound as e:
            raise NotFound(str(e)) from e
        except ops.NotApproved as e:
            raise BadRequest(str(e)) from e
        except PermissionError as e:
            raise Forbidden(str(e)) from e
        except (IllegalTransition, ValueError) as e:
            raise BadRequest(str(e)) from e
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


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
                pass  # opened by another thread
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


def _run_id(run_id):
    """A positive integer run id, spelled plainly; anything else is NotFound."""
    if not str(run_id).isascii() or not str(run_id).isdigit() or int(run_id) < 1:
        raise NotFound(f"run {run_id}")
    return int(run_id)


class _Rollback(Exception):
    pass


class UiView:
    """The sync contract hub.web expects (see hub/web.py), over a Store.

    Only reachable through UI-authenticated routes, so mutations here act as the owner.
    """

    def __init__(self, store: Store, services=None, watchdog=None, dispatcher=None, runs_dir=None):
        self.s = store
        from .task_views import RepoStates
        self.repo_states = RepoStates()
        self.services = services      # hub.services.Services: garden queue, tools, probes
        self.watchdog = watchdog
        self.dispatcher = dispatcher  # hub.dispatch.DispatcherService
        self.runs_dir = runs_dir

    @property
    def c(self):
        return self.s.conn

    def _reason(self, t):
        for e in reversed(self.s.list_events(task_id=t["id"], kind="status", limit=20)):
            r = e["data"].get("reason")
            if r:
                return r
        return ""

    def last_updated(self):
        return time.strftime("%H:%M:%S")

    def inbox_questions(self):
        out = []
        tuner = {p["id"] for p in self.tuner_proposals() if p.get("question")}
        from drover.ledger.waiting import Waiting
        for q in Waiting(self.c, 'owner').questions():
            if str(q["id"]) in tuner or q['proposal'] or not q['visible']:
                continue
            opts = [{"id": o["label"], "label": o["label"], "recommended": o.get("recommended")}
                    for o in q["options"]]
            out.append({"id": str(q["id"]), "title": q["title"], "body": q["body"], "options": opts})
        return out

    def _task_view(self, task, configured=None):
        from .task_views import derive
        configured = repos.load_all() if configured is None else configured
        prog = configured.get(str(task.get("repo") or "").lower())
        tid = task["id"]
        from drover.ledger.board import waiting_labels
        from drover.ledger.waiting import Waiting
        labels = waiting_labels(self.c, Waiting(self.c).pending(("task", tid)))
        deps = ops.deps_of(self.c, tid)
        incoming = [dict(task_id=r[0], kind=r[1]) for r in self.c.execute(
            "SELECT task_id, kind FROM task_deps WHERE depends_on=? ORDER BY task_id", (tid,))]
        related = ops.related_of(self.c, tid)
        task = {**task, "state": task["status"], "waiting_labels": labels,
                "reason": "" if labels else self._reason(task),
                "can_accept_findings": ops.read_only_result(self.c, task) is not None,
                "deps": deps, "dependents": incoming, "related": related,
                "connected_count": len({d["depends_on"] for d in deps} |
                                       {d["task_id"] for d in incoming} | set(related))}
        task.pop("blocker", None)
        task.update(derive(task, prog, self.repo_states.get(prog) if prog else None))
        return task

    def task_detail(self, tid):
        task = self._task_view(self.s.get_task(tid))
        connections = []
        for label, entries in (("Depends on", [(d["depends_on"], d["kind"]) for d in task["deps"]]),
                               ("Depends on this", [(d["task_id"], d["kind"]) for d in task["dependents"]]),
                               ("Related", [(other, "related") for other in task["related"]])):
            for other, kind in entries:
                connections.append(dict(self.s.get_task(other), relation=label, link_kind=kind))
        return dict(task=task, connections=connections, runs=self.s.list_runs(tid),
                    events=self.s.list_events(task_id=tid, limit=100000))

    def edit_task(self, tid, **fields):
        return self.s.update_task(tid, actor="owner", **fields)

    def needs_owner_items(self):
        configured = repos.load_all()
        return [self._task_view(t, configured) for t in self.s.list_tasks(status="needs-owner")]

    def _low_confidence_ids(self):
        """Task ids whose import-task event carries the importer's low-confidence tag."""
        low = set()
        for e in ops.events(self.c, kind="import-task"):
            if e["task_id"] is not None:
                (low.add if "low-confidence" in (e["data"].get("tags") or []) else low.discard)(e["task_id"])
        return low

    def import_proposals(self):
        epics = {e["id"]: e["title"] for e in self.s.list_epics()}
        low = self._low_confidence_ids()
        configured = repos.load_all()
        return [{**self._task_view(t, configured), "repo": t["repo"] or "",
                 "reason": epics.get(t["epic_id"], ""), "epic_id": t["epic_id"] or "",
                 "epic": epics.get(t["epic_id"], ""), "low_confidence": t["id"] in low}
                for t in self.s.list_tasks(status="proposed")]

    def tuner_proposals(self):
        """Open tuner questions (tuner.py asks one ledger question per proposal, on its own epic), then any
        legacy list stored in the tuner_proposals setting."""
        eid = ops.get_setting(self.c, "tuner_epic")
        qs = [q for q in self.s.list_questions(open_only=True)
              if eid and q["target_kind"] == "epic" and q["target_id"] == eid]
        out = [{"id": str(q["id"]), "title": q["title"], "summary": q["body"], "question": True,
                "options": [{"id": o["label"], "label": o["label"], "recommended": o.get("recommended")}
                            for o in q["options"]]} for q in qs]
        return out + self.s._json_setting("tuner_proposals", [])

    def garden_batches(self):
        return self.s._json_setting("garden_batches", [])

    def failed_blocked_items(self):
        out = []
        configured = repos.load_all()
        for t in self.s.list_tasks(status="failed,blocked"):
            task = self._task_view(t, configured)
            out.append({**task, "reason": "; ".join(task["waiting_labels"]) or self._reason(t)})
        return out

    def stats(self, since=None, task=None):
        return self.s.stats(since, task)

    def seat_order(self):
        return self.s.seat_order()

    def usage_alerts(self):
        return self.s.usage_alerts()

    def set_usage(self, seat, window, pct, resets):
        return self.s.set_usage(seat, window, pct, resets)

    def archived_tasks(self):
        return [t for t in self.s.list_tasks(limit=100000, include_archived=True) if t["archived"]]

    def archive_task(self, tid, archived=True):
        return self.s.archive_task(tid, archived)

    def delete_task(self, tid, confirm=False):
        return self.s.delete_task(tid, confirm)

    def task(self, tid):
        return self.s.get_task(tid)

    def plan_epics(self):
        tasks = self.s.list_tasks(limit=100000)
        low = self._low_confidence_ids()
        by_epic: dict = {}
        configured = repos.load_all()
        for t in tasks:
            by_epic.setdefault(t["epic_id"], []).append(
                {**self._task_view(t, configured), "low": "1" if t["id"] in low else None})
        out = [{"id": e["id"], "title": e["title"], "tasks": by_epic.pop(e["id"], [])}
               for e in self.s.list_epics()]
        if by_epic.get(None):
            out.append({"id": "", "title": "(no epic)", "tasks": by_epic[None]})
        return out

    def runs(self):
        rows = self.c.execute(
            "select r.*, t.title as task_title from runs r join tasks t on t.id=r.task_id "
            "where t.archived=0 order by r.id desc limit 50").fetchall()
        out = []
        for r in rows:
            toks = (r["tokens_in"] or 0) + (r["tokens_out"] or 0)
            out.append({"id": str(r["id"]), "task": r["task_title"], "model": r["model"] or "",
                        "effort": r["effort"] or "", "backend": r["backend"],
                        "tokens": toks or "", "allowance_used": contract.format_allowance(r["allowance_used"]),
                        "outcome": r["outcome"] or "running",
                        "wall_time": f"{r['wall_s']:.0f}s" if r["wall_s"] is not None else ""})
        return out

    def decider_accuracy(self):
        groups: dict[str, list] = {}
        for d in self.s.list_decisions():
            groups.setdefault(d["question_name"], []).append(d)

        def pct(ds):
            done = [d for d in ds if d["outcome"] is not None]
            good = [d for d in done if d["outcome"] in (True, "good", "correct", "ok", d["answer"])]
            return f"{100 * len(good) // len(done)}%" if done else "-"

        return [{"question": q, "shadow": pct([d for d in ds if d["shadow"]]),
                 "live": pct([d for d in ds if not d["shadow"]]), "n": len(ds)}
                for q, ds in groups.items()]

    def landing(self):
        return {"queue": [{"id": t["id"], "title": t["title"], "state": t["status"]}
                          for t in self.s.list_tasks(status="gated,reviewed")],
                "batch": self.s._json_setting("landing_batch"),
                "gate": self.s._json_setting("gate", {}),
                "repos": [{"name": repos.repo_name(prog), "ahead": count}
                          for prog in repos.load_all().values()
                          if (count := origin.ahead(repos.native(repos.repo_path(prog)), repos.remote_name(prog), repos.default_branch(prog)))]}

    def push_main(self, name):
        """Owner action; repo paths and remote names come only from validated configuration."""
        prog = repos.load_all().get(name.lower())
        if prog is None:
            raise NotFound("unknown repo")
        try:
            origin.push_main(repos.native(repos.repo_path(prog)), repos.remote_name(prog), repos.default_branch(prog))
        except origin.SyncError as e:
            ops.ask_question(self.c, 'epic', landing._epic(self.c, 'Landing'), 'Push main failed', str(e),
                             options=[{'label': 'Acknowledged'}], actor='owner')
            self.s._changed("repo.push-failed", repo=name)
            raise BadRequest(str(e)) from e
        self.s._changed("repo.pushed", repo=name)

    def health(self):
        hb = self.s.get_heartbeat() or {}
        res = hb.get("resources") or {}
        if isinstance(res, dict):
            res = [{"name": k, "verdict": (v.get("verdict", "") if isinstance(v, dict) else ""),
                    "value": (v.get("value", v) if isinstance(v, dict) else v)}
                   for k, v in res.items()]
        alw = hb.get("allowance") or []
        if isinstance(alw, dict):
            alw = [dict(v, plan=k) if isinstance(v, dict) else {"plan": k, "used_pct": v}
                   for k, v in alw.items()]
        wd = [e for e in self.s.list_events(limit=500) if e["kind"].startswith("watchdog.")]
        out = {"resources": res, "allowance": alw,
               "heartbeat_age": f"{time.time() - hb['ts']:.0f}s" if hb else "unknown",
               "watchdog": [{"at": e["at"][11:19], "action": e["data"].get("message", e["kind"])}
                            for e in reversed(wd[-10:])],
               "dispatch": self.dispatch_state(), "dispatcher": self._dispatcher_state(hb),
               "keepalive": self._keepalive_state(), "tools": self._tools()}
        if self.services is not None:
            out["ollama"] = self.services.ollama_status()
            out["disks"] = self.services.disk_free()
        return out

    def _dispatcher_state(self, hb):
        on = self.s.dispatch_enabled()
        running = bool(self.dispatcher and self.dispatcher.running)
        age = time.time() - hb["ts"] if hb else None
        return {"state": "paused" if not on else ("running" if running else "enabled, thread not running"),
                "thread": running, "heartbeat_age": f"{age:.0f}s" if age is not None else "unknown",
                "error": getattr(self.dispatcher, "error", None) if self.dispatcher else None}

    def _keepalive_state(self):
        w = self.watchdog
        if w is None:
            return {"state": "unknown", "needed": False}
        needed = w.wsl_needed()
        alive = w.keepalive_alive()
        return {"state": "holding WSL up" if alive else ("needed, not up" if needed else "idle"),
                "needed": needed, "alive": alive}

    def _tools(self):
        s = self.s.get_settings()
        return {"claude": s.get("wsl_bin_claude", ""), "codex": s.get("wsl_bin_codex", "")}

    def _target(self, kind, iid):
        """Resolve a UI item (task/import/owner/epic/question/run) to a comment target."""
        if kind == "epic":
            return "epic", iid
        if kind in ("task", "import", "owner"):
            return "task", iid
        if kind == "question":
            q = ops.get_question(self.c, int(iid))
            return q["target_kind"], q["target_id"]
        if kind == "run":
            row = self.c.execute("select task_id from runs where id=?", (int(iid),)).fetchone()
            if row is None:
                raise NotFound(f"run {iid}")
            return "task", row[0]
        raise BadRequest(f"unknown kind {kind}")

    def comments(self, kind, iid):
        tk, tid = self._target(kind, iid)
        return [{"author": c["author"], "at": c["at"][11:16], "body": c["body"],
                 "urgent": bool(c["urgent"]), "replan": bool(c["replan"])}
                for c in self.s.list_comments(tk, tid)]

    def add_comment(self, kind, iid, body, urgent=False, replan=False):
        tk, tid = self._target(kind, iid)
        return self.s.add_comment(tk, tid, "owner", body, urgent, replan)

    def answer_question(self, qid, option=None, text=None):
        return self.s.answer_question(int(qid), option, text)

    def _drop(self, key, iid):
        left = [i for i in self.s._json_setting(key, []) if str(i.get("id")) != str(iid)]
        self.s._put_json_setting(key, left)

    def decide_item(self, kind, iid, decision):
        if decision not in ("approve", "reject"):
            raise BadRequest(decision)
        if kind in ("task", "owner", "import"):
            t = self.s.get_task(iid)
            if decision == "reject":
                self.s.set_status(iid, "cancelled", "owner", "owner rejected")
            elif t["status"] in ("proposed", "draft"):
                self.s.approve(iid)
            elif ops.read_only_result(self.c, t) is not None:
                self.s.accept_read_only(iid)
            else:
                self.s.set_status(iid, "ready", "owner", "owner approved")
        elif kind == "garden" and decision == "approve":
            self.approve_garden(iid)
        elif kind in ("tuner", "garden"):
            self._drop("tuner_proposals" if kind == "tuner" else "garden_batches", iid)
            self.s.add_event(f"{kind}.{decision}", "owner", {"id": iid})
            self.s.publish("ledger", {"kind": f"{kind}.{decision}"})
        else:
            raise BadRequest(f"unknown kind {kind}")

    def approve_garden(self, batch, branches=None):
        """Owner approval of a garden batch (all rows, or the named subset). The exact approved rows are written
        by importer.record_approval; the executor later reads them back and nothing else."""
        self.garden_batch(batch)  # only an open proposal can be approved, once
        try:
            rows = importer.record_approval(self.c, batch, "owner", branches)
        except ops.NotFound as e:
            raise NotFound(str(e)) from e
        except ValueError as e:
            raise BadRequest(str(e)) from e
        self._drop("garden_batches", batch)
        if branches is not None:  # unticked rows stay open as a remainder batch with its own question
            left = {r["branch"] for r in self.garden_entries(batch)} - {r["branch"] for r in rows}
            importer.repropose(self.c, batch, left)
        self.s.publish("ledger", {"kind": "garden.approve", "id": batch, "rows": len(rows)})
        if self.services is not None:
            self.services.submit_garden(batch)
        return rows

    def garden_entries(self, batch):
        for e in reversed(ops.events(self.c, kind="import-batch")):
            if e["data"].get("id") == batch:
                return e["data"]["entries"]
        return []

    def garden_batch(self, batch):
        for b in self.s._json_setting("garden_batches", []):
            if str(b.get("id")) == str(batch):
                return b
        raise NotFound(f"garden batch {batch}")

    def approve_imports(self, ids):
        for i in ids:
            self.s.approve(i)

    def approve_epic(self, epic_id):
        return self.s.approve_epic(epic_id)

    def reorder(self, kind, iid, before=None, after=None):
        self.s.reorder(kind, iid, before, after)

    # -- pause toggle ----------------------------------------------------
    def dispatch_state(self):
        on = self.s.dispatch_enabled()
        try:
            repos = list(dispatch_repos.get(lambda k, d=None: ops.get_setting(self.s.conn, k, d)) or ())
        except ValueError:
            repos = ["INVALID: all blocked"]
        return {"enabled": on, "label": "RUNNING" if on else "PAUSED", "repos": repos}

    def set_dispatch(self, enabled):
        return self.s.set_dispatch_enabled(enabled, actor="owner")

    # -- run detail ------------------------------------------------------
    def run_detail(self, run_id):
        rid = _run_id(run_id)
        r = self.c.execute("select r.*, t.title as task_title from runs r join tasks t on t.id=r.task_id "
                           "where r.id=?", (rid,)).fetchone()
        if r is None:
            raise NotFound(f"run {run_id}")
        return {"id": str(r["id"]), "task": r["task_title"], "task_id": r["task_id"], "model": r["model"] or "",
                "effort": r["effort"] or "", "backend": r["backend"], "role": r["role"] if "role" in r.keys() else "",
                "outcome": r["outcome"] or "running", "live": r["outcome"] is None,
                "allowance_used": contract.format_allowance(r["allowance_used"]),
                "wall_time": f"{r['wall_s']:.0f}s" if r["wall_s"] is not None else ""}

    def run_tail(self, run_id, max_bytes=65536):
        """The end of <runs>/<id>/out.log as text. Read-only; the template escapes it."""
        from drover import runner
        rid = _run_id(run_id)
        if self.runs_dir is None:
            return ""
        path = runner.run_dir(self.runs_dir, rid) / "out.log"
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - max_bytes))
                data = f.read()
        except OSError:
            return ""
        text = data.decode("utf-8", "replace")
        return ("... (earlier output not shown)\n" if size > max_bytes else "") + text
