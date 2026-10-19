# Task context

Tasks have a short title, a Markdown `description` (`goal` is still accepted and
returned for old clients), and a `kind`: research, investigation, implementation,
fix, review, docs, or mechanical. Existing ledger rows migrate to implementation
with write access. New research, investigation and review tasks default to
`write: false`; the other kinds default to true. Changing kind preserves the
stored access decision. An explicit `write` overrides the creation default.

```sh
python -m drover.ledger add task "Investigate startup" --repo drover \
  --kind investigation --description "Compare the startup paths." \
  --read-access drover@main
```

`--write` / `--no-write`, `--branch`, repeatable `--related` and `--dep` are also
available. The importer and hub bulk import accept the context fields.

The JSON API uses the same fields on POST `/api/v1/tasks` and PATCH
`/api/v1/tasks/{id}`. For example:

```json
{
  "title": "Document startup",
  "kind": "docs",
  "description": "Explain **startup** and its configuration.",
  "branch": "docs/startup",
  "read_access": ["drover@main"],
  "deps": [{"depends_on": "T-1", "kind": "waits-for"}],
  "related": ["T-2"]
}
```

Dependencies may also be plain ids (meaning `blocks`). PATCH replaces the
outgoing dependency list and the symmetric related list when supplied; an empty
list clears it. Unspecified lists are preserved. Missing ids, self-links and
cycles are refused. Connection edits and task edits share a transaction. Related
links have one canonical row and appear from either task.

Read entries are configured repo names, optionally followed by `@branch`.
Omitting `read_access` selects the task's repo at its configured default branch
(`main` when its config is unavailable). Unknown task repos remain visible with
an “unknown repo” warning; explicit read declarations require known configs,
and execution refuses unresolved repos or refs. Existing rows store the repo
name, which resolves to its configured default branch at launch.

Read inputs are independent, shallow, detached checkouts under
`<state>/read-checkouts`, cached by source, branch and commit. The owner checkout
is never switched and its uncommitted files are never exposed in these snapshots.
Files and directories are marked read-only as defense in depth; permissions on
Windows alone do not prevent creating files. Codex's sandbox enforces the write
boundary. Snapshots are listed in the brief and read through their absolute paths;
they are never passed to `--add-dir`, which grants write access. Drover clears
inherited extra writable roots, disables the shared `/tmp` and `$TMPDIR` write
roots, and refuses snapshots overlapping the writable workspace. Write workers
receive a private per-run scratch directory as their only extra writable root,
with `TMPDIR`, `TEMP` and `TMP` set to that directory. Approval escalation is
disabled. Tasks with `write: false` force `-s read-only` and do not create a
task worktree or writable scratch directory. These settings use the documented
[Codex sandbox configuration](https://learn.chatgpt.com/docs/config-file/config-reference).
Before accepting any worker outcome, Drover checks read snapshot
HEAD and status, including staged, untracked and ignored changes. A changed or
unverifiable snapshot blocks the run before any commit or checks run. The launch
manifest is retained in an owner-reserved event for restart verification.
Read-only workers report the existing read checkout HEAD with their findings;
that SHA identifies an input, not a new commit. Completion is marked Waiting on you
for review of the findings. The owner sees **Accept findings** on the page and in
the inbox. Acceptance moves successful read-only work to the existing terminal
`landed` state (shown as merged), satisfying blocking dependencies, and records the owner's decision
and run id. It creates no commit, checks run or merge queue entry. This shortcut requires
the latest run to be a finished `read-only-done` worker run and is owner-only;
failed or unfinished work retains the retry approval behavior. Write tasks keep the existing
commit, checks and merge path.

The Inbox and Plan reuse a task card with a link to `/tasks/{id}`. The task page
supports Card and Page views, owner editing, both dependency directions, related
tasks, full access declarations, runs and the event timeline. Mutations use the
existing UI authentication and same-origin CSRF checks. Automatic refresh compares
edit controls with their loaded values: prefilled fields do not pause updates,
actual edits pause them, and reverting the edits allows updates again.

Branch/worktree state is derived from disk, cached for five seconds per repo.
A background thread runs bounded git queries; a request immediately returns the
last result, or shows “checking disk…” on a cold/error cache. Generated names are
`drover/<id>` and `<configured worktrees>/drover-<id>`. An observed branch or
registered worktree is `exists`; an absent recorded value is `missing`; an absent
generated value is `planned`. Worktree directories must still exist. A task
naming an existing branch can therefore show “existing branch” and “worktree:
planned”.

Markdown supports headings, paragraphs, emphasis, lists, inline and fenced code,
and safe HTTP(S), mail and relative links. Raw HTML is always escaped; script
URLs are refused. No Markdown dependency is required. Long paths and code wrap,
and task field grids stack at phone widths.
