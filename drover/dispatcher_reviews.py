"""Dispatcher reviews steps."""
import json
import re
from pathlib import Path

from . import contract, dispatch_repos, repos
from . import config as config_mod
from . import route as route_mod
from .ledger import states
from .dispatcher_admission import MAX_PROVIDER_ERRORS, MAX_REVIEW_TRIES, at_top, effort_for, failures, place, seat_of


class ReviewSteps:
    @staticmethod
    def _read_json(path):
        try:
            d = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        return d if isinstance(d, dict) else None

    def _start_replans(self, policy, admitted, slots):
        def assign(task, inputs):
            route = self._route({**task, 'role': 'planner', 'model_pin': None}, policy, admitted)
            if route.get('backend') not in admitted:
                return None
            placement, _ = place(task, route, inputs['checkout'], self.pipeline.repo(task))
            if placement is None or self._is_unavailable(route['backend'], placement):
                return None
            if placement == 'wsl' and (not self.launcher.wsl_up() or self._wsl_blocked()):
                return None
            seat = seat_of(route)
            return {**route, 'seat': seat['name'] if seat else None,
                    'effort': effort_for(route['model'], route.get('effort')),
                    'harness': route.get('harness', route['backend']), 'placement': placement}
        allow = dispatch_repos.allowed(self.port.get_setting)
        started, inspected = [], {}                  # inspections are cached for this tick only
        for _ in range(slots):
            if not self._launch_allowed():
                break
            batch = self.replanning.prepare(assign, now=self.now(), inspected=inspected,
                                            allow=lambda t: dispatch_repos.permits(allow, t["repo"]))
            if batch is None:
                break
            launched = self._spawn({**batch.task, 'write': False, 'read_checkouts': batch.read_checkouts,
                                   'read_dirs': list(batch.read_roots)}, batch.assignment, policy,
                                  batch.assignment['placement'], batch.cwd, batch.brief, 'planner',
                                  {'request_ids': list(batch.request_ids), 'task_ids': list(batch.task_ids)}, run_id=batch.run_id)
            if launched is not None:
                started.append(launched)
        return started

    def _finish_planner(self, run_id, task_id, spec, result):
        usage = {**result.get('usage', {}), 'allowance_used': self._allowance_used(spec)}
        if spec.get('launch_cancelled'):
            result = {**result, 'status': contract.UNVERIFIED}
        return self.replanning.finish(run_id, {**result, 'usage': usage}, now=self.now())

    def _start_reviews(self, policy, admitted, slots):
        """Start a fresh review run for each gated task that has none. Derived from the ledger each tick."""
        p, started = self.port, []
        live_tasks = {tid for _, tid, _ in self.live.values()}
        allow = dispatch_repos.allowed(p.get_setting)
        for t in p.list_tasks("gated"):
            if len(started) >= slots:
                break
            if not dispatch_repos.permits(allow, t["repo"]):
                continue
            runs = p.list_runs(t["id"])
            if p.waiting.pending(("task", t["id"])) or t["id"] in live_tasks or not self._awaiting_review(runs):
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
        rev = repos.review_config(prog)
        model = rev["high_risk_model"] if task.get("risk") == "high" else rev["model"]
        candidate = {"backend": rev["backend"], "model": model, "effort": rev.get("effort")}
        seats = [s for s in config_mod.get()["seats"] if s["backend"] == rev["backend"]
                 and (not rev.get("seat") or s["name"] == rev["seat"])]
        data = {**policy["data"], "routes": {**policy["data"].get("routes", {}), "review": [candidate]}}
        base = route_mod.choose({**task, "role": "review"}, data,
                                seats=seats,
                                seat_load=self._seat_load(), platforms=repos.run_config(prog)["platforms"],
                                **self._seat_route_kw())
        if base.get("backend") and base["backend"] not in admitted:
            return {**base, "backend": None, "reason": f"{base['backend']} not admitted for review"}
        return base

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
        rec = contract.parse_review_record(result.get("text") or "") if result["status"] in (contract.DONE, contract.BLOCKED, contract.NO_RECORD) else None
        verdict = str((rec or {}).get("verdict") or "").strip()
        findings = (rec or {}).get('findings') or []
        blocking = [f for f in findings if not isinstance(f, dict) or f['kind'] == 'blocking']
        followups = [f for f in findings if isinstance(f, dict) and f['kind'] == 'follow-up']
        landed = re.fullmatch(r"LAND@([0-9a-fA-F]{40})", verdict)  # a prefix is not a licence
        exact = bool(sha and re.fullmatch(r'[0-9a-fA-F]{40}', sha) and
                     str((rec or {}).get('sha', '')).lower() == sha.lower())
        if ((landed and sha and sha.lower() == landed.group(1).lower()) or
                (verdict == 'HOLD' and exact and followups)) and not blocking:
            outcome = contract.LAND
        elif (verdict == 'HOLD' or landed) and blocking:
            outcome = contract.HOLD
        elif result.get('provider_error') and result['status'] in (contract.ERROR, contract.NO_RECORD, contract.TIMEOUT):
            outcome = contract.PROVIDER_ERROR
        else:
            outcome = result['status'] if result['status'] in (contract.TIMEOUT, contract.ERROR) else contract.NO_RECORD
        if outcome in (contract.LAND, contract.HOLD) and followups:
            p.propose_review_followups(task_id, run_id, followups)
        request = next((r for r in p.waiting.replans() if r['task_id'] == task_id), None)
        if request:
            p.finish_run(run_id, outcome, review_verdict=verdict or None, **usage)
            p.append_event('replan.stage-finished', {'request': request['id'], 'run': run_id,
                           'worker': worker_id, 'stage': 'review', 'receipt': rec}, task_id)
            return
        if outcome == contract.LAND:
            head = self.pipeline.head(p.get_task(task_id))
            if head != sha:
                p.finish_run(run_id, contract.UNVERIFIED, note="worktree moved during the review", **usage)
                return p.set_status(task_id, states.READY, reason="the worktree moved during the review")
            p.finish_run(run_id, contract.LAND, review_verdict=f"LAND@{sha}", note=(rec.get("note") or None), **usage)
            p.append_event("review-done", {"run": run_id, "verdict": f"LAND@{sha}", "findings": rec["findings"]}, task_id)
            return p.set_status(task_id, states.REVIEWED, reason=f"review LAND@{sha[:12]}")
        if outcome == contract.HOLD:
            findings = "\n".join(f"- {f['detail'] if isinstance(f, dict) else f}" for f in blocking)
            excerpt = "Review HOLD. Findings:\n" + findings
            p.finish_run(run_id, contract.HOLD, review_verdict="HOLD", note=(rec.get("note") or None),
                         gate_receipt=contract.gate_receipt(False, excerpt=excerpt, review=True), **usage)
            p.append_event("review-done", {"run": run_id, "verdict": "HOLD", "findings": rec["findings"]}, task_id)
            # One bounded extra repair round for a short, concrete review, once per retry window.
            task = p.get_task(task_id)
            holds = [r for r in p.list_runs(task_id) if r['role'] == 'review' and r['outcome'] == contract.HOLD
                     and r['id'] > task['retry_after_run']]
            used = any(e['task_id'] == task_id and e['data'].get('after_run') == task['retry_after_run']
                       for e in p.events('review-fix-round'))
            worker = next(r for r in p.list_runs(task_id) if r['id'] == worker_id)
            count = failures(p.list_runs(task_id), task['retry_after_run'])
            if len(holds) >= 2 and count % 2 == 0 and at_top(worker) and len(blocking) <= 2 and not used:
                p.set_handoff(task_id, excerpt)
                p.append_event('review-fix-round', {'run': run_id, 'after_run': task['retry_after_run']}, task_id)
                return p.set_status(task_id, 'ready', reason='one more fix round for concrete review findings')
            return self._failed(task_id, worker_id, excerpt)
        if outcome == contract.PROVIDER_ERROR:
            p.finish_run(run_id, contract.PROVIDER_ERROR, note=f"provider error: {result['provider_error']}"[:290], **usage)
            p.append_event("provider-error", {"run": run_id, "message": result["provider_error"][:300]}, task_id)
            if self._provider_streak(task_id, p.list_runs(task_id)) >= MAX_PROVIDER_ERRORS:
                p.set_status(task_id, states.NEEDS_OWNER, reason=f"{MAX_PROVIDER_ERRORS} provider errors in a row")
            else:
                p.waiting.delay(task_id, "review", "provider", self.now(), minutes=5)
            return
        p.finish_run(run_id, outcome, note=(f"no usable verdict ({verdict or result['status']})")[:280], **usage)
        if self._review_streak(p.list_runs(task_id)) >= MAX_REVIEW_TRIES:
            p.set_status(task_id, states.NEEDS_OWNER, reason=f"{MAX_REVIEW_TRIES} reviews in a row gave no usable verdict")
