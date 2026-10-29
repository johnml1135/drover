"""Dispatcher gates steps."""
import uuid

from . import contract, dispatch_repos, repos
from . import repos as repos_mod
from . import launch as launch_mod
from .dispatcher_admission import DEFAULT_INFRA_RETRIES, EXCERPT_LINES


class GateSteps:
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

    def _start_gates(self, policy):
        """Start the gate run of every running task whose worker finished done. Derived from the ledger each tick."""
        p, started = self.port, []
        live_tasks = {tid for _, tid, _ in self.live.values()} | {tid for tid, _, _ in self.cancelling.values()}
        self.ci_runners.heartbeat({tid for _, tid, spec in self.live.values() if spec.get("role") == "gate"})
        allow = dispatch_repos.allowed(p.get_setting)
        for t in p.list_tasks("running"):
            runs = p.list_runs(t["id"])
            if not dispatch_repos.permits(allow, t["repo"]):
                continue                             # the owner limited dispatch to other repos
            if p.waiting.pending(("task", t["id"])) or t["id"] in live_tasks or not self._awaiting_gate(runs):
                continue
            try:
                if self._begin_gate(t, runs):
                    started.append(t["id"])
            except Exception as e:
                self._safe_event("gate-error", {"error": repr(e)[:300]}, t["id"])
                self.ci_runners.release(t["id"])
                if p.get_task(t["id"])["status"] == "running":
                    p.set_status(t["id"], "needs-owner", reason=f"could not start the gate: {e!r}"[:300])
        return started

    def _begin_gate(self, task, runs):
        if not self._launch_allowed(task):
            return False
        if self.resources_fn(new_workers=0)['verdict'] == 'hold':
            return False
        p, pl, tid = self.port, self.pipeline, task["id"]
        worker = self._last_nongate(runs)
        sha = self._event_for("worker-done", "run", worker["id"]).get("sha")
        prog = pl.repo(task)
        if prog is None or repos.ci_policy(prog)["unknown"] or not task.get("worktree") or not sha:
            p.set_status(tid, "needs-owner", reason="cannot gate: no repo gate, worktree or commit")
            return False
        head = pl.head(task)
        if head != sha:
            self._gate_failed(task, worker, f"worker reported commit {sha[:12]} but its worktree is at {head[:12]}; "
                                            "commit the work and report that commit", 0, None)
            return False
        platform = self._next_gate_platform(task, prog, worker["id"], sha)
        if platform == "wsl" and not self.launcher.wsl_up():
            p.note_wsl_needed(tid)
            return False
        if platform == "wsl" and self._wsl_blocked():
            return False
        if self.ci_runners.reserve(task, platform=platform) is None:
            return False                                     # waiting for a gate slot
        gate_run = p.start_run(tid, "gate", "local", "gate")
        if hasattr(p, "set_run_context"):
            p.set_run_context(gate_run, placement=platform)
        nonce = uuid.uuid4().hex
        try:
            plan = pl.ci.prepare(prog, task["worktree"], sha, gate_run, platform=platform,
                                 priority=task["priority"])
            receipt = plan["receipt"]
            spec = {**plan["spec"], "nonce": nonce, "task": task, 'control_epoch': self._control_epoch}
            p.append_event("gate-started", {"run": worker["id"], "gate_run": gate_run, "sha": sha, "kind": plan["kind"],
                                            "platform": platform, "receipt": str(receipt), "diff_lines": plan["diff_lines"],
                                            "files": plan["files"][:200], "scope": plan["scope"]}, tid)
            p.append_event("run-launching", {"run": gate_run, "placement": spec["placement"], "nonce": nonce,
                                           'control_epoch': self._control_epoch}, tid)
            spec['_admission_check'] = lambda: p.require_admission(spec['control_epoch'], tid)
            spec['_admission_check']()
            self.ci_runners.pin(tid)
            handle = self.launcher.launch(spec)
        except launch_mod.AdmissionHeld as error:
            self.ci_runners.release(tid)
            p.finish_run(gate_run, 'control-held', note=str(error))
            return False
        except Exception as e:
            if not getattr(e, 'cancel_requested', False):
                self.ci_runners.release(tid)
            self._launch_error(task, gate_run, {"backend": "local"}, platform, e, nonce, role="gate")
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
        """Run WSL first in the shared checkout, stopping on failure; every platform must pass."""
        platforms = repos_mod.gate_order(repos_mod.gate_platforms(prog))
        done = self._gate_passed_platforms(task["id"], worker_id, sha)
        return next((x for x in platforms if x not in done), platforms[0])

    def _finish_gate(self, run_id, task_id, spec, result):
        """A gate run ended: read its receipt, amend the worker run to gated or gate-fail, then move the task."""
        p = self.port
        info = self._event_for("gate-started", "gate_run", run_id)
        worker_id, sha = info.get("run"), info.get("sha")
        task = p.get_task(task_id)
        worker = next(r for r in p.list_runs(task_id) if r["id"] == worker_id)
        evaluated = self.pipeline.ci.result(run_id, result)
        receipt = evaluated["receipt"]
        passed = evaluated["passed"] and receipt.get("sha") == sha
        excerpt = None if passed else evaluated["excerpt"]
        infra = not passed and evaluated["infra"]
        row = p.finish_run(run_id, contract.CI_PASSED if passed else contract.CI_FAILED,
                           note=(None if passed else excerpt.splitlines()[0][:280]),
                           tokens_in=0, tokens_out=0, tokens_cached=0)
        self.ci_runners.release(task_id)
        request = next((r for r in p.waiting.replans() if r['task_id'] == task_id), None)
        if request:
            p.append_event('replan.stage-finished', {'request': request['id'], 'run': run_id,
                           'worker': worker_id, 'stage': 'gate', 'receipt': receipt}, task_id)
            return
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
