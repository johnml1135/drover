"""Render BOARD.md from the ledger: no agent in the loop."""
import json

from . import ops
from .states import STATES


def esc(text):
    """Make untrusted text safe inline: one line, no leading markdown control, no table or code breaks."""
    s = " ".join(str(text).split())
    s = s.replace("\\", "\\\\").replace("|", "\\|").replace("`", "\\`").replace("<", "&lt;").replace("[", "\\[").replace("]", "\\]")
    if s and s[0] in "#>-*+=~_":
        s = "\\" + s
    return s


def _line(t):
    return f"- {t['id']} [{t['priority']}/{t['size']}] {esc(t['title'])}"


def render(conn, now=None):
    now = now or ops.clock()
    today = now.strftime("%Y-%m-%d")
    tasks = ops.list_tasks(conn)
    by = {s: [t for t in tasks if t["status"] == s] for s in STATES}
    out = [f"# Drover board", f"", f"Generated {ops.stamp(now)}", "", "## Counts", "", "| State | Tasks |", "|---|---|"]
    out += [f"| {s} | {len(by[s])} |" for s in STATES]
    out += ["", "## Running", ""]
    out += [f"{_line(t)} ({esc(t['owner_kind'] or '?')}/{esc(t['model'] or '?')}, attempt {t['attempts']})"
            for t in by["claimed"] + by["running"]] or ["- none"]
    out += ["", "## Blocked", ""]
    out += [f"{_line(t)}: {esc((t['blocker'] or {}).get('reason', 'unknown'))}" for t in by["blocked"]] or ["- none"]
    out += ["", "## Needs owner", ""]
    out += [_line(t) for t in by["needs-owner"]] or ["- none"]
    out += ["", "## Open questions", ""]
    qs = ops.open_questions(conn)
    out += [f"- Q{q['id']} ({q['target_kind']} {q['target_id']}): {esc(q['title'])}" for q in qs] or ["- none"]
    landed = []
    for e in ops.events(conn, kind="status"):
        if e["data"].get("to") == "landed" and e["at"].startswith(today):
            landed.append(ops.get_task(conn, e["task_id"]))
    out += ["", "## Landed today", ""] + ([_line(t) for t in landed] or ["- none"]) + [""]
    return "\n".join(out)
