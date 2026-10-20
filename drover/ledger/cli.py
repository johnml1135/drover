"""`python -m drover.ledger`: init, add, show, ready, claim, approve, block, board."""
import argparse
import json
import sys
from pathlib import Path

from . import board, ops
from ..task_context import KINDS
from .db import HubRequired, connect, default_path, main_error


def _emit(args, obj, text):
    print(json.dumps(obj, indent=2, sort_keys=True) if args.json else text)


def _task_text(t):
    return f"{t['id']} {t['status']} [{t['priority']}/{t['size']}/{t['risk']}] {t['title']}"


def cmd_init(conn, a):
    _emit(a, {"ok": True}, "ledger initialised")


def cmd_add(conn, a):
    if a.kind == "epic":
        e = ops.create_epic(conn, a.repo, a.title, a.plan or "", a.actor)
        return _emit(a, e, f"{e['id']} {e['title']}")
    deps = [d.split(":", 1) if ":" in d else d for d in a.dep]
    t = ops.create_task(conn, a.title, a.repo, a.epic, a.repo_path, a.goal or "", a.accept, a.scope, a.priority,
                        a.size, a.risk, "draft" if a.draft else "proposed", a.actor, deps, kind=a.task_kind, write=a.write, read_access=a.read_access,
                        branch=a.branch, related=a.related)
    _emit(a, t, _task_text(t))


def cmd_show(conn, a):
    if a.id.startswith("E-"):
        e = ops.get_epic(conn, a.id)
        return _emit(a, e, f"{e['id']} {e['status']} {e['title']}")
    t = ops.get_task(conn, a.id)
    t["deps"] = ops.deps_of(conn, a.id)
    t["runs"] = ops.list_runs(conn, a.id)
    from .waiting import Waiting
    t.pop("blocker", None)
    t["waits"] = Waiting(conn).pending(("task", a.id))
    t["waiting_labels"] = board.waiting_labels(conn, t["waits"])
    lines = [_task_text(t), f"  goal: {t['goal']}", f"  scope: {', '.join(t['scope_files'])}",
             f"  deps: {', '.join(d['depends_on'] + '(' + d['kind'] + ')' for d in t['deps']) or 'none'}",
             f"  attempts: {t['attempts']}  runs: {len(t['runs'])}"]
    lines.extend("  " + label for label in t["waiting_labels"])
    _emit(a, t, "\n".join(lines))


def cmd_ready(conn, a):
    ts = ops.ready_tasks(conn, a.repo, a.repo_path)
    _emit(a, ts, "\n".join(_task_text(t) for t in ts) or "nothing ready")


def cmd_claim(conn, a):
    t = ops.claim_next(conn, {"owner_kind": a.owner, "model": a.model, "branch": a.branch, "worktree": a.worktree,
                              "repo": a.repo, "repo_path": a.repo_path, "actor": a.owner})
    _emit(a, t, _task_text(t) if t else "nothing to claim")
    return 0 if t else 3


def cmd_approve(conn, a):
    t = ops.approve(conn, a.id, a.actor)
    _emit(a, t, _task_text(t))


def cmd_block(conn, a):
    t = ops.block(conn, a.id, a.reason, a.actor)
    _emit(a, t, _task_text(t))


def cmd_board(conn, a):
    text = board.render(conn)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    _emit(a, {"board": text}, text)


def parser():
    p = argparse.ArgumentParser(prog="python -m drover.ledger")
    p.add_argument("--db", help="ledger path (default $DROVER_LEDGER, else <state dir>\\ledger.db on Windows)")
    p.add_argument("--json", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    a = sub.add_parser("add")
    a.add_argument("kind", choices=("epic", "task"))
    a.add_argument("title")
    a.add_argument("--repo", default="drover")
    a.add_argument("--epic")
    a.add_argument("--repo_path")
    a.add_argument("--goal", "--description", dest="goal")
    a.add_argument("--kind", dest="task_kind", choices=KINDS, default="implementation")
    a.add_argument("--write", action=argparse.BooleanOptionalAction, default=None)
    a.add_argument("--read-access", action="append")
    a.add_argument("--branch")
    a.add_argument("--related", action="append", default=[])
    a.add_argument("--plan")
    a.add_argument("--accept", action="append", default=[])
    a.add_argument("--scope", action="append", default=[])
    a.add_argument("--priority", default="P2", choices=("P0", "P1", "P2", "P3", "P4"))
    a.add_argument("--size", default="M", choices=("S", "M", "L"))
    a.add_argument("--risk", default="low", choices=("low", "high"))
    a.add_argument("--dep", action="append", default=[], help="TASK or TASK:waits-for")
    a.add_argument("--draft", action="store_true")
    a.add_argument("--actor", default="system")
    s = sub.add_parser("show")
    s.add_argument("id")
    r = sub.add_parser("ready")
    r.add_argument("--repo")
    r.add_argument("--repo_path")
    c = sub.add_parser("claim")
    c.add_argument("--owner", default="worker")
    c.add_argument("--model")
    c.add_argument("--branch")
    c.add_argument("--worktree")
    c.add_argument("--repo")
    c.add_argument("--repo_path")
    ap = sub.add_parser("approve")
    ap.add_argument("id")
    ap.add_argument("--actor", default="system")
    b = sub.add_parser("block")
    b.add_argument("id")
    b.add_argument("reason")
    b.add_argument("--actor", default="system")
    bd = sub.add_parser("board")
    bd.add_argument("--out", help="write BOARD.md here")
    return p


COMMANDS = {"init": cmd_init, "add": cmd_add, "show": cmd_show, "ready": cmd_ready, "claim": cmd_claim,
            "approve": cmd_approve, "block": cmd_block, "board": cmd_board}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--json" in argv:  # accept --json anywhere
        argv = ["--json", *(a for a in argv if a != "--json")]
    args = parser().parse_args(argv)
    try:
        path = Path(args.db) if args.db else default_path()
    except HubRequired as e:
        return main_error(str(e))
    conn = connect(path)
    try:
        return COMMANDS[args.cmd](conn, args) or 0
    except (ops.NotFound, ops.IllegalTransition, PermissionError, ValueError) as e:
        return main_error(str(e))
    finally:
        conn.close()
