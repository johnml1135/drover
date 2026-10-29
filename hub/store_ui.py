import logging
import time
import threading
from concurrent.futures import ThreadPoolExecutor

from drover import contract, dispatch_repos, importer, landing, origin, repos
from drover.ledger import ops

from .store_common import BadRequest, NotFound, _run_id
from .store_ledger import Store

REPO_TTL = 30  # seconds before ui_repos() starts a background refresh

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
        self._repo_lock, self._repo_cache, self._repo_at = threading.Lock(), None, 0
        self._repo_gen, self._repo_pending, self._repo_pending_gen = 0, None, 0
        self._repo_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='repo-config')
    def close(self):
        self.repo_states.close()
        self._repo_pool.shutdown(wait=False, cancel_futures=True)
    def invalidate_repos(self):
        """A change event: the next ui_repos() waits for a fresh load, and a load already running
        (started before the change) is discarded when it finishes."""
        with self._repo_lock:
            self._repo_gen += 1
            self._repo_at = None
    def _load_repos(self, gen):
        try:
            loaded = repos.load_all()
        except Exception:
            logging.getLogger("hub").exception("repo config refresh failed")
            with self._repo_lock:
                if self._repo_cache is not None and gen == self._repo_gen:
                    self._repo_at = time.monotonic()  # back off: keep the last good copy for a full TTL
            raise
        with self._repo_lock:
            if gen == self._repo_gen:
                self._repo_cache, self._repo_at = loaded, time.monotonic()
        return loaded
    def refresh_repos(self):
        """Start one load for the current generation unless it is already running; never blocks."""
        with self._repo_lock:
            if self._repo_pending is None or self._repo_pending.done() or self._repo_pending_gen != self._repo_gen:
                self._repo_pending_gen = self._repo_gen
                self._repo_pending = self._repo_pool.submit(self._load_repos, self._repo_gen)
            return self._repo_pending
    def ui_repos(self):
        """Fresh -> the cache. TTL expiry -> the last good copy now, refreshed in the background (one at a time).
        Cold or invalidated by a change event -> wait for a load that started after the change."""
        cache, at = self._repo_cache, self._repo_at
        if cache is not None and at is not None:
            if time.monotonic() - at >= REPO_TTL:
                self.refresh_repos()
            return cache
        while True:
            try:
                self.refresh_repos().result()
            except Exception:
                if cache is None:
                    raise
                return cache
            if self._repo_at is not None:
                return self._repo_cache
    def inbox_count(self):
        counts = self.c.execute("SELECT status, count(*) FROM tasks WHERE archived=0 "
                                "AND status IN ('needs-owner','proposed','failed','blocked') GROUP BY status").fetchall()
        tasks = {row[0]: row[1] for row in counts}
        return (min(tasks.get('needs-owner', 0), 500) + min(tasks.get('proposed', 0), 500)
                + min(tasks.get('failed', 0) + tasks.get('blocked', 0), 500)
                + len(self.inbox_questions()) + len(self.notification_attention())
                + len(self.tuner_proposals()) + len(self.garden_batches()))

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

    def notification_attention(self):
        from drover.notifications import Notifications
        return Notifications(self.c).owner_attention()

    def inbox_questions(self):
        out = []
        tuner = {p["id"] for p in self.tuner_proposals() if p.get("question")}
        from drover.ledger.waiting import Waiting
        for q in Waiting(self.c, 'owner').questions():
            if str(q["id"]) in tuner or q['proposal'] or not q['visible']:
                continue
            opts = [{"id": o["label"], "label": o["label"], "recommended": o.get("recommended")}
                    for o in q["options"]]
            out.append({"id": str(q["id"]), "title": q["title"], "body": q["body"], "options": opts,
                        "target_kind": q["target_kind"], "target_id": q["target_id"]})
        return out

    def _task_view(self, task, configured=None):
        from .task_views import derive
        configured = self.ui_repos() if configured is None else configured
        prog = configured.get(str(task.get("repo") or "").lower())
        tid = task["id"]
        from drover.ledger.board import waiting_labels
        from drover.ledger.waiting import Waiting
        labels = waiting_labels(self.c, Waiting(self.c).pending(("task", tid)))
        deps = ops.deps_of(self.c, tid)
        incoming = [dict(task_id=r[0], kind=r[1]) for r in self.c.execute(
            "SELECT task_id, kind FROM task_deps WHERE depends_on=? ORDER BY task_id", (tid,))]
        related = ops.related_of(self.c, tid)
        from drover.notifications import Notifications
        groups = Notifications(self.c).task_groups(tid)
        labels += ["Waiting on group " + g["group_id"] + ": " + g["readiness_reason"] for g in groups if not g["ready"]]
        task = {**task, "groups": groups, "state": task["status"], "waiting_labels": labels,
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
        configured = self.ui_repos()
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
        configured = self.ui_repos()
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
        configured = self.ui_repos()
        for t in self.s.list_tasks(status="failed,blocked"):
            task = self._task_view(t, configured)
            out.append({**task, "reason": "; ".join(task["waiting_labels"]) or self._reason(t)})
        return out

    def stats(self, since=None, task=None, series_only=False):
        return self.s.stats(since, task, series_only)

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
        configured = self.ui_repos()
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
                "repos": [{"name": repos.repo_name(prog), "ahead": snapshot.get("ahead") if snapshot else None,
                           "checking": snapshot is None}
                          for prog in self.ui_repos().values()
                          if (snapshot := self.repo_states.get(prog)) is None or snapshot.get("ahead") != 0]}
    def push_main(self, name):
        """Owner action; repo paths and remote names come only from validated configuration."""
        from drover.notifications.controls import admission
        control = admission(self.c)
        def guard():
            current = admission(self.c, epoch=control['epoch'])
            if not current['allowed']:
                raise BadRequest(current['reason'])
        guard()
        prog = repos.load_all().get(name.lower())
        if prog is None:
            raise NotFound("unknown repo")
        try:
            origin.push_main(repos.native(repos.repo_path(prog)), repos.remote_name(prog), repos.default_branch(prog),
                             guard=guard)
        except origin.SyncError as e:
            ops.ask_question(self.c, 'epic', landing._epic(self.c, 'Landing'), 'Push main failed', str(e),
                             options=[{'label': 'Acknowledged'}], actor='owner')
            self.s._changed("repo.push-failed", repo=name)
            raise BadRequest(str(e)) from e
        self.s._changed("repo.pushed", repo=name)
    def health(self):
        from drover.notifications import Notifications
        notification_health = Notifications(self.c).health()
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
        out = {"notifications": notification_health, "resources": res, "allowance": alw,
               "heartbeat_age": f"{time.time() - hb['ts']:.0f}s" if hb else "unknown",
               "watchdog": [{"at": e["at"][11:19], "action": e["data"].get("message", e["kind"])}
                            for e in reversed(wd[-10:])],
               "dispatch": self.dispatch_state(), "dispatcher": self._dispatcher_state(hb),
               "keepalive": self._keepalive_state(), "tools": self._tools()}
        if self.services is not None:
            out["ollama"], out["disks"] = self.services.health_snapshot
            at = self.services.health_snapshot_at
            out["snapshot_age"] = max(0, int(self.services.clock() - at)) if at is not None else None
            try:
                bench = self.s._json_setting("ollama_bench")
                if bench is not None:
                    out["ollama"] = {**out["ollama"], "p95_ms": bench.get("p95_ms")}
            except (ValueError, AttributeError):
                out["ollama"] = {**out["ollama"], "p95_ms": None}
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
        if kind == "director":
            from drover.notifications import Notifications
            Notifications(self.c).role_state(iid)
            return kind, iid
        if kind == "group":
            from drover.notifications import Notifications
            Notifications(self.c).get_group(iid)
            return kind, iid
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
