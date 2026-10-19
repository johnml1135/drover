"""Cross-platform worker runner: owns one worker's process tree, timeouts and files under <runs-dir>/<run-id>/.

    python -m drover.runner --run-id ID --runs-dir DIR --wall-s N --idle-s N [--stdin-file F] [--detach] -- argv...
    python -m drover.runner stop --run-id ID --runs-dir DIR [--reason rotation|requested]
    python -m drover.runner find [--json]

Files: out.log (worker stdout+stderr, appended), status.json (atomic), record.json (last action record in the
output), stop-request (written by `stop`), runner.log (the runner's own stdout/stderr when detached).
POSIX: the worker is its own process group, killed with killpg. Windows: the worker is in a Job Object.
Callers must only signal pids returned by find_runners().
"""
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
KILL_AFTER_S = 15
POLL_S = 0.2
SCAN_BYTES = 8192
MAX_SCAN_TRIES = 20
NOTE_MAX = 300
BLOCKER_KEYS = ("kind", "detail")
STOP_FILE, STATUS_FILE, RECORD_FILE, OUT_FILE, RUNNER_LOG = "stop-request", "status.json", "record.json", \
    "out.log", "runner.log"
STOP_REASONS = ("rotation", "requested")
DISTRO_RE = re.compile(r"^[A-Za-z0-9._-]+$")
WINPATH_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$", re.S)
RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
NONCE_RE = re.compile(r"^[A-Za-z0-9]{16,64}$")
SHA_RE = re.compile(r"[0-9a-f]{7,40}")
ARTIFACT_URI_RE = re.compile(r"hub:tasks/[A-Za-z0-9][A-Za-z0-9._-]*/result\.md")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
PYTHON_RE = re.compile(r"(?i)python(w)?(\d+(\.\d+)?)?(\.exe)?")
KIND_MAX, DETAIL_MAX = 40, 2000
DETACH_WAIT_S = 5


class ScanError(RuntimeError):
    """The process list could not be read: callers must treat runners as unknown, not as absent."""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_dir(runs_dir, run_id):
    if not RUN_ID_RE.fullmatch(str(run_id)) or str(run_id) in (".", ".."):
        raise ValueError(f"bad run id {run_id!r}")
    return Path(runs_dir) / str(run_id)


# ---- files ------------------------------------------------------------------------------------------------
def write_json_atomic(path, obj):
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
    for i in range(20):           # Windows refuses the replace while a reader has the target open
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == 19:
                raise
            time.sleep(0.05)


def read_status(runs_dir, run_id):
    try:
        return json.loads((run_dir(runs_dir, run_id) / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---- action record ----------------------------------------------------------------------------------------
def _record_from(obj):
    """Normalize a worker claim; artifact persistence and delivery authority are checked by the dispatcher."""
    if not isinstance(obj, dict) or obj.get("status") not in ("done", "blocked", "ready_to_commit"):
        return None
    note, blocker = str(obj.get("note") or "")[:NOTE_MAX], obj.get("blocker")
    sha, status = str(obj.get("sha") or ""), obj["status"]
    sha = sha if SHA_RE.fullmatch(sha) else ""
    if status == "ready_to_commit":
        message = obj.get("commit_message")
        if isinstance(message, str) and message.strip() and "\0" not in message:
            return {"status": status, "sha": "", "commit_message": message.strip(), "blocker": None, "note": note}
        status, blocker = "blocked", {"kind": "no-commit-message", "detail": "ready_to_commit requires a commit message"}
    if status == "done" and "artifact" in obj:
        artifact = obj["artifact"]
        uri = artifact.get("uri") if isinstance(artifact, dict) else None
        digest = artifact.get("sha256") if isinstance(artifact, dict) else None
        if (obj.get("sha") in (None, "") and isinstance(uri, str) and ARTIFACT_URI_RE.fullmatch(uri)
                and isinstance(digest, str) and SHA256_RE.fullmatch(digest)):
            return {"status": status, "sha": "", "artifact": {"uri": uri, "sha256": digest},
                    "blocker": None, "note": note}
        status, blocker = "blocked", {"kind": "invalid-artifact",
                                      "detail": "done requires a normalized hub task-result URI and sha256, without a commit sha"}
    if status == "done" and not sha:
        status, blocker = "blocked", {"kind": "no-sha", "detail": "worker reported done without a valid commit sha"}
    if status == "blocked":
        if not isinstance(blocker, dict) or not all(blocker.get(k) for k in BLOCKER_KEYS):
            return None
        blocker = {"kind": str(blocker["kind"])[:KIND_MAX], "detail": str(blocker["detail"])[:DETAIL_MAX]}
    else:
        blocker = None
    return {"status": status, "sha": sha, "blocker": blocker, "note": note}


def parse_action_record(text):
    """The last valid action record in the last SCAN_BYTES of text, or None. Bounded: at most MAX_SCAN_TRIES
    candidate objects, walking back from each "status" key."""
    text = (text or "")[-SCAN_BYTES:]
    dec, tries, end = json.JSONDecoder(), 0, len(text)
    while tries < MAX_SCAN_TRIES:
        pos = text.rfind('"status"', 0, end)
        if pos < 0:
            return None
        end, start = pos, pos
        for _ in range(4):               # the record's own brace may sit behind a nested one
            start = text.rfind("{", 0, start)
            if start < 0:
                break
            tries += 1
            try:
                obj, _ = dec.raw_decode(text[start:])
            except ValueError:
                continue
            rec = _record_from(obj)
            if rec:
                return rec
    return None


def _message_text(ev):
    """The text of a final-message event (claude result or assistant, codex agent_message), else None."""
    if not isinstance(ev, dict):
        return None
    t = ev.get("type")
    if t == "result" and isinstance(ev.get("result"), str):
        return ev["result"]
    if t == "assistant" and isinstance(ev.get("message"), dict):
        content = ev["message"].get("content")
        texts = [b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)] \
            if isinstance(content, list) else []
        return "\n".join(texts) if texts else None
    item = ev.get("item")
    if t == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message" \
            and isinstance(item.get("text"), str):
        return item["text"]
    return None


def scan_record(out_path):
    """The action record in the last SCAN_BYTES of out.log, trusted only from the final assistant or result
    message of a stream-json / codex stream. Anything else (a tool result the worker printed) can only yield a
    blocked record, never done."""
    try:
        with open(out_path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - SCAN_BYTES))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    last = None
    for line in tail.splitlines():
        try:
            text = _message_text(json.loads(line))
        except ValueError:
            continue
        if text is not None:
            last = text
    rec = parse_action_record(last) if last is not None else None
    if rec:
        return rec
    # unframed output or a cut-off event: only a blocked record is believed
    rec = parse_action_record(tail) or parse_action_record(tail.replace('\\"', '"'))
    return rec if rec and rec["status"] == "blocked" else None


# ---- process tree -----------------------------------------------------------------------------------------
class PosixTree:
    def __init__(self, proc):
        self.proc, self.pgid = proc, proc.pid

    def _group_alive(self):
        try:
            os.killpg(self.pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _sig(self, sig):
        try:
            os.killpg(self.pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def kill(self, grace=KILL_AFTER_S):
        """SIGTERM the group, then SIGKILL whatever is left after grace seconds."""
        self._sig(signal.SIGTERM)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            self.proc.poll()            # reap the leader so a zombie does not keep the group "alive"
            if not self._group_alive():
                break
            time.sleep(0.05)
        if self._group_alive():          # not after the group is gone: the pgid could be reused
            self._sig(signal.SIGKILL)
        self.proc.wait()

    def reap_stragglers(self):
        """The worker exited; end anything it left behind in the group."""
        if self._group_alive():
            self._sig(signal.SIGKILL)

    def close(self):
        pass


if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    class _BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _IOC(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("ro", "wo", "oo", "rb", "wb", "ob")]

    class _EXT(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BASIC), ("IoInfo", _IOC), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JOB_BASIC_AND_IO_ACCOUNTING_CLASS = 8
    JOB_EXTENDED_LIMIT_CLASS = 9
    CREATE_SUSPENDED = 0x4
    CREATE_NEW_PROCESS_GROUP = 0x200
    CREATE_NO_WINDOW = 0x08000000
    DETACHED_PROCESS = 0x8
    CREATE_BREAKAWAY_FROM_JOB = 0x01000000

    def _resume(handle):
        nt = ctypes.WinDLL("ntdll")
        nt.NtResumeProcess.argtypes = [wintypes.HANDLE]
        return nt.NtResumeProcess(int(handle))

    class WindowsTree:
        """A Job Object that kills its whole tree on TerminateJobObject, or when the runner dies (KILL_ON_CLOSE)."""

        def __init__(self, proc):
            self.proc, self.job = proc, None
            try:
                self.job = self._make_job(proc)
                if _resume(proc._handle) != 0:      # the worker started suspended so no child escapes the job
                    raise OSError("could not resume the suspended worker")
            except BaseException:
                self.close_and_kill()
                raise

        @staticmethod
        def _make_job(proc):
            job = _k32.CreateJobObjectW(None, None)
            if not job:
                raise ctypes.WinError(ctypes.get_last_error())
            info = _EXT()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _k32.SetInformationJobObject(job, JOB_EXTENDED_LIMIT_CLASS, ctypes.byref(info),
                                                ctypes.sizeof(info)) \
                    or not _k32.AssignProcessToJobObject(job, int(proc._handle)):
                err = ctypes.get_last_error()
                _k32.CloseHandle(job)
                raise ctypes.WinError(err)
            return job

        def metrics(self):
            class Accounting(ctypes.Structure):
                _fields_ = [(n, ctypes.c_int64) for n in ("user", "kernel", "period_user", "period_kernel")] + [
                    (n, wintypes.DWORD) for n in ("faults", "total", "active", "terminated")]
            class AccountingAndIO(ctypes.Structure):
                _fields_ = [("BasicInfo", Accounting), ("IoInfo", _IOC)]
            accounting, ext = AccountingAndIO(), _EXT()
            for kind, obj in ((JOB_BASIC_AND_IO_ACCOUNTING_CLASS, accounting), (JOB_EXTENDED_LIMIT_CLASS, ext)):
                if not _k32.QueryInformationJobObject(self.job, kind, ctypes.byref(obj), ctypes.sizeof(obj), None):
                    raise ctypes.WinError(ctypes.get_last_error())
            basic = accounting.BasicInfo
            return ((basic.user + basic.kernel) / 10_000_000, ext.PeakJobMemoryUsed,
                    accounting.IoInfo.rb + accounting.IoInfo.wb)

        def close_and_kill(self):
            if self.job:
                _k32.TerminateJobObject(self.job, 1)
                _k32.CloseHandle(self.job)
                self.job = None
            self.proc.kill()
            self.proc.wait()

        def kill(self, grace=KILL_AFTER_S):
            if self.job:
                _k32.TerminateJobObject(self.job, 1)
            try:
                self.proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()

        def reap_stragglers(self):
            if self.job:
                _k32.TerminateJobObject(self.job, 1)

        def close(self):
            if self.job:
                _k32.CloseHandle(self.job)
                self.job = None


def spawn_worker(argv, out_f, stdin_f, cwd, env):
    """(proc, tree). The worker's stdout and stderr both go to out_f."""
    kw = dict(stdin=stdin_f, stdout=out_f, stderr=subprocess.STDOUT, cwd=cwd, env=env)
    if IS_WINDOWS:
        proc = subprocess.Popen(argv, creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_SUSPENDED | CREATE_NO_WINDOW,
                                **kw)
        return proc, WindowsTree(proc)
    proc = subprocess.Popen(argv, start_new_session=True, **kw)
    return proc, PosixTree(proc)


# ---- supervising ------------------------------------------------------------------------------------------
class Status:
    def __init__(self, path, run_id, nonce):
        self.path = path
        self.d = {"run_id": run_id, "state": "starting", "pid": None, "runner_pid": os.getpid(),
                  "started": now_iso(), "ended": None, "exit_code": None, "reason": None, "nonce": nonce,
                  "detached_in_job": os.environ.get("DROVER_DETACHED_IN_JOB") == "1"}

    def set(self, **kw):
        self.d.update(kw)
        write_json_atomic(self.path, self.d)


def _read_stop_file(d):
    try:
        obj = json.loads((d / STOP_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def read_stop(d, nonce):
    """The stop reason when a stop-request carrying this launch's nonce exists, else None."""
    obj = _read_stop_file(d)
    if not obj or obj.get("nonce") != nonce:
        return None
    return obj.get("reason") if obj.get("reason") in STOP_REASONS else "requested"


def request_stop(runs_dir, run_id, reason="requested", nonce=None):
    """Write the stop-request for the launch recorded in status.json. False when there is no launch to stop.

    A caller that chose the launch's nonce (--nonce) passes it: the request can then be written before status.json
    exists, and a runner that starts late honours it."""
    if nonce:
        if not NONCE_RE.fullmatch(str(nonce)):
            raise ValueError("bad nonce")
        d = run_dir(runs_dir, run_id)
        d.mkdir(parents=True, exist_ok=True)
        write_json_atomic(d / STOP_FILE, {"reason": reason, "nonce": str(nonce)})
        return True
    st = read_status(runs_dir, run_id)
    if not st or not st.get("nonce"):
        return False
    write_json_atomic(run_dir(runs_dir, run_id) / STOP_FILE, {"reason": reason, "nonce": st["nonce"]})
    return True


def _scratch_path(value):
    root, path = Path("/tmp/drover-scratch"), Path(value)
    parent = path.parent
    direct = parent == root
    task_scratch = Path(os.environ.get("TMPDIR", ""))
    nested = (not direct and parent == task_scratch and parent.parent == root and parent.is_dir()
              and not parent.is_symlink() and parent.resolve() == parent)
    if (IS_WINDOWS or (not direct and not nested) or root.resolve() != root
            or path.is_symlink() or path.resolve().parent != parent):
        raise ValueError("scratch must be under /tmp/drover-scratch/ or its assigned TMPDIR")
    return path


def _cleanup_scratch(path):
    try:
        shutil.rmtree(_scratch_path(path))
    except (OSError, ValueError):
        pass


def worker_scratch_env(scratch):
    """Environment for a WSL worker's private temp and .NET caches; the global NuGet package cache stays shared."""
    scratch = Path(scratch)
    return {
        "TMPDIR": str(scratch), "TMP": str(scratch), "TEMP": str(scratch),
        "DOTNET_CLI_HOME": str(scratch / "dotnet-home"),
        "NUGET_HTTP_CACHE_PATH": str(scratch / "nuget-http"),
        "NUGET_PLUGINS_CACHE_PATH": str(scratch / "nuget-plugins"),
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1",
        "NUGET_PACKAGES": str(Path.home() / ".nuget" / "packages"),
    }


def supervise(run_id, runs_dir, argv, wall_s=0, idle_s=0, stdin_file=None, cwd=None, kill_after=KILL_AFTER_S,
              poll_s=POLL_S, nonce=None, scratch_dir=None):
    """Run the worker to its end. Returns the final status dict."""
    d = run_dir(runs_dir, run_id)
    d.mkdir(parents=True, exist_ok=True)
    out = d / OUT_FILE
    nonce = nonce or os.environ.get("DROVER_RUN_NONCE") or uuid.uuid4().hex
    st = Status(d / STATUS_FILE, run_id, nonce)
    st.set()
    stale = _read_stop_file(d)
    if stale is not None and stale.get("nonce") != nonce:       # left by an earlier launch of this run id
        try:
            (d / STOP_FILE).unlink()
        except OSError:
            pass
    sig_reason, old_handlers = [], {}
    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        if hasattr(signal, name):
            sig = getattr(signal, name)
            old_handlers[sig] = signal.signal(sig, lambda *_: sig_reason.append("signal"))
    scratch = None
    try:
        if scratch_dir:
            try:
                path = _scratch_path(scratch_dir)
                path.parent.mkdir(mode=0o700, exist_ok=True)
                path.mkdir(mode=0o700)
                scratch = path
                path.chmod(0o700)
            except (OSError, ValueError) as e:
                st.set(state="exited", ended=now_iso(), exit_code=None, reason=f"spawn-failed: {e}")
                return st.d
        return _supervise(st, d, out, argv, wall_s, idle_s, stdin_file, cwd, kill_after, poll_s, nonce, sig_reason,
                          scratch)
    finally:
        if scratch is not None:
            _cleanup_scratch(scratch)
        for sig, h in old_handlers.items():
            signal.signal(sig, h)


def _supervise(st, d, out, argv, wall_s, idle_s, stdin_file, cwd, kill_after, poll_s, nonce, sig_reason, scratch=None):
    env = {k: v for k, v in os.environ.items() if k not in ("DROVER_RUN_NONCE", "DROVER_DETACHED_IN_JOB")}
    env["DROVER_RUN_ID"] = st.d["run_id"]
    if scratch is not None:
        env.update(worker_scratch_env(scratch))
    for key in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        if env.get(key):
            env[key] = os.path.expanduser(env[key])
    stdin_f = subprocess.DEVNULL
    try:
        if stdin_file:
            stdin_f = open(stdin_file, "rb")
        with open(out, "ab") as out_f:
            proc, tree = spawn_worker(argv, out_f, stdin_f, cwd, env)
    except OSError as e:
        st.set(state="exited", ended=now_iso(), exit_code=None, reason=f"spawn-failed: {e}")
        return st.d
    finally:
        if stdin_file and stdin_f is not subprocess.DEVNULL:
            stdin_f.close()
    from .metrics import Peaks, proc_group
    peaks = Peaks(tree.metrics if IS_WINDOWS else lambda: proc_group(proc.pid))
    watch = None
    try:
        st.set(state="running", pid=proc.pid)
        watch = open(out, "rb")        # an open handle's size is exact on Windows, where stat() can lag
        t0 = last_change = time.monotonic()
        size, outcome = 0, None
        while proc.poll() is None:
            now = time.monotonic()
            if peaks.last is None or now - peaks.last[0] >= 1:
                st.d["peaks"] = dict(peaks.sample())
            watch.seek(0, 2)
            if watch.tell() != size:
                size, last_change = watch.tell(), now
            stop = read_stop(d, nonce) or (sig_reason[0] if sig_reason else None)
            if stop:
                outcome = ("killed", stop)
            elif wall_s and now - t0 > wall_s:
                outcome = ("timeout", f"wall-clock limit {wall_s:g}s")
            elif idle_s and now - last_change > idle_s:
                outcome = ("timeout", f"no output for {idle_s:g}s")
            if outcome:
                tree.kill(kill_after)
                break
            time.sleep(poll_s)
        else:
            tree.reap_stragglers()
        rc = proc.wait()
        state, reason = outcome or ("exited", None)
        rec = scan_record(out)
        if rec:
            write_json_atomic(d / RECORD_FILE, rec)
        st.set(state=state, reason=reason, exit_code=rc, ended=now_iso(), peaks=dict(peaks.sample()))
    except BaseException:
        try:
            tree.kill(min(kill_after, 3))       # never leave the worker running unsupervised
        finally:
            raise
    finally:
        if watch:
            watch.close()
        tree.close()
    return st.d


# ---- detaching --------------------------------------------------------------------------------------------
def detach(run_id, runs_dir, child_args, wait_s=DETACH_WAIT_S, nonce=None):
    """Re-launch this runner detached. Returns {runner_pid, run_id, dir, detached_in_job} once the child has left
    state `starting`, or {error, ...} if it died or did not start within wait_s."""
    d = run_dir(runs_dir, run_id)
    d.mkdir(parents=True, exist_ok=True)
    pkg_root = str(Path(__file__).resolve().parents[1])
    nonce = nonce or uuid.uuid4().hex
    env = {**os.environ, "DROVER_RUN_NONCE": nonce,
           "PYTHONPATH": os.pathsep.join(filter(None, [pkg_root, os.environ.get("PYTHONPATH")]))}
    cmd = [sys.executable, "-m", "drover.runner", *child_args]
    in_job = False
    write_json_atomic(d / STATUS_FILE, {"run_id": run_id, "state": "starting", "pid": None, "runner_pid": None,
                                        "started": now_iso(), "ended": None, "exit_code": None, "reason": None,
                                        "nonce": nonce, "detached_in_job": False})
    with open(d / RUNNER_LOG, "ab") as log:
        kw = dict(stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True)
        if IS_WINDOWS:
            flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            try:
                proc = subprocess.Popen(cmd, env=env, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kw)
            except OSError:         # the parent's job does not allow breakaway: the runner dies with that job
                in_job = True
                log.write(b"warning: breakaway from the parent job was refused; this runner is still in it\n")
                log.flush()
                proc = subprocess.Popen(cmd, env={**env, "DROVER_DETACHED_IN_JOB": "1"}, creationflags=flags, **kw)
        else:
            proc = subprocess.Popen(cmd, env=env, start_new_session=True, **kw)
    # proc.pid may be a venv launcher over the real runner: report the pid the runner wrote itself
    info = {"runner_pid": None, "launcher_pid": proc.pid, "run_id": run_id, "dir": str(d),
            "detached_in_job": in_job}
    deadline = time.monotonic() + wait_s
    while True:
        st = read_status(runs_dir, run_id)
        if st and st.get("state") != "starting" and st.get("runner_pid"):
            info["runner_pid"] = st["runner_pid"]
            return info
        if proc.poll() is not None and not (st and st.get("state") != "starting") or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    info["error"] = "runner exited before starting the worker" if proc.poll() is not None \
        else f"runner still starting after {wait_s:g}s"
    return info


# ---- finding runners --------------------------------------------------------------------------------------
def parse_runner_argv(argv):
    """The run id when argv is exactly `python -m drover.runner [run args] --run-id ID [...]`, else None.

    Exact on purpose: argv[0] must be a python interpreter and argv[1:3] must be `-m drover.runner`, so a wrapper
    (wsl.exe, bash -c), a script that mentions the marker, or the stop/find subcommands never match. --run-id is
    only read before any `--`, so a worker's own arguments cannot spoof it."""
    argv = list(argv)
    if len(argv) < 4 or not PYTHON_RE.fullmatch(re.split(r"[\\/]", argv[0])[-1]) or argv[1:3] != ["-m", "drover.runner"]:
        return None
    rest = argv[3:]
    if "--" in rest:
        rest = rest[:rest.index("--")]
    if rest and rest[0] in ("stop", "find"):
        return None
    for j, a in enumerate(rest):
        if a == "--run-id" and j + 1 < len(rest) and RUN_ID_RE.fullmatch(rest[j + 1]):
            return rest[j + 1]
        if a.startswith("--run-id=") and RUN_ID_RE.fullmatch(a[9:]):
            return a[9:]
    return None


def _proc_cmdlines_linux():
    for e in os.scandir("/proc"):
        if e.name.isdigit():
            try:
                raw = Path(e.path, "cmdline").read_bytes()
            except OSError:
                continue
            yield int(e.name), [a.decode("utf-8", errors="replace") for a in raw.split(b"\0") if a]


def _split_windows_cmdline(line):
    n = ctypes.c_int()
    shell32 = ctypes.WinDLL("shell32")
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    ptr = shell32.CommandLineToArgvW(line, ctypes.byref(n))
    if not ptr:
        return []
    try:
        return [ptr[i] for i in range(n.value)]
    finally:
        k = ctypes.WinDLL("kernel32")
        k.LocalFree.argtypes = [ctypes.c_void_p]
        k.LocalFree(ptr)


def _powershell():
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    p = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(p) if p.exists() else "powershell"


def _proc_cmdlines_windows():
    """(pid, argv, parent pid) rows from CIM. Raises ScanError when the list cannot be read."""
    script = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
              "Get-CimInstance Win32_Process | Select ProcessId,ParentProcessId,CommandLine | ConvertTo-Json")
    try:
        r = subprocess.run([_powershell(), "-NoProfile", "-Command", script], capture_output=True, timeout=60)
        if r.returncode != 0:
            raise ScanError(f"powershell exited {r.returncode}")
        text = r.stdout.decode("utf-8", errors="replace").strip()
        rows = json.loads(text)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        raise ScanError(f"cannot list processes: {e!r}") from e
    rows = [rows] if isinstance(rows, dict) else rows
    if not isinstance(rows, list) or not rows:
        raise ScanError("process list came back empty")
    out = []
    for row in rows:
        if isinstance(row, dict) and row.get("CommandLine") and isinstance(row.get("ProcessId"), int):
            out.append((row["ProcessId"], _split_windows_cmdline(row["CommandLine"]), row.get("ParentProcessId")))
    return out


def find_runners(run_id=None, cmdlines=None):
    """[{pid, run_id}] for live runner processes, one per run id. A venv launcher over its child python gives two
    matching processes; the child is reported, as it is the runner whose pid status.json records. Raises ScanError when the process
    list cannot be read on Windows: treat that as unknown, never as no runners.

    `cmdlines` (iterable of (pid, argv[, parent pid])) is for injection in tests."""
    if cmdlines is None:
        cmdlines = _proc_cmdlines_windows() if IS_WINDOWS else _proc_cmdlines_linux()
    by_id = {}
    for row in cmdlines:
        pid, argv, ppid = (tuple(row) + (None,))[:3]
        rid = parse_runner_argv(argv)
        if rid and (run_id is None or rid == run_id):
            by_id.setdefault(rid, []).append((pid, ppid))
    found = []
    for rid, procs in by_id.items():
        parents = {pp for _, pp in procs}
        leaves = [p for p, _ in procs if p not in parents] or [p for p, _ in procs]
        found.append({"pid": min(leaves), "run_id": rid})
    return sorted(found, key=lambda r: r["pid"])


# ---- from Windows into WSL --------------------------------------------------------------------------------
def to_wsl_path(arg):
    """C:\\drover\\runs -> /mnt/c/drover/runs; also `--opt=R:\\x`. Other arguments pass through."""
    m = WINPATH_RE.match(arg)
    if m:
        return f"/mnt/{m.group(1).lower()}/" + m.group(2).replace("\\", "/")
    if arg.startswith("--") and "=" in arg:
        k, _, v = arg.partition("=")
        if WINPATH_RE.match(v):
            return f"{k}={to_wsl_path(v)}"
    return arg


def wsl_argv(distro, runner_args, cd=None, env=None):
    """wsl.exe -d <distro> [--cd <dir>] --exec python3 -m drover.runner <args>.

    --exec runs python3 directly, with no shell, so no argument is expanded or re-quoted. Windows paths are mapped
    to /mnt/<d>/ in the runner's own options (before `--`) and in cd; the worker's argv after `--` is never changed.
    env ({NAME: value}, values mapped the same way) is passed through /usr/bin/env, since a Windows variable does not
    cross wsl.exe by itself."""
    if not DISTRO_RE.fullmatch(str(distro)) or str(distro).startswith("-"):
        raise ValueError(f"bad WSL distro name {distro!r}")
    args = [str(a) for a in runner_args]
    split = args.index("--") if "--" in args else len(args)
    mapped = [to_wsl_path(a) for a in args[:split]] + args[split:]
    argv = ["wsl.exe", "-d", distro]
    if cd:
        argv += ["--cd", to_wsl_path(str(cd))]
    if env:
        for name, value in env.items():
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", str(name)):
                raise ValueError(f"bad environment variable name {name!r}")
        argv += ["--exec", "/usr/bin/env", *[f"{k}={to_wsl_path(str(v))}" for k, v in env.items()], "python3"]
    else:
        argv += ["--exec", "python3"]
    return argv + ["-m", "drover.runner", *mapped]


# ---- CLI --------------------------------------------------------------------------------------------------
def _num(s):
    v = float(s)
    if v < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return v


def _nonce(s):
    if not NONCE_RE.fullmatch(s):
        raise argparse.ArgumentTypeError("must be 16-64 letters or digits")
    return s


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "stop":
        p = argparse.ArgumentParser(prog="drover.runner stop")
        p.add_argument("--run-id", required=True)
        p.add_argument("--runs-dir", required=True)
        p.add_argument("--reason", choices=STOP_REASONS, default="requested")
        p.add_argument("--nonce", type=_nonce, help="the launch nonce, when status.json may not exist yet")
        a = p.parse_args(argv[1:])
        if not request_stop(a.runs_dir, a.run_id, a.reason, a.nonce):
            print(f"no launch recorded for {a.run_id}; nothing to stop", file=sys.stderr)
            return 2
        print(f"stop requested for {a.run_id} ({a.reason})")
        return 0
    if argv and argv[0] == "find":
        p = argparse.ArgumentParser(prog="drover.runner find")
        p.add_argument("--json", action="store_true")
        a = p.parse_args(argv[1:])
        try:
            runners = find_runners()
        except ScanError as e:
            print(f"unknown: {e}", file=sys.stderr)
            return 3
        print(json.dumps(runners) if a.json else "\n".join(f"{r['pid']} {r['run_id']}" for r in runners))
        return 0
    head, worker = (argv[:argv.index("--")], argv[argv.index("--") + 1:]) if "--" in argv else (argv, [])
    p = argparse.ArgumentParser(prog="drover.runner")
    p.add_argument("--run-id", required=True)
    p.add_argument("--runs-dir", required=True)
    p.add_argument("--wall-s", type=_num, default=0)
    p.add_argument("--idle-s", type=_num, default=0)
    p.add_argument("--stdin-file")
    p.add_argument("--cwd")
    p.add_argument("--scratch-dir", help="private worker temp directory under /tmp/drover-scratch/ (WSL only)")
    p.add_argument("--kill-after", type=_num, default=KILL_AFTER_S)
    p.add_argument("--detach", action="store_true")
    p.add_argument("--nonce", type=_nonce, help="the launch nonce (default: generated); stop-requests must carry it")
    a = p.parse_args(head)
    if not worker:
        p.error("no worker command after --")
    if a.detach:
        info = detach(a.run_id, a.runs_dir, [x for x in head if x != "--detach"] + ["--"] + worker, nonce=a.nonce)
        print(json.dumps(info))
        return 1 if "error" in info else 0
    st = supervise(a.run_id, a.runs_dir, worker, a.wall_s, a.idle_s, a.stdin_file, a.cwd, a.kill_after, nonce=a.nonce,
                   scratch_dir=a.scratch_dir)
    print(json.dumps(st))
    return 0 if st["state"] == "exited" and st["exit_code"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
