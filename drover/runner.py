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

from .runner_common import (POLL_S, WATCH_GLOBS, STOP_FILE, STATUS_FILE, RECORD_FILE, OUT_FILE, RUNNER_LOG,
                            STOP_REASONS, RUN_ID_RE, NONCE_RE, DETACH_WAIT_S, scratch_path, now_iso, run_dir,
                            write_json_atomic, read_status)
from .runner_record import (SCAN_BYTES, MAX_SCAN_TRIES, NOTE_MAX, BLOCKER_KEYS, SHA_RE, ARTIFACT_URI_RE, SHA256_RE,
                            KIND_MAX, DETAIL_MAX, _record_from, parse_action_record, _message_text, _reverse_lines,
                            scan_record)
from .runner_supervise import (Status, _read_stop_file, read_stop, request_stop, _cleanup_scratch, _watched_sizes,
                               supervise, prepare_writable_roots, _supervise)

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
