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
    "The Drover gate runs the full WSL and Windows suites at your exact SHA afterwards: run targeted tests only, then "
    "report ready_to_commit (or done) once the work is done. A sandbox limit (hung process check, restore, network, "
    "writing outside the root, no Windows Python, local sockets, headless browsers) is not a blocker; list it in note. "
    "The approved task, goal and acceptance are "
    "the approved design: do not wait on skill approval gates. Block only if the design contradicts the acceptance or "
    "needs an owner decision the task does not settle, and write that as a plain question.\n"
    "Use blocked only when the plan, not your code or the sandbox, stops you.")


READ_ONLY_RECORD = (
    "## Finish\n"
    "Report your findings, then end your final message with exactly one JSON object and nothing after it:\n"
    '{"status": "done" | "blocked", "sha": "<read checkout HEAD for done, otherwise empty>", '
    '"commit_message": "", "blocker": {"kind": "<short kind>", "detail": "<what is wrong>"} or null, '
    '"note": "<=300 chars"}\n'
    "This task is read-only. Do not edit files or create a commit. For done, report the existing read checkout HEAD "
    "from git rev-parse HEAD. Drover checks for changes and sends the findings to the owner.")


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
    '"findings": [{"kind":"blocking" | "follow-up", "detail":"<concrete problem, file and line, what to change>"}], '
    '"note": "<=300 chars"}\n'
    "Judge against the task text, including its Out of scope section. Blocking findings are defects introduced by "
    "this diff against this task. Gaps or improvements beyond the task are follow-up findings; they become proposed "
    "related tasks. Only blocking findings HOLD. With only follow-ups, return LAND@<the reviewed sha>.")


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
            f"Type: {t.get('kind', 'implementation')}\n"
            f"Access: {'write (own worktree; finish ready_to_commit)' if t.get('write', True) else 'read-only (no edits or commits; finish done)'}\n"
            f"Read access: {', '.join(t.get('read_access') or [t.get('repo') or 'unknown'])}\n"
            f"Read directories: {', '.join(t.get('read_dirs') or []) or '(resolved at launch)'}\n"
            f"size {t.get('size', 'M')}, risk {t.get('risk', 'low')}, priority {t.get('priority', 'P2')}, "
            f"repo {t.get('repo') or '-'}, branch {t.get('branch') or '-'}\n\n"
            f"Description: {t.get('description') or t.get('goal') or t['title']}\n\nAcceptance:\n{acc}")


def _scope_block(scope):
    return "## Scope\n" + "\n".join(f"- {s}" for s in scope) if scope else ""


def _last_lines(text, n):
    return "\n".join(text.splitlines()[-n:])


def _section(title, body):
    return f"## {title}\n{body}" if body else ""


def framework_text(root):
    from . import placement
    if not root:
        return ''
    paths = ('openspec/', '.specify/', 'docs/superpowers/')
    if placement.routes_through_wsl(root):
        # A trailing slash lists children; -d then hides directories containing only files.
        result = placement.git(root, 'ls-tree', '-d', '--name-only', 'HEAD', '--',
                               *(p.rstrip('/') for p in paths), timeout=10)
        found = [p for p in paths if any(name == p.rstrip('/') or name.startswith(p)
                 for name in result.stdout.splitlines())] if not result.returncode else []
    else:
        found = [p for p in paths if (Path(placement.native(root)) / p).is_dir()]
    names = {'openspec/': 'OpenSpec', '.specify/': 'Spec Kit', 'docs/superpowers/': 'superpowers'}
    return ('Framework paths: ' + ', '.join(f'{names[p]} ({p})' for p in found) + '. Use these existing paths. ' if found else
            'No spec framework detected. ') + (
            "Do not create a different framework's artifacts. The task description is the approved design; "
            "never wait for design or plan approval.")


def assemble(task, retry_excerpt=None, handoff=None, notes=None, scope=None, pinned=None):
    """-> {'system_append', 'user_prompt', 'tokens', 'budget'}. `scope` entries are 'path' or 'path:a-b'."""
    size = task.get("size", "M")
    budget = BUDGET_TOKENS[size]
    system = pinned if pinned is not None else pinned_text()
    scope = scope if scope is not None else task.get("scope_files") or []
    excerpt = _last_lines(retry_excerpt, EXCERPT_LINES) if retry_excerpt else ""
    notes_text = "\n".join(f"- {n}" for n in notes or [])
    framework = framework_text(task.get('worktree'))

    def build(excerpt, notes_text):
        parts = [_task_block(task), _scope_block(scope), _section("Repo instructions", task.get("repo_brief", "")),
                 _section("Repository spec framework", framework),
                 _section("Earlier runs of this task", task.get("retry_work", "")),
                 _section("Last gate failure (fix this)", f"```\n{excerpt}\n```" if excerpt else ""),
                 _section("Handoff from the previous worker", handoff or ""),
                 _section("Owner notes", notes_text), READ_ONLY_RECORD if task.get("write") is False else ACTION_RECORD]
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



def assemble_replans(items, pinned=None) -> dict:
    """One bounded read-only brief; task authority and required context are never trimmed."""
    system = pinned if pinned is not None else pinned_text()
    budget = BUDGET_TOKENS['M']
    shared, references = [], {}
    sections, history = [], []
    for item in items:
        inputs = dict(item['inputs'])
        refs = []
        for checkout in inputs.pop('read_checkouts', []):
            key = (checkout['path'], checkout['sha'])
            if key not in references:
                references[key] = len(shared)
                shared.append(checkout)
            refs.append(references[key])
        sections.append('\n\n'.join((
            _task_block({**item['task'], 'write': False, 'description': item['task'].get('goal')}), _scope_block(item['task'].get('scope_files')),
            json.dumps({**inputs, 'read_checkouts': refs}, ensure_ascii=True),
            _section('Requesting blocker', json.dumps(item.get('trigger'))))))
        runs = [{'id': r['id'], 'outcome': r.get('outcome'), 'note': (r.get('note') or '')[:NOTE_MAX],
                 'gate': _last_lines(str(r.get('gate_receipt') or ''), EXCERPT_LINES)} for r in item['runs'][-20:]]
        notes = [str(n)[:500] for n in item['notes'][-20:]]
        history.append([runs, notes])
    finish = ('Read-only planner: do not edit files, commit, or run commands needing write access. '
              'Return each task independently. Allowed task results: '
              '{"action":"revise","goal":"...","acceptance":["..."],"scope_files":["..."]}, '
              '{"action":"split","tasks":[{"key":"a","title":"...","goal":"...",'
              '"acceptance":["..."],"scope_files":["..."],"deps":[]}, ...]}, or '
              '{"action":"needs-owner","question":"..."}. '
              'Split into 2..8 children; dependencies name local keys within that task only. '
              'Preserve required acceptance and scope. End with exactly one final JSON object, '
              'with no trailing text, at most 32768 UTF-8 bytes. ')
    finish += ('Use the flat task result.' if len(items) == 1 else
               'Use {"results":[{"task_id":"T-...","result":{allowed task result}}, ...]}. '
               'Only the listed task IDs grant authority; no cross-task edits.')
    mandatory = '\n\n'.join([*sections, _section('Shared immutable read checkouts', json.dumps(shared)), finish])
    if estimate_tokens(system + mandatory) > budget:
        raise BriefTooLarge('mandatory replan context exceeds brief budget')
    def build():
        return mandatory + '\n\n' + '\n\n'.join(
            _section('Recent runs and owner notes for ' + item['task']['id'], json.dumps(h))
            for item, h in zip(items, history))
    user = build()
    # Reduce membership before discarding optional history; a singleton keeps the old trimming policy.
    if len(items) > 1 and estimate_tokens(system + user) > budget:
        raise BriefTooLarge('complete batch exceeds brief budget')
    while estimate_tokens(system + user) > budget and any(a or b for a, b in history):
        for runs, notes in history:
            if runs or notes:
                (runs if runs else notes).pop(0)
        user = build()
    return dict(system_append=system, user_prompt=user, tokens=estimate_tokens(system + user), budget=budget)
