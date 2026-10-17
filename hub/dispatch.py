"""The dispatcher inside the hub (ADR 0002): a thread with DirectPort on its own ledger connection.

A file lock held for the thread's life keeps a second dispatcher (another hub, `python -m drover.dispatcher`) out.
"""
import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

from drover import gate, landing, repos, runner
from drover.dispatcher import Dispatcher
from drover.launch import RunnerLauncher
from drover.ledger import ops
from drover.lock import FileLock, LockHeld
from drover.pipeline import Pipeline, truthy
from drover.port import DirectPort

log = logging.getLogger("hub.dispatcher")


class HubPort(DirectPort):
    """DirectPort that also tells the UI (the bus) about what the dispatcher changes, and keeps the heartbeat the
    health page reads."""

    def __init__(self, store, **kw):
        super().__init__(store.conn, **kw)
        self.store = store

    def _pub(self, name, **data):
        self.store.publish(name, data)

    def claim_next(self, worker=None, exclude=()):
        t = super().claim_next(worker, exclude)
        if t:
            self._pub("task.claimed", id=t["id"])
        return t

    def start_run(self, task_id, role, backend, model=None, effort=None):
        rid = super().start_run(task_id, role, backend, model, effort)
        self._pub("run.started", id=rid, task_id=task_id)
        return rid

    def finish_run(self, run_id, outcome, **fields):
        r = super().finish_run(run_id, outcome, **fields)
        self._pub("run.finished", id=run_id, task_id=r["task_id"], outcome=outcome)
        return r

    def set_status(self, task_id, new, reason=None, blocker=None, data=None):
        r = super().set_status(task_id, new, reason=reason, blocker=blocker, data=data)
        self._pub("task.status", id=task_id, status=new)
        return r

    def heartbeat(self, data=None):
        super().heartbeat(data)
        self.store.put_heartbeat(dict(data or {}))


LANDING_INTERVAL_S = 120.0
WSL_BIN_KEYS = {"claude": "wsl_bin_claude", "codex": "wsl_bin_codex"}


class LandingService:
    """Every LANDING_INTERVAL_S while dispatch is enabled: land batches of reviewed tasks, per repo and land policy.

    It writes the settings `landing_batch` ({status, tasks:[{id,title}]}, empty when none runs) and `gate`
    ({status, median, red_rate}) that the Landing page reads. A repo lands on its local default branch unless its
    config says land.push = true. The thread has its own ledger connection (the store's per-thread one)."""

    def __init__(self, store, settings, interval_s=LANDING_INTERVAL_S, repos_dir=None, gate_kw=None, run=subprocess.run):
        self.store, self.settings, self.interval_s = store, settings, interval_s
        self.repos_dir, self.gate_kw, self.run = repos_dir, gate_kw or {}, run
        self._stop, self._thread, self.error = threading.Event(), None, None

    def _distro(self):
        return self.store.get_settings().get("wsl_distro") or self.settings.distro

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="landing", daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:                     # a bad pass must not end the loop
                self.error = repr(e)
                log.exception("landing pass failed")
            self._stop.wait(self.interval_s)

    def stop(self, timeout=10):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def gate_fn(self, prog, base_ref, out_dir, conn):
        """The batch gate: the repo's full command on its gate platform. A WSL gate started from the Windows hub
        runs `python3 -m drover.gate run` through wsl.exe with every path mapped to /mnt/<d>/."""
        command = prog["gate"]["commands"]["full"]
        platforms = repos.gate_platforms(prog)
        runs = [self._gate_on(x, command, base_ref, out_dir, conn, prog) for x in platforms]
        return runs[0] if len(runs) == 1 else self._both(runs, platforms)

    def _gate_on(self, platform, command, base_ref, out_dir, conn, prog):
        if platform == "wsl" and os.name == "nt":
            return self._wsl_gate(command, base_ref, out_dir, float(prog["gate"]["timeout_s"]) + 120)
        return landing.make_gate(command, base_ref, out_dir, priority="P1", db_path=self.store.path,
                                 max_seconds=int(prog["gate"]["timeout_s"]), **self.gate_kw)

    @staticmethod
    def _both(runs, platforms):
        """gate platform `both`: run the platforms in order and stop at the first red receipt (tagged with its
        platform); green only when every platform's receipt is green."""
        def run(worktree, tip, tasks):
            receipt = None
            for platform, one in zip(platforms, runs):
                receipt = one(worktree, tip, tasks)
                if receipt.get("status") == "failed":
                    return {**receipt, "platform": platform}
            return {**receipt, "platform": "+".join(platforms)}
        return run

    def _wsl_gate(self, command, base_ref, out_dir, timeout_s=3600.0):
        drover_dir = runner.to_wsl_path(str(Path(__file__).resolve().parents[1]))

        def run(worktree, tip, tasks):
            out = Path(out_dir) / f"gate-{tip[:12]}-{len(tasks)}.json"
            argv = ["wsl.exe", "-d", self._distro(), "--cd", drover_dir, "--exec", "python3", "-m", "drover.gate", "run",
                    "--out", runner.to_wsl_path(str(out)), "--repo", runner.to_wsl_path(str(worktree)), "--base",
                    base_ref, "--priority", "P1", "--json", "--", *command]
            try:
                proc = self.run(argv, capture_output=True, text=True, timeout=timeout_s)
            except subprocess.TimeoutExpired:
                raise gate.GateError(f"the WSL gate timed out after {timeout_s:g}s")
            try:
                return json.loads(out.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                stderr = '\n'.join((proc.stderr or '').splitlines()[-20:])
                raise gate.GateError(f"the WSL gate wrote no receipt (wsl.exe return code {proc.returncode}): {e}"
                                     + (f"\nstderr:\n{stderr}" if stderr else '')) from e
        return run

    def tick(self):
        """One pass over every repo that has reviewed tasks. Returns {repo: batch result}."""
        conn = self.store.conn
        if not truthy(ops.get_setting(conn, "dispatch_enabled", "false")):
            return {}
        pipe = Pipeline(DirectPort(conn, actor="landing"), repos_dir=self.repos_dir, runs_dir=self.settings.runs_dir)
        out = {}
        for name in sorted({t["repo"] for t in ops.list_tasks(conn, status="reviewed")}):
            prog = pipe.repo({"repo": name})
            if prog is None or prog["gate"]["unknown"]:
                continue
            out[name] = self._land_repo(conn, name, prog)
        return out

    def _land_repo(self, conn, name, prog):
        repo = repos.native(prog["repo"])
        push = prog["land"]["push"]
        base_ref = f"{prog['remote']}/{prog['default_branch']}" if push else prog["default_branch"]
        out_dir = Path(self.settings.runs_dir) / "landing"
        out_dir.mkdir(parents=True, exist_ok=True)
        work = Path(repos.native(prog["worktrees"])) / f"drover-landing-{name.lower()}"
        size = 1 if prog["land"]["policy"] == "single" else None
        hold = landing.dirty_checkout_hold(conn, repo, prog["default_branch"], name)
        if hold:
            return hold
        batch = landing.form_batch(conn, repo, base_ref, size, name) if not landing.held(
            conn, landing.git(repo, "rev-parse", base_ref).stdout.strip()) else []
        if not batch:
            return {"outcome": "empty"}
        tasks = [{"id": t["id"], "title": t["title"]} for t in batch]
        ops.set_setting(conn, "landing_batch", json.dumps({"status": "landing", "tasks": tasks}))
        try:
            r = landing.run_batch(conn, repo, self.gate_fn(prog, base_ref, out_dir, conn), remote=prog["remote"],
                                  branch=prog["default_branch"], size=size, repo_name=name, workdir=work, push=push)
        finally:
            ops.set_setting(conn, "landing_batch", "")
            self._write_gate(conn)
        if r["outcome"] == "refused" and "uncommitted changes" in str(r.get("error")):
            self._ask_clean(conn, name, r["error"])
        return r

    @staticmethod
    def _write_gate(conn):
        v = landing.velocity(conn)
        last = conn.execute("SELECT outcome FROM landing_batches ORDER BY id DESC LIMIT 1").fetchone()
        ops.set_setting(conn, "gate", json.dumps({
            "status": last[0] if last else "idle",
            "median": f"{v['median_gate_s']:.0f}s" if v["median_gate_s"] is not None else None,
            "red_rate": f"{v['red_rate']:.0%}" if v["red_rate"] is not None else None}))

    @staticmethod
    def _ask_clean(conn, name, error):
        """Landing waits for the owner's checkout of the default branch to be clean: ask once while that is open."""
        key = f"_land_dirty_q_{name.lower()}"
        qid = ops.get_setting(conn, key)
        if qid and ops.get_question(conn, int(qid))["status"] == "open":
            return
        qid = ops.ask_question(conn, "epic", landing._epic(conn, "Landing"), f"Landing {name} is waiting for a clean checkout",
                               error, options=[{"label": "Retry landing", "description": "it retries every 2 minutes anyway",
                                                "recommended": True}], actor="landing")
        ops.set_setting(conn, key, qid)


class DispatcherService:
    def __init__(self, store, settings, make_dispatcher=None, lock_path=None, sleep=None, landing_service=None):
        self.store, self.settings = store, settings
        self.landing = landing_service if landing_service is not None else (
            LandingService(store, settings) if make_dispatcher is None else None)
        self.make_dispatcher = make_dispatcher or self._default_dispatcher
        self.lock = FileLock(lock_path or settings.dispatcher_lock)
        self._stop, self._thread = threading.Event(), None
        self.dispatcher, self.error = None, None

    def _distro(self):
        return self.store.get_settings().get("wsl_distro") or self.settings.distro

    def _wsl_bins(self):
        """{'claude': path, 'codex': path} from the ledger settings ops-ui fills in; unset keys fall back to bare names."""
        s = self.store.get_settings()
        return {n: s.get(k) for n, k in WSL_BIN_KEYS.items() if s.get(k)}

    def _default_dispatcher(self, port):
        launcher = RunnerLauncher(runs_dir=self.settings.runs_dir, distro=self._distro(), wsl_bins=self._wsl_bins)
        return Dispatcher(port, launcher=launcher, pipeline=Pipeline(port, runs_dir=self.settings.runs_dir))

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        """Take the lock and start the thread. False (and an event) when another dispatcher holds the lock."""
        if self.running:
            return True
        try:
            self.lock.acquire()
        except LockHeld as e:
            log.error("dispatcher not started: %s", e)
            self.store.add_event("hub.dispatcher", "hub", {"message": f"dispatcher not started: {e}"})
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="dispatcher", daemon=True)
        self._thread.start()
        if self.landing is not None:
            self.landing.start()
        return True

    def _loop(self):
        try:
            port = HubPort(self.store)                  # built here: the ledger connection is per thread
            self.dispatcher = self.make_dispatcher(port)
            while not self._stop.is_set():
                try:
                    self.dispatcher.tick()
                except Exception as e:                  # a bad tick must not end the dispatcher
                    self.error = repr(e)
                    log.exception("dispatcher tick failed")
                    try:
                        self.store.add_event("hub.dispatcher", "hub", {"message": f"tick failed: {e!r}"})
                    except Exception:
                        pass
                self._stop.wait(self.settings.dispatch_interval_s)
        except Exception as e:
            self.error = repr(e)
            log.exception("dispatcher thread died")

    def stop(self, timeout=10):
        self._stop.set()
        if self.landing is not None:
            self.landing.stop(timeout)
        if self._thread is not None:
            self._thread.join(timeout)
        if not self.running:
            self.lock.release()
