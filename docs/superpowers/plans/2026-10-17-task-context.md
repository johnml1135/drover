# Task Context Implementation Plan

> **For agentic workers:** Execute inline in the assigned T-7 worktree. Drover owns commits and landing.

**Goal:** Give owners and workers complete task context, safe access declarations, and compact/full task views.

**Architecture:** Append a ledger migration; centralize task-kind/access validation and transactional connections in ledger operations. Add a cached background disk-state reader and a deliberately small HTML-escaping Markdown renderer for the hub. Extend the existing launch seam only to enforce declared access.

**Tech Stack:** Python stdlib, SQLite, FastAPI/Pydantic, Jinja2, existing htmx.

**Spec:** User's T-7 task and acceptance list in this session.

## Global Constraints

- Edit only drover, hub, tests, docs; no dependencies, services, commits, pushes, landing, or seats/stats changes.
- Existing rows remain implementation/write tasks. `goal` remains the storage field and API alias; `description` is the preferred API/UI name.
- Research/investigation/review default to read-only; explicit access overrides persist across kind edits.
- Preserve authentication and CSRF; all owner UI mutations use the existing guarded router.
- Minimal worker integration is required for access enforcement; scheduling, routing, gating, and landing policies stay intact.

## Review Focus

- Failed connection replacement must roll back scalar edits and previous links.
- Unknown repos remain visible; explicit read entries must refer to configured repos.
- Raw HTML, unsafe links, long code/paths and unbroken text remain safe and fit at phone width.
- Existing branch lookup must not block HTML requests or run one git command per card.
- Read-only runs reject any changed read checkout, including untracked files and HEAD changes.

### Task 1: Ledger and input contracts

- [x] Append V4 kind/write/read_access migration and symmetric task_links table.
- [x] Extend task create/update, default access by kind, validate read declarations and transactional dependency/related replacement.
- [x] Expose description/goal aliases, branch, kind, write/read_access and connections in API/importers/CLI.
- [x] Test old-schema migration, defaults, constraints, aliases, link validation, rollback and CLI/import behavior.

### Task 2: Worker access

- [x] Include kind, write/read-only and read declarations in every worker brief.
- [x] Prepare isolated detached read checkouts from configured refs, never a non-default branch in the owner's checkout.
- [x] Supply read directories, force Codex read-only sandbox for read tasks, avoid task worktree creation, and block modifications before any commit/gate.
- [x] Test argv, path mapping, read checkout selection, brief and modification refusal.

### Task 3: Task views

- [x] Derive repo/branch/worktree context using a short background cache with bounded git calls.
- [x] Add escaping Markdown renderer, shared card partial, full page and card/page toggle, owner edit form.
- [x] Wire inbox/plan context, both dependency directions, symmetric related links, runs and events.
- [x] Test derived states, both views, partials, Markdown safety, auth/CSRF and narrow-screen styling.

### Task 4: Verification and handoff

- [x] Run the WSL stdlib suite; leave uv hub and Windows suites to Drover's gate as directed in the handoff.
- [x] Review diff and scope, document limits and results; leave ready_to_commit with a concrete commit message.

## Execution notes

- Worktree verified: drover/T-7, clean baseline at task start.
- User supplied an implementation task and explicitly authorized completion; no additional design approval needed.


## Verification and review (T-7 worker)

- WSL: `python3 -m unittest discover -s tests -t .` — 808 tests, OK, 14 skips.
- Hub through existing uv environment, `--no-sync`, with the temporary pipe-wakeup
  adapter — 921 tests, OK, 12 skips. Three skips are the real socket-listener
  integration tests: this task sandbox denies all socket creation. The other
  skips are the suite's existing platform/environment skips.
- Focused context/view/access run — 25 tests, OK. Restart-manifest regression
  added afterward and run with the access suite (7 tests, OK).
- `python3 -m compileall -q drover hub tests` and `git diff --check` passed.
- Windows Python could not start: WSL interop reports
  `UtilBindVsockAnyPort: socket failed 1`. Windows suite remains unverified.
- The available Chromium headless executable cannot start because `libnspr4.so`
  is absent. Phone wrapping is implemented and view HTML tested; actual 375px
  browser geometry still needs the host check. No dependency was installed.
- Self-review covered scope, SQL migration/rollback, Markdown escaping, cache
  nonblocking behavior, access manifests and owner/CSRF boundaries. No seat/stats,
  routing, gate policy or landing changes. Worker lifecycle changes only enforce
  access and route read-only findings to owner review.
- Ruling: unknown task repos may be stored for visibility, but explicit read
  declarations and execution require configured repos; existing records resolve
  their repo default branch at launch.
- Ruling: read-only completion uses needs-owner for findings review, since the
  existing task states have no terminal state for a result without a commit.
- Read snapshot fetches are shallow and independent (no absolute git alternates),
  allowing the worker to read the same snapshot from Windows or WSL.
- No git add, commit, push, merge or landing performed. Drover must commit and run
  the host gates against the resulting SHA before marking READY.

Suggested commit subject: Tasks carry context, access declarations and connected card and page views

Suggested commit body: Add the kind/access migration and shared validation across
API, importer and CLI; enforce isolated read checkouts and Codex read-only runs.
Render safe Markdown task pages and shared cards with cached disk state,
connections, owner editing, run outcomes and event history. Cover migration,
contracts, access enforcement, restart manifests and task views.

- Final review fix: read-only briefs now request findings and the existing read
  checkout HEAD, with no commit instructions. Regression observed RED, then GREEN;
  worker/launch/dispatcher/pipeline verification: 181 tests, OK, one platform skip.
- Handoff state: implementation is uncommitted and scoped correctly; completion
  is blocked on the Windows, real-listener and browser host checks above. This
  worker does not claim READY or a passing receipt for an uncommitted SHA.

## Continuation review and verification

- Reviewed the existing implementation against T-7's acceptance criteria,
  including migration defaults and constraints, input aliases and validation,
  atomic connection replacement, symmetric related links, access enforcement
  and restart manifests, cached disk state, Markdown escaping, both task views,
  owner authentication and CSRF. No additional code fixes were needed.
- Fresh WSL command: `python3 -m unittest discover -s tests -t .` — 809 tests
  in 50.005 seconds, OK, 14 skips (exit 0).
- Fresh `python3 -m compileall -q drover hub tests` and `git diff --check`
  passed. All changes remain within drover, hub, tests and docs.
- Per the current handoff, uv hub, Windows and real-listener suites belong to
  Drover's gate, and the 375px browser check belongs to the reviewer or owner.
  These were not rerun in this continuation and are not worker blockers.
- This supersedes the earlier blocked handoff: status is ready_to_commit,
  with changes left uncommitted for Drover. No exact-SHA gate receipt or READY
  claim is made. No git add, commit, push, merge or landing was performed.

## Gate HOLD corrections

- Removed read snapshots from Codex `--add-dir`: that flag grants write access.
  Launch now clears inherited writable roots, excludes shared temp roots, disables
  approval escalation and rejects overlapping read/write paths, including Windows
  casing and symlinks. Write workers get one private per-run scratch root and temp
  environment; read-only workers get no writable scratch. Chmod remains defense
  in depth, with the Codex sandbox providing the access boundary.
- Added owner acceptance of successful read-only findings through the existing
  guarded Approve action, labelled **Accept findings** in the inbox and task page.
  The latest run must be a finished `read-only-done` worker run. Acceptance records
  the owner and run, moves to terminal `landed`, and unblocks dependencies without
  committing, gating or adding landing metadata. Write tasks, failed runs and open
  runs cannot use this transition. This supersedes the earlier lifecycle ruling
  that stopped at needs-owner.
- Refresh now compares text, select and check controls to their loaded defaults.
  Prefilled task fields allow timer/SSE refresh; edits pause it and reverting edits
  resumes it. The shipped script is exercised with a small DOM fixture using an
  available system JavaScript runtime (Duktape on this worker; no install).
- Observed RED regressions for all three findings before fixes. Final WSL suite:
  `python3 -m unittest discover -s tests -t .` — 819 tests in 47.530 seconds,
  OK, 14 skips. The strengthened acceptance regression also passed afterward.
  `python3 -m compileall -q drover hub tests` and `git diff --check` passed.
- Added HTTP coverage for acceptance labels and owner/CSRF enforcement; hub tests
  remain skipped without hub dependencies here. The uv hub, Windows and actual
  sandbox/platform checks belong to Drover's gate; the phone browser check belongs
  to the reviewer/owner. No exact-SHA gate receipt is claimed.
- Changes remain uncommitted in drover/T-7 and within drover, hub, tests and docs.
  Status: ready_to_commit. No git add, commit, push, merge or landing performed.
