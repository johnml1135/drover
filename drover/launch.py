"""Launchers: start one fresh worker for a brief through drover.runner and follow it by its files.

A spec is {run_id, backend, model, effort, cwd, placement, system_append, user_prompt, task, wall_s, idle_s}.
RunnerLauncher.launch() writes the brief to <runs>/<id>/in.txt and starts `python -m drover.runner --detach` (on
Windows directly, for WSL through placement_mod.wsl_argv). The handle's poll() gives None while the run is live, then a
result {status, record, usage, text, handoff, returncode, timeout}. status is done|ready_to_commit|blocked (the worker's own
action-state record), no-record (it ended without one), rotate (the Haiku context ceiling stopped it; handoff is
set), timeout or error. The runner owns the process tree and the timeouts; the handle only reads and asks it to stop.
"""
from drover import placement as placement_mod
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from . import config, task_access
from . import runner
from .runner import ScanError
from .placement import NO_WINDOW

HAIKU_CEILING = 80_000        # the pricing line is 100K; rotate before it
TAIL_LINES = 200
TAIL_FLUSH_EVERY = 5
HANDOFF_MAX = 1500
NOTE_MAX = 300
EFFORT = {"claude": "high", "codex": "xhigh"}   # fallback only; the dispatcher sets the effort per tier


def __getattr__(name):   # MODEL_IDS and RUNS_DEFAULT stay importable; they follow the active config
    if name == "MODEL_IDS":
        return config.get()["models"]["ids"]
    if name == "RUNS_DEFAULT":
        return str(config.state_path("runs"))
    raise AttributeError(name)


POLL_S = 0.5
MAX_LINE = 1 << 18           # a longer line is cut (and the rest of it skipped) so a newline-less flood stays bounded
START_TIMEOUT_S = 90         # a run still `starting` after this is stopped and reported as an error
DETACH_TIMEOUT_S = 40
SCAN_TIMEOUT_S = 90
TERMINAL = ("exited", "killed", "timeout")
IN_FILE, TAIL_FILE = "in.txt", "tail.log"
FRESH_FILES = (runner.OUT_FILE, runner.RECORD_FILE, runner.STATUS_FILE, runner.STOP_FILE, runner.RUNNER_LOG,
               TAIL_FILE, "tail.tmp")
parse_action_record = runner.parse_action_record


ENV_NAMES = ("CLAUDE_CONFIG_DIR", "CODEX_HOME")


def seat_env(spec):
    """The seat's worker environment from the spec: only CLAUDE_CONFIG_DIR and CODEX_HOME, nothing else."""
    return {k: str(v) for k, v in (spec.get("env") or {}).items() if k in ENV_NAMES and v}


def _claude_edit_rule(path):
    """An absolute Claude Edit rule (also covers Write and NotebookEdit)."""
    path = str(path).replace("\\", "/").rstrip("/")
    return f"Edit(//{path.lstrip('/')}/**)"


class LaunchError(RuntimeError):
    def __init__(self, msg, missing=False, cancel_requested=False):
        super().__init__(msg)
        self.missing = missing   # the backend binary does not exist
        self.cancel_requested = cancel_requested   # a runner may still come up; a stop-request was written for it


def _git_dirs(cwd):
    workspace = Path(placement_mod.native(cwd)).resolve()

    def resolve(base, value):
        if not value:
            raise ValueError("empty git metadata path")
        path = placement_mod.native(value)
        return (base / path).resolve(strict=True)

    try:
        for parent in (workspace, *workspace.parents):
            marker = parent / ".git"
            try:
                marker.lstat()
            except FileNotFoundError:
                continue
            if marker.is_dir():
                gitdir = marker.resolve(strict=True)
            else:
                prefix, sep, target = marker.read_text(encoding="utf-8").strip().partition(":")
                if prefix != "gitdir" or not sep:
                    raise ValueError("invalid git metadata pointer")
                gitdir = resolve(parent, target.strip())
            common_file = gitdir / "commondir"
            try:
                common_file.lstat()
            except FileNotFoundError:
                common = gitdir
            else:
                common = resolve(gitdir, common_file.read_text(encoding="utf-8").strip())
            if not gitdir.is_dir() or not common.is_dir():
                raise ValueError("git metadata is not a directory")
            return workspace, gitdir, common
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot resolve git metadata for Claude sandbox: {exc}") from exc
    return workspace, None, None


def external_git_dirs(cwd):
    workspace, gitdir, common = _git_dirs(cwd)
    return list(dict.fromkeys(str(p) for p in (common, gitdir) if p and not p.is_relative_to(workspace)))


def build_argv(spec):
    """(argv, stdin_text) for the spec's backend. The brief goes on stdin, the system block on the command line.
    A spec that carries its own `argv` (the gate run) is started as given."""
    if spec.get("argv"):
        argv = [str(a) for a in spec["argv"]]
        _reject_permission_bypass(argv)
        return argv, spec.get("user_prompt") or ""
    for root in spec.get("run_config", {}).get("sandbox_writable_roots", []):
        task_access.check_read_roots(root, spec.get("read_dirs") or [])
    m, e = config.model_id(spec["model"]), spec.get("effort") or EFFORT.get(spec["backend"])
    exe = spec.get("binary") or spec["backend"]
    if spec["backend"] == "claude":
        readonly = spec.get("role") == "review" or spec.get("write") is not True
        placement = spec.get("placement", "windows")
        # Flags and settings verified against Claude Code 2.1.303. WSL requires
        # bubblewrap; Windows Bash runs unsandboxed under a Job Object (trade-off accepted by owner, Q5).
        argv = [exe, "-p", "--output-format", "stream-json", "--verbose",
                "--append-system-prompt", spec["system_append"], "--model", m, "--effort", e,
                "--permission-mode", "dontAsk" if readonly else "acceptEdits",
                "--permission-prompts", "none", "--setting-sources", ""]
        permissions = {"allow": ["Read", "Grep", "Glob"], "deny": []}
        settings = {"env": dict(spec.get("run_config", {}).get("env", {})), "permissions": permissions}
        scratch = spec.get("scratch_dir")
        if scratch:
            task_access.check_read_roots(scratch, spec.get("read_dirs") or [])
            scratch = placement_mod.to_wsl_path(str(scratch)) if placement == "wsl" else placement_mod.to_windows_path(scratch)
            settings["env"].update({"TMPDIR": scratch, "TMP": scratch, "TEMP": scratch})
        if readonly:
            tools = "Read,Grep,Glob,Bash" if placement == "wsl" else "Read,Grep,Glob"
            argv.extend(["--tools", tools])
            permissions["deny"].extend(["Edit", "Write", "NotebookEdit"])
            if placement != "wsl":
                # Without a filesystem sandbox, even Bash could write.
                permissions["deny"].append("Bash")
        else:
            task_access.check_read_roots(spec["cwd"], spec.get("read_dirs") or [])
            if scratch:
                permissions["allow"].append(_claude_edit_rule(scratch))
            if placement != "wsl":
                # acceptEdits covers file tools. Windows shell execution uses
                # a Job Object (no filesystem sandbox) rather than Claude's POSIX sandbox.
                permissions["allow"].append("Bash")
        reads = []
        if spec.get("read_dirs"):
            reads = [str(path) for path in spec["read_dirs"]]
            if placement == "wsl":
                reads = [placement_mod.to_wsl_path(path) for path in reads]
            else:
                reads = [placement_mod.to_windows_path(os.path.expanduser(path)) for path in reads]
            permissions["deny"].extend(_claude_edit_rule(path) for path in reads)
        worker_path = placement_mod.to_wsl_path if placement == "wsl" else placement_mod.to_windows_path
        workspace = spec.get("cwd") or os.getcwd()
        cwd = worker_path(os.path.expanduser(str(workspace)))
        external_git = [worker_path(path) for path in external_git_dirs(workspace)]
        permissions["deny"].extend(_claude_edit_rule(path) for path in external_git)
        if placement == "wsl":
            defaults = ["/tmp/claude", "/private/tmp/claude", "~/.claude/debug"]
            filesystem = {}
            if readonly:
                # "/" alone does not stop Bash writing to cwd (shown on 2.1.303):
                # deny cwd explicitly, plus the implicit default grants.
                filesystem["denyWrite"] = ["/", cwd, *reads, *external_git, *defaults]
                if scratch:
                    filesystem["allowWrite"] = [scratch]
            else:
                filesystem["allowWrite"] = [cwd, *([scratch] if scratch else []),
                                             *spec.get("run_config", {}).get("sandbox_writable_roots", [])]
                # allowWrite is additive. Claude also grants the linked worktree's
                # shared Git directory and these temp/log directories by default.
                filesystem["denyWrite"] = [*reads, *external_git, *defaults]
            settings["sandbox"] = {
                "enabled": True,
                "failIfUnavailable": True,
                "allowUnsandboxedCommands": False,
                "autoAllowBashIfSandboxed": True,
                "filesystem": filesystem,
            }
        argv.extend(["--settings", json.dumps(settings, separators=(",", ":"))])
    elif spec["backend"] == "codex":
        # codex exec has no system-prompt flag; developer_instructions is the config key for that layer
        sandbox = "read-only" if (spec.get("role") == "review" or spec.get("write") is False) else spec.get("sandbox", "workspace-write")
        if sandbox not in ("read-only", "workspace-write"):
            raise ValueError(f"unsupported codex sandbox {sandbox!r}")
        cwd = placement_mod.to_wsl_path(spec["cwd"]) if spec.get("placement") == "wsl" else str(spec["cwd"])
        if sandbox == "workspace-write":
            task_access.check_read_roots(spec["cwd"], spec.get("read_dirs") or [])
        scratch = spec.get("scratch_dir") if sandbox == "workspace-write" else None
        if scratch:
            task_access.check_read_roots(scratch, spec.get("read_dirs") or [])
            scratch = placement_mod.to_wsl_path(str(scratch)) if spec.get("placement") == "wsl" else placement_mod.to_windows_path(scratch)
        roots = [r for r in (scratch,) if r]
        if sandbox == "workspace-write" and spec.get("placement") == "wsl":
            roots.extend(spec.get("run_config", {}).get("sandbox_writable_roots", []))
        argv = [exe, "exec", "--json", "-s", sandbox, "-C", cwd, "-m", m, "-c", f"model_reasoning_effort={e}",
                "-c", f"developer_instructions={json.dumps(spec['system_append'])}",
                "-c", 'approval_policy="never"',
                "-c", f"sandbox_workspace_write.writable_roots={json.dumps(roots)}",
                "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
                "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true", "-"]
        # Both sandbox modes allow reads outside cwd. --add-dir grants WRITE
        # access, so read snapshots must never be passed that way. Override
        # inherited writable roots and implicit temp roots as well: snapshots
        # can live under the state dir in TEMP or /tmp. Chmod is only hygiene,
        # not an access boundary (in particular on Windows).
        shell_env = dict(spec.get("run_config", {}).get("env", {}))
        if scratch:
            shell_env.update({key: scratch for key in ("TMPDIR", "TMP", "TEMP")})
        if shell_env:
            values = ",".join(f"{json.dumps(key)}={json.dumps(value)}" for key, value in shell_env.items())
            argv[-1:-1] = ["-c", f"shell_environment_policy.set={{{values}}}"]
    else:
        raise ValueError(f"headless launcher cannot run backend {spec['backend']!r}")
    _reject_permission_bypass(argv)
    return argv, spec["user_prompt"]


def _reject_permission_bypass(argv):
    """Keep worker and supplied argv away from Claude's permission bypass modes."""
    for i, arg in enumerate(argv):
        if arg.partition("=")[0] in ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"):
            raise ValueError("permission bypass is forbidden")
        if arg == "--permission-mode" and i + 1 < len(argv) and argv[i + 1] == "bypassPermissions":
            raise ValueError("permission bypass is forbidden")
        if arg == "--permission-mode=bypassPermissions":
            raise ValueError("permission bypass is forbidden")
        value = arg.partition("=")[2] if arg.startswith("--settings=") else (
            argv[i + 1] if arg == "--settings" and i + 1 < len(argv) else "")
        if "bypassPermissions" in value:
            raise ValueError("permission bypass is forbidden")


# Provider phrasing only (Anthropic API error types, Claude Code's "API Error: <status>" line, Codex's capacity and
# limit messages). Bare words such as "authentication" or "500" are not here: a failing test mentions them too.
PROVIDER_ERROR = re.compile(
    r"overloaded_error|rate_limit_error|api_error\b|authentication_error|permission_error|"
    r"^\s*API Error:?\s*(429|401|5\d\d)\b|Selected model is at capacity|model is at capacity|"
    r"Credit balance is too low|OAuth token has expired|usage limit (reached|has been reached)|"
    r"you(?:'|\u2019)ve hit your (usage )?limit|rate limit (exceeded|reached)|\b429 Too Many Requests|"
    r"(?:^|\W)503 Service Unavailable|Please run /login|Invalid API key", re.I)
PROVIDER_STATUS = (401, 429, 500, 502, 503, 504, 529)
PROGRESS_EVENTS = ("assistant", "user", "item.started", "item.completed", "turn.started", "turn.completed")


def provider_error_of(ev):
    """The message of a provider-side error that ended a turn, else None. Qualifies only: an `error` or
    `turn.failed` stream event (codex) or an error `result` (claude) whose text uses provider phrasing, or an
    error `result` carrying a structured api_error_status. Free text a worker printed never qualifies."""
    t = ev.get("type")
    msg = None
    if t == "error":
        msg = ev.get("message")
    elif t == "turn.failed":
        err = ev.get("error")
        msg = err.get("message") if isinstance(err, dict) else err
    elif t == "result" and ev.get("is_error"):
        status = ev.get("api_error_status")
        if isinstance(status, int) and not isinstance(status, bool) and status in PROVIDER_STATUS:
            return f"API Error: {status} {str(ev.get('result') or '')}"[:300].strip()
        msg = ev.get("result")
    if isinstance(msg, str) and PROVIDER_ERROR.search(msg):
        return msg[:300]
    return None


class Usage:
    """Token totals plus the context size of the latest request, from claude stream-json or codex --json events."""

    def __init__(self):
        self.tin = self.tout = self.cached = self.context = 0
        self.text, self._claude_final = "", False
        self.sessions, self.compactions, self.models = set(), 0, {}
        self._messages = set()
        self.provider_error = None

    def feed(self, ev):
        t = ev.get("type")
        perr = provider_error_of(ev)
        if perr:
            self.provider_error = perr
        elif t in PROGRESS_EVENTS:
            self.provider_error = None       # the run went on after the error: it is not what ended it
        if ev.get("session_id"):
            self.sessions.add(ev["session_id"])
        if t == "thread.started" and ev.get("thread_id"):
            self.sessions.add(ev["thread_id"])
        if t == "system" and ev.get("subtype") == "compact_boundary":
            self.compactions += 1
        if t == "assistant":
            message = ev.get("message") or {}
            key = message.get("id")
            if key and key in self._messages:
                return
            if key:
                self._messages.add(key)
            u = (ev.get("message") or {}).get("usage") or {}
            ctx = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) \
                + (u.get("cache_creation_input_tokens") or 0)
            self.context = ctx or self.context
            if not self._claude_final:
                self.tin += u.get("input_tokens") or 0
                self.tout += u.get("output_tokens") or 0
                self.cached += u.get("cache_read_input_tokens") or 0
            model = (ev.get("message") or {}).get("model")
            if model:
                dest = self.models.setdefault(model, {"tokens_in": 0, "tokens_out": 0, "tokens_cached": 0,
                                                      "tokens_cache_write": 0})
                for field, source in (("tokens_in", "input_tokens"), ("tokens_out", "output_tokens"),
                                      ("tokens_cached", "cache_read_input_tokens"),
                                      ("tokens_cache_write", "cache_creation_input_tokens")):
                    dest[field] += u.get(source) or 0
            for b in (ev.get("message") or {}).get("content") or []:
                if b.get("type") == "text":
                    self.text = b.get("text", "")
        elif t == "result":
            self.text = ev.get("result") or self.text
            if isinstance(ev.get("modelUsage"), dict):
                self.models = {model: {"tokens_in": values.get("inputTokens", 0),
                                       "tokens_out": values.get("outputTokens", 0),
                                       "tokens_cached": values.get("cacheReadInputTokens", 0),
                                       "tokens_cache_write": values.get("cacheCreationInputTokens", 0)}
                               for model, values in ev["modelUsage"].items()}
            u = ev.get("usage")
            if u:
                self._claude_final = True
                self.tin, self.tout = u.get("input_tokens") or 0, u.get("output_tokens") or 0
                self.cached = u.get("cache_read_input_tokens") or 0
        elif t == "turn.completed":
            u = ev.get("usage") or {}
            self.context = u.get("input_tokens") or self.context
            self.tin += u.get("input_tokens") or 0
            self.tout += u.get("output_tokens") or 0
            self.cached += u.get("cached_input_tokens") or 0
        elif t == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
            self.text = ev["item"].get("text", "")

    def as_dict(self):
        return {"tokens_in": self.tin, "tokens_out": self.tout, "tokens_cached": self.cached,
                "context": self.context, "sessions": len(self.sessions) or int(bool(
                    self.tin or self.tout or self.cached or self.text or self.models)), "compactions": self.compactions,
                "tokens_by_model": self.models}


def _one_line(text, limit=160):
    """One tail line per message. A message holding an action record keeps its end (the record) whole."""
    text = " ".join(str(text).split())
    return text[-1000:] if '"status"' in text else text[:limit]


def summarise(ev, raw):
    t = ev.get("type")
    if t == "assistant":
        parts = []
        for b in (ev.get("message") or {}).get("content") or []:
            parts.append("tool: " + b.get("name", "?") if b.get("type") == "tool_use" else _one_line(b.get("text", "")))
        return "assistant: " + " | ".join(p for p in parts if p)
    if t == "item.completed":
        it = ev.get("item") or {}
        return f"{it.get('type')}: {_one_line(it.get('text') or it.get('command') or '')}"
    return raw[:200]


def rotation_handoff(task, usage, ceiling):
    text = (f"Rotated: context reached ~{usage.context} tokens (ceiling {ceiling}); never compact.\n"
            f"Goal: {task.get('goal') or task['title']}\n"
            f"Done (last worker message): {usage.text.strip()[-700:] or 'nothing reported'}\n"
            "Next: continue from the worktree state; read `git log` and `git status` for what exists, "
            "then finish the acceptance list.\nOpen questions: none recorded.")
    return text[:HANDOFF_MAX]


class Tail:
    def __init__(self, path):
        self.path, self.lines, self.pending, self.error = Path(path), deque(maxlen=TAIL_LINES), 0, None

    def add(self, line):
        self.lines.append(line.rstrip("\n"))
        self.pending += 1
        if self.pending >= TAIL_FLUSH_EVERY:
            self.flush()

    def flush(self):
        """Never raises: a tail write failure is remembered once and the run carries on."""
        self.pending = 0
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            self.error = self.error or repr(e)


class RunnerHandle:
    """Follows one run through <runs>/<id>/{status.json,out.log,record.json}, by offset, from the dispatcher's own
    launch or after a restart (adopt): it keeps no pipe to the worker. Stop is a nonce-checked stop-request file,
    the same one `python -m drover.runner stop` writes."""

    def __init__(self, spec, runs_dir, ceiling, pid=None, clock=time.time, poll_s=POLL_S,
                 start_timeout_s=START_TIMEOUT_S, tail=None, autostart=True):
        self.spec, self.runs_dir, self.ceiling = spec, runs_dir, ceiling
        self.run_id, self.pid, self.clock, self.poll_s = str(spec["run_id"]), pid, clock, poll_s
        self.placement, self.start_timeout_s = spec.get("placement", "windows"), start_timeout_s
        self.dir = runner.run_dir(runs_dir, self.run_id)
        self.tail = tail or Tail(self.dir / TAIL_FILE)
        self.started, self.offset, self.buf, self.skipping = clock(), 0, b"", False
        self.rotate, self.result, self.last_status = False, None, None
        self._lock, self._done, self._usage = threading.Lock(), threading.Event(), Usage()
        self._cancelled = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        if autostart:
            self._thread.start()

    # ---- output ---------------------------------------------------------------------------------------
    def _ingest(self, final=False):
        try:
            with open(self.dir / runner.OUT_FILE, "rb") as f:
                f.seek(self.offset)
                data = f.read(1 << 20)
        except OSError:
            data = b""
        self.offset += len(data)
        lines = (self.buf + data).split(b"\n")
        self.buf = lines.pop()
        if final and self.buf:
            lines.append(self.buf)
            self.buf = b""
        if len(self.buf) > MAX_LINE:                 # no newline yet and already too long: keep the head only
            lines.append(self.buf[:MAX_LINE])
            self.buf, self.skipping = b"", True
        for raw in lines:
            if self.skipping:                        # the rest of an over-long line
                self.skipping = False
                continue
            if not self.rotate:
                self._line(raw[:MAX_LINE].decode("utf-8", errors="replace"))
        if len(data) == 1 << 20:
            self._ingest(final)

    def _line(self, raw):
        try:
            ev = json.loads(raw)
        except ValueError:
            ev = None
        if isinstance(ev, dict):
            self._usage.feed(ev)
            self.tail.add(summarise(ev, raw.strip()))
        elif raw.strip():
            self.tail.add(raw.strip()[:200])
        if self.ceiling and self._usage.context > self.ceiling and not self.rotate:
            self.rotate = True
            self.stop("rotation")

    # ---- status ---------------------------------------------------------------------------------------
    def status(self):
        """status.json now (also remembered as last_status); None when it is unreadable."""
        st = runner.read_status(self.runs_dir, self.run_id)
        if st is not None:
            self.last_status = st
        return st

    def terminal(self):
        st = self.status()
        return st if st and st.get("state") in TERMINAL else None

    def stop(self, reason="requested"):
        """Ask the runner to end this run; False when no launch is recorded (nothing to stop)."""
        try:
            return runner.request_stop(self.runs_dir, self.run_id, reason)
        except OSError:
            return False

    def cancel(self):
        """Stop following this run (the dispatcher dropped it); no result is published, even by a pass in flight."""
        with self._lock:
            self._cancelled = True
        self._done.set()

    def _publish(self, result):
        with self._lock:
            if self._cancelled:
                return
            if self.result is None:
                self.result = result
        self._done.set()

    def _run(self):
        try:
            while not self._done.is_set():
                self._ingest()
                st = self.terminal()
                if st:
                    self._ingest(final=True)
                    self.tail.flush()
                    return self._publish(self._finish(st))
                if self.last_status and self.last_status.get("state") == "starting" \
                        and self.clock() - self.started > self.start_timeout_s:
                    self.stop()                     # the runner may yet start: its stop-request then ends it at once
                    return self._publish(self._error(f"runner still starting after {self.start_timeout_s:g}s"))
                self._done.wait(self.poll_s)
        except Exception as e:  # a crash must surface as a result
            self._publish(self._error(f"handle failed: {e!r}"))

    def _error(self, text):
        return {"status": "error", "record": None, "usage": self._usage.as_dict(), "text": text, "handoff": None,
                "returncode": None, "timeout": None, "tail_error": self.tail.error, "adopted": False}

    def _record(self):
        try:
            obj = json.loads((self.dir / runner.RECORD_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return runner._record_from(obj)

    def _finish(self, st):
        usage, record = self._usage, self._record()
        state, reason, rc = st.get("state"), st.get("reason") or "", st.get("exit_code")
        rotate = self.rotate or (state == "killed" and reason == "rotation")
        if record:                          # a finished record beats rotation and a late stop
            status, rotate = record["status"], False
        elif rotate:
            status = "rotate"
        elif state == "timeout":
            status = "timeout"
        elif state == "killed" or rc not in (0, None) or reason.startswith("spawn-failed"):
            status = "error"
        else:
            status = "no-record"
        text = usage.text if status not in ("error", "timeout") or not reason else f"{reason}. {usage.text}"
        return {"status": status, "record": record, "usage": usage.as_dict(), "text": text,
                "handoff": rotation_handoff(self.spec["task"], usage, self.ceiling) if rotate else None,
                "returncode": rc, "timeout": reason if state == "timeout" else None, "tail_error": self.tail.error,
                "provider_error": None if record else usage.provider_error,
                "adopted": bool(self.spec.get("adopted")), "peaks": st.get("peaks", {})}

    def poll(self):
        return self.result if self._done.is_set() else None

    def wait(self, timeout=None):
        self._done.wait(timeout)
        return self.poll()


class Launcher:
    def launch(self, spec):
        raise NotImplementedError


def _fresh_run_dir(d):
    """An empty run directory: a reused run id must not inherit the old output, record, status or stop-request."""
    d.mkdir(parents=True, exist_ok=True)
    for name in FRESH_FILES:
        try:
            (d / name).unlink(missing_ok=True)
        except OSError as e:
            raise LaunchError(f"cannot clear {d / name}: {e}") from e


def _last_json(text):
    for line in reversed((text or "").strip().splitlines()):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


class RunnerLauncher(Launcher):
    """Every worker goes through drover.runner. Windows placement runs the runner with the hub's python; WSL
    placement runs it through wsl.exe --exec with --cd to the drover directory so `python3 -m drover.runner`
    imports. The brief is the file in.txt (the runner opens it as the worker's stdin)."""

    def __init__(self, runs_dir=None, distro=None, drover_dir=None, python=None, run=subprocess.run,
                 which=shutil.which, ceilings=None, windows=sys.platform == "win32", wsl_bins=None, **handle_kw):
        self.windows = windows
        self.wsl_bins = wsl_bins or {}      # {'claude': '/abs/claude', 'codex': ...} or a callable returning it (ledger settings)
        self.runs_dir = str(runs_dir or config.state_path("runs"))
        self._distro, self.run, self.which = distro, run, which
        self.drover_dir = str(drover_dir or Path(__file__).resolve().parents[1])
        self.python, self.handle_kw = python or sys.executable, handle_kw
        self.ceilings = {"haiku": HAIKU_CEILING, **(ceilings or {})}

    @property
    def distro(self):
        return placement_mod.distro(self._distro)

    def ceiling(self, model):
        return next((v for k, v in self.ceilings.items() if k in model.lower()), None)

    def windows_exe(self, name):
        """The real executable for `name` on Windows (claude.exe). A .cmd/.bat shim would pass the brief and the
        system prompt through cmd.exe parsing, so it is refused."""
        exe = None if Path(name).suffix else self.which(name + ".exe")
        exe = exe or self.which(name)
        if exe is None:
            raise LaunchError(f"cannot start {name}: not found on PATH", missing=True)
        if Path(exe).suffix.lower() not in (".exe", ".com"):
            raise LaunchError(f"{name} resolves to {exe}, a script shim; refusing to run it through cmd.exe "
                              f"(install the native executable)", missing=True)
        return exe

    def wsl_exe(self, argv):
        """argv with a bare `claude`/`codex` replaced by the absolute WSL path the hub discovered (wsl.exe --exec has
        no login PATH). Without a usable absolute path the bare name is kept."""
        bins = self.wsl_bins() if callable(self.wsl_bins) else self.wsl_bins
        path = str((bins or {}).get(argv[0]) or "")
        return [path, *argv[1:]] if path.startswith("/") and "\n" not in path else argv

    def runner_args(self, spec, d, argv, cwd):
        scratch = ["--scratch-dir", spec["scratch_dir"]] if (
            spec.get("placement") == "wsl" and spec.get("scratch_dir")) else []
        return ["--detach", "--run-id", str(spec["run_id"]), "--nonce", spec["nonce"], "--runs-dir", self.runs_dir,
                "--wall-s", f"{spec.get('wall_s') or 0:g}", "--idle-s", f"{spec.get('idle_s') or 0:g}",
                "--stdin-file", str(d / IN_FILE), "--cwd", str(cwd), *scratch,
                "--run-config", json.dumps(spec.get("run_config", {})), "--", *argv]

    def runner_argv(self, runner_args, cd=None, env=None):
        """Invoke the runner on the selected WSL distribution; worker argv stays verbatim."""
        args = [str(a) for a in runner_args]
        split = args.index("--") if "--" in args else len(args)
        args = [placement_mod.to_wsl_path(a) for a in args[:split]] + args[split:]
        return placement_mod.WSL(self.distro).command(["python3", "-m", "drover.runner", *args], cd, env)

    def command(self, spec, d, argv):
        """(command, env) that starts the detached runner for this spec's placement_mod."""
        cwd = os.path.expanduser(str(spec["cwd"]))
        if spec.get("placement") == "wsl":
            argv = self.wsl_exe(argv)
            return placement_mod.ci_runner_command(self.runner_args(spec, d, argv, cwd),
                                                    self.drover_dir, self.distro, seat_env(spec), self.windows)
        try:
            cwd = placement_mod.native_runner_cwd(cwd, self.windows)
        except ValueError as error:
            raise LaunchError(str(error)) from error
        if self.windows:
            argv = [self.windows_exe(argv[0]), *argv[1:]]
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [self.drover_dir, os.environ.get("PYTHONPATH")])),
               **seat_env(spec)}
        if spec.get("backend") == "claude" and spec.get("scratch_dir"):
            env.update({name: str(spec["scratch_dir"]) for name in ("TMPDIR", "TMP", "TEMP")})
        return [self.python, "-m", "drover.runner", *self.runner_args(spec, d, argv, cwd)], env

    def launch(self, spec):
        rid = spec["run_id"]
        spec.setdefault("nonce", uuid.uuid4().hex)           # the dispatcher normally picks and records it first
        d = runner.run_dir(self.runs_dir, rid)
        _fresh_run_dir(d)
        claude_worker = spec["backend"] == "claude" and not spec.get("argv")
        codex_writer = (spec["backend"] == "codex" and not spec.get("argv") and spec.get("write") is not False
                        and spec.get("role") != "review" and spec.get("sandbox", "workspace-write") == "workspace-write")
        if claude_worker or codex_writer:
            # Keep worker temp files out of shared /tmp (which may contain read
            # snapshots), and outside the run's control files and task diff.
            scratch = f"/tmp/drover-scratch/{rid}-{spec['nonce']}" if spec.get("placement") == "wsl" else (
                tempfile.mkdtemp(prefix="scratch-", dir=d))
            spec = {**spec, "scratch_dir": scratch}
        argv, stdin_text = build_argv(spec)
        (d / IN_FILE).write_text(stdin_text, encoding="utf-8")
        cmd, env = self.command(spec, d, argv)
        info, err = None, None
        try:
            r = self.run(cmd, capture_output=True, text=True, timeout=DETACH_TIMEOUT_S, env=env, stdin=subprocess.DEVNULL,
                         cwd=self.drover_dir if env else None, **placement_mod.NO_WINDOW)
            info = _last_json(r.stdout)
            if info is None or r.returncode not in (0, 1):
                err = f"runner start failed (rc {r.returncode}): {(r.stderr or r.stdout or '')[-300:].strip()}"
        except FileNotFoundError as e:
            raise LaunchError(f"cannot start {cmd[0]}: {e}", missing=True) from e
        except (OSError, subprocess.SubprocessError) as e:
            err = f"runner start failed: {e!r}"
        st = runner.read_status(self.runs_dir, rid)
        started = bool(st and (st.get("state") != "starting" or st.get("runner_pid")))
        definite = bool(info and info.get("error") and "still starting" not in info["error"])
        if definite:                      # the runner itself says it died: not "maybe started"
            err, started = err or f"{info['error']}: {self._log_tail(d)}", False
        if st and str(st.get("reason") or "").startswith("spawn-failed"):
            raise LaunchError(f"cannot start worker: {st['reason']}"[:300],
                              missing="No such file" in st["reason"] or "cannot find" in st["reason"].lower())
        if err and not started:
            raise LaunchError(err[:300], cancel_requested=self.cancel(rid, spec["nonce"]))
        pid = (info or {}).get("runner_pid") or (st or {}).get("runner_pid")
        return RunnerHandle(spec, self.runs_dir, self.ceiling(spec["model"]), pid=pid, **self.handle_kw)

    def cancel(self, run_id, nonce=None):
        """Write the nonce-checked stop-request for a runner that may yet start. With the launch's nonce it is
        written even before status.json exists, and a late runner honours it. True when one was written."""
        try:
            return runner.request_stop(self.runs_dir, run_id, "requested", nonce)
        except (OSError, ValueError):
            return False

    stop_run = cancel

    @staticmethod
    def _log_tail(d):
        try:
            return (d / runner.RUNNER_LOG).read_text(encoding="utf-8", errors="replace")[-200:].strip()
        except OSError:
            return ""

    def adopt(self, spec, started=None):
        """A handle for a run that is already going (or finished) in its run directory, read from the files."""
        h = RunnerHandle(spec, self.runs_dir, self.ceiling(spec["model"]), **self.handle_kw)
        if started is not None:
            h.started = started
        return h

    def wsl_up(self):
        from .resources import wsl_running
        return wsl_running(self.distro)

    # ---- finding runners ------------------------------------------------------------------------------
    def runners(self, want_wsl=False):
        """All runner markers, including CI runs that have no task-ledger run id."""
        found = {str(row['run_id']): 'windows' for row in runner.find_runners()}
        if want_wsl:
            for rid in self._wsl_runners():
                found.setdefault(str(rid), 'wsl')
        return found

    def scan(self, want_wsl=False):
        """Integer task-run markers for dispatcher recovery; CI keeps its own persisted context."""
        return normalize_run_ids(self.runners(want_wsl))

    def _wsl_runners(self):
        cmd, env = placement_mod.ci_runner_command(["find", "--json"], self.drover_dir, self.distro,
                                                    windows=self.windows)
        try:
            r = self.run(cmd, capture_output=True, text=True, timeout=SCAN_TIMEOUT_S, env=env, **placement_mod.NO_WINDOW)
            rows = json.loads(r.stdout)
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            raise ScanError(f"cannot list WSL runners: {e!r}") from e
        if r.returncode != 0 or not isinstance(rows, list):
            raise ScanError(f"WSL runner find exited {r.returncode}")
        return [row["run_id"] for row in rows if isinstance(row, dict) and type(row.get("run_id")) in (int, str)]


def normalize_run_ids(found):
    """Integer ledger run ids only; ignore unrelated or malformed process markers."""
    out = {}
    for rid, placement in found.items():
        if type(rid) not in (int, str):
            continue
        try:
            out[int(rid)] = placement
        except ValueError:
            continue
    return out


class PaneLauncher(Launcher):
    """Reserved pane launcher; headless RunnerLauncher is the supported implementation."""

    def launch(self, spec):
        raise NotImplementedError("PaneLauncher is not implemented; use RunnerLauncher")
