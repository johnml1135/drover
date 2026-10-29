"""Dispatcher finish steps."""
import json

from . import contract, task_access
from .pipeline import WorktreeRefused
from .dispatcher_admission import (MAX_FINISH_TRIES, MAX_ROTATIONS, MAX_PROVIDER_ERRORS, BACKOFF_MAX_MIN,
                                   ENV_BLOCK_KINDS, PENDING_KEY, failures, at_top, ladders)


class FinishSteps:
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
        spec = self.live.get(run_id, (None, None, {}))[2]
        if spec.get('role') == 'planner':
            try:
                self.replanning.finish(run_id, {'status': contract.UNVERIFIED, 'text': '', 'usage': {}}, now=self.now())
            except Exception as error:
                self._safe_event('replan-recovery-needed', {'run': run_id, 'error': repr(error)[:300]}, task_id)
            return
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
        if role == "planner":
            return self._finish_planner(run_id, task_id, spec, result)
        if role == "gate":
            return self._finish_gate(run_id, task_id, spec, result)
        if role == "review":
            return self._finish_review(run_id, task_id, spec, result)
        if any(r['task_id'] == task_id for r in p.waiting.replans()) and st not in (contract.DONE, 'ready_to_commit'):
            # A request supersedes this worker; its failure must not start another stage.
            return p.finish_run(run_id, contract.UNVERIFIED, note=note, **usage)
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
            self.port.waiting.request_replan(task_id, "blocker", {"blocker": rec["blocker"]})
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
        marker = self.port.get_task(task_id)['retry_after_run']
        history, worker_id = [], 0
        for r in runs:
            if r['role'] == 'worker':
                worker_id = r['id']
            if worker_id > marker and r['role'] in ('worker', 'review') and r['ended'] is not None:
                history.append(r)
        n = 0
        for r in reversed(history):
            if r['outcome'] != contract.PROVIDER_ERROR:
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
        p.waiting.delay(task_id, "worker", "provider", self.now(), minutes=minutes)

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
        task = p.get_task(task_id)
        f = failures(runs, task['retry_after_run'])
        if not f:
            return
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
            return self.port.waiting.request_replan(task_id, "plan-wrong", {"failures": f})
        if second and at_top(run):
            return self.port.waiting.request_replan(task_id, "retries-exhausted", {"failures": f, "model": run["model"]})
        ladder = ladders().get(run["backend"], [])
        new = ladder[ladder.index(run["model"]) + 1] if second and run["model"] in ladder else run["model"]
        p.append_event("retry" if not second else "escalate", {"failures": f, "model": run["model"], "to": new}, task_id)
        p.set_status(task_id, "ready", reason=rule)

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
