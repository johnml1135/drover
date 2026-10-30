"""The dispatcher's view of a repo: worktrees, the unknown-gate question and review inputs.

The dispatcher (drover.dispatcher) owns runs and the ledger transitions; this module owns git and the repo
config. Git is always driven with an argv list, never a shell. Paths in the ledger stay in the repo config's form
(R:\\repos\\x); `repos.native` turns one into a path this OS can open, and the launcher maps it for WSL.
"""
import json
import re
import subprocess
import time
from fnmatch import fnmatchcase
from pathlib import Path

from . import config, ownership, ci, placement, origin, repos
from .ledger import states

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


def git(repo, *args, check=False, timeout=None, **kw):
    """git with an argv list. A timeout is a failed command (rc 124), never a hang."""
    try:
        r = placement.git(repo, *args, timeout=timeout, **kw)
    except subprocess.TimeoutExpired:
        r = subprocess.CompletedProcess(args, 124, "", f"git {args[0]} timed out after {timeout}s")
    if check and r.returncode:
        raise WorktreeRefused(f"git {' '.join(args[:3])}: {(r.stderr or r.stdout).strip()[-300:]}")
    return r


def join_cfg(base, name):
    """base/name in the form `base` is written in (Windows backslashes or POSIX slashes)."""
    return base.rstrip("\\/") + ("\\" if "\\" in base else "/") + name


class Repository:
    """Repository effects used by pipeline policy and CI planning.

    Calls observe fresh Git state; the default adapter does not cache evidence.
    Tests can supply commit/worktree facts without emulating the Git CLI.
    """
    def load(self, prog, revision="HEAD", checkout=None):
        return repos.load(prog, revision, checkout)

    def reachable(self, path):
        return placement.reachable(path)

    def canonical(self, path):
        return placement.canonical(path)

    def prepare_worktree(self, path):
        return placement.prepare_worktree(path)

    def changed_paths(self, repo, base, tip="HEAD"):
        return [p for p in git(repo, "diff", "--name-only", "--no-renames", "-z",
                               f"{base}...{tip}", check=True).stdout.split("\0") if p]

    def changed_docs_only(self, repo, base, tip, globs):
        return ci.changed_docs_only(repo, base, tip, globs)

    def is_checkout(self, path):
        return (Path(path) / ".git").exists()

    def head(self, path):
        return git(path, "rev-parse", "HEAD", check=True).stdout.strip()

    def branch(self, path):
        return git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    def status(self, path):
        """Short status including untracked files, or None when Git cannot report it."""
        result = git(path, "status", "--short", "--untracked-files=all", timeout=10)
        return None if result.returncode else result.stdout.strip()

    def inventory(self, repo, side=None):
        result = git(repo, "worktree", "list", "--porcelain", **({"side": side} if side else {}))
        if result.returncode:
            return []
        return [dict(line.partition(" ")[::2] for line in block.splitlines())
                for block in result.stdout.split("\n\n")]

    def owned(self, path, tid):
        return ownership.owned_by(path, OWNER_PREFIX + tid)

    def dirty(self, path):
        return ci.dirty(path)

    def repair(self, path):
        return placement.relativize_gitdir(path)

    def tag(self, path, name, tid):
        ownership.tag(path, name, OWNER_PREFIX + tid, time.time() + 86400)

    def sync(self, repo, remote, default):
        return origin.sync(repo, remote, default, run=git, timeout=FETCH_TIMEOUT_S)

    def start_ref(self, repo, branch, base, remote):
        for ref in (f"refs/heads/{branch}", f"refs/remotes/{remote}/{branch}"):
            if git(repo, "rev-parse", "--verify", "--quiet", ref).returncode == 0:
                return ref
        return base

    def add(self, repo, dest, branch, start_ref, remote="origin"):
        if start_ref == f"refs/heads/{branch}":
            tail = [str(dest), branch]
        else:
            tail = ["-b", branch, str(dest), start_ref]
        placement.add_worktree(repo, dest, *tail, run=git, error=WorktreeRefused)

    def remove(self, repo, path):
        placement.unlock_worktree(repo, path)
        git(repo, "worktree", "remove", str(path), check=True, side="wsl")

    def changes(self, repo):
        changed = git(repo, "diff", "--name-only", "--no-renames", "-z", "HEAD", check=True).stdout
        untracked = git(repo, "ls-files", "--others", "--exclude-standard", "-z", check=True).stdout
        return sorted(set(p for p in (changed + untracked).split("\0") if p))

    def trailers(self, repo):
        text = git(repo, "log", "-1", "--format=%(trailers:key=Drover-Task,valueonly)%x00"
                   "%(trailers:key=Drover-Run,valueonly)", check=True).stdout
        return [s.strip() for s in text.split("\0")]

    def ahead(self, repo, base):
        return git(repo, "rev-list", "--count", f"{base}..HEAD").stdout.strip() not in ("", "0")

    def commit(self, repo, message):
        git(repo, "add", "-A", check=True)
        git(repo, "commit", "-m", message, check=True)
        return git(repo, "rev-parse", "HEAD", check=True).stdout.strip()

    def diff_lines(self, repo, base, sha):
        rows = git(repo, "diff", "--numstat", "--no-renames", f"{base}...{sha}").stdout.splitlines()
        return sum(int(n) for row in rows for n in row.split("\t")[:2] if n.isdigit())

    def diff_text(self, repo, base, sha):
        return git(repo, "diff", "--no-renames", f"{base}...{sha}").stdout


class Pipeline:
    def __init__(self, port, repos_map=None, repos_dir=None, runs_dir=None, *, repository=None):
        self.port = port
        self._repos = repos_map if repos_map is not None else None
        self.repos_dir = repos_dir
        self.runs_dir = str(runs_dir or config.state_path("runs"))
        self.repository = repository or Repository()
        self.ci = ci.CI(self.runs_dir, source=repository)

    def commit_ready(self, task, run_id, message):
        """Commit scoped work; repository read failures follow the normal refusal path."""
        try:
            return self._commit_ready(task, run_id, message)
        except ci.CIError as error:
            raise WorktreeRefused(str(error)) from error

    def _commit_ready(self, task, run_id, message):
        """Refuse before touching the index on a scope violation."""
        if task.get("write") is False:
            raise CommitRefused("read-only task cannot commit")
        if not task.get("worktree"):
            raise CommitRefused("ready_to_commit has no task worktree")
        repo = repos.native(task["worktree"])
        paths = self.repository.changes(repo)
        trailers = self.repository.trailers(repo)
        if trailers == [str(task["id"]), str(run_id)]:
            if paths:
                raise CommitRefused("worktree changed after Drover committed this run", paths)
            return self.repository.head(repo)
        scope = [re.sub(r":\d+(?:-\d+)?$", "", s.replace("\\", "/")).removeprefix("./").rstrip("/")
                 for s in task.get("scope_files") or []]

        def out_of_scope(ps):
            return [p for p in ps if not any(fnmatchcase(p, s) or p.startswith(s + "/") for s in scope)]
        if not paths:
            prog = self.repo(task)
            base = self.base_ref(prog) if prog else None
            if base and not any(trailers) and self.repository.ahead(repo, base):
                ahead = self.repository.changed_paths(repo, base, "HEAD")
                outside = out_of_scope(sorted(ahead))
                if outside:
                    raise CommitRefused("out-of-scope changes: " + ", ".join(outside), outside)
                return self.repository.head(repo)   # committed by the worker: gate HEAD
            raise CommitRefused("no changes")
        outside = out_of_scope(paths)
        if outside:
            raise CommitRefused("out-of-scope changes: " + ", ".join(outside), outside)
        text = f"{message.strip()}\n\nDrover-Task: {task['id']}\nDrover-Run: {run_id}\n"
        return self.repository.commit(repo, text)

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
        if checkout and not self.repository.reachable(checkout):
            checkout = None      # recover stale worktrees using the reachable machine checkout's config
        try:
            prog = self.repository.load(prog, checkout=checkout)
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
            self.port.set_status(tid, states.NEEDS_OWNER, reason=f"no repo config for {task.get('repo')!r}")
            return False
        waiting = self.port.waiting
        name = repo_key(repos.repo_name(prog))
        origin_cause = cause_key("origin", name)
        if waiting.state(origin_cause) in ("open", "attempting"):
            if waiting.state(origin_cause) == "attempting":
                waiting.consume_retry(origin_cause)  # abandoned probes reopen; no new grant is claimed here
            waiting.wait(("task", tid), OWNER_ANSWER, cause_key(origin_cause), cause=origin_cause,
                         data={"mode": "blocked"})
            return False
        if not repos.ci_policy(prog)["unknown"]:
            return True
        cause = cause_key("script", name)
        waiting.wait(("task", tid), OWNER_ANSWER, cause_key(cause), cause=cause,
                     data={"mode": "needs-owner"})
        hint = repos.ci_policy(prog).get("hint")
        if not any(r['cause'] == cause and r['question_id'] is not None for r in waiting.pending()):
            waiting.ask(cause, "task", tid, f"What is the test script path for {name}?",
                        "Add [scripts] test to .drover/config.toml, or answer with its relative .ps1 script path."
                        + (f" Hint: {hint}." if hint else ""))
        return False

    # ---- worktrees -----------------------------------------------------------------------------------
    def worktree(self, task, where=None):
        """The task's worktree path (config form), created on first use, for placement `where` ("windows" when
        omitted; the dispatcher decides it first). Windows gates require a shared worktree (ADR 0007).
        An existing worktree is used only when it is clean or the task owns it (ownership tag owner drover:<id>);
        otherwise WorktreeRefused. Syncs local main first, branches from it, renews ownership."""
        tid = task["id"]
        if not TASK_ID_RE.match(str(tid)):
            raise WorktreeRefused(f"task id {tid!r} is not safe in a path")
        t = self.port.get_task(tid)
        prog = self.repo(t)
        if prog is None:
            raise WorktreeRefused(f"no repo config for {t.get('repo')!r}")
        repo = repos.native(repos.repo_path(prog))
        if not self.repository.is_checkout(repo):
            raise WorktreeRefused(f"{repos.repo_path(prog)} is not a git checkout")
        have = t.get("worktree")
        branch = t.get("branch")
        if branch and branch == repos.default_branch(prog):
            raise WorktreeRefused(f"task branch {branch!r} is the default branch; work happens on its own branch")
        reuse, why = None, None
        if have and self.repository.reachable(repos.native(have)):
            reuse = have
        else:
            found, reusable = self._worktree_of_branch(repo, branch) if branch else (None, False)
            if reusable:
                reuse = found
            elif found or have:
                # Never prune or delete the stale registration: it may be another host's, or the owner's.
                why = f"registered worktree {found or have} is unreachable from this host"
        if reuse:
            path = self.repository.canonical(repos.native(reuse))
            self._not_main(path, repo, prog)
            if reuse != have and not placement.drive(path):
                prog = self.repo({**t, "worktree": reuse})
                if prog is None:
                    raise WorktreeRefused(f"no repo config for worktree {reuse}")
            path_parts = placement.path_parts(path)
            shared_parts = placement.path_parts(repos.worktree_root(prog, "windows"))
            on_shared_root = len(path_parts) > len(shared_parts) and path_parts[:len(shared_parts)] == shared_parts
            if "windows" in repos.gate_platforms(prog) and not placement.drive(path) and not on_shared_root:
                root_parts = placement.path_parts(repos.worktree_root(prog, "wsl"))
                if len(path_parts) <= len(root_parts) or path_parts[:len(root_parts)] != root_parts:
                    raise WorktreeRefused(f"branch {branch} is checked out at {reuse}, outside Drover's worktrees; move or remove it")
                if self.repository.changes(path):
                    raise WorktreeRefused(f"worktree {reuse} has uncommitted changes and must move to the shared root for the Windows gate")
                if not branch or self.repository.branch(path) != branch:
                    raise WorktreeRefused(f"worktree {reuse} is not the task's own branch; left untouched")
                root = repos.worktree_root(prog, "windows")
                if not placement.drive(root):
                    raise WorktreeRefused(f"worktrees root {root} is not reachable from Windows")
                return self._create(t, prog, repo, branch, where="windows", move_from=reuse)
            dirty = self._dirty_foreign(path, tid)
            if not dirty and self._owned(path, tid) and not self.repository.repair(path):
                self.port.append_event("worktree-repair-failed", {"path": str(path), "branch": branch}, tid)
                why = f"worktree {reuse} has a gitdir pointer that could not be made relative; left untouched"
            elif not dirty:
                reuse_cfg = placement.config_path(path, repos.repo_path(prog))
                try:
                    self.repository.tag(path, repos.repo_name(prog), tid)
                except (ValueError, OSError, subprocess.SubprocessError) as e:
                    raise WorktreeRefused(f"could not record ownership of {path}: {e}") from e
                if reuse_cfg != t.get("worktree") or branch != t.get("branch"):
                    self._persist(tid, branch, reuse_cfg)
                return reuse_cfg
            else:
                why = (f"worktree {reuse} has someone else's uncommitted changes ({dirty.splitlines()[0][:100]}); "
                       "left untouched")
        if why:
            self.port.append_event("worktree-fallback", {"source_branch": branch, "reason": why}, tid)
        return self._create(t, prog, repo, branch, source=branch if why else None, where=where)

    def _not_main(self, path, repo, prog):
        """Never hand out the repo's own checkout (or anything sitting on the default branch) to a worker."""
        if Path(path).resolve() == Path(repo).resolve():
            raise WorktreeRefused(f"{path} is the main checkout of {repos.repo_name(prog)}, not a task worktree")
        head = self.repository.branch(path)
        if head == repos.default_branch(prog):
            raise WorktreeRefused(f"{path} is on the default branch {head!r}; a task needs its own branch")

    def _persist(self, tid, branch, wt):
        self.port.update_task(tid, branch=branch, worktree=wt)

    def _worktree_of_branch(self, repo, branch):
        """Return registration evidence and whether its owning inventory permits reuse."""
        # Each Git understands its own absolute administrative pointers. Windows Git can
        # mis-spell ext4 registrations as H:/home/...; their placement lock takes precedence.
        registered = None
        for side in ((None, "wsl") if placement.IS_WINDOWS else (None,)):
            for fields in self.repository.inventory(repo, side):
                if fields.get("branch") != f"refs/heads/{branch}" or not fields.get("worktree"):
                    continue
                path = repos.native(fields["worktree"])
                registered = registered or path
                owner = fields.get("locked")
                if placement.IS_WINDOWS and owner in ("windows", "wsl") and owner != (side or "windows"):
                    continue
                # Prefer the owning inventory's spelling even when the directory is gone:
                # the caller needs registration evidence to branch from the committed tip.
                registered = path
                if self.repository.reachable(path):
                    return path, True
        return registered, False

    def _dirty_foreign(self, path, tid):
        """The first dirty line when `path` has uncommitted changes that are not this task's, else ''."""
        if self._owned(path, tid):
            return ""
        return self.repository.dirty(path)

    def _owned(self, path, tid):
        return self.repository.owned(path, tid)

    def _create(self, t, prog, repo, branch, source=None, where=None, move_from=None):
        tid, remote, default = t["id"], repos.remote_name(prog), repos.default_branch(prog)
        from .ledger.waiting import OWNER_ANSWER, cause_key, repo_key
        waiting = self.port.waiting
        name = repo_key(repos.repo_name(prog))
        cause = cause_key("origin", name)
        if waiting.state(cause) in ("open", "authorized", "attempting") and not waiting.consume_retry(cause):
            waiting.wait(("task", tid), OWNER_ANSWER, cause_key(cause), cause=cause,
                         data={"mode": "blocked"})
            return False
        try:
            self.repository.sync(repo, remote, default)
        except origin.FetchFailed as e:
            waiting.wait(("task", tid), OWNER_ANSWER, cause_key(cause), cause=cause,
                         data={"mode": "needs-owner", "note": str(e)})
            if not any(r['cause'] == cause and r['question_id'] is not None for r in waiting.pending()):
                waiting.ask(cause, "task", tid, f"Origin sync paused: {name}", str(e),
                            options=[{"label": "Retry origin sync", "recommended": True}], allow_text=False)
            self.port.append_event("origin-sync-failed", {"repo": name, "reason": str(e)})
            return False
        except origin.SyncError as e:
            raise WorktreeRefused(str(e)) from e
        self.port.append_event("origin-synced", {"repo": name})
        base = self.base_ref(prog)
        if source:                       # the branch's own worktree is unusable: branch from its committed tip
            branch, base = f"drover/{tid}", source
        branch = branch or f"drover/{tid}"
        start_ref = self.repository.start_ref(repo, branch, base, remote)
        if where == "wsl":
            try:
                creation_policy = self.repository.load(prog, start_ref, repo)
            except repos.RepoError as e:
                raise WorktreeRefused(f"could not load gate policy at {start_ref}: {e}") from e
            if "windows" in repos.gate_platforms(creation_policy):
                where = "windows"
                root = repos.worktree_root(prog, where)
                if not placement.drive(root):
                    raise WorktreeRefused(f"worktrees root {root} is not reachable from Windows")
        name = f"drover-{tid}"
        root = repos.worktree_root(prog, where or "windows")
        remote_fs = placement.routes_through_wsl(root)       # a Linux path seen from a Windows hub: only WSL opens it
        dest = join_cfg(root, name) if remote_fs else str(Path(repos.native(join_cfg(root, name))).resolve())
        dest_cfg = placement.config_path(dest, repos.repo_path(prog))
        exists = self.repository.reachable(dest) if remote_fs else Path(dest).exists()
        if exists:
            if self._dirty_foreign(dest, tid) or not self._owned(dest, tid):
                raise WorktreeRefused(f"{dest_cfg} exists and is not this task's worktree")
            if move_from and self.repository.branch(dest) != branch:
                raise WorktreeRefused(f"{dest_cfg} is not on task branch {branch}")
        if move_from:
            self.repository.remove(repo, repos.native(move_from))
        try:
            if not exists:
                if not remote_fs:                   # git creates missing parents of an ext4 destination itself
                    Path(dest).parent.mkdir(parents=True, exist_ok=True)
                self._add(repo, dest, branch, start_ref, remote)
            try:
                self.repository.tag(dest, repos.repo_name(prog), tid)
            except (ValueError, OSError, subprocess.SubprocessError) as e:
                raise WorktreeRefused(f"created {dest} but could not record ownership: {e}") from e
            self._persist(tid, branch, dest_cfg)
        except Exception as e:
            if move_from:
                self._persist(tid, branch, None)
                raise WorktreeRefused(f"could not replace worktree for branch {branch}: {e}") from e
            raise
        if move_from:
            self.port.append_event("worktree-replaced", {"path": move_from, "branch": branch, "root": root}, tid)
        return dest_cfg

    def _add(self, repo, dest, branch, base, remote="origin"):
        self.repository.add(repo, dest, branch, base, remote)

    # ---- gate plan -----------------------------------------------------------------------------------
    def base_ref(self, prog):
        """Task diffs, reviews and new branches use local main, including its unpushed landings."""
        return repos.default_branch(prog)

    def status(self, cwd):
        return self.repository.status(cwd)

    def head(self, task):
        # Gate reads retain CI timeouts and environment; commit recovery uses pipeline Git.
        source = self.ci.source
        return (source.head if source else ci.head)(repos.native(task["worktree"]))

    def diff_lines(self, repo, base, sha):
        return self.repository.diff_lines(repo, base, sha)

    def diff_text(self, repo, base, sha):
        return self.repository.diff_text(repo, base, sha)
