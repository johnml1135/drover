"""Render BOARD.md from the ledger: no agent in the loop."""
from datetime import datetime, timezone

from . import ops
from . import states
from .states import STATES
from .waiting import Waiting, TASK_TERMINAL, REPLAN, DEADLINE, BASE


def esc(text):
    """Make untrusted text safe inline: one line, no leading markdown control, no table or code breaks."""
    s = " ".join(str(text).split())
    s = s.replace("\\", "\\\\").replace("|", "\\|").replace("`", "\\`").replace("<", "&lt;").replace("[", "\\[").replace("]", "\\]")
    if s and s[0] in "#>-*+=~_":
        s = "\\" + s
    return s


def _line(t):
    return f"- {t['id']} [{t['priority']}/{t['size']}] {esc(t['title'])}"


def waiting_labels(conn, waits):
    """Owner-facing text for decoded Waiting records, shared by board, CLI and hub."""
    labels = []
    for row in waits:
        if row['kind'] in (TASK_TERMINAL, REPLAN):
            source = row['data']['source']
            label = 'Waiting on ' + ops.get_task(conn, source)['title'] + ' (' + source + ')'
        elif row['kind'] == DEADLINE:
            until = datetime.fromtimestamp(row['timeout_at'], timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            label = 'Waiting until ' + until
        elif row['kind'] == BASE:
            label = 'Waiting on base CI'
        else:
            label = 'Waiting on you'
        if row['data'].get('note'):
            label += ': ' + row['data']['note']
        labels.append(label)
    return labels


def render(conn, now=None):
    now = now or ops.clock()
    today = now.strftime("%Y-%m-%d")
    tasks = ops.list_tasks(conn)
    waiting = Waiting(conn)
    by = {s: [t for t in tasks if t["status"] == s] for s in STATES}
    out = [f"# Drover board", f"", f"Generated {ops.stamp(now)}", "", "## Counts", "", "| State | Tasks |", "|---|---|"]
    out += [f"| {s} | {len(by[s])} |" for s in STATES]
    out += ["", "## Running", ""]
    out += [f"{_line(t)} ({esc(t['owner_kind'] or '?')}/{esc(t['model'] or '?')}, attempt {t['attempts']})"
            for t in by["claimed"] + by["running"]] or ["- none"]
    out += ["", "## Blocked", ""]
    out += [f"{_line(t)}: {esc('; '.join(waiting_labels(conn, waiting.pending(('task', t['id'])))))}"
            for t in by[states.BLOCKED]] or ["- none"]
    out += ["", "## Needs owner", ""]
    out += [f"{_line(t)}: {esc('; '.join(waiting_labels(conn, waiting.pending(('task', t['id'])))) or 'Waiting on you')}"
            for t in by[states.NEEDS_OWNER]] or ["- none"]
    out += ["", "## Open questions", ""]
    qs = ops.open_questions(conn)
    out += [f"- Q{q['id']} ({q['target_kind']} {q['target_id']}): {esc(q['title'])}" for q in qs] or ["- none"]
    landed = []
    for e in ops.events(conn, kind="status"):
        if e["data"].get("to") == "landed" and e["at"].startswith(today):
            try:
                task = ops.get_task(conn, e["task_id"])
            except ops.NotFound:
                continue  # the owner may delete a task while retaining its audit events
            if not task["archived"]:
                landed.append(task)
    out += ["", "## Landed today", ""] + ([_line(t) for t in landed] or ["- none"]) + [""]
    return "\n".join(out)
