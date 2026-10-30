"""The dispatcher: one tick = heartbeat, reap, admit, claim ready work, route, place, brief, launch. No LLM.

It runs inside the Windows hub (ADR 0002) and launches every worker through drover.runner, on Windows or in WSL.
It claims only `ready` tasks (the ledger enforces it), never compacts (rotation writes a handoff), retries a failed
gate on the same task, escalates the tier after 2 failures and asks for a replan on the four section-12 triggers.
decide.retry_or_escalate runs in shadow: logged, never acted on.

The mandatory pipeline (drover.pipeline) makes checks and reviews runs too, followed like workers: a worker that reports
done is finished as `done` and then gated by a `gate` run (repo script under drover.runner, on the repo's gate
platform), a gated task is reviewed by a fresh `review` run, and LAND@sha moves it to reviewed. What is due is derived
from the ledger on every tick (so a hub restart loses nothing). While `dispatch_enabled` is false the dispatcher
claims and launches nothing, but still reaps, adopts and recovers.
"""
from . import placement, repos
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

from dataclasses import dataclass, field

from . import dispatch_repos
from . import task_access
from . import brief as brief_mod
from . import config as config_mod
from . import contract
from .ledger import ci as ci_slots
from . import repos as repos_mod
from . import runner as runner_mod
from . import resources as resources_mod
from . import route as route_mod
from datetime import datetime, timezone

from . import launch as launch_mod
from .launch import RunnerLauncher
from .ledger import states
from .pipeline import WorktreeRefused, truthy
from .runner import ScanError


from .dispatcher_admission import (
    DEFAULT_INFRA_RETRIES, FAIL_OUTCOMES, WALL_MIN, IDLE_MIN, MAX_LAUNCH_ERRORS,
    MAX_PROVIDER_ERRORS, CLAIM_STALE_S, SCAN_EVERY_S, ORPHAN_SCANS, BACKOFF_MAX_MIN,
    UNAVAILABLE_S, MAX_FINISH_TRIES, CHEAP_MODELS, MAX_ROTATIONS, EXCERPT_LINES,
    MAX_REVIEW_TRIES, MAX_GATE_LAUNCH_ERRORS, PENDING_KEY, ENV_BLOCK_KINDS,
    ladders, start_idx, seat_of, placement_for, place, effort_for, fail_runs, failures,
    next_tier, at_top, _epoch, AdmissionSteps,
)
from .dispatcher_finish import FinishSteps
from .dispatcher_gates import GateSteps
from .dispatcher_reviews import ReviewSteps
from .dispatcher_launch import LaunchSteps
from .dispatcher_recovery import RecoverySteps

IS_WINDOWS = os.name == "nt"   # the dispatcher lock is not cross-OS, so only Windows may run a dispatcher


def __getattr__(name):   # LADDERS and START_IDX stay importable; they follow the active config
    if name == "LADDERS":
        return ladders()
    if name == "START_IDX":
        return start_idx()
    raise AttributeError(name)

def _real_decide(*a, **kw):
    from . import decide
    return decide.decide(*a, recorder=lambda result: None, **kw)    # the dispatcher records into the ledger itself


@dataclass
class DispatchRuntime:
    """The execution environment shared by worker launch, resource checks and recovery."""
    launcher: object = field(default_factory=RunnerLauncher)
    resources: object = resources_mod.tick
    now: object = time.time
    directors: object = None
    completion_wake: object = None  # receives run ids after their completion has been committed by reap()


class Dispatcher(FinishSteps, GateSteps, ReviewSteps, LaunchSteps, RecoverySteps, AdmissionSteps):
    def __init__(self, port, pipeline, runtime=None, usage=None, route=route_mod.choose, decide=_real_decide):
        from .usage import Poller
        from .replan import Replanning
        self.port, self.pipeline = port, pipeline
        self.replanning = Replanning(pipeline)
        runtime = runtime or DispatchRuntime()
        self.launcher, self.resources_fn, self.now = runtime.launcher, runtime.resources, runtime.now
        self.stats_resources_fn = lambda: self.resources_fn(new_workers=0, with_wsl_memory=True)
        self.usage_poller = usage or Poller(port.conn, now=self.now, launcher=self.launcher)
        self.route_fn, self.decide_fn = route, decide
        self.ci_runners = ci_slots.Runners(port.conn, port.get_setting)
        self.scan_every_s, self.last_scan, self.scan_unknown = SCAN_EVERY_S, None, False
        self.recovered = False
        self.unavailable = {}                # (backend, placement) -> when its binary was found missing
        self.cancelling = {}                 # run_id -> (task_id, placement): cancelled runner may still exist
        self.finish_tries = {}
        self.live = {}                       # run_id -> (handle, task_id, spec)
        self.directors = runtime.directors
        self.completion_wake = runtime.completion_wake
        self._control_epoch = None
        self.missed = {}                     # run_id -> consecutive clean scans that did not find its runner
        self.cursor = None                   # last event id seen by the stale-brief check
        self.last_verdict = None
        self._stats_hour, self._stats_resources = None, {}
        self.last_resources = {}             # the newest resources.check result: its `wsl` part gates WSL work only

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
        from .ledger.waiting import DEADLINE
        if any(row["kind"] == DEADLINE and row["data"]["role"] == "review"
               for row in p.waiting.pending(("task", tid))):
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

    def tick(self):
        self._control_epoch = self.port.admission()['epoch']
        self._seat_tick()
        p = self.port
        policy = p.get_policy()
        enabled = truthy(p.get_setting("dispatch_enabled", "false"))
        beat = {"live": len(self.live), "state": "running" if enabled else "paused", "gate_queue": self.ci_runners.snapshot()}
        p.heartbeat(beat)
        planner_tasks = {tid for _, _, sp in self.live.values() if sp.get("role") == "planner" for tid in sp["task_ids"]}
        reaped = self.reap()
        if reaped and self.completion_wake is not None:
            try:
                self.completion_wake(tuple(reaped))
            except Exception:
                pass  # a lost hint is recovered by the next bounded periodic tick
        out = {"reaped": reaped, "launched": [], "verdict": None, "admitted": [], "recovered": [], "gated": [],
               "reviewed": [], "paused": not enabled}
        in_use = lambda: p.conn.execute("SELECT COUNT(*) FROM runs WHERE ended IS NULL AND role<>'gate'").fetchone()[0]
        def report(skipped, stop=""):      # heartbeat `not_dispatched` {reason: count}; no event per task
            if stop:
                skipped[stop] = p.conn.execute("SELECT COUNT(*) FROM tasks WHERE status='ready' AND archived=0").fetchone()[0]
            if any(skipped.values()):
                p.heartbeat({**beat, "not_dispatched": {k: n for k, n in skipped.items() if n}})
            return out
        if not self.recovered:
            out["recovered"] = self.recover_orphans()      # its scan counts as this tick's scan
        else:
            out["recovered"] = self.watch_runs()
        out["recovered"] += self.requeue_stale_claims()
        self.check_stale()
        from .ledger.waiting import DEADLINE
        p.waiting.expire(self.now())
        if any(r['kind'] == DEADLINE and r['data']['reason_code'] == 'wsl-down' for r in p.waiting.pending()) and self.launcher.wsl_up():
            p.append_event('capacity-available', {'placement': 'wsl'})
        res = self.resources_fn(new_workers=1)
        self.last_resources = res
        if res.get('pause'):
            p.set_setting('dispatch_enabled', 'false')
            enabled, out['paused'] = False, True
            for token in res.get('critical_tokens', []):
                # Tokens are consumed only after the sticky ledger pause commits.
                (config_mod.state_dir(config_mod.get()) / 'disk' / token).unlink(missing_ok=True)
        if self.directors is not None:
            def director_slots():
                return max(0, min(policy['parallel_cap'] - in_use(), res.get('slots', 0)))
            out['directors'] = self.directors.tick(p.admission(epoch=self._control_epoch),
                dict(slots=director_slots, admit=self._director_admission))
        else:
            from .directors import service_tick
            service_tick(p, self.now)
        if p.get_setting('admission_hold'):
            self._admission_report()
        control = p.admission(epoch=self._control_epoch)
        if not control['allowed']:
            self._stop_for_control(control['epoch'])
            out['control'] = control
            return report({}, "control-stopped")
        self._stop_for_control(control['epoch'], changed_only=True)
        enabled = truthy(p.get_setting('dispatch_enabled', 'false'))
        out['paused'] = not enabled
        if not enabled:                              # paused: in-flight runs are still reaped, adopted and recovered
            return report({}, "paused")
        out["verdict"] = res["verdict"]
        if res["verdict"] != self.last_verdict:
            p.append_event("resources", {"verdict": res["verdict"], "reasons": res.get("reasons", [])})
            self.last_verdict = res["verdict"]
        if res["verdict"] != "hold":
            out["gated"] = self._start_gates(policy)     # a gate is CPU work the lease paces, not new worker load
        if res["verdict"] != "go":                   # queue: no new claims; hold: nothing starts either
            return report({}, f"resources-{res['verdict']}")
        admitted = list(dict.fromkeys(s["backend"] for s in config_mod.get()["seats"]
                                     if not self._backend_down(s["backend"])))
        out["admitted"] = admitted
        if not admitted:
            self._admission_report()
            return report({}, "no-backend-admitted")
        slots = min(policy["parallel_cap"] - in_use(), res.get("slots", 0) - out.get('directors', {}).get('capacity_used', 0))
        out["reviewed"] = self._start_reviews(policy, admitted, max(slots, 0))
        slots -= len(out["reviewed"])
        out["planned"] = self._start_replans(policy, admitted, max(slots, 0))
        slots -= len(out["planned"])
        skip = {row["subject_key"] for row in p.waiting.pending() if row["subject_kind"] == "task"}
        skip.update(planner_tasks)
        skip.update(tid for _, tid, _ in self.live.values())
        skipped = {}
        while slots > 0 and self._launch_allowed():
            task = p.claim_next({}, exclude=skip)
            if task is None:
                break
            assert task["status"] == "claimed"
            skip.add(task["id"])                     # whatever happens to it, not again this tick
            got = self._admit(task, policy, admitted)
            if isinstance(got, str):
                skipped[got] = skipped.get(got, 0) + 1
                continue
            route, placement, wt = got
            if self._launch(task, route, policy, placement, cwd=wt) is not None \
                    and task["id"] in [t for _, t, _ in self.live.values()]:
                out["launched"].append(task["id"])
                slots -= 1
        out['paused'] = not truthy(p.get_setting('dispatch_enabled', 'false'))
        return report(skipped, "no-slots" if slots <= 0 else "")


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
