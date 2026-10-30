"""Dispatcher recovery steps."""
import json

from . import contract
from . import launch as launch_mod
from .runner import ScanError
from .dispatcher_admission import _epoch, ORPHAN_SCANS, CLAIM_STALE_S


class RecoverySteps:
    def _open_runs(self):
        """[(task, run)] for every open run of a running or claimed task that this dispatcher is not following."""
        live_tasks = {tid for _, tid, _ in self.live.values()}
        out = []
        for status in ("running", "claimed", "gated"):      # a gated task may have a review in flight
            for t in self.port.list_tasks(status):
                if t["id"] not in live_tasks:
                    out += [(t, r) for r in self.port.list_runs(t["id"]) if r["ended"] is None]
        seen = {r['id'] for _, r in out}
        # Durable membership also finds enrollment before dispatch.access and resolved sources.
        for batch in self.port.waiting.replan_runs():
            run = batch['run']
            if run['ended'] is None and run['id'] not in seen and run['id'] not in self.live:
                out.append((self.port.get_task(run['task_id']), run))
                seen.add(run['id'])
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
        cleared = {e['data']['run'] for e in p.events('launch-cancel-cleared')}
        for event in p.events('launch-cancel'):
            rid = event['data']['run']
            if rid not in cleared:
                self.cancelling[rid] = (event['task_id'], places.get(rid, 'unknown'), nonces.get(rid))
        recovered, live_tasks = [], {tid for _, tid, _ in self.live.values()}
        for t in p.list_tasks("running"):
            runs = p.list_runs(t["id"])
            if p.waiting.pending(("task", t["id"])) or t["id"] in live_tasks or any(r["ended"] is None for r in runs):
                continue
            last = runs[-1] if runs else None
            if (last and last['id'] not in cleared and last["outcome"] == "launch-error"
                    and "cancel-requested" in (last["note"] or "")):
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
        launched = next((e['data'] for e in reversed(self.port.events('run-launching'))
                         if e['data'].get('run') == run['id']), {})
        spec = {"run_id": run["id"], "role": run["role"], "backend": run["backend"], "model": run["model"], "task": task,
                "adopted": True, "placement": placement, "wall_s": self._wall_s(task, policy),
                "idle_s": self._idle_s(policy), 'nonce': launched.get('nonce'),
                'control_epoch': launched.get('control_epoch') or 0, 'output_snapshot': launched.get('output_snapshot')}
        saved = next((e["data"] for e in self.port.events("dispatch.access")
                      if e["data"].get("run") == run["id"]), {})
        if "read_checkouts" in saved:
            spec.update(read_checkouts=saved["read_checkouts"], write=saved.get("write", True))
        if run['role'] == 'planner':
            batch = self.replanning.restore(run['id'])
            spec.update(seat=batch.assignment.get('seat'), request_ids=list(batch.request_ids), task_ids=list(batch.task_ids), write=False,
                        cwd=batch.cwd, read_dirs=list(batch.read_roots), read_checkouts=batch.read_checkouts,
                        launch_cancelled=run['id'] in self.cancelling,
                        sandbox='read-only', wall_s=600, idle_s=120)
        if hasattr(self.port, "conn"):
            row = self.port.conn.execute('SELECT data FROM run_stats WHERE run_id=?', (run["id"],)).fetchone()
            if row:
                spec["seat"] = json.loads(row["data"]).get("seat") or spec.get("seat")
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
            self.ci_runners.release(tid)
            p = self.port
            p.append_event("launch-cancel-cleared", {"run": rid}, tid)
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
        if spec.get('role') == 'planner':
            self.replanning.finish(run_id, {'status': contract.ORPHANED, 'text': '', 'usage': {}}, now=self.now())
            return task_id
        p.finish_run(run_id, "orphaned", note="runner gone with no terminal status")
        if spec.get("role") in ("gate", "review"):
            self.ci_runners.release(task_id)               # the gate or review is derived again from the ledger next tick
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
