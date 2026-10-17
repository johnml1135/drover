"""Worker brief assembly: pinned block in the system layer, everything else in the user prompt, token-budgeted."""
import hashlib
import json
from pathlib import Path

PINNED_PATH = Path(__file__).with_name("pinned.md")
BUDGET_TOKENS = {"S": 6000, "M": 15000, "L": 15000}
EXCERPT_LINES = 60
NOTE_MAX = 300
ACTION_RECORD = (
    "## Finish\n"
    "End your final message with exactly one JSON object and nothing after it:\n"
    '{"status": "done" | "ready_to_commit" | "blocked", "sha": "<commit sha or empty>", '
    '"commit_message": "<subject and optional body for ready_to_commit, otherwise empty>", '
    '"blocker": {"kind": "<short kind>", "detail": "<what is wrong>"} or null, "note": "<=300 chars"}\n'
    "Codex workers: do not run git add/commit; leave changes in the worktree and report ready_to_commit with a "
    "commit message. Drover checks scope and makes the commit. Claude workers may commit and report done with the SHA.\n"
    "Use blocked only when the plan or environment, not your code, stops you.")


REVIEW_BUDGET_TOKENS = {"S": 12000, "M": 30000, "L": 30000}   # a reviewer reads a diff, so it gets more than a worker
REVIEW_RECORD = (
    "## Finish\n"
    "You are a fresh reviewer: you did not write this code. Judge only the diff against the task and its acceptance "
    "list; read the worktree when the diff is truncated or unclear. Do not change any file.\n"
    "You must NOT run tests, builds or other commands that need write access. "
    "Acceptance items about tests passing are satisfied by the gate. "
    "Checks you cannot perform are not grounds for HOLD; mention them in note.\n"
    "End your final message with exactly one JSON object and nothing after it:\n"
    '{"status": "done", "sha": "<the reviewed sha>", "verdict": "LAND@<the reviewed sha>" | "HOLD", '
    '"findings": ["<one concrete problem per entry, with file and line>"], "note": "<=300 chars"}\n'
    "LAND only when every acceptance item is met and nothing in the diff is wrong or unsafe. Otherwise HOLD, and each "
    "finding must say what to change.")


class BriefTooLarge(ValueError):
    pass


def estimate_tokens(text):
    return -(-len(text) // 3)


def pinned_text(path=None):
    return Path(path or PINNED_PATH).read_text(encoding="utf-8")


def pinned_version(path=None):
    return hashlib.sha256(pinned_text(path).encode("utf-8")).hexdigest()[:12]


def _task_block(t):
    acc = "\n".join(f"- {a}" for a in t.get("acceptance") or []) or "- (none given)"
    return (f"## Task {t['id']}: {t['title']}\n"
            f"size {t.get('size', 'M')}, risk {t.get('risk', 'low')}, priority {t.get('priority', 'P2')}, "
            f"repo {t.get('repo') or '-'}, branch {t.get('branch') or '-'}\n\n"
            f"Goal: {t.get('goal') or t['title']}\n\nAcceptance:\n{acc}")


def _scope_block(scope):
    return "## Scope\n" + "\n".join(f"- {s}" for s in scope) if scope else ""


def _last_lines(text, n):
    return "\n".join(text.splitlines()[-n:])


def _section(title, body):
    return f"## {title}\n{body}" if body else ""


def assemble(task, retry_excerpt=None, handoff=None, notes=None, scope=None, pinned=None):
    """-> {'system_append', 'user_prompt', 'tokens', 'budget'}. `scope` entries are 'path' or 'path:a-b'."""
    size = task.get("size", "M")
    budget = BUDGET_TOKENS[size]
    system = pinned if pinned is not None else pinned_text()
    scope = scope if scope is not None else task.get("scope_files") or []
    excerpt = _last_lines(retry_excerpt, EXCERPT_LINES) if retry_excerpt else ""
    notes_text = "\n".join(f"- {n}" for n in notes or [])

    def build(excerpt, notes_text):
        parts = [_task_block(task), _scope_block(scope),
                 _section("Last gate failure (fix this)", f"```\n{excerpt}\n```" if excerpt else ""),
                 _section("Handoff from the previous worker", handoff or ""),
                 _section("Owner notes", notes_text), ACTION_RECORD]
        return "\n\n".join(p for p in parts if p)

    user = build(excerpt, notes_text)
    # lossless-ish trimming, lowest value first: oldest excerpt lines, then owner notes; never pinned/task/handoff
    lines = excerpt.splitlines()
    while estimate_tokens(system + user) > budget and len(lines) > 5:
        lines = lines[len(lines) // 4 + 1:]
        user = build("\n".join(lines), notes_text)
    if estimate_tokens(system + user) > budget and notes_text:
        notes_text = notes_text[:max(0, len(notes_text) - (estimate_tokens(system + user) - budget) * 3)]
        user = build("\n".join(lines), notes_text)
    tokens = estimate_tokens(system + user)
    if tokens > budget:
        raise BriefTooLarge(f"brief for {task['id']} is ~{tokens} tokens; the {size} budget is {budget}")
    return {"system_append": system, "user_prompt": user, "tokens": tokens, "budget": budget}


def assemble_review(task, diff, sha, base, pinned=None, gate=None):
    """The reviewer's brief: pinned block, the task with its acceptance, and `git diff <base>...<sha>` cut to the review
    budget (the tail of an over-long diff is dropped and says so). -> {'system_append','user_prompt','tokens','budget','truncated'}."""
    budget = REVIEW_BUDGET_TOKENS[task.get("size", "M")]
    system = pinned if pinned is not None else pinned_text()
    receipt = (gate or {}).get("receipt") or {}
    summary = [f"{k}: {receipt[k]}" for k in ("kind", "status") if receipt.get(k) is not None]
    duration = (gate or {}).get("duration_s")
    if duration is not None:
        summary.append(f"duration: {duration:.3f} s")
    gate_text = f"Drover's gate already ran the repo's test command on this exact SHA ({sha}) and passed."
    if summary:
        gate_text += "\nGate receipt: " + "; ".join(summary)

    def build(d):
        return "\n\n".join(p for p in (
            _task_block(task), _section("Gate", gate_text),
            f"## Diff under review\n`git diff {base}...{sha}`\n```diff\n{d}\n```", REVIEW_RECORD) if p)
    room = budget - estimate_tokens(system + build(""))
    if room < 200:
        raise BriefTooLarge(f"review brief for {task['id']} leaves no room for a diff in the {budget}-token budget")
    truncated = estimate_tokens(diff) > room
    if truncated:
        keep = diff[:room * 3 - 120]
        keep = keep[:keep.rfind("\n")] if "\n" in keep else keep
        diff = f"{keep}\n[diff truncated: {len(diff.splitlines()) - len(keep.splitlines())} more lines; read the worktree]"
    user = build(diff)
    return {"system_append": system, "user_prompt": user, "tokens": estimate_tokens(system + user), "budget": budget,
            "truncated": truncated}


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="drover.brief", description="Show the pinned block version or a sample brief.")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    info = {"pinned_version": pinned_version(), "pinned_lines": len(pinned_text().splitlines()),
            "budgets": BUDGET_TOKENS}
    print(json.dumps(info) if a.json else "pinned v%(pinned_version)s, %(pinned_lines)d lines, budgets %(budgets)s" % info)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
