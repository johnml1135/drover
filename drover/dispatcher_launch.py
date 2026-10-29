"""Dispatcher launch steps."""
import uuid

from . import contract, repos
from . import brief as brief_mod
from . import config as config_mod
from . import launch as launch_mod
from .dispatcher_admission import (effort_for, seat_of, MAX_LAUNCH_ERRORS, UNAVAILABLE_S, WALL_MIN, IDLE_MIN)


class LaunchSteps:
    def check_stale(self):
        evs = self.port.events("status", 0)
        if self.cursor is None:
            self.cursor = max((e["id"] for e in evs), default=0)    # skip history on first tick
            return
        for e in [e for e in evs if e["id"] > self.cursor]:
            self.cursor = e["id"]
            if e["data"].get("to") != "landed":
                continue
            for dep in self.port.dependents(e["task_id"]):
                t = self.port.get_task(dep)
                if t["status"] not in ("ready", "draft"):
                    continue
                r = self._ask("landed_diff_stale", {"landed": e["task_id"], "waiting": dep, "goal": t["goal"]},
                              "no", dep)
                if r and r.get("acted") and r.get("answer"):
                    self.port.waiting.request_replan(dep, "stale-brief", {"landed": e["task_id"]})

    def _launch(self, task, route, policy, placement, cwd=None):
        p = self.port
        runs = p.list_runs(task["id"])
        last = self._last_nongate(runs)
        excerpt = contract.receipt_excerpt(last["gate_receipt"]) \
            if last and last["outcome"] in ("gate-fail", "hold") else None
        prog = self.pipeline.repo(task)
        if prog and repos.brief_text(prog):
            task = {**task, "repo_brief": repos.brief_text(prog)}
        try:
            b = brief_mod.assemble(task, retry_excerpt=excerpt, handoff=task.get("handoff") or None,
                                   notes=p.owner_notes(task["id"]))
        except brief_mod.BriefTooLarge as e:
            return p.set_status(task["id"], "needs-owner", reason=str(e))
        cwd = cwd if cwd is not None else self.pipeline.worktree(task)
        if not cwd:
            return p.set_status(task["id"], "needs-owner", reason="task has no worktree")
        return self._spawn(task, route, policy, placement, cwd, b, "worker")

    def _spawn(self, task, route, policy, placement, cwd, b, role, extra=None, run_id=None):
        """Start one run of `role` (worker or review) for the brief `b`; returns its run id, or None on a launch error."""
        p = self.port
        if not self._launch_allowed(task):
            if run_id is not None and role == 'planner':
                self.replanning.finish(run_id, {'status': contract.UNVERIFIED, 'text': '', 'usage': {}}, now=self.now())
            return None
        effort = route.get("effort") if role == "review" and route.get("effort") else effort_for(
            route["model"], route.get("effort"))
        if run_id is None:
            run_id = p.start_run(task["id"], role, route["backend"], route["model"], effort)
        elif not any(r['id'] == run_id and r['role'] == role and r['ended'] is None
                     for r in p.list_runs(task['id'])):
            raise ValueError('preattached run mismatch')
        nonce = uuid.uuid4().hex
        seat = seat_of(route)
        if hasattr(p, "set_run_context"):
            p.set_run_context(run_id, seat=seat["name"] if seat else None, placement=placement,
                              policy_version=policy.get("version"))
        prog = self.pipeline.repo(task)
        spec = {"run_config": repos.run_config(prog), "run_id": run_id, "role": role, "backend": route["backend"], "model": route["model"], "effort": effort,
                "seat": seat["name"] if seat else None, "binary": seat["binary"] if seat else route["backend"],
                "env": config_mod.seat_env(seat),
                "write": False if role == "planner" else task.get("write", True), "read_dirs": task.get("read_dirs", []),
                "read_checkouts": task.get("read_checkouts", []),
                "sandbox": "read-only" if role in ("review", "planner") or task.get("write") is False else (seat or {}).get("sandbox", "workspace-write"),
                "cwd": cwd, "placement": placement, "nonce": nonce, "task": task, "wall_s": self._wall_s(task, policy),
                'control_epoch': self._control_epoch,
                "idle_s": self._idle_s(policy), "system_append": b["system_append"], "user_prompt": b["user_prompt"],
                "allowance_before": contract.used_from_headroom(self._reading_for_route(route)), **(extra or {})}
        if role == "planner":
            spec.update(write=False, sandbox='read-only', wall_s=600, idle_s=120)
        if role == "review":
            p.append_event("review-started", {"run": run_id, "sha": extra["sha"], "worker_run": extra["worker_run"]},
                           task["id"])
        p.append_event("dispatch.access", {"run": run_id, "read_checkouts": spec["read_checkouts"],
                                           "write": spec["write"], **({k: spec[k] for k in ("request_ids", "task_ids")} if role == "planner" else {})}, task["id"])
        p.append_event("run-launching", {"run": run_id, "placement": placement, "nonce": nonce,
                                       'control_epoch': self._control_epoch}, task["id"])  # before any start
        try:
            spec['_admission_check'] = lambda: p.require_admission(spec['control_epoch'], task['id'])
            spec['_admission_check']()
            handle = self.launcher.launch(spec)
        except launch_mod.AdmissionHeld as error:
            if role == 'planner':
                self.replanning.finish(run_id, {'status': contract.UNVERIFIED, 'text': '', 'usage': {}}, now=self.now())
            else:
                p.finish_run(run_id, 'control-held', note=str(error))
                if role == 'worker':
                    p.set_status(task['id'], 'ready', reason=str(error))
            return None
        except Exception as e:
            return self._launch_error(task, run_id, route, placement, e, nonce, role)
        self.live[run_id] = (handle, task["id"], spec)
        p.append_event("run-process", {"run": run_id, "runner_pid": getattr(handle, "pid", None),
                                       "placement": placement}, task["id"])
        if role == "worker" and task.get("handoff"):
            p.set_handoff(task["id"], "")
        return run_id

    def _launch_error(self, task, run_id, route, placement, exc, nonce=None, role="worker"):
        """A launch failure is not a worker failure: it never escalates. Three in a row go to the owner. When a stop
        was requested for a runner that may still come up, the task is not requeued until a scan shows no runner.
        A gate or review launch error leaves the task where it is (the next tick derives the same work again)."""
        p, tid = self.port, task["id"]
        cancelled = getattr(exc, "cancel_requested", False)
        if role == 'planner':
            if cancelled:
                p.append_event('launch-cancel', {'run': run_id, 'placement': placement}, tid)
                self.cancelling[run_id] = (tid, placement, nonce)
            self.replanning.finish(run_id, {'status': contract.LAUNCH_ERROR, 'text': '', 'usage': {}}, now=self.now())
            if getattr(exc, 'missing', False) or isinstance(exc, FileNotFoundError):
                self.unavailable[(route['backend'], placement)] = self.now()
            return None
        p.finish_run(run_id, "launch-error", note=(f"launch failed: {exc}"[:270] + (" cancel-requested" if cancelled else "")))
        if cancelled:
            self.cancelling[run_id] = (tid, placement, nonce)
            p.append_event("launch-cancel", {"run": run_id, "placement": placement}, tid)
        if getattr(exc, "missing", False) or isinstance(exc, FileNotFoundError):
            self.unavailable[(route["backend"], placement)] = self.now()
        streak = 0
        for r in reversed(p.list_runs(tid)):
            if r["outcome"] != "launch-error":
                break
            streak += 1
        if streak >= MAX_LAUNCH_ERRORS:
            return p.set_status(tid, "needs-owner", reason=f"{streak} launch errors in a row: {exc}"[:300])
        if cancelled:
            return None
        if role == "worker":
            return p.set_status(tid, "ready", reason="launch failed")
        return None

    def _is_unavailable(self, backend, placement):
        key = (backend, placement)
        at = self.unavailable.get(key)
        if at is not None and self.now() - at > UNAVAILABLE_S:
            del self.unavailable[key]               # re-probe: the next launch finds out again
            return False
        return at is not None

    def _backend_down(self, backend):
        """Missing on its default placement (Claude on Windows, Codex in WSL): not offered to the router at all."""
        seats = config_mod.seats_for(backend)
        return self._is_unavailable(backend, seats[0]["placement"] if seats else "windows")

    @staticmethod
    def _wall_s(task, policy):
        m = {**WALL_MIN, **(policy["data"].get("wall_min") or {})}
        return float(m[task.get("size", "M")]) * 60

    @staticmethod
    def _idle_s(policy):
        return float(policy["data"].get("idle_min", IDLE_MIN)) * 60
