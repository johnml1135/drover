"""The dispatcher's view of a repo: worktrees, the unknown-gate question, gate plans and review inputs.

The dispatcher (drover.dispatcher) owns runs and the ledger transitions; this module owns git and the repo
config. Git is always driven with an argv list, never a shell. Paths in the ledger stay in the repo config's form
(R:\\repos\\x); `repos.native` turns one into a path this OS can open, and the launcher maps it for WSL.
"""
import json
import re
import subprocess
import sys
from fnmatch import fnmatchcase
from pathlib import Path

from . import config, garden, gate, placement, origin, repos, runner

TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
OWNER_PREFIX = "drover:"
DISPATCH_KEY = "dispatch_enabled"
GATE_CMD_KEY = "gate_cmd_"
GATE_Q_KEY = "_gate_question_"
UNKNOWN_REASON = "unknown gate"
TRUE = ("1", "true", "yes", "on")


class WorktreeRefused(Exception):
    """The task cannot have a worktree (dirty and not owned, no repo, git failed); the reason goes to the owner."""


class CommitRefused(WorktreeRefused):
    def __init__(self, reason, paths=()):
        super().__init__(reason)
        self.paths = list(paths)


def truthy(v):
    return str(v).strip().lower() in TRUE


FETCH_TIMEOUT_S = 120


def git(repo, *args, check=False, timeout=None):
    """git with an argv list. A timeout is a failed command (rc 124), never a hang."""
    try:
        r = placement.git(repo, *args, timeout=timeout)
    except subprocess.TimeoutExpired:
        r = subprocess.CompletedProcess(args, 124, "", f"git {args[0]} timed out after {timeout}s")
    if check and r.returncode:
        raise WorktreeRefused(f"git {' '.join(args[:3])}: {(r.stderr or r.stdout).strip()[-300:]}")
    return r


def join_cfg(base, name):
    """base/name in the form `base` is written in (Windows backslashes or POSIX slashes)."""
    return base.rstrip("\\/") + ("\\" if "\\" in base else "/") + name


def add_worktree(repo, dest, *tail, run=None, error=WorktreeRefused):
    run = run or git
    """Create a worktree; old git may use absolute pointers only on a native filesystem."""
    r = run(repo, '-c', 'core.autocrlf=false', 'worktree', 'add', '--relative-paths', *tail, check=False)
    rejected = re.search(r'(unknown|unrecognized|unsupported|invalid) (option|argument|switch)[^\n]*relative-paths',
                         r.stderr, re.IGNORECASE)
    if r.returncode and rejected:
        if not placement.is_linux_native(dest):
            raise error('this git has no `worktree add --relative-paths`, and a worktree under '
                        f'{dest} is opened from both Windows and WSL: update git (2.48 or newer)')
        r = run(repo, '-c', 'core.autocrlf=false', 'worktree', 'add', *tail, check=False)
    if r.returncode:
        raise error(f'git worktree add failed: {r.stderr.strip()[-300:]}')


class Pipeline:
    def __init__(self, port, repos_map=None, repos_dir=None, runs_dir=None, python=None, wsl_python="python3"):
        self.port = port
        self._repos = repos_map if repos_map is not None else None
        self.repos_dir = repos_dir
        self.runs_dir = str(runs_dir or config.state_path("runs"))
        self.python, self.wsl_python = python or sys.executable, wsl_python

    def commit_ready(self, task, run_id, message):
        """Commit scoped work left by a sandboxed worker; refuse before touching the index on a scope violation."""
        if task.get("write") is False:
            raise CommitRefused("read-only task cannot commit")
        if not task.get("worktree"):
            raise CommitRefused("ready_to_commit has no task worktree")
        repo = repos.native(task["worktree"])
        changed = git(repo, "diff", "--name-only", "--no-renames", "-z", "HEAD", check=True).stdout
        untracked = git(repo, "ls-files", "--others", "--exclude-standard", "-z", check=True).stdout
        paths = sorted(set(p for p in (changed + untracked).split("\0") if p))
        trailers = git(repo, "log", "-1", "--format=%(trailers:key=Drover-Task,valueonly)%x00"
                       "%(trailers:key=Drover-Run,valueonly)", check=True).stdout
        if [s.strip() for s in trailers.split("\0")] == [str(task["id"]), str(run_id)]:
            if paths:
                raise CommitRefused("worktree changed after Drover committed this run", paths)
            return git(repo, "rev-parse", "HEAD", check=True).stdout.strip()
        scope = [re.sub(r":\d+(?:-\d+)?$", "", s.replace("\\", "/")).removeprefix("./").rstrip("/")
                 for s in task.get("scope_files") or []]

        def out_of_scope(ps):
            return [p for p in ps if not any(fnmatchcase(p, s) or p.startswith(s + "/") for s in scope)]
        if not paths:
            prog = self.repo(task)
            base = self.base_ref(prog) if prog else None
            if base and not trailers.strip("\0 \n") and git(repo, "rev-list", "--count", f"{base}..HEAD").stdout.strip() not in ("", "0"):
                ahead = git(repo, "diff", "--name-only", "--no-renames", "-z", f"{base}...HEAD", check=True).stdout
                outside = out_of_scope(sorted(p for p in ahead.split("\0") if p))
                if outside:
                    raise CommitRefused("out-of-scope changes: " + ", ".join(outside), outside)
                return git(repo, "rev-parse", "HEAD", check=True).stdout.strip()   # committed by the worker: gate HEAD
            raise CommitRefused("no changes")
        outside = out_of_scope(paths)
        if outside:
            raise CommitRefused("out-of-scope changes: " + ", ".join(outside), outside)
        git(repo, "add", "-A", check=True)
        text = f"{message.strip()}\n\nDrover-Task: {task['id']}\nDrover-Run: {run_id}\n"
        git(repo, "commit", "-m", text, check=True)
        return git(repo, "rev-parse", "HEAD", check=True).stdout.strip()

    # ---- switch and config ---------------------------------------------------------------------------
    def enabled(self):
        return truthy(self.port.get_setting(DISPATCH_KEY, "false"))

    def all_repos(self):
        if self._repos is None:
            self._repos = repos.load_all(self.repos_dir)
        return self._repos

    def repo(self, task):
        """The task's repo config with the owner's gate answer applied; None when the repo has no config."""
        prog = self.all_repos().get(str(task.get("repo") or "").lower())
        if prog is None:
            return None
        checkout = task.get("worktree")
        if checkout and not placement.reachable(checkout):
            checkout = None      # recover stale worktrees using the reachable machine checkout's config
        try:
            prog = repos.load(prog, checkout=checkout)
        except repos.RepoError:
            return None
        override = self._owner_gate_cmd(prog["name"].lower()) if prog["gate"]["unknown"] else None
        if override:
            prog = repos.with_test_script(prog, override)
        return prog

    def _owner_gate_cmd(self, name):
        """The gate command the owner gave, or None. The setting is trusted only while it names the repo's own
        unknown-gate question, that question is answered, its answer text is exactly this command and the answer
        was recorded by the owner: a value written straight into settings (any API client can PUT one) is ignored."""
        try:
            raw = json.loads(self.port.get_setting(GATE_CMD_KEY + name) or "")
            qid = int(raw["question"])
            if str(qid) != str(self.port.get_setting(GATE_Q_KEY + name)):
                return None
            q = self.port.get_question(qid)
        except (ValueError, TypeError, KeyError, AttributeError):
            return None
        answered = [e for e in self.port.events("question-answered") if e["data"].get("question") == qid]
        if q["status"] != "answered" or (q.get("answer_text") or "").strip() != raw.get("cmd") \
                or not answered or answered[-1]["actor"] != "owner":
            return None
        try:
            repos.normalize({"name": name, "repo": ".", "scripts": {"test": raw["cmd"]}})
        except repos.RepoError:
            return None
        return raw["cmd"]

    # ---- the unknown-gate question -------------------------------------------------------------------
    def gate_known(self, task):
        """True when the task's repo can be gated. Otherwise the task is sent to the owner (needs-owner) and False
        comes back: a repo without config gets a reason; an unknown gate gets the question, asked once per repo."""
        prog = self.repo(task)
        tid = task["id"]
        if prog is None:
            self.port.set_status(tid, "needs-owner", reason=f"no repo config for {task.get('repo')!r}")
            return False
        if origin.paused(self.port, prog["name"]):
            self.port.set_status(tid, "ready", reason="origin sync paused; answer the repo alert to retry")
            return False
        if not prog["gate"]["unknown"]:
            return True
        name = prog["name"]
        qid = self.port.get_setting(GATE_Q_KEY + name.lower())
        reason = f"{UNKNOWN_REASON}: {name}"
        if qid is None:
            hint = prog["gate"].get("hint")
            qid = self.port.ask_question(
                "task", tid, f"What is the test script path for {name}?",
                body=("Add [scripts] test to drover.toml, or answer with its relative .ps1 script path."
                      + (f" Hint: {hint}." if hint else "")), needs_owner=True)
            self.port.set_setting(GATE_Q_KEY + name.lower(), qid)
        else:
            self.port.set_status(tid, "needs-owner", reason=f"{reason} (question {qid})")
        return False

    def sync_gates(self):
        """Turn an answered unknown-gate question into the repo's gate command and requeue every task of that
        repo it held (the ledger requeues only the task the question targeted). Returns the requeued task ids."""
        out = []
        for name, prog in self.all_repos().items():
            qid = self.port.get_setting(GATE_Q_KEY + name)
            if qid is None or self._owner_gate_cmd(name):
                continue
            q = self.port.get_question(int(qid))
            text = (q.get("answer_text") or "").strip()
            if q["status"] != "answered" or not text:
                continue
            self.port.set_setting(GATE_CMD_KEY + name, json.dumps({"cmd": text, "question": int(qid), "actor": "owner"}))
            if self._owner_gate_cmd(name) is None:           # not an owner's answer, or not a usable command
                self.port.set_setting(GATE_CMD_KEY + name, "")
                continue
            for t in self.port.list_tasks("needs-owner"):
                if str(t["repo"]).lower() == name and self._held_for_gate(t["id"]):
                    self.port.set_status(t["id"], "ready", reason="gate command answered")
                    out.append(t["id"])
        return out

    def _held_for_gate(self, task_id):
        last = [e for e in self.port.events("status") if e["task_id"] == task_id]
        return bool(last) and str(last[-1]["data"].get("reason") or "").startswith(UNKNOWN_REASON)

    # ---- worktrees -----------------------------------------------------------------------------------
    def worktree(self, task):
        """The task's worktree path (config form), created on first use. Idempotent: a persisted worktree is reused.
        An existing worktree is used only when it is clean or the task owns it (garden tag owner drover:<id>);
        otherwise WorktreeRefused. Syncs local main first, branches from it, tags for garden."""
        tid = task["id"]
        if not TASK_ID_RE.match(str(tid)):
            raise WorktreeRefused(f"task id {tid!r} is not safe in a path")
        t = self.port.get_task(tid)
        prog = self.repo(t)
        if prog is None:
            raise WorktreeRefused(f"no repo config for {t.get('repo')!r}")
        repo = repos.native(prog["repo"])
        if not (Path(repo) / ".git").exists():
            raise WorktreeRefused(f"{prog['repo']} is not a git checkout")
        have = t.get("worktree")
        branch = t.get("branch")
        if branch and branch == prog["default_branch"]:
            raise WorktreeRefused(f"task branch {branch!r} is the default branch; work happens on its own branch")
        reuse, why = None, None
        if have and placement.reachable(repos.native(have)):
            reuse = have
        else:
            found = self._worktree_of_branch(repo, branch) if branch else None
            if found and placement.reachable(found):
                reuse = found
            elif found or have:
                # Never prune or delete the stale registration: it may be another host's, or the owner's.
                why = f"registered worktree {found or have} is unreachable from this host"
        if reuse:
            path = placement.canonical(repos.native(reuse))
            self._not_main(path, repo, prog)
            dirty = self._dirty_foreign(path, tid)
            if not dirty and self._owned(path, tid) and not placement.relativize_gitdir(path):
                self.port.append_event("worktree-repair-failed", {"path": str(path), "branch": branch}, tid)
                why = f"worktree {reuse} has a gitdir pointer that could not be made relative; left untouched"
            elif not dirty:
                reuse_cfg = placement.config_path(path, prog["repo"])
                if reuse_cfg != t.get("worktree") or branch != t.get("branch"):
                    self._persist(tid, branch, reuse_cfg)
                return reuse_cfg
            else:
                why = (f"worktree {reuse} has someone else's uncommitted changes ({dirty.splitlines()[0][:100]}); "
                       "left untouched")
        if why:
            self.port.append_event("worktree-fallback", {"source_branch": branch, "reason": why}, tid)
        return self._create(t, prog, repo, branch, source=branch if why else None)

    @staticmethod
    def _not_main(path, repo, prog):
        """Never hand out the repo's own checkout (or anything sitting on the default branch) to a worker."""
        if Path(path).resolve() == Path(repo).resolve():
            raise WorktreeRefused(f"{path} is the main checkout of {prog['name']}, not a task worktree")
        head = git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if head == prog["default_branch"]:
            raise WorktreeRefused(f"{path} is on the default branch {head!r}; a task needs its own branch")

    def _persist(self, tid, branch, wt):
        self.port.update_task(tid, branch=branch, worktree=wt)

    def _worktree_of_branch(self, repo, branch):
        r = git(repo, "worktree", "list", "--porcelain")
        path = None
        for line in r.stdout.splitlines():
            if line.startswith("worktree "):
                path = line[9:]
            elif line == f"branch refs/heads/{branch}" and path:
                return repos.native(path)
        return None

    def _dirty_foreign(self, path, tid):
        """The first dirty line when `path` has uncommitted changes that are not this task's, else ''."""
        if self._owned(path, tid):
            return ""
        return gate.dirty(path)

    @staticmethod
    def _owned(path, tid):
        try:
            tag = garden.read_tag(garden.admin_dir_of(path)) or {}
        except (ValueError, OSError):
            return False
        return tag.get("owner") == OWNER_PREFIX + tid

    def _create(self, t, prog, repo, branch, source=None):
        tid, remote, default = t["id"], prog["remote"], prog["default_branch"]
        if origin.paused(self.port, prog["name"]):
            raise WorktreeRefused("origin sync paused; answer the repo alert to retry")
        try:
            origin.sync(repo, remote, default, run=git, timeout=FETCH_TIMEOUT_S)
        except origin.FetchFailed as e:
            origin.pause(self.port, prog["name"], "task", tid, str(e))
            raise WorktreeRefused(str(e)) from e
        except origin.SyncError as e:
            raise WorktreeRefused(str(e)) from e
        base = self.base_ref(prog)
        name = f"drover-{tid}"
        dest = str(Path(repos.native(join_cfg(prog["worktrees"], name))).resolve())
        dest_cfg = placement.config_path(dest, prog["repo"])
        if source:                       # the branch's own worktree is unusable: branch from its committed tip
            branch, base = f"drover/{tid}", source
        branch = branch or f"drover/{tid}"
        if Path(dest).exists():
            if self._dirty_foreign(dest, tid) or not self._owned(dest, tid):
                raise WorktreeRefused(f"{dest_cfg} exists and is not this task's worktree")
        else:
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            self._add(repo, dest, branch, base, remote)
            try:
                garden.tag_worktree(dest, prog["name"], "slice", owner=OWNER_PREFIX + tid, purpose=str(t["title"])[:120],
                                    base=base)
            except (ValueError, OSError) as e:
                raise WorktreeRefused(f"created {dest} but could not tag it for garden: {e}")
        self._persist(tid, branch, dest_cfg)
        return dest_cfg

    def _add(self, repo, dest, branch, base, remote="origin"):
        """Choose the branch; Placement creates a worktree both operating systems can read."""
        def have(ref):
            return git(repo, "rev-parse", "--verify", "--quiet", ref).returncode == 0
        if have(f"refs/heads/{branch}"):
            tail = [str(dest), branch]
        elif have(f"refs/remotes/{remote}/{branch}"):
            tail = ["-b", branch, str(dest), f"{remote}/{branch}"]
        else:
            tail = ["-b", branch, str(dest), base]
        add_worktree(repo, dest, *tail)

    # ---- gate plan -----------------------------------------------------------------------------------
    def base_ref(self, prog):
        """Task diffs, reviews and new branches use local main, including its unpushed landings."""
        return prog["default_branch"]

    def head(self, task):
        return gate.head(repos.native(task["worktree"]))

    def diff_lines(self, repo, base, sha):
        r = git(repo, "diff", "--numstat", "--no-renames", f"{base}...{sha}")
        total = 0
        for line in r.stdout.splitlines():
            a, _, rest = line.partition("\t")
            d = rest.partition("\t")[0]
            total += (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)
        return total

    def diff_text(self, repo, base, sha):
        return git(repo, "diff", "--no-renames", f"{base}...{sha}").stdout

    def gate_plan(self, task, prog, sha, platform=None):
        """Plan the repo script at this exact commit, with generic changed-file context."""
        repo = repos.native(task["worktree"])
        prog = repos.load(prog, sha, repo)
        platform = platform or repos.gate_platforms(prog)[0]
        base = self.base_ref(prog)
        paths = gate.changed_paths(repo, base, sha)
        kind = "full"
        commands = [repos.script_argv(prog, platform=platform)]
        g = prog["gate"]
        if g["docs_only"] and gate.changed_docs_only(repo, base, sha, g["docs_only_globs"]):
            kind = "docs"
            commands = gate.docs_commands(base, sha, repos.script_argv(prog, "docs", platform) if repos.has_script(prog, "docs") else None)
        env = {**repos.run_config(prog)["env"], "DROVER_CHANGED_FILES": json.dumps(paths), "DROVER_CI_KIND": "checks" if kind == "full" else kind}
        return {"kind": kind, "commands": commands, "scope": None, "base": base, "repo": repo, "sha": sha,
                "env": env, "sandbox_writable_roots": repos.run_config(prog)["sandbox_writable_roots"],
                "diff_lines": self.diff_lines(repo, base, sha), "files": paths, "platform": platform}

    def gate_argv(self, plan, gate_run, priority, prog, platform=None):
        """(argv, receipt path) that runs the plan through gatejob on its selected gate platform.
        The plan file is written into the gate run's directory; for WSL every path in it and
        in argv is mapped to /mnt/<d>/."""
        d = runner.run_dir(self.runs_dir, gate_run)
        d.mkdir(parents=True, exist_ok=True)
        wsl = (platform or plan.get("platform") or repos.gate_platforms(prog)[0]) == "wsl"
        def m(p):
            return placement.to_wsl_path(str(p)) if wsl else str(p)
        out = d / "receipt.json"
        body = {"repo": m(plan["repo"]), "base": plan["base"], "sha": plan["sha"], "commands": plan["commands"],
                "scope": plan["scope"], "kind": plan.get("kind"), "out": m(out), "priority": priority,
                "env": plan.get("env", {}), "sandbox_writable_roots": plan.get("sandbox_writable_roots", [])}
        (d / "plan.json").write_text(json.dumps(body), encoding="utf-8")
        script = Path(__file__).with_name("gatejob.py")
        argv = [self.wsl_python if wsl else self.python, m(script), "--plan", m(d / "plan.json")]
        return argv, out
