"""The dispatcher's view of a repo: worktrees, the unknown-gate question and review inputs.

The dispatcher (drover.dispatcher) owns runs and the ledger transitions; this module owns git and the repo
config. Git is always driven with an argv list, never a shell. Paths in the ledger stay in the repo config's form
(R:\\repos\\x); `repos.native` turns one into a path this OS can open, and the launcher maps it for WSL.
"""
import json
import re
import subprocess
from fnmatch import fnmatchcase
from pathlib import Path

from . import config, garden, ci, placement, origin, repos

TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
OWNER_PREFIX = "drover:"
DISPATCH_KEY = "dispatch_enabled"
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
    def __init__(self, port, repos_map=None, repos_dir=None, runs_dir=None):
        self.port = port
        self._repos = repos_map if repos_map is not None else None
        self.repos_dir = repos_dir
        self.runs_dir = str(runs_dir or config.state_path("runs"))
        self.ci = ci.CI(self.runs_dir)

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
        if repos.ci_policy(prog)["unknown"]:
            from .ledger.waiting import cause_key, repo_key
            answer = self.port.waiting.answer(cause_key("script", repo_key(repos.repo_name(prog))))
            if answer:
                prog = repos.with_test_script(prog, answer["text"])
        return prog


    # ---- the unknown-gate question -------------------------------------------------------------------


    def prepare_dispatch(self, task) -> bool:
        """Require a known script and register one shared owner decision when absent."""
        from .ledger.waiting import OWNER_ANSWER, cause_key, repo_key
        prog = self.repo(task)
        tid = task["id"]
        if prog is None:
            self.port.set_status(tid, "needs-owner", reason=f"no repo config for {task.get('repo')!r}")
            return False
        waiting = self.port.waiting
        name = repo_key(repos.repo_name(prog))
        origin_cause = cause_key("origin", name)
        if waiting.state(origin_cause) in ("open", "attempting"):
            waiting.wait(("task", tid), OWNER_ANSWER, cause_key(origin_cause), cause=origin_cause,
                         data={"mode": "blocked"})
            return False
        if not repos.ci_policy(prog)["unknown"]:
            return True
        cause = cause_key("script", name)
        waiting.wait(("task", tid), OWNER_ANSWER, cause_key(cause), cause=cause,
                     data={"mode": "needs-owner"})
        hint = repos.ci_policy(prog).get("hint")
        waiting.ask(cause, "task", tid, f"What is the test script path for {name}?",
                    "Add [scripts] test to .drover/config.toml, or answer with its relative .ps1 script path."
                    + (f" Hint: {hint}." if hint else ""))
        return False

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
        repo = repos.native(repos.repo_path(prog))
        if not (Path(repo) / ".git").exists():
            raise WorktreeRefused(f"{repos.repo_path(prog)} is not a git checkout")
        have = t.get("worktree")
        branch = t.get("branch")
        if branch and branch == repos.default_branch(prog):
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
                reuse_cfg = placement.config_path(path, repos.repo_path(prog))
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
            raise WorktreeRefused(f"{path} is the main checkout of {repos.repo_name(prog)}, not a task worktree")
        head = git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if head == repos.default_branch(prog):
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
        return ci.dirty(path)

    @staticmethod
    def _owned(path, tid):
        try:
            tag = garden.read_tag(garden.admin_dir_of(path)) or {}
        except (ValueError, OSError):
            return False
        return tag.get("owner") == OWNER_PREFIX + tid

    def _create(self, t, prog, repo, branch, source=None):
        tid, remote, default = t["id"], repos.remote_name(prog), repos.default_branch(prog)
        from .ledger.waiting import OWNER_ANSWER, cause_key, repo_key
        waiting = self.port.waiting
        name = repo_key(repos.repo_name(prog))
        cause = cause_key("origin", name)
        if waiting.state(cause) in ("open", "authorized", "attempting") and not waiting.consume_retry(cause):
            waiting.wait(("task", tid), OWNER_ANSWER, cause_key(cause), cause=cause,
                         data={"mode": "blocked"})
            raise WorktreeRefused("origin sync paused; answer the repo alert to retry")
        try:
            origin.sync(repo, remote, default, run=git, timeout=FETCH_TIMEOUT_S)
        except origin.FetchFailed as e:
            waiting.wait(("task", tid), OWNER_ANSWER, cause_key(cause), cause=cause,
                         data={"mode": "needs-owner", "note": str(e)})
            waiting.ask(cause, "task", tid, f"Origin sync paused: {name}", str(e),
                        options=[{"label": "Retry origin sync", "recommended": True}])
            self.port.append_event("origin-sync-failed", {"repo": name, "reason": str(e)})
            raise WorktreeRefused(str(e)) from e
        except origin.SyncError as e:
            raise WorktreeRefused(str(e)) from e
        self.port.append_event("origin-synced", {"repo": name})
        base = self.base_ref(prog)
        name = f"drover-{tid}"
        dest = str(Path(repos.native(join_cfg(repos.worktree_root(prog), name))).resolve())
        dest_cfg = placement.config_path(dest, repos.repo_path(prog))
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
                garden.tag_worktree(dest, repos.repo_name(prog), "slice", owner=OWNER_PREFIX + tid, purpose=str(t["title"])[:120],
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
        return repos.default_branch(prog)

    def head(self, task):
        return ci.head(repos.native(task["worktree"]))

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
