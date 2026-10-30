"""Dispatcher admission steps."""
import json
from datetime import datetime, timezone
from pathlib import Path

from . import contract, placement, repos, task_access
from . import config as config_mod
from . import launch as launch_mod
from . import route as route_mod
from .pipeline import WorktreeRefused, truthy

from .ci_common import INFRA_RETRIES
FAIL_OUTCOMES = contract.FAIL_OUTCOMES   # worker failures; launch-error/orphaned never count
WALL_MIN = {"S": 30, "M": 60, "L": 120}
IDLE_MIN = 10
MAX_LAUNCH_ERRORS = 3
MAX_PROVIDER_ERRORS = 5     # provider errors in a row (never counted as task failures) before the owner is alerted
CLAIM_STALE_S = 300
SCAN_EVERY_S = 60            # how often live runs are checked against the process list
ORPHAN_SCANS = 2             # consecutive clean scans that miss a run's runner before it is orphaned
BACKOFF_MAX_MIN = 30
UNAVAILABLE_S = 600          # a missing binary is re-probed (tried again) after this
MAX_FINISH_TRIES = 3
CHEAP_MODELS = ("haiku", "luna")
MAX_ROTATIONS = 3
EXCERPT_LINES = contract.EXCERPT_LINES
MAX_REVIEW_TRIES = 3         # reviews that produce no usable verdict in a row before the owner is asked
MAX_GATE_LAUNCH_ERRORS = 3
PENDING_KEY = "_pending_outcomes"
ENV_BLOCK_KINDS = ("verification_environment", "missing_windows_worker", "host_verification")



def ladders():
    """{backend: [model, ...] cheapest first} from the config's models.ladders."""
    return config_mod.get()["models"]["ladders"]


def start_idx():
    return config_mod.get()["models"]["start"]


def seat_of(route):
    """The config seat a route names, else the backend's best seat; None when there is none."""
    seat = config_mod.seat_by_name(route.get("seat")) if route.get("seat") else None
    if seat is None:
        seats = config_mod.seats_for(route.get("backend"))
        seat = seats[0] if seats else None
    return seat


def placement_for(task, route, worktree=None, platforms=None):
    """'wsl' or 'windows'. A Codex seat runs where its seat is (WSL; config refuses Windows); a WSL Claude seat runs in
    WSL; a linux platform tag (or the router's platform) means WSL; the repo supplies its default and
    the repo allows, else WSL (owner rule: windows + claude, or wsl + claude or codex); Claude otherwise runs on
    Windows (unless the worktree is Linux-only)."""
    seat = seat_of(route)
    plat = str(route.get("platform") or "").lower() or route_mod.task_platform(task)
    if route.get("backend") == "codex":
        return seat["placement"] if seat else "wsl"
    if plat == "linux":
        return "wsl"
    if plat == "windows":
        return "windows"           # a WSL seat cannot honour this: place() sends the task to the owner
    if seat and seat["placement"] == "wsl":
        return "wsl"
    return "wsl" if worktree and placement.is_linux_native(worktree) else "windows"


def place(task, route, worktree, prog):
    """(placement, None), or (None, reason) for incompatible task, seat and repo platforms."""
    plats = repos.run_config(prog)["platforms"]
    want = placement_for(task, route, worktree, plats)
    seat = seat_of(route)
    if want == "windows" and worktree and placement.is_linux_native(worktree):
        # A worktree on WSL ext4 (ADR 0007) cannot be opened by a Windows worker: say so rather than launch one.
        return None, f"needs windows but its worktree {worktree} is on the WSL filesystem"[:300]
    allowed = route_mod.seat_placements(seat) if seat else config_mod.PLACEMENTS
    hard = bool(route_mod.task_platform(task) or route.get("platform") ) \
        or route.get("backend") == "codex" or bool(worktree and placement.is_linux_native(worktree))
    default = repos.run_config(prog)["default"]
    if not hard and default in plats and default in allowed:
        return default, None
    if want in plats and want in allowed:
        return want, None
    options = [x for x in dict.fromkeys([default, *plats]) if x in plats and x in allowed]
    if options and not hard:
        return options[0], None
    if want not in allowed:
        return None, f"needs {want} but seat {seat['name']} runs only in {', '.join(allowed)}"[:300]
    name = (prog or {}).get("name") or "the repo"
    return None, f"needs {want} but {name} runs only on {', '.join(plats)}"[:300]


def effort_for(model, effort=None):
    """Cheap-rung models (Haiku, Luna) always run at the cheap tier's effort; others take the router's effort, else
    their tier's (config models.efforts)."""
    cfg = config_mod.get()
    cheap = any(model in ladder[:1] for ladder in cfg["models"]["ladders"].values()) \
        or any(m in model.lower() for m in CHEAP_MODELS)
    return cfg["models"]["efforts"]["cheap"] if cheap else effort or config_mod.tier_effort(model, cfg)


def fail_runs(runs, after_run=0):
    """Unique substantive worker failures in the current retry window."""
    out, seen, worker = [], set(), None
    for r in runs:
        role = r.get('role', 'worker')
        if role == 'worker':
            worker = r
            failed = r['outcome'] in FAIL_OUTCOMES
        else:
            failed = role == 'review' and r['outcome'] == contract.HOLD and worker is not None
        if failed and worker['id'] > after_run and worker['id'] not in seen:
            out.append(worker)
            seen.add(worker['id'])
    return out


def failures(runs, after_run=0):
    return len(fail_runs(runs, after_run))


def next_tier(runs, base, after_run=0):
    """Step up once per two distinct substantive worker failures."""
    fails = fail_runs(runs, after_run)
    last = fails[-1] if fails else None
    if not last or last['backend'] not in ladders():
        return base
    ladder = ladders()[last['backend']]
    idx = ladder.index(last['model']) if last['model'] in ladder else start_idx()['M']
    idx = min(idx + (1 if len(fails) % 2 == 0 else 0), len(ladder) - 1)
    return {'backend': last['backend'], 'model': ladder[idx]}


def at_top(run):
    ladder = ladders().get(run["backend"], [])
    return not ladder or run["model"] not in ladder or ladder.index(run["model"]) == len(ladder) - 1


def _epoch(stamp):
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


class AdmissionSteps:
    def _seat_load(self):
        """{seat name: live non-gate runs}; legacy runs with no seat count on their backend's best seat."""
        load = {}
        for _, _, spec in self.live.values():
            if spec.get("role") == "gate":
                continue
            name = spec.get("seat") or (seat_of(spec) or {}).get("name")
            load[name] = load.get(name, 0) + 1
        # Durable associations count even when adoption or termination is unknown.
        for row in self.port.conn.execute("SELECT spec FROM director_runs WHERE state<>'finished'"):
            name = json.loads(row['spec'])['seat']
            load[name] = load.get(name, 0) + 1
        return load

    def _launch_guard(self, task=None):
        if not truthy(self.port.get_setting('dispatch_enabled', 'false')):
            raise launch_mod.AdmissionHeld('dispatch paused')
        return self.port.require_admission(self._control_epoch, task['id'] if task else None)

    def _launch_allowed(self, task=None):
        try:
            self._launch_guard(task)
            return True
        except launch_mod.AdmissionHeld as error:
            if task and self.port.get_task(task['id'])['status'] == 'claimed':
                self.port.set_status(task['id'], 'ready', reason=str(error))
            return False

    def _stop_for_control(self, epoch, changed_only=False):
        from .directors import stop_intent
        # Include durable launch intents even if a restart has not adopted them.
        specs = {rid: spec for rid, (_, _, spec) in self.live.items()}
        launches = {e['data'].get('run'): e['data'] for e in self.port.events('run-launching')}
        for task, run in self._open_runs():
            saved = launches.get(run['id'], {})
            specs.setdefault(run['id'], dict(run_id=run['id'], nonce=saved.get('nonce'),
                                             control_epoch=saved.get('control_epoch') or 0))
        for spec in specs.values():
            if changed_only and (spec.get('control_epoch') or 0) == epoch:
                continue
            stop_intent(self.port, spec, epoch, lambda value: self.launcher.cancel(value['run_id'], value['nonce']))

    def _director_admission(self, spec):
        seat = config_mod.seat_by_name(spec['seat'])
        if seat is None or seat['backend'] != spec['backend']:
            return 'seat-not-configured'
        if seat['name'] not in [s['name'] for s in config_mod.director_seats()]:
            return 'seat-not-authorized-for-director'
        if spec['placement'] not in route_mod.seat_placements(seat):
            return 'seat-placement-incompatible'
        if self.last_resources.get('verdict') != 'go':
            return 'resources-unavailable'
        if self._side_blocked(spec['placement']):
            return 'resources-unavailable'
        if spec['placement'] == 'wsl' and (not self.launcher.wsl_up() or self._wsl_blocked()):
            return 'wsl-unavailable'
        if seat['max_workers'] and self._seat_load().get(seat['name'], 0) >= seat['max_workers']:
            return 'seat-capacity-unavailable'
        if self._is_unavailable(spec['backend'], spec['placement']):
            return 'backend-unavailable'
        if not route_mod._seat_admits(seat, self.usage_poller.refresh(seat), protected=not seat['director']):
            return 'allowance-unavailable'
        return None

    def _route_inputs(self, task, admitted):
        """One router call shape: seat load, repo platforms and measured per-seat allowance."""
        prog = self.pipeline.repo(task)
        kw = {"seat_load": self._seat_load(), "platforms": repos.run_config(prog)["platforms"] if prog else None,
              **self._seat_route_kw()}
        kw["seat_headroom"] = {name: h for name, h in kw["seat_headroom"].items()
                               if config_mod.seat_by_name(name)["backend"] in admitted}
        return kw

    def _route(self, task, policy, admitted):
        t = task
        pin = task.get('model_pin') if task.get('role', 'worker') == 'worker' else None
        routing_data = policy['data']
        if pin:
            role_key = route_mod.route_key('worker', task.get('size'))
            routing_data = {**routing_data, 'routes': {**routing_data.get('routes', {}), role_key: [pin]}}
        base = self.route_fn(t, routing_data, **self._route_inputs(task, admitted))
        self._admission_report()
        if base.get("borrow_seat"):
            seat = base["borrow_seat"]
            from .ledger.waiting import OWNER_ANSWER, cause_key
            waiting = self.port.waiting
            cause = cause_key("borrow", task["id"], seat)
            answer = waiting.answer(cause)
            if answer is None:
                waiting.wait(("task", task["id"]), OWNER_ANSWER, cause_key(cause), cause=cause,
                             data={"mode": "needs-owner"})
                waiting.ask(cause, "task", task["id"], f"Allow director to borrow {seat}?",
                            "This approval applies to director work on this task and account.",
                            options=[{"label": "Approve borrowing"}, {"label": "Keep default seat"}], allow_text=False)
                return base
            approved = answer["choice"] == "Approve borrowing"
            t = {**t}
            t.pop("director_seat", None)
            if approved:
                t["director_seat"] = seat
            base = route_mod.choose(t, policy["data"], **self._route_inputs(task, admitted),
                                    director_borrow_approved=approved)
        if not base.get("backend"):
            return base
        if task.get("role") in route_mod.DIRECTOR_ROLES:
            return base
        route = pin or next_tier(self.port.list_runs(task["id"]), base, task.get('retry_after_run', 0))
        if route["model"] != base["model"]:
            route = {**route, "effort": None}   # the router's effort was picked for the cheaper model
        role_key = route_mod.route_key(task.get("role", "worker"), task.get("size"))
        data = {**policy["data"], "routes": {**policy["data"].get("routes", {}), role_key: [route]}}
        return route_mod.choose(t, data, seats=config_mod.get()["seats"],
                                **self._route_inputs(task, admitted))

    def _admission_report(self):
        from . import usage
        seats = config_mod.get()['seats']
        heads = {seat['name']: usage.headroom(self.usage_poller.conn, seat['name'], self.now()) for seat in seats}
        load = self._seat_load()
        rows = usage.order(self.usage_poller.conn, seats, now=self.now(), seat_load=load)
        errors = json.loads(self.port.get_setting('seat_health', '{}'))
        backends, worker_ok = {}, False
        for row in rows:
            seat = config_mod.seat_by_name(row['seat'])
            down = self._is_unavailable(seat['backend'], seat['placement'])
            health = errors.get(seat['name'])
            full = seat['max_workers'] and load.get(seat['name'], 0) >= seat['max_workers']
            allowance_ok = route_mod._seat_admits(seat, heads[seat['name']], protected=True)
            worker = allowance_ok and not full and not down and not health
            worker_ok |= worker
            if health:
                reason = f'seat health error: {health}'
            elif down:
                reason = 'backend unavailable'
            elif full:
                reason = 'max_workers reached'
            elif not allowance_ok:
                reason = route_mod._seat_admission_reason(seat, heads[seat['name']], protected=True)
            else:
                reason = row['reason']
            backends.setdefault(seat['backend'], []).append({'seat': seat['name'], 'admitted': bool(worker), 'reason': reason})
        # Usage priority can change without changing the hold. Keep its durable identity stable.
        for rows in backends.values():
            rows.sort(key=lambda row: row['seat'])
        report = {'held': not worker_ok, 'backends': backends if not worker_ok else {}}
        encoded = json.dumps(report, sort_keys=True)
        if self.port.get_setting('admission_hold') != encoded:
            self.port.set_setting('admission_hold', encoded)
            self.port.append_event('admission-hold' if report['held'] else 'admission-released', report)

    def _seat_tick(self):
        from . import stats
        self.usage_poller.poll(config_mod.get()["seats"], self._seat_load())
        current = stats.hour(self.now())
        if current != self._stats_hour:
            self._stats_resources = self.stats_resources_fn()
            self._stats_hour = current
        stats.sample(self.usage_poller.conn, self._stats_resources, self.now())
        stats.prune(self.usage_poller.conn, self.now(), getattr(self.launcher, "runs_dir", None))

    def _seat_headroom(self, backend):
        heads = [self.usage_poller.refresh(s) for s in config_mod.seats_for(backend)]
        return max((h for h in heads if h), key=lambda h: h["remaining_pct"], default=None)

    def _seat_route_kw(self):
        from . import usage
        seats = config_mod.get()["seats"]
        heads = {s["name"]: self.usage_poller.refresh(s) for s in seats}
        return {"seat_headroom": heads,
                "seat_order": usage.order(self.usage_poller.conn, seats, now=self.now(), seat_load=self._seat_load())}

    def _reading_for_route(self, route):
        if route.get("seat"):
            from . import usage
            seat = config_mod.seat_by_name(route["seat"])
            return usage.headroom(self.usage_poller.conn, seat["name"], self.now()) if seat else None
        return self._headroom(route["backend"])

    def _wsl_blocked(self):
        """Why WSL work must wait (the wsl tracker: distro down or memory near the .wslconfig cap), else ''."""
        w = (self.last_resources or {}).get("wsl") or {}
        return "; ".join(w.get("reasons") or ["WSL tracker"]) if w.get("verdict", "go") != "go" else ""

    def _side_blocked(self, placement):
        """Why work placed on this side must wait: that side's RAM is short while another side's is not, else ''."""
        w = ((self.last_resources or {}).get("side_admission") or {}).get(placement) or {}
        return "; ".join(w.get("reasons") or ["RAM"]) if w.get("verdict", "go") != "go" else ""

    def _headroom(self, plan):
        try:
            return self._seat_headroom(plan)
        except Exception:
            return None

    def _defer(self, task, why, *, reason_code="admission"):
        """Return a claim to ready and let Waiting own its durable retry deadline."""
        p, tid = self.port, task["id"]
        p.set_status(tid, "ready", reason=why)
        wait_id = p.waiting.delay(tid, "worker", reason_code, self.now())
        row = next(row for row in p.waiting.pending(("task", tid)) if row["id"] == wait_id)
        p.append_event("backoff", {"minutes": row["data"]["minutes"], "why": why}, tid)

    def _worktree(self, task, where=None):
        """The task's worktree path, or False when it could not have one (it is then needs-owner with the reason).
        `where` is the placement decided first; a new worktree is created on that side's filesystem."""
        try:
            if task.get("read_access"):
                task["read_checkouts"] = task_access.prepare(
                    task["read_access"], self.pipeline.all_repos(),
                    Path(self.pipeline.runs_dir).parent / "read-checkouts")
                task["read_dirs"] = [r["path"] for r in task["read_checkouts"]]
            if task.get("write") is False:
                if not task.get("read_dirs"):
                    raise WorktreeRefused("read-only task needs at least one read repository")
                return task["read_dirs"][0]
            return self.pipeline.worktree(task, where)
        except WorktreeRefused as e:
            self.port.set_status(task["id"], "needs-owner", reason=str(e)[:300])
        except Exception as e:                       # a git failure must not end the tick
            self._safe_event("worktree-error", {"error": repr(e)[:300]}, task["id"])
            self.port.set_status(task["id"], "needs-owner", reason=f"worktree failed: {e!r}"[:300])
        return False

    def _admit(self, task, policy, admitted):
        """(route, placement, worktree) to launch the claimed task, else a reason-code string; placement precedes the worktree (ADR 0007)."""
        p, tid = self.port, task["id"]
        if task.get("write", True) and not self.pipeline.prepare_dispatch(task):
            return "no-gate"                         # no known gate for the repo: the owner was asked
        route = self._route(task, policy, admitted)
        if p.waiting.pending(("task", tid)):
            return "waiting"
        if route.get("needs_owner"):
            p.set_status(tid, "needs-owner", reason=route["reason"])
            return "needs-owner"
        backend = route.get("backend")
        if backend not in admitted:
            self._defer(task, route.get("reason") or f"{backend} not admitted")
            return "backend-not-admitted"
        where, had, repo = None, task.get("worktree"), self.pipeline.repo(task)
        if task.get("write", True) and repo is not None:
            where, why = place(task, route, had, repo)
            if where is None:
                p.set_status(tid, "needs-owner", reason=why)
                return "needs-owner"
            if where == "wsl" and not self.launcher.wsl_up():
                p.note_wsl_needed(tid)
                self._defer(task, "WSL is not running", reason_code="wsl-down")
                return "wsl-down"
        wt = self._worktree(task, where)
        if wt is False:
            return "worktree-refused"                # the task went to the owner
        placement, why = (where, None) if where is not None and had and wt == had else place(task, route, wt, repo)   # old worktree keeps its side
        if placement is None:
            p.set_status(tid, "needs-owner", reason=why)
            return "needs-owner"
        if self._is_unavailable(backend, placement):
            self._defer(task, f"{backend} is not installed on {placement}")
            return "backend-unavailable"
        if placement == "wsl" and not self.launcher.wsl_up():
            p.note_wsl_needed(tid)
            self._defer(task, "WSL is not running", reason_code="wsl-down")
            return "wsl-down"
        blocked = self._side_blocked(placement) or (placement == "wsl" and self._wsl_blocked())
        if blocked:
            self._defer(task, blocked)
            return "resources"
        return route, placement, wt
