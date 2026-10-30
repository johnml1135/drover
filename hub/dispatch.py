"""The dispatcher inside the hub (ADR 0002): a thread with DirectPort on its own ledger connection.
A file lock held for the thread's life keeps a second dispatcher (another hub, `python -m drover.dispatcher`) out.
"""
from drover import placement
import json
import logging
import threading
from pathlib import Path
from drover import ci, contract, disk, dispatch_repos, landing, origin, repos
from drover.dispatcher import Dispatcher, DispatchRuntime
from drover.launch import RunnerLauncher
from drover.ledger import ops, landing as landing_storage
from drover.lock import FileLock, LockHeld
from drover.notifications import Notifications
from drover.pipeline import Pipeline, truthy
from drover.port import DirectPort
from .events import CoalescedWake

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

    def set_status(self, task_id, new, reason=None, data=None):
        r = super().set_status(task_id, new, reason=reason, data=data)
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
    config says landing.push = true. The thread has its own ledger connection (the store's per-thread one)."""

    def __init__(self, store, settings, interval_s=LANDING_INTERVAL_S, repos_dir=None):
        self.store, self.settings, self.interval_s = store, settings, interval_s
        self._wake = CoalescedWake()
        self.repos_dir = repos_dir
        self.ci = ci.CI(settings.runs_dir, RunnerLauncher(runs_dir=settings.runs_dir, distro=self._distro))
        self._stop, self._thread, self.error = threading.Event(), None, None

    def _distro(self):
        return placement.distro(self.settings.distro, self.store.get_settings())

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="landing", daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            generation = self._wake.generation()
            if self._stop.is_set():
                break
            try:
                self.tick()
            except Exception as e:                     # a bad pass must not end the loop
                self.error = repr(e)
                log.exception("landing pass failed")
            self._wake.wait(generation, self.interval_s)

    def wake(self):
        """Request one more ledger-derived landing pass; this hint grants no landing eligibility."""
        self._wake.wake()

    def stop(self, timeout=10):
        self._stop.set()
        self._wake.wake()
        if self._thread is not None:
            self._thread.join(timeout)

    def tick(self):
        """One pass over every repo that has reviewed tasks. Returns {repo: batch result}."""
        conn = self.store.conn
        from drover.notifications.controls import admission
        control = admission(conn)
        if not control['allowed']:
            self.ci.stop_active(conn, control['epoch'])
            return {}
        if not truthy(ops.get_setting(conn, "dispatch_enabled", "false")):
            return {}
        allow = dispatch_repos.allowed(lambda k, d=None: ops.get_setting(conn, k, d))
        pipe = Pipeline(DirectPort(conn, actor="landing"), repos_dir=self.repos_dir, runs_dir=self.settings.runs_dir)
        out = {}
        for name in sorted({t["repo"] for t in ops.list_tasks(conn, status="reviewed")}):
            if not dispatch_repos.permits(allow, name):
                continue                             # blocked like pause: the owner limited dispatch to other repos
            prog = pipe.repo({"repo": name})
            if prog is None or repos.ci_policy(prog)["unknown"]:
                continue
            out[name] = self._land_repo(conn, name, prog)
        return out

    def _land_repo(self, conn, name, prog):
        from drover.notifications.controls import admission
        epoch = admission(conn)['epoch']
        repo = repos.native(repos.repo_path(prog))
        land = repos.landing_config(prog)
        push = land["push"]
        base_ref = repos.default_branch(prog)
        out_dir = Path(self.settings.runs_dir) / "landing"
        out_dir.mkdir(parents=True, exist_ok=True)
        work = Path(repos.native(repos.worktree_root(prog))) / f"drover-landing-{name.lower()}"
        try:
            self.ci.resume(prog, work, conn=conn, terminal_outcomes=True)
        except ci.CIDeferred as error:
            return {"outcome": "queued", "reason": str(error)}
        if disk.admission(disk.read())['verdict'] == 'hold':
            return {'outcome': contract.HOLD}
        size = 1 if land["policy"] == "single" else None
        def gate(checkout, commit, tasks, *, platform=None, signature=None, module=None, tier=None):
            ops.set_setting(conn, "landing_batch", json.dumps({"status": "landing", "tasks": [
                {"id": t["id"], "title": t["title"]} for t in tasks]}))
            if not tasks and not module:
                raise ci.CIInfrastructure(dict(reason='failed CI did not identify one diagnostic module', platform=platform))
            return self.ci.run(prog, checkout, commit, base=commit if module else base_ref, conn=conn, control_epoch=epoch,
                               platforms=[platform] if platform else None, module=module, tier=tier,
                               force_full=bool(platform) or any(t.get('base_repair') for t in tasks))
        try:
            r = landing.run_batch(conn, repo, gate, remote=repos.remote_name(prog),
                                  branch=base_ref, size=size, repo_name=name, workdir=work, push=push, control_epoch=epoch,
                                  required_platforms=repos.gate_platforms(prog), observe=origin.observe)
        finally:
            ops.set_setting(conn, "landing_batch", "")
            self._write_gate(conn)
            if getattr(self, "store", None) is not None:
                self.store._changed("landing.changed", repo=name)
        return r

    @staticmethod
    def _write_gate(conn):
        v = landing.velocity(conn)
        last = landing_storage.last_outcome(conn)
        ops.set_setting(conn, "gate", json.dumps({
            "status": last or "idle",
            "detail": landing_storage.last_detail(conn),
            "median": f"{v['median_gate_s']:.0f}s" if v["median_gate_s"] is not None else None,
            "red_rate": f"{v['red_rate']:.0%}" if v["red_rate"] is not None else None}))

class DispatcherService:
    def __init__(self, store, settings, make_dispatcher=None, lock_path=None, sleep=None, landing_service=None):
        self.store, self.settings = store, settings
        self.landing = landing_service if landing_service is not None else (
            LandingService(store, settings) if make_dispatcher is None else None)
        self.make_dispatcher = make_dispatcher or self._default_dispatcher
        self.lock = FileLock(lock_path or settings.dispatcher_lock)
        self._stop, self._thread = threading.Event(), None
        self._wake = CoalescedWake()
        self.dispatcher, self.error = None, None
        self._seat_home_distro = None

    def _distro(self):
        return placement.distro(self.settings.distro, self.store.get_settings())

    def _wsl_bins(self):
        """{'claude': path, 'codex': path} from the ledger settings ops-ui fills in; unset keys fall back to bare names."""
        s = self.store.get_settings()
        return {n: s.get(k) for n, k in WSL_BIN_KEYS.items() if s.get(k)}

    def _default_dispatcher(self, port):
        from drover.directors import DirectorRuntime, RunnerDirectorAdapter
        launcher = RunnerLauncher(runs_dir=self.settings.runs_dir, distro=self._distro, wsl_bins=self._wsl_bins,
                                  on_complete=self.wake_dispatcher)
        self._validate_seat_homes(port, force=True)
        runtime = DispatchRuntime(launcher, directors=DirectorRuntime(port, RunnerDirectorAdapter(launcher)),
                                  completion_wake=self.completion_committed)
        return Dispatcher(port, Pipeline(port, runs_dir=self.settings.runs_dir), runtime)

    def _validate_seat_homes(self, port, *, force=False):
        from drover import config, seat_paths
        distro = self._distro()
        if not force and distro == self._seat_home_distro:
            return
        errors = seat_paths.validate(config.get(), distro=distro)
        previous = json.loads(port.get_setting('seat_health', '{}'))
        port.set_setting('seat_health', json.dumps(errors, sort_keys=True))
        if errors:
            port.set_setting('dispatch_enabled', 'false')
            port.append_event('seat-home-error', {'seats': errors, 'paused': True, 'distro': distro})
        elif previous or self._seat_home_distro is not None:
            port.append_event('seat-home-cleared', {'seats': {}, 'distro': distro})
        # Clearing health never changes the owner's dispatch switch.
        self._seat_home_distro = distro

    def wake_dispatcher(self):
        """Request a dispatcher pass after a runner result becomes available."""
        self._wake.wake()

    def wake_landing(self):
        """Request a landing pass; normal reviewed-task selection remains authoritative."""
        if self.landing is not None:
            self.landing.wake()

    def completion_committed(self, run_ids):
        """Wake both ledger consumers only after dispatcher reap has committed its completion effects."""
        if run_ids:
            self.wake_dispatcher()
            self.wake_landing()

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
                generation = self._wake.generation()
                if self._stop.is_set():
                    break
                try:
                    self._validate_seat_homes(port)
                    self.dispatcher.tick()
                except Exception as e:                  # a bad tick must not end the dispatcher
                    self.error = repr(e)
                    log.exception("dispatcher tick failed")
                    try:
                        self.store.add_event("hub.dispatcher", "hub", {"message": f"tick failed: {e!r}"})
                    except Exception:
                        pass
                try:
                    # Dispatch reconciles stale questions before selecting wakes.
                    # This also runs on paused/failed ticks, independently of any agent.
                    notices = Notifications(port.conn).wake_landing_questions()
                    if notices:
                        self.store.publish('notification.owner-wake', {'questions': [n['question'] for n in notices]})
                except Exception as e:
                    self.error = repr(e)
                    log.exception('landing question monitor failed')
                self._wake.wait(generation, self.settings.dispatch_interval_s)
        except Exception as e:
            self.error = repr(e)
            log.exception("dispatcher thread died")

    def stop(self, timeout=10):
        self._stop.set()
        self._wake.wake()
        if self.landing is not None:
            self.landing.stop(timeout)
        if self._thread is not None:
            self._thread.join(timeout)
        if not self.running:
            self.lock.release()
