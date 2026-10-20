"""Background services the owner UI relies on: the garden executor, the daily tuner check and WSL tool discovery.

One thread (`services`) owns all three, so each uses its own ledger connection through the Store. Everything
that touches the machine (wsl.exe, git, disks, Ollama) is injectable, so tests never reach real state.
"""
from drover import placement
import json
import logging
import os
import queue
import re
import threading
import time
import urllib.request
from pathlib import Path

from drover import config, importer, tuner
from drover.tool_discovery import DISCOVERY_SCRIPT, parse_discovery_result
from drover.ledger import ops

log = logging.getLogger("hub.services")

TOOLS = ("claude", "codex")
ABS_POSIX = re.compile(r"^/[A-Za-z0-9._+@:=,/-]+$")
DAY_S = 86400


def valid_tool_path(line: str, tool: str) -> str | None:
    """An absolute POSIX path named `tool`, with no traversal or odd characters; else None."""
    line = line.strip()
    if not ABS_POSIX.match(line) or "//" in line:
        return None
    if line.startswith("/mnt/"):  # a Windows drive: not a Linux install, and not ours to trust
        return None
    parts = line.split("/")[1:]
    if any(p in ("", ".", "..") for p in parts) or parts[-1] != tool:
        return None
    return line


def parse_discovery(output: str) -> dict:
    """{tool: path} for each tool whose path validates. Login-shell noise, aliases and extra lines are ignored."""
    return parse_discovery_result(output)[0]


REPO_NAME = re.compile(r"[A-Za-z0-9._-]+")


def repo_path(name):
    """A bare repo name resolved under paths.repos_root (DROVER_REPOS_DIR; default ~/repos). No paths, no traversal."""
    if not isinstance(name, str) or not REPO_NAME.fullmatch(name) or name in (".", ".."):
        raise ValueError(f"bad repo name {name!r}")
    root = Path(config.native(os.environ.get("DROVER_REPOS_DIR") or config.get()["paths"]["repos_root"])).resolve()
    path = (root / name).resolve()
    if path.parent != root:
        raise ValueError(f"repo {name!r} resolves outside {root}")
    return str(path)


def remove_worktree_row(repo, row, base="origin/main"):
    """Remove one approved row's worktree with garden.py's own checks (clean, not busy, no lock note, rescue tag
    for unmerged commits). Returns garden's {ok, reason|record}."""
    from drover import garden
    wt = (row.get("worktree") or "").strip()
    common, found = garden.entries(repo)
    entry = next((e for e in found if garden.norm_path(e.path) == garden.norm_path(wt)), None)
    if entry is None:
        return {"ok": False, "reason": "no such linked worktree"}
    if entry.side != garden.this_side():
        return {"ok": False, "reason": "the other side's worktree"}
    return garden.remove_worktree(common, entry, base, garden.Proc(), delete_branch=False)


def real_disks():
    from drover import resources
    free = resources.read_disks(resources.is_windows())
    return {t["path"] or t["name"]: free.get(t["name"]) for t in config.get()["trackers"]
            if t["kind"] == "disk" and t["enabled"]}


def real_ollama(url, timeout=1.5):
    """True when the Ollama HTTP API answers."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=timeout) as r:
            return r.status == 200
    except (OSError, ValueError):
        return False


class Services:
    def __init__(self, store, settings, wsl=None, remove=None, tuner_mod=tuner, disks=real_disks,
                 ollama=real_ollama, clock=time.time):
        from hub.watchdog import real_wsl
        self.store, self.settings = store, settings
        self.wsl = wsl or real_wsl
        self.remove = remove or self.default_remove
        self.tuner, self.disks, self.ollama, self.clock = tuner_mod, disks, ollama, clock
        self.rescue_root = Path(settings.runs_dir).parent / "rescue"
        self._current = None
        self._queued = set()
        self._garden = queue.Queue()
        self._stop = threading.Event()
        self._thread = None
        self._wsl_was_up = False
        self._probe_cache = (0.0, None)
        self._tried = 0.0

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._loop, name="services", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._garden.put(None)
        if self._thread is not None:
            self._thread.join(10)

    def _loop(self):
        try:
            self.resume_garden()
            self.discover_tools()          # at hub start
            self._wsl_was_up = self.wsl_up()
        except Exception:
            log.exception("services start-up failed")
        while not self._stop.is_set():
            try:
                self.process_pending(block=60)
                self.tick()
            except Exception:  # the thread must outlive any one failure
                log.exception("services tick failed")

    def tick(self):
        """Once per wake: tools when WSL first comes up, the tuner when a day has passed."""
        self.check_wsl()
        self.check_tuner()

    # -- WSL tool discovery -----------------------------------------------
    def distro(self):
        return placement.distro(self.settings.distro, self.store.get_settings())

    def wsl_up(self) -> bool:
        """Is the distro running? `wsl -l --running` lists without booting anything."""
        rc, out = self.wsl(["-l", "--running"], self.settings.wsl_timeout_s)
        names = {ln.replace("(Default)", "").strip().lower() for ln in out.splitlines()}
        return rc == 0 and self.distro().lower() in names

    def discover_tools(self) -> dict:
        """Run the fixed discovery command, validate, store `wsl_bin_claude` / `wsl_bin_codex`. Returns what was
        stored. A missing or invalid tool keeps its previous setting."""
        self._tried = self.clock()
        rc, out = self.wsl(["-d", self.distro(), "--exec", "bash", "-lc", DISCOVERY_SCRIPT],
                           self.settings.wsl_timeout_s)
        found, reasons = parse_discovery_result(out) if rc == 0 else ({}, {})
        have = self.store.get_settings()
        patch = {f"wsl_bin_{t}": p for t, p in found.items() if have.get(f"wsl_bin_{t}") != p}
        if patch:
            self.store.update_settings(patch, author="wizard")
        missing = [t for t in TOOLS if t not in found]
        self.store.add_event("wsl.tools", "hub", {"found": found, "missing": missing, "reasons": reasons, "rc": rc})
        return found

    def check_wsl(self):
        up = self.wsl_up()
        have = self.store.get_settings()
        if up and (not self._wsl_was_up or not all(have.get(f"wsl_bin_{t}") for t in TOOLS)):
            if not self._wsl_was_up or self.clock() - self._tried > 600:
                self.discover_tools()
        self._wsl_was_up = up


    # -- tuner -------------------------------------------------------------
    def check_tuner(self, force_check=False):
        """At most daily: ask the tuner whether a pass is due; run one only then. Returns the pass result or None."""
        now = self.clock()
        last = float(ops.get_setting(self.store.conn, "_tuner_checked", 0) or 0)
        if not force_check and now - last < DAY_S:
            return None
        ops.set_setting(self.store.conn, "_tuner_checked", now)
        verdict = self.tuner.status(self.store.conn, now)["due"]
        if not verdict["due"]:
            return None
        res = self.tuner.run_pass(self.store.conn, now)
        self.store.add_event("tuner.pass", "tuner", {"ran": res.get("ran"), "version": res.get("version"),
                                                     "proposals": res.get("proposals", [])})
        self.store.publish("ledger", {"kind": "tuner.pass", "proposals": len(res.get("proposals", []))})
        return res

    # -- garden executor ---------------------------------------------------
    def submit_garden(self, batch):
        if batch not in self._queued:   # approve and resume must not queue one batch twice
            self._queued.add(batch)
            self._garden.put(batch)

    def process_pending(self, block=0):
        """Run every queued garden batch (waiting up to `block` seconds for the first)."""
        ran = []
        try:
            item = self._garden.get(timeout=block) if block else self._garden.get_nowait()
        except queue.Empty:
            return ran
        while item is not None:
            self._queued.discard(item)
            ran.append(self.run_garden(item))
            try:
                item = self._garden.get_nowait()
            except queue.Empty:
                break
        return ran

    def _last_approval(self, batch):
        return max((e["id"] for e in importer._approvals(self.store.conn) if e["data"].get("id") == batch), default=0)

    def resume_garden(self):
        """Batches approved before a restart whose latest approval never finished."""
        conn = self.store.conn
        for batch in {e["data"].get("id") for e in importer._approvals(conn)}:
            last = self._last_approval(batch)
            if not any(e["id"] > last and e["data"].get("id") == batch for e in ops.events(conn, kind="garden.done")):
                self.submit_garden(batch)

    def _batch_info(self, batch):
        for e in reversed(ops.events(self.store.conn, kind="import-batch")):
            if e["data"].get("id") == batch:
                return e["data"].get("repo"), e["data"].get("group")
        return None, None

    def _row_event(self, batch, row, kind, **data):
        self.store.add_event(f"garden.row.{kind}", "garden", {"batch": batch, "branch": row["branch"], **data})

    def default_remove(self, repo, row):
        """One approved row: a worktree goes through garden.remove_worktree, a branch with no worktree through
        garden.delete_branch (local only; snapshot/archive batches also get a bundle)."""
        from drover import garden
        path = repo_path(repo)
        if (row.get("worktree") or "").strip():
            return remove_worktree_row(path, row)

        def log(step, **data):
            self.store.add_event(f"garden.branch.{step}", "garden", {"batch": self._current, **data})
        return garden.delete_branch(path, row["branch"], row, log=log, rescue_root=self.rescue_root,
                                    bundle=row.get("group") == "owner-review-snapshot")

    def run_garden(self, batch):
        """Remove exactly importer.approved_rows(batch), one row at a time, logging and publishing each. A row
        already logged since the latest approval is never repeated; rows that failed are re-proposed afterwards."""
        conn = self.store.conn
        self._current = batch
        rows = importer.approved_rows(conn, batch)
        repo, group = self._batch_info(batch)
        last = self._last_approval(batch)
        logged = {e["data"].get("branch") for k in ("removed", "kept-repropose", "error")
                  for e in ops.events(conn, kind=f"garden.row.{k}")
                  if e["data"].get("batch") == batch and e["id"] > last}
        counts = {"removed": 0, "kept-repropose": 0, "error": 0}
        total = len(rows)
        for i, row in enumerate(rows, 1):
            if row["branch"] in logged:
                continue
            try:
                res = self.remove(repo, dict(row, group=group)) if repo else {"ok": False, "reason": "batch has no repo"}
                kind = "removed" if res.get("ok") else "kept-repropose" if res.get("repropose") else "error"
            except Exception as e:
                res = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
                kind = "error"
            counts[kind] += 1
            self._row_event(batch, row, kind, reason=res.get("reason", ""), worktree=row.get("worktree", ""))
            self.store.publish("ledger", {"kind": "garden.progress", "id": batch, "done": i, "total": total,
                                          "row": row["branch"], "result": kind})
        failed = importer.failed_rows(conn, batch)
        again = importer.repropose(conn, batch, failed) if failed else None
        self.store.add_event("garden.done", "garden", {"id": batch, "total": total, **counts,
                                                       "reproposed": again["id"] if again else None})
        self.store.publish("ledger", {"kind": "garden.done", "id": batch, **counts})
        self._current = None
        return counts

    # -- probes for the Health tab ------------------------------------------
    def ollama_status(self):
        """Reachability (cached 30 s) and p95 from a stored decide bench (the `ollama_bench` setting, a JSON
        {"p95_ms": ...} written by `python -m drover.decide bench`'s caller), if any."""
        at, cached = self._probe_cache
        if cached is None or self.clock() - at > 30:
            url = self.store.get_settings().get("ollama_url") or self.settings.ollama_url
            cached = bool(self.ollama(url))
            self._probe_cache = (self.clock(), cached)
        p95 = None
        raw = self.store.get_settings().get("ollama_bench")
        try:
            p95 = json.loads(raw).get("p95_ms") if raw else None
        except (ValueError, AttributeError):
            pass
        return {"reachable": cached, "p95_ms": p95}

    def disk_free(self):
        out = []
        for name, gb in self.disks().items():
            out.append({"name": name, "free_gb": None if gb is None else round(gb, 1)})
        return out

