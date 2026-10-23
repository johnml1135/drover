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
from . import placement
from .placement import IS_WINDOWS, KILL_AFTER_S, ScanError

POLL_S = 0.2
WATCH_GLOBS = ("*.step*.log",)       # additional output files watched by existing worker runs
SCAN_BYTES = 8192
MAX_SCAN_TRIES = 20
NOTE_MAX = 300
BLOCKER_KEYS = ("kind", "detail")
STOP_FILE, STATUS_FILE, RECORD_FILE, OUT_FILE, RUNNER_LOG = "stop-request", "status.json", "record.json", \
    "out.log", "runner.log"
STOP_REASONS = ("rotation", "requested")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
NONCE_RE = re.compile(r"^[A-Za-z0-9]{16,64}$")
SHA_RE = re.compile(r"[0-9a-f]{7,40}")
ARTIFACT_URI_RE = re.compile(r"hub:tasks/[A-Za-z0-9][A-Za-z0-9._-]*/result\.md")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
KIND_MAX, DETAIL_MAX = 40, 2000
DETACH_WAIT_S = 5


PYTHON_RE = re.compile(r"(?i)python(w)?(\d+(\.\d+)?)?(\.exe)?")


def parse_runner_argv(argv):
    """Recognize only a direct Python runner invocation; worker arguments cannot spoof the marker."""
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


def _runner_runs_dir(argv):
    """The runs directory from a valid runner command line, else None."""
    argv = list(argv)
    if parse_runner_argv(argv) is None:
        return None
    rest = argv[3:]
    if "--" in rest:
        rest = rest[:rest.index("--")]
    for j, arg in enumerate(rest):
        if arg == "--runs-dir" and j + 1 < len(rest):
            return rest[j + 1]
        if arg.startswith("--runs-dir="):
            return arg[len("--runs-dir="):]
    return None


def _same_path(left, right):
    try:
        return os.path.normcase(os.path.abspath(os.path.expanduser(str(left)))) == \
            os.path.normcase(os.path.abspath(os.path.expanduser(str(right))))
    except (OSError, TypeError, ValueError):
        return False


def find_runners(run_id=None, cmdlines=None, runs_dir=None):
    """One live leaf runner per run id, optionally restricted to an owner directory.

    Failed discovery raises ScanError; it never means there are no runners.
    """
    if cmdlines is None:
        cmdlines = placement.process_cmdlines()
    by_id = {}
    for row in cmdlines:
        pid, argv, ppid = (tuple(row) + (None,))[:3]
        rid = parse_runner_argv(argv)
        if rid and (run_id is None or rid == run_id) and \
                (runs_dir is None or _same_path(_runner_runs_dir(argv), runs_dir)):
            by_id.setdefault(rid, []).append((pid, ppid))
    found = []
    for rid, procs in by_id.items():
        parents = {pp for _, pp in procs}
        leaves = [p for p, _ in procs if p not in parents] or [p for p, _ in procs]
        found.append({"pid": min(leaves), "run_id": rid})
    return sorted(found, key=lambda r: r["pid"])


def scratch_path(value):
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
    """The last valid action record in the complete final message, or None."""
    text = text or ""
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
        texts = [b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text"
                 and isinstance(b.get("text"), str)] \
            if isinstance(content, list) else []
        return "\n".join(texts) if texts else None
    item = ev.get("item")
    if t == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message" \
            and isinstance(item.get("text"), str):
        return item["text"]
    return None


def _reverse_lines(path):
    """Read complete lines backwards, including arbitrarily long stream JSON events."""
    with open(path, 'rb') as stream:
        stream.seek(0, 2)
        end, fragments = stream.tell(), []
        while end:
            start = max(0, end - SCAN_BYTES)
            stream.seek(start)
            parts = stream.read(end - start).split(b'\n')
            if len(parts) == 1:
                fragments.append(parts[0])
            else:
                yield parts[-1] + b''.join(reversed(fragments))
                yield from reversed(parts[1:-1])
                fragments = [parts[0]]
            end = start
        if fragments:
            yield b''.join(reversed(fragments))


def scan_record(out_path):
    """Trust only the final assistant/result message, even beyond the last 8 KB.

    Unframed output may report a blocker, but never authorize delivery.
    """
    try:
        tail = []
        size = 0
        for raw in _reverse_lines(out_path):
            if size < SCAN_BYTES:
                tail.append(raw[-SCAN_BYTES:])
                size += len(raw)
            try:
                text = _message_text(json.loads(raw))
            except (ValueError, UnicodeDecodeError):
                continue
            if text is not None:
                return parse_action_record(text)
    except OSError:
        return None
    text = b'\n'.join(reversed(tail)).decode('utf-8', errors='replace')
    rec = parse_action_record(text) or parse_action_record(text.replace('\\"', '"'))
    return rec if rec and rec["status"] == "blocked" else None


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


def _cleanup_scratch(path):
    try:
        shutil.rmtree(scratch_path(path))
    except (OSError, ValueError):
        pass


def _watched_sizes(d, globs):
    """((name, size), ...) of the run directory files matching `globs`, sorted by name."""
    seen = []
    for g in globs:
        for f in d.glob(g):
            try:
                seen.append((f.name, f.stat().st_size))
            except OSError:
                pass
    return tuple(sorted(seen))


def supervise(run_id, runs_dir, argv, wall_s=0, idle_s=0, stdin_file=None, cwd=None, kill_after=KILL_AFTER_S,
              poll_s=POLL_S, nonce=None, scratch_dir=None, watch_globs=WATCH_GLOBS, run_config=None):
    """Run the worker to its end. Returns the final status dict. Growth of out.log or of any file in the run
    directory matching `watch_globs` (a gate's per-step logs) counts as activity for the idle watchdog."""
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
                path = scratch_path(scratch_dir)
                path.parent.mkdir(mode=0o700, exist_ok=True)
                path.mkdir(mode=0o700)
                scratch = path
                path.chmod(0o700)
            except (OSError, ValueError) as e:
                st.set(state="exited", ended=now_iso(), exit_code=None, reason=f"spawn-failed: {e}",
                       missing_executable=False)
                return st.d
        with placement.cleanup_process_record(Path(runs_dir).parent):
            return _supervise(st, d, out, argv, wall_s, idle_s, stdin_file, cwd, kill_after, poll_s, nonce, sig_reason,
                              scratch, watch_globs, run_config)
    finally:
        if scratch is not None:
            _cleanup_scratch(scratch)
        for sig, h in old_handlers.items():
            signal.signal(sig, h)


def prepare_writable_roots(roots):
    """Pre-create explicitly configured sandbox roots under /tmp, refusing traversal and symlinks."""
    for value in placement.sandbox_roots(roots):
        path = Path(value)
        if not path.is_absolute() or not path.is_relative_to('/tmp') or path == Path('/tmp') \
                or '..' in path.parts or path.resolve() != path:
            raise ValueError(f"sandbox writable root must be under /tmp without symlinks: {value}")
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o777)


def _supervise(st, d, out, argv, wall_s, idle_s, stdin_file, cwd, kill_after, poll_s, nonce, sig_reason, scratch=None,
               watch_globs=WATCH_GLOBS, run_config=None):
    env = {k: v for k, v in os.environ.items() if k not in ("DROVER_RUN_NONCE", "DROVER_DETACHED_IN_JOB")}
    env["DROVER_RUN_ID"] = st.d["run_id"]
    run_config = run_config or {}
    try:
        prepare_writable_roots(run_config.get("sandbox_writable_roots", []))
    except (OSError, ValueError) as e:
        st.set(state="exited", ended=now_iso(), exit_code=None, reason=f"spawn-failed: {e}",
               missing_executable=False)
        return st.d
    if scratch is not None:
        env.update({key: str(scratch) for key in ("TMPDIR", "TMP", "TEMP")})
    env.update(run_config.get("env", {}))
    for key in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        if env.get(key):
            env[key] = os.path.expanduser(env[key])
    stdin_f = subprocess.DEVNULL
    try:
        if stdin_file:
            stdin_f = open(stdin_file, "rb")
        with open(out, "ab") as out_f:
            proc, tree = placement.spawn_worker(argv, out_f, stdin_f, cwd, env)
    except OSError as e:
        # ENOENT can name the cwd or stdin file rather than the executable. Windows may omit the filename.
        missing = isinstance(e, FileNotFoundError) and bool(argv) and (
            e.filename == argv[0] or e.filename is None and (cwd is None or Path(cwd).is_dir()))
        st.set(state="exited", ended=now_iso(), exit_code=None, reason=f"spawn-failed: {e}",
               missing_executable=missing)
        return st.d
    finally:
        if stdin_file and stdin_f is not subprocess.DEVNULL:
            stdin_f.close()
    from .metrics import Peaks
    peaks = Peaks(tree.metrics)
    watch = None
    try:
        with placement.cleanup_process_record(d.parent.parent, cwd, pid=proc.pid):
            st.set(state="running", pid=proc.pid)
            watch = open(out, "rb")        # an open handle's size is exact on Windows, where stat() can lag
            t0 = last_change = time.monotonic()
            size, outcome, watched = 0, None, ()
            while proc.poll() is None:
                now = time.monotonic()
                if peaks.last is None or now - peaks.last[0] >= 1:
                    st.set(peaks=dict(peaks.sample()), heartbeat=now_iso())
                watch.seek(0, 2)
                if watch.tell() != size:
                    size, last_change = watch.tell(), now
                seen = _watched_sizes(d, watch_globs)
                if seen != watched:
                    watched, last_change = seen, now
                stop = read_stop(d, nonce) or (sig_reason[0] if sig_reason else None)
                if stop:
                    outcome = ("killed", stop)
                elif wall_s and now - t0 > wall_s:
                    outcome = ("timeout", f"wall-clock limit {wall_s:g}s")
                elif idle_s and now - last_change > idle_s:
                    steps = [m.group(1) for name, _ in watched for m in [re.search(r"\.(step\d+)\.log$", name)] if m]
                    step = max(steps, key=lambda n: int(n[4:]), default=None)
                    outcome = ("timeout", f"no output for {idle_s:g}s" + (f" (stalled in {step})" if step else ""))
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
        proc, in_job = placement.detach(cmd, env, log)
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
    p.add_argument("--run-config", type=json.loads, default={})
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
                   scratch_dir=a.scratch_dir, run_config=a.run_config)
    print(json.dumps(st))
    return 0 if st["state"] == "exited" and st["exit_code"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
