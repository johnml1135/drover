"""The dispatcher: one tick = heartbeat, reap, admit, claim ready work, route, place, brief, launch. No LLM.

It runs inside the Windows hub (ADR 0002) and launches every worker through drover.runner, on Windows or in WSL.
It claims only `ready` tasks (the ledger enforces it), never compacts (rotation writes a handoff), retries a failed
gate on the same task, escalates the tier after 2 failures and asks for a replan on the four section-12 triggers.
decide.retry_or_escalate runs in shadow: logged, never acted on.

The mandatory pipeline (drover.pipeline) makes checks and reviews runs too, followed like workers: a worker that reports
done is finished as `done` and then gated by a `gate` run (gatejob under drover.runner, on the repo's gate
platform), a gated task is reviewed by a fresh `review` run, and LAND@sha moves it to reviewed. What is due is derived
from the ledger on every tick (so a hub restart loses nothing). While `dispatch_enabled` is false the dispatcher
claims and launches nothing, but still reaps, adopts and recovers.
"""
from . import placement
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

from dataclasses import dataclass, field

from . import task_access
from . import brief as brief_mod
from . import config as config_mod
from . import contract
from . import gate as gate_mod
from . import repos as repos_mod
from . import runner as runner_mod
from . import resources as resources_mod
from . import route as route_mod
from datetime import datetime, timezone

from . import launch as launch_mod
from .launch import RunnerLauncher
from .pipeline import WorktreeRefused, truthy
from .runner import ScanError

DEFAULT_INFRA_RETRIES = 2                 # gate reruns on the same commit before the owner is asked; setting gate_infra_retries
FAIL_OUTCOMES = contract.FAIL_OUTCOMES   # worker failures; launch-error/orphaned never count
WALL_MIN = {"S": 30, "M": 60, "L": 120}
IDLE_MIN = 10
MAX_LAUNCH_ERRORS = 3
MAX_PROVIDER_ERRORS = 5     # provider errors in a row (never counted as task failures) before the owner is alerted
PROVIDER_COOLDOWN_S = 300   # a review is not restarted this soon after a provider error
CLAIM_STALE_S = 300
SCAN_EVERY_S = 60            # how often live runs are checked against the process list
ORPHAN_SCANS = 2             # consecutive clean scans that miss a run's runner before it is orphaned
BACKOFF_MAX_MIN = 30
UNAVAILABLE_S = 600          # a missing binary is re-probed (tried again) after this
MAX_FINISH_TRIES = 3
IS_WINDOWS = os.name == "nt"   # the dispatcher lock is not cross-OS, so only Windows may run a dispatcher
PLATFORM_RE = re.compile(r"(?<![\w-])platform:\s*(linux|windows)\b", re.I)
CHEAP_MODELS = ("haiku", "luna")
MAX_ROTATIONS = 3
EXCERPT_LINES = contract.EXCERPT_LINES
LEASE_TTL_S = 600.0          # a gate ticket outlives one slow tick (a scan can take 90s)
MAX_REVIEW_TRIES = 3         # reviews that produce no usable verdict in a row before the owner is asked
MAX_GATE_LAUNCH_ERRORS = 3
PENDING_KEY = "_pending_outcomes"


def ladders():
    """{backend: [model, ...] cheapest first} from the config's models.ladders."""
    return config_mod.get()["models"]["ladders"]


def start_idx():
    return config_mod.get()["models"]["start"]


def __getattr__(name):   # LADDERS and START_IDX stay importable; they follow the active config
    if name == "LADDERS":
        return ladders()
    if name == "START_IDX":
        return start_idx()
    raise AttributeError(name)


def seat_of(route):
    """The config seat a route names, else the backend's best seat; None when there is none."""
    seat = config_mod.seat_by_name(route.get("seat")) if route.get("seat") else None
    if seat is None:
        seats = config_mod.seats_for(route.get("backend"))
        seat = seats[0] if seats else None
    return seat


def _platform_tag(task):
    """'linux' or 'windows' from the task's platform field, a `platform:linux` tag, or the same text in title/goal."""
    plat = str(task.get("platform") or "").lower()
    tags = [str(t).lower().replace(" ", "") for t in task.get("tags") or ()]
    for t in tags:
        if t.startswith("platform:"):
            plat = plat or t[9:]
    if not plat:
        m = PLATFORM_RE.search(f"{task.get('title', '')} {task.get('goal', '')}")
        plat = m.group(1).lower() if m else ""
    return plat if plat in ("linux", "windows") else None


def placement_for(task, route, worktree=None, platforms=None):
    """'wsl' or 'windows'. A Codex seat runs where its seat is (WSL; config refuses Windows); a WSL Claude seat runs in
    WSL; a linux platform tag (or the router's platform) means WSL; the repo supplies its default and
    the repo allows, else WSL (owner rule: windows + claude, or wsl + claude or codex); Claude otherwise runs on
    Windows (unless the worktree is Linux-only)."""
    seat = seat_of(route)
    plat = str(route.get("platform") or "").lower() or _platform_tag(task)
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
    plats = ((prog or {}).get("run") or {}).get("platforms") or list(config_mod.PLACEMENTS)
    want = placement_for(task, route, worktree, plats)
    seat = seat_of(route)
    allowed = route_mod.seat_placements(seat) if seat else config_mod.PLACEMENTS
    hard = bool(_platform_tag(task) or route.get("platform") ) \
        or route.get("backend") == "codex" or bool(worktree and placement.is_linux_native(worktree))
    default = ((prog or {}).get("run") or {}).get("default")
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


def fail_runs(runs):
    """The worker runs that count as failures, in order: a worker run that failed, and the worker run a review run
    put on HOLD (the reviewer's own backend and model are not the ladder's). Gate runs never count."""
    out, worker = [], None
    for r in runs:
        if r.get("role", "worker") == "worker":
            worker = r
            if r["outcome"] in FAIL_OUTCOMES:
                out.append(r)
        elif r["role"] == "review" and r["outcome"] == "hold" and worker is not None:
            out.append(worker)
    return out


def failures(runs):
    return len(fail_runs(runs))


def next_tier(runs, base):
    """Route after failures: stay on the last backend; step up one tier after every 2nd failure."""
    fails = fail_runs(runs)
    last = fails[-1] if fails else None
    if not last or last["backend"] not in ladders():
        return base
    ladder = ladders()[last["backend"]]
    idx = ladder.index(last["model"]) if last["model"] in ladder else start_idx()["M"]
    idx = min(idx + (1 if failures(runs) % 2 == 0 else 0), len(ladder) - 1)
    return {"backend": last["backend"], "model": ladder[idx]}


def at_top(run):
    ladder = ladders().get(run["backend"], [])
    return not ladder or run["model"] not in ladder or ladder.index(run["model"]) == len(ladder) - 1


def _epoch(stamp):
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def _real_decide(*a, **kw):
    from . import decide
    return decide.decide(*a, recorder=lambda result: None, **kw)    # the dispatcher records into the ledger itself


ENV_BLOCK_KINDS = ("verification_environment", "missing_windows_worker")


@dataclass
class DispatchRuntime:
    """The execution environment shared by worker launch, resource checks and recovery."""
    launcher: object = field(default_factory=RunnerLauncher)
    resources: object = resources_mod.check
    now: object = time.time


class Dispatcher:
    def __init__(self, port, pipeline, runtime=None, usage=None, route=route_mod.choose, decide=_real_decide):
        from .usage import Poller
        self.port, self.pipeline = port, pipeline
        runtime = runtime or DispatchRuntime()
        self.launcher, self.resources_fn, self.now = runtime.launcher, runtime.resources, runtime.now
        self.stats_resources_fn = lambda: self.resources_fn(new_workers=0, with_wsl_memory=True)
        self.usage_poller = usage or Poller(port.conn, now=self.now, launcher=self.launcher)
        self.route_fn, self.decide_fn = route, decide
        self.tickets = {}                    # task_id -> gate lease ticket
        self.scan_every_s, self.last_scan, self.scan_unknown = SCAN_EVERY_S, None, False
        self.recovered = False
        self.unavailable = {}                # (backend, placement) -> when its binary was found missing
        self.wsl_waiting = set()             # re-checked next tick when the watchdog brings WSL up
        self.cancelling = {}                 # run_id -> (task_id, placement): cancelled runner may still exist
        self.finish_tries = {}
        self.live = {}                       # run_id -> (handle, task_id, spec)
        self.missed = {}                     # run_id -> consecutive clean scans that did not find its runner
        self.cursor = None                   # last event id seen by the stale-brief check
        self.last_verdict = None
        self._stats_hour, self._stats_resources = None, {}
        self.last_resources = {}             # the newest resources.check result: its `wsl` part gates WSL work only

    # ---- helpers ------------------------------------------------------------------------------------
    def _ask(self, name, state, rule_answer, task_id):
        """Call the decider (never raises), record the decision, return the raw result or None."""
        try:
            r = self.decide_fn(name, json.dumps(state), rule_answer=rule_answer)
            answer = r.get("answer") if r.get("acted") else r.get("shadow_answer")
            did = self.port.record_decision(
                question_name=name, criteria_version=str(r.get("criteria_version") or ""),
                model=r.get("model") or "", state_hash=r.get("state_hash") or "", answer=answer,
                probabilities=r.get("probabilities"), confidence=r.get("confidence"),
                shadow=not r.get("acted"), rule_answer=rule_answer)
            return {**r, "decision_id": did if isinstance(did, int) else None}
        except Exception as e:
            self.port.append_event("decide-error", {"question": name, "error": repr(e)}, task_id)
            return None

    def _replan(self, task_id, trigger, **data):
        self.port.append_event("replan-request", {"trigger": trigger, **data}, task_id)
        self.port.replan_hold(task_id, "replan")

    def _release_stale_holds(self):
        """Release holds whose source task is landed, cancelled or out of replan (also clears holds left from before
        this sweep existed). Scans the blocked tasks once per tick (cost grows with their number); never raises."""
        try:
            return self.port.release_stale_holds()
        except Exception as e:
            self._safe_event("stale-hold-error", {"error": repr(e)[:300]})
            return []

    # ---- reap ---------------------------------------------------------------------------------------
    def reap(self):
        """Finish runs whose result is in. A run stays in `live` until _finish succeeds; a failing finish is logged and
        retried next tick, and after MAX_FINISH_TRIES the task goes to the owner with the error."""
        done = []
        for run_id, (handle, task_id, spec) in list(self.live.items()):
            result = handle.poll()
            if result is None:
                continue
            try:
                self._finish(run_id, task_id, spec, result)
            except Exception as e:
                n = self.finish_tries[run_id] = self.finish_tries.get(run_id, 0) + 1
                self._safe_event("finish-error", {"run": run_id, "try": n, "error": repr(e)[:300]}, task_id)
                if n < MAX_FINISH_TRIES:
                    continue
                self._give_up(run_id, task_id, e)
            del self.live[run_id]
            self.finish_tries.pop(run_id, None)
            done.append(run_id)
        return done

    def _safe_event(self, kind, data, task_id=None):
        try:
            self.port.append_event(kind, data, task_id)
        except Exception:
            pass

    def _give_up(self, run_id, task_id, exc):
        p = self.port
        for fn in (lambda: p.finish_run(run_id, "error", note=f"finish failed: {exc!r}"[:300]),
                   lambda: p.set_status(task_id, "needs-owner", reason=f"could not finish run {run_id}: {exc!r}"[:300])):
            try:
                fn()
            except Exception as e:
                self._safe_event("finish-error", {"run": run_id, "error": repr(e)[:300], "gave_up": True}, task_id)

    def _finish(self, run_id, task_id, spec, result):
        p, u, rec = self.port, result["usage"], result.get("record") or {}
        if hasattr(p, "set_run_context"):
            fields = {k: u[k] for k in ("sessions", "compactions", "tokens_by_model") if k in u}
            fields.update(result.get("peaks") or {})
            if fields:
                p.set_run_context(run_id, **fields)
        usage = dict(tokens_in=u["tokens_in"], tokens_out=u["tokens_out"], tokens_cached=u["tokens_cached"],
                     allowance_used=self._allowance_used(spec))
        note = rec.get("note") or None
        st = result["status"]
        if result.get("tail_error"):
            p.append_event("tail-error", {"run": run_id, "error": result["tail_error"]}, task_id)
        role = spec.get("role", "worker")
        if role == "gate":
            return self._finish_gate(run_id, task_id, spec, result)
        if role == "review":
            return self._finish_review(run_id, task_id, spec, result)
        try:
            task_access.verify(spec.get("read_checkouts") or [])
            if (spec.get("adopted") and p.get_task(task_id).get("read_access")
                    and "read_checkouts" not in spec):
                raise ValueError("read access verification unavailable after dispatcher restart")
        except (ValueError, OSError) as e:
            p.finish_run(run_id, "blocked", note=str(e)[:300], **usage)
            return p.set_status(task_id, "needs-owner", reason=str(e)[:300])
        if spec.get("write", p.get_task(task_id).get("write", True)) is False and st in ("done", "ready_to_commit"):
            p.finish_run(run_id, "read-only-done", note=note, **usage)
            p.append_event("read-only-done", {"run": run_id, "note": note}, task_id)
            return p.set_status(task_id, "needs-owner", reason="Read-only work completed; approve to accept the findings and complete the task")
        if result.get("provider_error") and st in ("error", "no-record", "timeout"):
            return self._provider_error(task_id, run_id, result["provider_error"], usage)
        if st == "timeout":
            p.finish_run(run_id, "timeout", note=f"timeout: {result.get('timeout')}"[:300], **usage)
            self._failed(task_id, run_id, f"worker timed out ({result.get('timeout')})")
        elif st == "rotate":
            p.set_handoff(task_id, result["handoff"])
            p.finish_run(run_id, "rotated", note="context ceiling; handoff written", **usage)
            rotations = sum(1 for r in p.list_runs(task_id) if r["outcome"] == "rotated")
            if rotations >= MAX_ROTATIONS:
                p.set_status(task_id, "needs-owner", reason=f"rotated {rotations} times without finishing")
            else:
                p.set_status(task_id, "ready", reason="rotated")
        elif st == "blocked" and self._env_block_to_ready(task_id, run_id, rec, usage, result):
            pass
        elif st == "blocked":
            p.finish_run(run_id, "blocked", note=note, **usage)
            self._replan(task_id, "blocker", blocker=rec["blocker"])
        elif st == "ready_to_commit":
            try:
                sha = self.pipeline.commit_ready(p.get_task(task_id), run_id, rec["commit_message"])
            except (WorktreeRefused, OSError) as e:
                p.finish_run(run_id, "blocked", note=str(e)[:300], **usage)
                p.append_event("commit-refused", {"run": run_id, "reason": str(e),
                                                  "paths": getattr(e, "paths", [])}, task_id)
                p.set_status(task_id, "needs-owner", reason=str(e))
                return
            result = {**result, "record": {**rec, "status": "done", "sha": sha}}
            self._after_done(task_id, result, run_id, usage, note)
        elif st == "done":
            self._after_done(task_id, result, run_id, usage, note)
        else:
            p.finish_run(run_id, st, note=(result.get("text") or "")[-250:] or None, **usage)
            self._failed(task_id, run_id, "no action-state record" if st == "no-record" else "worker error")

    def _provider_streak(self, task_id, runs):
        """Provider-error runs in a row at the end of the history, counted only after the task's last status change
        by someone other than the dispatcher (an owner requeue starts the count again)."""
        since = max((e.get("at") or "" for e in self.port.events("status")
                     if e.get("task_id") == task_id and e.get("actor") not in ("dispatcher", None)), default="")
        n = 0
        for r in reversed([r for r in runs if r["ended"] is not None]):
            if r["outcome"] != "provider-error" or (r["ended"] or "") < since:
                break
            n += 1
        return n

    def _provider_error(self, task_id, run_id, message, usage):
        """A provider-side error (capacity, rate limit, 5xx, expired auth) says nothing about the task: the run ends
        `provider-error`, which FAIL_OUTCOMES leaves out, so it counts toward no retry, escalate or replan. The task
        goes back to ready behind a growing backoff; a persistent streak goes to the owner."""
        p = self.port
        p.finish_run(run_id, "provider-error", note=f"provider error: {message}"[:290], **usage)
        p.append_event("provider-error", {"run": run_id, "message": message[:300]}, task_id)
        streak = self._provider_streak(task_id, p.list_runs(task_id))
        if streak >= MAX_PROVIDER_ERRORS:
            return p.set_status(task_id, "needs-owner", reason=f"{streak} provider errors in a row: {message}"[:300])
        minutes = min(BACKOFF_MAX_MIN, 2 ** (streak - 1))
        p.set_status(task_id, "ready", reason=f"provider error: {message}"[:300])
        p.set_backoff(task_id, self.now() + minutes * 60, minutes)

    def _env_block_to_ready(self, task_id, run_id, rec, usage, result):
        """A worker blocked only by the sandbox (the Drover gate runs the full suites) with work in its worktree is
        committed and sent to the gate; no replan. False when it does not apply or the commit is refused."""
        kind = str((rec.get("blocker") or {}).get("kind") or "").lower().replace("-", "_")
        if kind not in ENV_BLOCK_KINDS:
            return False
        p, task = self.port, self.port.get_task(task_id)
        if task.get("write", True) is False:
            return False
        note = f"sandbox limit, sent to gate: {(rec.get('blocker') or {}).get('detail', '')} {rec.get('note') or ''}".strip()[:300]
        try:
            sha = self.pipeline.commit_ready(task, run_id, str(task.get("title") or task_id).strip() or task_id)
        except (WorktreeRefused, OSError):
            return False
        p.append_event("env-block-ready", {"run": run_id, "kind": kind, "sha": sha}, task_id)
        self._after_done(task_id, {**result, "record": {**rec, "status": "done", "sha": sha}}, run_id, usage, note)
        return True

    def _allowance_used(self, spec):
        """What this run cost per window: the used-% now minus the used-% at launch. None when either is unknown
        (an adopted run has no launch reading). Parallel runs share the window, so this is an upper bound."""
        before = spec.get("allowance_before")
        if not before:
            return None
        return contract.used_delta(before, contract.used_from_headroom(self._reading_for_route(spec)))

    def _failed(self, task_id, run_id, excerpt):
        """A failure of the worker run `run_id` (a gate-fail, a worker error, or a review HOLD of that run)."""
        p = self.port
        runs = p.list_runs(task_id)
        run = next(r for r in runs if r["id"] == run_id)
        f = failures(runs)
        second = f % 2 == 0
        rule = "escalate" if second else "retry"
        state = {"task": task_id, "failures": f, "size": p.get_task(task_id)["size"], "model": run["model"],
                 "excerpt": excerpt[-1500:]}
        self._settle(task_id, passed=False)                           # the run that just failed was the "next run"
        r = self._ask("retry_or_escalate", state, rule, task_id)      # shadow: logged, never acted on
        if r and r.get("decision_id"):
            self._pend(task_id, r["decision_id"], "retry_or_escalate", took=rule)
        r = self._ask("plan_wrong", state, "no", task_id)
        if r and r.get("acted") and r.get("answer"):
            return self._replan(task_id, "plan-wrong", failures=f)
        if second and at_top(run):
            return self._replan(task_id, "retries-exhausted", failures=f, model=run["model"])
        ladder = ladders().get(run["backend"], [])
        new = ladder[ladder.index(run["model"]) + 1] if second and run["model"] in ladder else run["model"]
        p.append_event("retry" if not second else "escalate", {"failures": f, "model": run["model"], "to": new}, task_id)
        p.set_status(task_id, "ready", reason=rule)

    # ---- decision outcomes ----------------------------------------------------------------------------
    def _pending(self):
        try:
            return json.loads(self.port.get_setting(PENDING_KEY) or "{}")
        except ValueError:
            return {}

    def _pend(self, task_id, decision_id, question, took=None):
        """Remember a decision whose truth is known only after the task's next run (kept in the ledger, so a restart
        does not lose it)."""
        pend = self._pending()
        pend.setdefault(task_id, []).append({"id": decision_id, "q": question, "took": took})
        self.port.set_setting(PENDING_KEY, json.dumps(pend))

    def _settle(self, task_id, passed):
        """Write the outcome of every decision pending for the task, now that its next run passed or failed.

        retry_or_escalate took `retry` or `escalate` (the rule's answer, since it runs in shadow):
          next run passed  -> label = what was taken (it worked);
          retry then failed -> label escalate, audit missed (it carried on and should have escalated);
          escalate then failed -> audit needless (the stronger model changed nothing), no label.
        gate_failure_class: correct = whether the next run's gate passed (the class is only ever checked by its fix)."""
        pend = self._pending()
        items = pend.pop(task_id, [])
        if not items:
            return
        self.port.set_setting(PENDING_KEY, json.dumps(pend))
        for it in items:
            if it["q"] == "gate_failure_class":
                outcome = contract.decision_outcome(correct=passed)
            elif passed:
                outcome = contract.decision_outcome(label=it["took"])
            elif it["took"] == "retry":
                outcome = contract.decision_outcome(label="escalate", audit="missed")
            else:
                outcome = contract.decision_outcome(audit="needless")
            try:
                self.port.record_outcome(it["id"], outcome)
            except Exception as e:
                self._safe_event("decide-error", {"question": it["q"], "outcome_error": repr(e)}, task_id)

    # ---- the pipeline: gate and review as runs ------------------------------------------------------
    @staticmethod
    def _last_nongate(runs):
        return next((r for r in reversed(runs) if r["role"] != "gate"), None)

    def _awaiting_gate(self, runs):
        """A worker finished `done` and nothing is in flight: its gate is due (or must be redone after a crash)."""
        last = self._last_nongate(runs)
        return bool(last and last["role"] == "worker" and last["outcome"] == "done"
                    and not any(r["ended"] is None for r in runs))

    def _awaiting_review(self, runs):
        """The task's newest worker run passed its gate and no review has given a verdict on it (a review that ended
        without a usable verdict does not count, so the next tick derives a fresh one)."""
        if any(r["ended"] is None for r in runs):
            return False
        for r in reversed(runs):
            if r["role"] == "gate" or (r["role"] == "review" and r["outcome"] in ("no-record", "error", "timeout", "provider-error")):
                continue
            return r["role"] == "worker" and r["outcome"] == "gated"
        return False

    def _event_for(self, kind, key, run_id):
        """The newest event of `kind` whose data[key] is run_id, as its data dict, else {}."""
        found = [e["data"] for e in self.port.events(kind) if e["data"].get(key) == run_id]
        return found[-1] if found else {}

    def _after_done(self, task_id, result, run_id, usage, note):
        """The worker says done at `sha`: finish its run as `done` (the gate amends it to gated or gate-fail). The task
        stays running; _start_gates picks it up."""
        self.port.append_event("worker-done", {"run": run_id, "sha": result["record"]["sha"]}, task_id)
        self.port.finish_run(run_id, "done", note=note, **usage)

    def _gate_limits(self):
        """(slots, reserved) from the hub settings, read on every lease attempt so a change applies on the next tick."""
        return gate_mod.configured_limits(self.port.get_setting)

    def _gate_queue(self):
        """{waiting, oldest_s, slots}: gate tickets not yet holding a slot, the longest wait, and the slot count in force."""
        try:
            now = time.time()
            wait = [now - t["requested"] for t in gate_mod.lease_queue(self.port.conn) if t["slot"] is None]
            return {"waiting": len(wait), "oldest_s": int(max(wait, default=0)), "slots": self._gate_limits()[0]}
        except Exception:
            return None

    def _lease(self, task):
        """A gate slot for the task, or None while it waits. One ticket per task, kept across ticks."""
        conn, tid = self.port.conn, task["id"]
        for _ in range(2):
            tk = self.tickets.get(tid)
            if tk is None:
                tk = self.tickets[tid] = gate_mod.lease_request(conn, tid, task["priority"], holder="dispatcher")
            try:
                return gate_mod.lease_acquire(conn, tk, *self._gate_limits(), ttl=LEASE_TTL_S)
            except gate_mod.GateError:          # the ticket expired while the dispatcher was paused or busy
                self.tickets.pop(tid, None)
        return None

    def _release(self, task_id):
        tk = self.tickets.pop(task_id, None)
        if tk is not None:
            try:
                gate_mod.lease_release(self.port.conn, tk)
            except Exception:
                pass

    def _beat_leases(self):
        gating = {tid for _, tid, sp in self.live.values() if sp.get("role") == "gate"}
        for tid, tk in list(self.tickets.items()):
            if tid in gating:
                try:
                    gate_mod.lease_heartbeat(self.port.conn, tk)
                except Exception:
                    pass

    def _start_gates(self, policy):
        """Start the gate run of every running task whose worker finished done. Derived from the ledger each tick."""
        p, started = self.port, []
        live_tasks = {tid for _, tid, _ in self.live.values()}
        self._beat_leases()
        for t in p.list_tasks("running"):
            runs = p.list_runs(t["id"])
            if t["id"] in live_tasks or not self._awaiting_gate(runs):
                continue
            try:
                if self._begin_gate(t, runs):
                    started.append(t["id"])
            except Exception as e:
                self._safe_event("gate-error", {"error": repr(e)[:300]}, t["id"])
                self._release(t["id"])
                if p.get_task(t["id"])["status"] == "running":
                    p.set_status(t["id"], "needs-owner", reason=f"could not start the gate: {e!r}"[:300])
        return started

    def _begin_gate(self, task, runs):
        p, pl, tid = self.port, self.pipeline, task["id"]
        worker = self._last_nongate(runs)
        sha = self._event_for("worker-done", "run", worker["id"]).get("sha")
        prog = pl.repo(task)
        if prog is None or prog["gate"]["unknown"] or not task.get("worktree") or not sha:
            p.set_status(tid, "needs-owner", reason="cannot gate: no repo gate, worktree or commit")
            return False
        head = pl.head(task)
        if head != sha:
            self._gate_failed(task, worker, f"worker reported commit {sha[:12]} but its worktree is at {head[:12]}; "
                                            "commit the work and report that commit", 0, None)
            return False
        platform = self._next_gate_platform(task, prog, worker["id"], sha)
        plan = pl.gate_plan(task, prog, sha, platform)
        if platform == "wsl" and not self.launcher.wsl_up():
            p.note_wsl_needed(tid)
            return False
        if platform == "wsl" and self._wsl_blocked():
            return False
        if self._lease(task) is None:
            return False                                     # waiting for a gate slot
        gate_run = p.start_run(tid, "gate", "local", "gate")
        if hasattr(p, "set_run_context"):
            p.set_run_context(gate_run, placement=platform)
        argv, receipt = pl.gate_argv(plan, gate_run, task["priority"], prog, platform)
        nonce = uuid.uuid4().hex
        spec = {"run_id": gate_run, "role": "gate", "backend": "local", "model": "gate", "effort": None, "argv": argv,
                "cwd": task["worktree"], "placement": platform, "nonce": nonce, "task": task,
                "wall_s": prog["gate"]["timeout_s"], "idle_s": prog["gate"]["idle_s"], "user_prompt": ""}
        p.append_event("gate-started", {"run": worker["id"], "gate_run": gate_run, "sha": sha, "kind": plan["kind"],
                                        "platform": platform, "receipt": str(receipt), "diff_lines": plan["diff_lines"],
                                        "files": plan["files"][:200], "scope": plan["scope"]}, tid)
        p.append_event("run-launching", {"run": gate_run, "placement": spec["placement"], "nonce": nonce}, tid)
        try:
            handle = self.launcher.launch(spec)
        except Exception as e:
            self._release(tid)
            self._launch_error(task, gate_run, {"backend": "local"}, spec["placement"], e, nonce, role="gate")
            return False
        self.live[gate_run] = (handle, tid, spec)
        p.append_event("run-process", {"run": gate_run, "runner_pid": getattr(handle, "pid", None),
                                       "placement": spec["placement"]}, tid)
        return True

    def _gate_passed_platforms(self, task_id, worker_id, sha):
        """Platforms whose gate run for this worker run and commit has passed (derived from the ledger)."""
        outcome = {r["id"]: r["outcome"] for r in self.port.list_runs(task_id) if r["role"] == "gate"}
        return {e["data"].get("platform") for e in self.port.events("gate-started")
                if e["data"].get("run") == worker_id and e["data"].get("sha") == sha and e["data"].get("gate_run")
                and outcome.get(e["data"]["gate_run"]) == "passed"}

    def _next_gate_platform(self, task, prog, worker_id, sha):
        """The first of the repo's gate platforms that has not yet passed for this commit: `both` is run in order
        windows then wsl, one gate run at a time, and every one must pass."""
        platforms = repos_mod.gate_platforms(prog)
        done = self._gate_passed_platforms(task["id"], worker_id, sha)
        return next((x for x in platforms if x not in done), platforms[0])

    def _finish_gate(self, run_id, task_id, spec, result):
        """A gate run ended: read its receipt, amend the worker run to gated or gate-fail, then move the task."""
        p = self.port
        info = self._event_for("gate-started", "gate_run", run_id)
        worker_id, sha = info.get("run"), info.get("sha")
        task = p.get_task(task_id)
        worker = next(r for r in p.list_runs(task_id) if r["id"] == worker_id)
        receipt = self._read_json(info.get("receipt"))
        passed = bool(receipt and receipt.get("passed") and receipt.get("sha") == sha and result["status"] != "timeout")
        excerpt = None
        if not passed:
            excerpt = self._gate_excerpt(run_id, receipt, info, result)
            infra = self._gate_infra(receipt, result)
        row = p.finish_run(run_id, "passed" if passed else "failed",
                           note=(None if passed else excerpt.splitlines()[0][:280]),
                           tokens_in=0, tokens_out=0, tokens_cached=0)
        self._release(task_id)
        if not passed and infra:
            return self._gate_infra_retry(task_id, worker_id, sha, run_id, excerpt)
        prog = self.pipeline.repo(task)
        platforms = repos_mod.gate_platforms(prog) if prog else []
        if passed and len(platforms) > 1 and \
                any(x not in self._gate_passed_platforms(task_id, worker_id, sha) for x in platforms):
            p.append_event("gate-platform-passed", {"run": worker_id, "gate_run": run_id,
                                                    "platform": info.get("platform")}, task_id)
            return                       # the next tick derives the other platform's gate
        if not passed and len(platforms) > 1:
            excerpt = f"[{info.get('platform') or spec.get('placement')} gate failed]\n{excerpt}"
        wall = row.get("wall_s") or 0.0
        summary = {"sha": sha, "status": (receipt or {}).get("status", "refused"), "scope": info.get("scope"),
                   "kind": info.get("kind"), "path": info.get("receipt"),
                   "steps": [{k: s.get(k) for k in ("exit_code", "status")} for s in (receipt or {}).get("steps", [])]}
        if passed:
            self._gate_passed(task, worker, sha, wall, info.get("diff_lines"), summary, run_id)
        else:
            self._gate_failed(task, worker, excerpt, wall, info.get("diff_lines"), summary, info.get("files") or [])

    @staticmethod
    def _gate_infra(receipt, result):
        """True when a failed gate run says nothing about the code: it timed out, was refused or crashed without a
        receipt (so its output names no test), or its failed step was killed rather than exiting nonzero. Such a run
        is redone, never charged to the worker. A step that exited nonzero is the code's failure, parsed or not."""
        if result["status"] == "timeout" or receipt is None or receipt.get("status") == "refused":
            return True
        step = next((s for s in receipt.get("steps") or [] if s.get("status") == "failed"), None)
        code = (step or {}).get("exit_code")
        return step is not None and not (isinstance(code, int) and code > 0)

    def _gate_infra_retry(self, task_id, worker_id, sha, gate_run, excerpt):
        """An infrastructure failure: the worker run stays `done`, so the next tick re-queues only the gate for the
        same commit, up to the `gate_infra_retries` setting; after that the owner decides (no retry, no replan)."""
        p = self.port
        try:
            limit = int(str(p.get_setting("gate_infra_retries", None) or DEFAULT_INFRA_RETRIES).strip())
        except ValueError:
            limit = DEFAULT_INFRA_RETRIES
        used = sum(1 for e in p.events("gate-infra-retry")
                   if e["data"].get("run") == worker_id and e["data"].get("sha") == sha)
        line = (excerpt or "gate failed").splitlines()[0][:280]
        if used >= max(limit, 0):
            p.set_status(task_id, "needs-owner", reason=f"gate infrastructure failed {used + 1} times: {line}"[:300])
            return
        p.append_event("gate-infra-retry", {"run": worker_id, "sha": sha, "gate_run": gate_run, "reason": line,
                                            "n": used + 1}, task_id)

    def _gate_passed(self, task, worker, sha, wall, diff_lines, summary, gate_run):
        p, tid = self.port, task["id"]
        g = {"ok": True, "receipt": summary, "diff_lines": diff_lines, "duration_s": wall}
        p.append_event(contract.GATE_EVENT, contract.gate_event(wall, False, worker["id"]), tid)
        p.append_event("gate-done", {"run": worker["id"], "g": g}, tid)
        p.amend_run(worker["id"], outcome="gated", gate_receipt=contract.gate_receipt(True, diff_lines, receipt=summary))
        self._settle(tid, passed=True)
        p.append_event("run-done", {"sha": sha, "run": worker["id"]}, tid)
        p.set_status(tid, "gated", reason="gate passed")

    def _gate_failed(self, task, worker, excerpt, wall, diff_lines, summary=None, files=()):
        p, tid = self.port, task["id"]
        excerpt = "\n".join(str(excerpt or "gate failed").splitlines()[-EXCERPT_LINES:])
        g = {"ok": False, "excerpt": excerpt, "diff_lines": diff_lines, "duration_s": wall, "receipt": summary}
        p.append_event(contract.GATE_EVENT, contract.gate_event(wall, True, worker["id"]), tid)
        p.append_event("gate-done", {"run": worker["id"], "g": g}, tid)
        p.amend_run(worker["id"], outcome="gate-fail",
                    gate_receipt=contract.gate_receipt(False, diff_lines, excerpt, receipt=summary))
        self._failed(tid, worker["id"], excerpt)          # settles what the previous failure left pending
        r = self._ask("gate_failure_class", {"task": tid, "excerpt": excerpt[-1500:]},
                      "env_flaky", tid)
        if r and r.get("decision_id"):
            self._pend(tid, r["decision_id"], "gate_failure_class")


    @staticmethod
    def _read_json(path):
        try:
            d = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        return d if isinstance(d, dict) else None

    def _gate_log(self, run_id, receipt):
        log = Path(receipt["log"]) if receipt and receipt.get("log") else None
        if log is None and getattr(self.pipeline, "runs_dir", None):
            log = runner_mod.run_dir(self.pipeline.runs_dir, run_id) / runner_mod.OUT_FILE
        try:
            return log.read_text(errors="replace") if log else ""
        except OSError:
            return ""

    def _gate_excerpt(self, run_id, receipt, info, result):
        """The failure excerpt (at most EXCERPT_LINES): the end of the gate's log. A refusal
        or crash has no receipt, so it is the end of the run's own output."""
        text = self._gate_log(run_id, receipt)
        lines = []
        if result["status"] == "timeout":
            lines.append(f"gate timed out ({result.get('timeout')})")
        elif receipt is None:
            lines.append(f"gate refused or crashed (exit {result.get('returncode')}):")
        if receipt is not None and result["status"] != "timeout":
            lines.append(f"CI command exited {receipt.get('exit_code', result.get('returncode'))}")
        tail = text.strip().splitlines()[-(EXCERPT_LINES - len(lines)):]
        return "\n".join(lines + tail)

    # ---- review ---------------------------------------------------------------------------------------
    def _start_reviews(self, policy, admitted, slots):
        """Start a fresh review run for each gated task that has none. Derived from the ledger each tick."""
        p, started = self.port, []
        live_tasks = {tid for _, tid, _ in self.live.values()}
        for t in p.list_tasks("gated"):
            if len(started) >= slots:
                break
            runs = p.list_runs(t["id"])
            if t["id"] in live_tasks or not self._awaiting_review(runs):
                continue
            try:
                if self._begin_review(t, runs, policy, admitted):
                    started.append(t["id"])
            except Exception as e:
                self._safe_event("review-error", {"error": repr(e)[:300]}, t["id"])
                if p.get_task(t["id"])["status"] == "gated":
                    p.set_status(t["id"], "needs-owner", reason=f"could not start the review: {e!r}"[:300])
        return started

    def _review_route(self, task, prog, policy, admitted):
        """Route the repo's review through the usual seat priority, capacity and allowance checks."""
        rev = prog["review"]
        model = rev["high_risk_model"] if task.get("risk") == "high" else rev["model"]
        candidate = {"backend": rev["backend"], "model": model, "effort": rev.get("effort")}
        seats = [s for s in config_mod.get()["seats"] if s["backend"] == rev["backend"]
                 and (not rev.get("seat") or s["name"] == rev["seat"])]
        data = {**policy["data"], "routes": {**policy["data"].get("routes", {}), "review": [candidate]}}
        base = route_mod.choose({**task, "role": "review"}, data,
                                {b: self._headroom(b) for b in admitted}, seats=seats,
                                seat_load=self._seat_load(), platforms=prog["run"]["platforms"],
                                **self._seat_route_kw())
        if base.get("backend") and base["backend"] not in admitted:
            return {**base, "backend": None, "reason": f"{base['backend']} not admitted for review"}
        return base

    def _begin_review(self, task, runs, policy, admitted):
        p, pl, tid = self.port, self.pipeline, task["id"]
        worker = next(r for r in reversed(runs) if r["role"] == "worker")
        sha = self._event_for("worker-done", "run", worker["id"]).get("sha")
        prog = pl.repo(task)
        if prog is None or not task.get("worktree") or not sha:
            p.set_status(tid, "needs-owner", reason="cannot review: no repo, worktree or commit")
            return False
        if self._review_streak(runs) >= MAX_REVIEW_TRIES:
            p.set_status(tid, "needs-owner", reason=f"{MAX_REVIEW_TRIES} reviews in a row gave no verdict")
            return False
        last = next((r for r in reversed(runs) if r["role"] == "review" and r["ended"]), None)
        if last and last["outcome"] == "provider-error" and self.now() - _epoch(last["ended"]) < PROVIDER_COOLDOWN_S:
            return False
        if pl.head(task) != sha:
            p.set_status(tid, "ready", reason="the worktree moved after the gate; work must be gated again")
            return False
        route = self._review_route(task, prog, policy, admitted)
        if not route or not route.get("backend"):
            p.append_event("review-hold", {"reason": (route or {}).get("reason") or "no eligible review route"}, tid)
            return False
        placement, why = place(task, route, task["worktree"], prog)
        if placement is None:
            p.set_status(tid, "needs-owner", reason=why)
            return False
        if self._is_unavailable(route["backend"], placement):
            return False
        if placement == "wsl" and not self.launcher.wsl_up():
            p.note_wsl_needed(tid)
            return False
        if placement == "wsl" and self._wsl_blocked():
            return False
        repo, base = repos_mod.native(task["worktree"]), pl.base_ref(prog)
        gate = self._event_for("gate-done", "run", worker["id"]).get("g") \
            or contract.parse_gate_receipt(worker.get("gate_receipt"))
        b = brief_mod.assemble_review(task, pl.diff_text(repo, base, sha), sha, base, gate=gate)
        return self._spawn(task, route, policy, placement, task["worktree"], b, "review",
                           {"sha": sha, "worker_run": worker["id"]}) is not None

    @staticmethod
    def _review_streak(runs):
        n = 0
        for r in reversed([r for r in runs if r["role"] == "review"]):
            if r["outcome"] in ("no-record", "error", "timeout"):
                n += 1
            else:
                break
        return n

    def _finish_review(self, run_id, task_id, spec, result):
        p = self.port
        info = self._event_for("review-started", "run", run_id)
        sha, worker_id = info.get("sha"), info.get("worker_run")
        u = result["usage"]
        usage = dict(tokens_in=u["tokens_in"], tokens_out=u["tokens_out"], tokens_cached=u["tokens_cached"],
                     allowance_used=self._allowance_used(spec))
        rec = contract.parse_review_record(result.get("text") or "") if result["status"] in ("done", "blocked", "no-record") else None
        verdict = str((rec or {}).get("verdict") or "").strip()
        landed = re.fullmatch(r"LAND@([0-9a-fA-F]{40})", verdict)       # the whole sha: a prefix is not a licence
        if landed and sha and sha.lower() == landed.group(1).lower():
            head = self.pipeline.head(p.get_task(task_id))
            if head != sha:
                p.finish_run(run_id, "unverified", note="worktree moved during the review", **usage)
                return p.set_status(task_id, "ready", reason="the worktree moved during the review")
            p.finish_run(run_id, "land", review_verdict=f"LAND@{sha}", note=(rec.get("note") or None), **usage)
            p.append_event("review-done", {"run": run_id, "verdict": f"LAND@{sha}", "findings": rec["findings"]}, task_id)
            return p.set_status(task_id, "reviewed", reason=f"review LAND@{sha[:12]}")
        if verdict == "HOLD" and rec["findings"]:
            findings = "\n".join(f"- {f}" for f in rec["findings"])
            excerpt = "Review HOLD. Findings:\n" + findings
            p.finish_run(run_id, "hold", review_verdict="HOLD", note=(rec.get("note") or None),
                         gate_receipt=contract.gate_receipt(False, excerpt=excerpt, review=True), **usage)
            p.append_event("review-done", {"run": run_id, "verdict": "HOLD", "findings": rec["findings"]}, task_id)
            return self._failed(task_id, worker_id, excerpt)
        if result.get("provider_error") and result["status"] in ("error", "no-record", "timeout"):
            p.finish_run(run_id, "provider-error", note=f"provider error: {result['provider_error']}"[:290], **usage)
            p.append_event("provider-error", {"run": run_id, "message": result["provider_error"][:300]}, task_id)
            if self._provider_streak(task_id, p.list_runs(task_id)) >= MAX_PROVIDER_ERRORS:
                p.set_status(task_id, "needs-owner", reason=f"{MAX_PROVIDER_ERRORS} provider errors in a row")
            return
        why = {"timeout": "timeout", "error": "error"}.get(result["status"], "no-record")
        p.finish_run(run_id, why, note=(f"no usable verdict ({verdict or result['status']})")[:280], **usage)
        if self._review_streak(p.list_runs(task_id)) >= MAX_REVIEW_TRIES:
            p.set_status(task_id, "needs-owner", reason=f"{MAX_REVIEW_TRIES} reviews in a row gave no usable verdict")

    # ---- trigger 3 ----------------------------------------------------------------------------------
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
                    self._replan(dep, "stale-brief", landed=e["task_id"])

    # ---- launch -------------------------------------------------------------------------------------
    def _launch(self, task, route, policy, placement, cwd=None):
        p = self.port
        runs = p.list_runs(task["id"])
        last = self._last_nongate(runs)
        excerpt = contract.receipt_excerpt(last["gate_receipt"]) \
            if last and last["outcome"] in ("gate-fail", "hold") else None
        prog = self.pipeline.repo(task)
        if prog and prog.get("brief"):
            task = {**task, "repo_brief": prog["brief"]}
        try:
            b = brief_mod.assemble(task, retry_excerpt=excerpt, handoff=task.get("handoff") or None,
                                   notes=p.owner_notes(task["id"]))
        except brief_mod.BriefTooLarge as e:
            return p.set_status(task["id"], "needs-owner", reason=str(e))
        cwd = cwd if cwd is not None else self.pipeline.worktree(task)
        if not cwd:
            return p.set_status(task["id"], "needs-owner", reason="task has no worktree")
        return self._spawn(task, route, policy, placement, cwd, b, "worker")

    def _spawn(self, task, route, policy, placement, cwd, b, role, extra=None):
        """Start one run of `role` (worker or review) for the brief `b`; returns its run id, or None on a launch error."""
        p = self.port
        effort = route.get("effort") if role == "review" and route.get("effort") else effort_for(
            route["model"], route.get("effort"))
        run_id = p.start_run(task["id"], role, route["backend"], route["model"], effort)
        nonce = uuid.uuid4().hex
        seat = seat_of(route)
        if hasattr(p, "set_run_context"):
            p.set_run_context(run_id, seat=seat["name"] if seat else None, placement=placement,
                              policy_version=policy.get("version"))
        prog = self.pipeline.repo(task)
        spec = {"run_config": (prog or {}).get("run", {}), "run_id": run_id, "role": role, "backend": route["backend"], "model": route["model"], "effort": effort,
                "seat": seat["name"] if seat else None, "binary": seat["binary"] if seat else route["backend"],
                "env": config_mod.seat_env(seat),
                "write": task.get("write", True), "read_dirs": task.get("read_dirs", []),
                "read_checkouts": task.get("read_checkouts", []),
                "sandbox": "read-only" if role == "review" or task.get("write") is False else (seat or {}).get("sandbox", "workspace-write"),
                "cwd": cwd, "placement": placement, "nonce": nonce, "task": task, "wall_s": self._wall_s(task, policy),
                "idle_s": self._idle_s(policy), "system_append": b["system_append"], "user_prompt": b["user_prompt"],
                "allowance_before": contract.used_from_headroom(self._reading_for_route(route)), **(extra or {})}
        if role == "review":
            p.append_event("review-started", {"run": run_id, "sha": extra["sha"], "worker_run": extra["worker_run"]},
                           task["id"])
        p.append_event("dispatch.access", {"run": run_id, "read_checkouts": spec["read_checkouts"],
                                           "write": spec["write"]}, task["id"])
        p.append_event("run-launching", {"run": run_id, "placement": placement, "nonce": nonce}, task["id"])  # before any start
        try:
            handle = self.launcher.launch(spec)
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
        p.finish_run(run_id, "launch-error", note=(f"launch failed: {exc}"[:270] + (" cancel-requested" if cancelled else "")))
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
            self.cancelling[run_id] = (tid, placement, nonce)
            p.append_event("launch-cancel", {"run": run_id, "placement": placement}, tid)
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

    # ---- orphan recovery ----------------------------------------------------------------------------
    def _open_runs(self):
        """[(task, run)] for every open run of a running or claimed task that this dispatcher is not following."""
        live_tasks = {tid for _, tid, _ in self.live.values()}
        out = []
        for status in ("running", "claimed", "gated"):      # a gated task may have a review in flight
            for t in self.port.list_tasks(status):
                if t["id"] not in live_tasks:
                    out += [(t, r) for r in self.port.list_runs(t["id"]) if r["ended"] is None]
        return out

    def _scan(self, want_wsl):
        """{run_id: placement}, or None when the process list could not be read (unknown, never "none")."""
        try:
            found = launch_mod.normalize_run_ids(self.launcher.scan(want_wsl))
        except ScanError as e:
            if not self.scan_unknown:
                self.port.append_event("scan-unknown", {"error": str(e)[:300]})
            self.scan_unknown = True
            return None
        self.scan_unknown = False
        return found

    def _placements(self):
        """{run_id: placement} from the events written before (run-launching) and after (run-process) a launch."""
        out = {}
        for kind in ("run-launching", "run-process", "launch-cancel"):
            for e in self.port.events(kind):
                if e["data"].get("placement"):
                    out[e["data"].get("run")] = e["data"]["placement"]
        return out

    def _nonces(self):
        return {e["data"].get("run"): e["data"].get("nonce") for e in self.port.events("run-launching")
                if e["data"].get("nonce")}

    @staticmethod
    def _needs_wsl_scan(placements):
        """WSL is scanned for a wsl run and for any run of unknown placement: unknown means both sides."""
        return any(pl not in ("windows",) for pl in placements)

    def recover_orphans(self):
        """At startup, follow every open run through its files: a run whose runner is alive, or whose status.json is
        terminal, is adopted and finished from its files. A run with neither is watched and orphaned only after two
        consecutive clean scans (watch_runs). A run of unknown placement is looked for on both sides. A scan that
        fails means unknown: nothing is recovered or requeued this tick, and recovery is retried on the next one.
        Returns the requeued task ids."""
        p, policy = self.port, self.port.get_policy()
        places, nonces = self._placements(), self._nonces()
        recovered, live_tasks = [], {tid for _, tid, _ in self.live.values()}
        for t in p.list_tasks("running"):
            runs = p.list_runs(t["id"])
            if t["id"] in live_tasks or any(r["ended"] is None for r in runs):
                continue
            last = runs[-1] if runs else None
            if last and last["outcome"] == "launch-error" and "cancel-requested" in (last["note"] or ""):
                self.cancelling[last["id"]] = (t["id"], places.get(last["id"], "unknown"), nonces.get(last["id"]))
                continue
            if self._awaiting_gate(runs):
                continue                                    # done and waiting for its gate: _start_gates derives it
            p.append_event("running-no-run", {}, t["id"])
            p.set_status(t["id"], "ready", reason="running with no open run")
            recovered.append(t["id"])
        opens = self._open_runs()
        if opens or self.cancelling:
            found = self._scan(self._needs_wsl_scan([places.get(r["id"], "unknown") for _, r in opens]
                                                    + [c[1] for c in self.cancelling.values()]))
            if found is None:
                return recovered
            for t, r in opens:
                self._adopt(t, r, found.get(r["id"]) or places.get(r["id"], "unknown"), policy)
                if r["id"] not in found:
                    self.missed[r["id"]] = 1       # its first clean scan without a runner
            self.last_scan = self.now()
        self.recovered = True
        return recovered

    def _adopt(self, task, run, placement, policy):
        spec = {"run_id": run["id"], "role": run["role"], "backend": run["backend"], "model": run["model"], "task": task,
                "adopted": True, "placement": placement, "wall_s": self._wall_s(task, policy),
                "idle_s": self._idle_s(policy)}
        saved = next((e["data"] for e in self.port.events("dispatch.access")
                      if e["data"].get("run") == run["id"]), {})
        if "read_checkouts" in saved:
            spec.update(read_checkouts=saved["read_checkouts"], write=saved.get("write", True))
        if hasattr(self.port, "conn"):
            row = self.port.conn.execute('SELECT data FROM run_stats WHERE run_id=?', (run["id"],)).fetchone()
            if row:
                spec["seat"] = json.loads(row["data"]).get("seat")
        handle = self.launcher.adopt(spec, _epoch(run["started"]))
        self.live[run["id"]] = (handle, task["id"], spec)
        self.port.append_event("run-adopted", {"run": run["id"], "placement": placement}, task["id"])

    def watch_runs(self):
        """Check unfinished runs against the process list (every scan_every_s). A run whose status.json is not
        terminal and whose runner was missed by ORPHAN_SCANS consecutive clean scans is orphaned and its task
        requeued. A launch that was cancelled is requeued only once a clean scan shows no runner for its run id. A
        failed scan is unknown: it requeues nothing and restarts the count. Runs this dispatcher launched fall back
        to their own handle's status when the scan is unavailable. Returns requeued task ids."""
        pending = {rid: v for rid, v in self.live.items() if v[0].poll() is None and v[0].terminal() is None}
        if not pending and not self.cancelling:
            return []
        if self.last_scan is not None and self.now() - self.last_scan < self.scan_every_s:
            return []
        found = self._scan(self._needs_wsl_scan([sp.get("placement", "unknown") for _, _, sp in pending.values()]
                                                + [c[1] for c in self.cancelling.values()]))
        if found is None:
            self.missed.clear()
            return []
        self.last_scan = self.now()
        out = []
        for rid, (tid, _, nonce) in list(self.cancelling.items()):
            if rid in found:
                stop = getattr(self.launcher, "stop_run", None)
                if stop:
                    stop(rid, nonce)                  # ask again; the task stays where it is
                continue
            del self.cancelling[rid]
            p = self.port
            if p.get_task(tid)["status"] == "running" and not any(r["ended"] is None for r in p.list_runs(tid)):
                p.set_status(tid, "ready", reason="cancelled launch: no runner holds the run")
                out.append(tid)
        for rid, (handle, tid, spec) in pending.items():
            if rid in found:
                self.missed.pop(rid, None)
                continue
            self.missed[rid] = self.missed.get(rid, 0) + 1
            if self.missed[rid] >= ORPHAN_SCANS and handle.terminal() is None:
                out.append(self._orphan(rid, tid))
        for rid in [r for r in self.missed if r not in self.live]:
            del self.missed[rid]
        return out

    def _orphan(self, run_id, task_id):
        handle, _, spec = self.live.pop(run_id)
        handle.cancel()
        self.missed.pop(run_id, None)
        p = self.port
        p.finish_run(run_id, "orphaned", note="runner gone with no terminal status")
        if spec.get("role") in ("gate", "review"):
            self._release(task_id)               # the gate or review is derived again from the ledger next tick
            return task_id
        if not any(r["ended"] is None for r in p.list_runs(task_id)) and p.get_task(task_id)["status"] in ("running", "claimed"):
            p.set_status(task_id, "ready", reason="orphaned run recovered")
        return task_id

    def requeue_stale_claims(self):
        """A claim with no open run for over CLAIM_STALE_S is a dispatcher that died between claim and start."""
        p, out = self.port, []
        live_tasks = {tid for _, tid, _ in self.live.values()}
        for t in p.list_tasks("claimed"):
            if t["id"] in live_tasks or self.now() - _epoch(t["updated"]) <= CLAIM_STALE_S:
                continue
            if any(r["ended"] is None for r in p.list_runs(t["id"])):
                continue
            p.append_event("claim-expired", {"claimed_s": int(self.now() - _epoch(t["updated"]))}, t["id"])
            p.set_status(t["id"], "ready", reason="stale claim with no run")
            out.append(t["id"])
        return out

    def _seat_load(self):
        """{seat name: live non-gate runs}; legacy runs with no seat count on their backend's best seat."""
        load = {}
        for _, _, spec in self.live.values():
            if spec.get("role") == "gate":
                continue
            name = spec.get("seat") or (seat_of(spec) or {}).get("name")
            load[name] = load.get(name, 0) + 1
        return load

    def _route_inputs(self, task, admitted):
        """One router call shape: seat load, repo platforms and measured per-seat allowance."""
        prog = self.pipeline.repo(task)
        kw = {"seat_load": self._seat_load(), "platforms": prog["run"]["platforms"] if prog else None,
              **self._seat_route_kw()}
        kw["seat_headroom"] = {name: h for name, h in kw["seat_headroom"].items()
                               if config_mod.seat_by_name(name)["backend"] in admitted}
        return kw

    def _route(self, task, policy, admitted):
        t = task
        heads = {b: self._headroom(b) for b in admitted}
        base = self.route_fn(t, policy["data"], heads, **self._route_inputs(task, admitted))
        if base.get("borrow_seat"):
            seat = base["borrow_seat"]
            if self._director_borrow_approved(task["id"], seat):
                base = route_mod.choose({**t, "director_seat": seat}, policy["data"],
                                        **self._route_inputs(task, admitted), director_borrow_approved=True)
            else:
                return base
        if not base.get("backend"):
            return base
        if task.get("role") in route_mod.DIRECTOR_ROLES:
            return base
        route = next_tier(self.port.list_runs(task["id"]), base)
        if route["model"] != base["model"]:
            route = {**route, "effort": None}   # the router's effort was picked for the cheaper model
        role_key = route_mod.route_key(task.get("role", "worker"), task.get("size"))
        data = {**policy["data"], "routes": {**policy["data"].get("routes", {}), role_key: [route]}}
        return route_mod.choose(t, data, seats=config_mod.get()["seats"],
                                **self._route_inputs(task, admitted))

    def _director_borrow_approved(self, task_id, seat):
        for ev in reversed(self.port.events("director-borrow")):
            if ev["actor"] == "dispatcher" and ev["task_id"] == task_id and ev["data"].get("seat") == seat:
                q = self.port.get_question(ev["data"]["question"])
                return q["status"] == "answered" and q["answer_choice"] == "Approve borrowing"
        qid = self.port.ask_question("task", task_id, f"Allow director to borrow {seat}?",
                                     "This approval applies to director work on this task and account.",
                                     options=[{"label": "Approve borrowing"}, {"label": "Keep default seat"}],
                                     needs_owner=True)
        self.port.append_event("director-borrow", {"seat": seat, "question": qid}, task_id)
        return False

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

    def _headroom(self, plan):
        try:
            return self._seat_headroom(plan)
        except Exception:
            return None

    # ---- tick ---------------------------------------------------------------------------------------
    def _defer(self, task, why):
        """Put a claimed task back and keep it out of the claim order for 1, 2, 4 ... 30 minutes, so it does not
        block the tasks behind it (head of line) and does not churn status events every tick."""
        p, tid = self.port, task["id"]
        prev = p.get_backoffs().get(tid)
        minutes = min(BACKOFF_MAX_MIN, prev["min"] * 2) if prev else 1
        p.set_status(tid, "ready", reason=why)
        p.set_backoff(tid, self.now() + minutes * 60, minutes)
        p.append_event("backoff", {"minutes": minutes, "why": why}, tid)

    def _worktree(self, task):
        """The task's worktree path, or False when it could not have one (it is then needs-owner with the reason)."""
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
            return self.pipeline.worktree(task)
        except WorktreeRefused as e:
            self.port.set_status(task["id"], "needs-owner", reason=str(e)[:300])
        except Exception as e:                       # a git failure must not end the tick
            self._safe_event("worktree-error", {"error": repr(e)[:300]}, task["id"])
            self.port.set_status(task["id"], "needs-owner", reason=f"worktree failed: {e!r}"[:300])
        return False

    def _prune_backoffs(self, backoffs):
        """Drop backoff entries of tasks that are no longer ready (landed, cancelled, running ...)."""
        if not backoffs:
            return backoffs
        ready = {t["id"] for t in self.port.list_tasks("ready")}
        for tid in [t for t in backoffs if t not in ready]:
            self.port.set_backoff(tid, None, 0)
            del backoffs[tid]
            self.wsl_waiting.discard(tid)
        return backoffs

    def tick(self):
        self._seat_tick()
        p = self.port
        policy = p.get_policy()
        enabled = truthy(p.get_setting("dispatch_enabled", "false"))
        p.heartbeat({"live": len(self.live), "state": "running" if enabled else "paused", "gate_queue": self._gate_queue()})
        out = {"reaped": self.reap(), "launched": [], "verdict": None, "admitted": [], "recovered": [], "gated": [],
               "reviewed": [], "paused": not enabled}
        if not self.recovered:
            out["recovered"] = self.recover_orphans()      # its scan counts as this tick's scan
        else:
            out["recovered"] = self.watch_runs()
        out["recovered"] += self.requeue_stale_claims()
        self.check_stale()
        self._release_stale_holds()
        if not enabled:                              # paused: in-flight runs are still reaped, adopted and recovered
            return out
        out["recovered"] += self.pipeline.sync_gates()
        res = self.resources_fn(new_workers=1)
        self.last_resources = res
        out["verdict"] = res["verdict"]
        if res["verdict"] != self.last_verdict:
            p.append_event("resources", {"verdict": res["verdict"], "reasons": res.get("reasons", [])})
            self.last_verdict = res["verdict"]
        if res["verdict"] != "hold":
            out["gated"] = self._start_gates(policy)     # a gate is CPU work the lease paces, not new worker load
        if res["verdict"] != "go":                   # queue: no new claims; hold: nothing starts either
            return out
        admitted = list(dict.fromkeys(s["backend"] for s in config_mod.get()["seats"]
                                     if not self._backend_down(s["backend"])))
        out["admitted"] = admitted
        if not admitted:
            return out
        in_use = sum(1 for _, _, sp in self.live.values() if sp.get("role") != "gate")
        slots = min(policy["parallel_cap"] - in_use, res.get("slots", 0))
        out["reviewed"] = self._start_reviews(policy, admitted, max(slots, 0))
        slots -= len(out["reviewed"])
        if self.wsl_waiting and self.launcher.wsl_up():       # WSL came up (the watchdog booted it): do not wait out the backoff
            for tid in self.wsl_waiting:
                p.set_backoff(tid, None, 0)
            self.wsl_waiting.clear()
        backoffs = self._prune_backoffs(p.get_backoffs())
        skip = {tid for tid, b in backoffs.items() if b["until"] > self.now()}
        while slots > 0:
            task = p.claim_next({}, exclude=skip)
            if task is None:
                break
            assert task["status"] == "claimed"
            skip.add(task["id"])                     # whatever happens to it, not again this tick
            if task.get("write", True) and not self.pipeline.gate_known(task):
                continue                             # no known gate for the repo: the owner was asked
            route = self._route(task, policy, admitted)
            if route.get("needs_owner"):
                p.set_status(task["id"], "needs-owner", reason=route["reason"])
                continue
            if route.get("backend") not in admitted:
                self._defer(task, route.get("reason") or f"{route.get('backend')} not admitted")
                continue
            wt = self._worktree(task)
            if wt is False:
                continue                             # refused: the task went to the owner
            placement, why = place(task, route, wt, self.pipeline.repo(task))
            if placement is None:
                p.set_status(task["id"], "needs-owner", reason=why)
                continue
            if self._is_unavailable(route["backend"], placement):
                self._defer(task, f"{route['backend']} is not installed on {placement}")
                continue
            if placement == "wsl" and not self.launcher.wsl_up():
                p.note_wsl_needed(task["id"])
                self._defer(task, "WSL is not running")
                self.wsl_waiting.add(task["id"])
                continue
            if placement == "wsl" and self._wsl_blocked():
                self._defer(task, self._wsl_blocked())
                continue
            if self._launch(task, route, policy, placement, cwd=wt) is not None \
                    and task["id"] in [t for _, t, _ in self.live.values()]:
                out["launched"].append(task["id"])
                slots -= 1
                if task["id"] in backoffs:
                    p.set_backoff(task["id"], None, 0)
        return out


def loop(dispatcher, interval=30, stop=lambda: False, sleep=time.sleep):
    while not stop():
        dispatcher.tick()
        sleep(interval)


def main(argv=None):
    import argparse
    from .ledger import connect, default_path
    from .lock import FileLock, LockHeld, default_path as lock_path
    from .port import DirectPort
    ap = argparse.ArgumentParser(prog="drover.dispatcher")
    ap.add_argument("--db", help="ledger path (direct mode; test or Windows use)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=30)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--lock", help="single-instance lock file (default: the hub's <state dir>\\dispatcher.lock)")
    a = ap.parse_args(argv)
    if not IS_WINDOWS:
        print("drover.dispatcher: refusing to start: the dispatcher runs only inside the Windows hub "
              "(its single-instance lock is not shared between Windows and WSL)", file=sys.stderr)
        return 2
    lock = FileLock(a.lock or lock_path())
    try:
        lock.acquire()
    except LockHeld as e:
        print(f"drover.dispatcher: refusing to start: {e} (the hub runs the dispatcher)", file=sys.stderr)
        return 2
    try:
        port = DirectPort(connect(a.db or default_path()))
        from .pipeline import Pipeline
        d = Dispatcher(port, Pipeline(port))
        if a.once:
            out = d.tick()
            print(json.dumps(out) if a.json else f"{out['verdict']}: launched {out['launched']}, reaped {out['reaped']}")
            return 0
        loop(d, a.interval)
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
