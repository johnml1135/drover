# Durable director work in Drover

Owner requested live dogfooding and issue collection on 2026-10-23.

The live Drover Plan contains epic **E-16: Durable directors: subscriptions, recovery and task joins**. The Windows hub owns these records; they were created through its HTTP API, without direct ledger access.

Implementation is on `design/durable-subscriptions` in `H:/repos/drover.worktrees/wsl-worktrees`. The [spec](../specs/2026-10-23-durable-subscriptions-design.md), [implementation plan](2026-10-23-durable-subscriptions.md), and ADRs 0008–0010 define the work.

| Drover task | Work | State when recorded |
| --- | --- | --- |
| T-fa | Ownership, subscriptions and personal summaries | Implemented and independently reviewed; integration pending |
| T-fb | Credentials, leases and atomic processing | Implemented and independently reviewed; integration pending |
| T-fc | Broadcasts, controls and accepted task joins | Implemented and independently reviewed; integration pending |
| T-fd | HTTP, CLI and owner views | Implemented and reviewed; Inbox regression tracked in T-fj |
| T-fe | Runtime supervision and execution fences | Implemented; fixes bdca38d independently approved; integration pending |
| T-ff | Acceptance, final verification and whole-branch review | Final source cc9b38e; all seven fixes independently approved; integration pending |
| T-fg | Observer errors can replace a still-live director | Fixed at bdca38d; independently reviewed; integration pending |
| T-fh | Lost launch responses overbook shared capacity | Fixed at bdca38d; independently reviewed; integration pending |
| T-fi | STOP/resume loses uncertain CI context/reservation | Fixed at bdca38d; independently reviewed; integration pending |
| T-fj | Owner Inbox notification attention inflates counts | Fixed at cf04e1d; independently reviewed; integration pending |
| T-fk | Production director cap enforcement and credential handoff | Supported executor unavailable; default execution disabled; related T-18 retained |
| T-fl | Supported Windows/WSL API topology | Tested private Windows loopback unreachable from Ubuntu; native Windows bridge works |
| T-fm | Full Windows failure classification | All thirteen remaining failures reproduced at fixed base; baseline debt retained |
| T-fn | Read-only Drover worker canary | Run 799 completed; readiness findings rejected; task closed with reason |
| T-fo | Canary missed confirmed uncertain-effect defects | Review-quality issue captured with exact evidence |
| T-fp | Partial issue assignment crashes on other unassigned partitions | Fixed at cf04e1d; independently reviewed; integration pending |
| T-fq | Worker bearer can change director execution settings | Fixed at 8361968; independently reviewed; integration pending |
| T-fr | Existing epic plan cannot be edited through hub API | Live OpenAPI confirms missing edit route; follow-up captured |
| T-fs | Legal large digest crashes replay and SSE | Final review I1 fixed at cc9b38e; scoped re-review approved; integration pending |
| T-ft | Owner manual push bypasses STOP | Final review I2 fixed at cc9b38e; scoped re-review approved; integration pending |
| T-fu | Ordinary claim/launch bypasses role storage hold | Final review I3 fixed at cc9b38e; scoped re-review approved; integration pending |
| T-fv | Existing unassigned issue stays muted after assignment | Final review I4 fixed at cc9b38e; scoped re-review approved; integration pending |
| T-fw | Worker lifecycle text forges accepted-landing notification | Final review I5 fixed at cc9b38e; scoped re-review approved; integration pending |
| T-fx | Migration identity test no longer starts at V10 | Final review M1 fixed at cc9b38e; scoped re-review approved; integration pending |
| T-fy | New auth fixture adds needless probe diagnostics | Final review M2 fixed at cc9b38e; scoped re-review approved; integration pending |

The phase and issue tasks remain drafts while their work is already owned by the active controller and implementer. Their descriptions/comments carry progress and evidence; drafts prevent a second dispatcher from duplicating the same branch work. They are not fabricated running or landed receipts. Only the separate read-only canary was approved for automatic execution. Existing tasks, dispatch scope/settings, and T-b7 were not changed.

## Actual dogfood execution

Drover admitted **T-fn, run 799** on 2026-10-23 at 22:50 UTC. The run records `codex`, `gpt-6-luna`, `xhigh`, and WSL placement. Its access event records `write=false` and a read checkout pinned to `437645caa5fd9f06649b687e1f6fda5278311f23`. A runner process was recorded. It completed in 63 seconds with outcome `read-only-done` and moved to `needs-owner`. The readiness report missed the three confirmed defects; T-fo records that false-negative. Its findings were rejected and the task closed as cancelled with an explicit review reason, without an automatic paid rerun or owner question. The pipeline delivery portion passed.

This exercises Drover's existing task/worker pipeline. It does not demonstrate that the new supervised director adapter supports live model execution. The canary may report the pinned snapshot while fixes continue on the development branch.

## Verification recorded in the epic

The native Windows integration run at `437645c` completed 1,841 tests in 119 modules (148 jobs) in 955.7 seconds, with 42 skips. It failed with 17 assertion failures and one error across eleven jobs in nine modules. This is baseline-before-fix evidence, not a green final branch gate.

The architecture tests exposed the new director helper naming and Dispatcher constructor seam; both were fixed at bdca38d and passed the native affected-code gate. Inbox count assertions returned 2 versus 1 and 7 versus 4; T-fj records their source-identity diagnosis and public red-to-green regressions at cf04e1d. A separate exported source snapshot at fixed base `95771b9` reproduced all thirteen other failed selectors with matching signatures (twelve assertion failures and one error). These failures predate this branch in the native Windows environment; T-fm retains them as baseline debt rather than silently discarding them. The Windows notification API module ran zero tests and skipped once; no API pass is inferred from it.

Separately, three native Windows real-listener tests and one real HTTP/CLI flow passed. The isolated Ubuntu-to-private-Windows-loopback flow was refused; no listening scope or firewall was widened. Interactive director push and a compatible production executor remain unverified.

Progress comments on E-16, T-fe, T-ff and T-fn retain the observed run identity, failure counts, owners and next steps in Drover itself. Continue adding newly discovered issues there and link their fixes and exact verification commits before closing them.

Runtime fix commit `bdca38d` closed all three Important findings in an independent scoped re-review, with no new findings. The native affected-code gate passed 78 tests in 43.800 seconds, with no skips. Linux covering evidence records 262 passes and 19 skips, plus three real zero-model CI recovery tests, two disk tests, and a final 38-test migration/CI slice. Final owner Inbox compatibility and five missing public cross-component acceptance scenarios passed independent Task 6 review at 8361968, with zero Important findings. The acceptance map contains 76 loader-validated selectors across all fourteen spec scenarios; this is a coverage map, not a claim that all selectors ran again at the final SHA. Linux affected checks passed 140 tests, final API/auth/acceptance checks passed 44, native Inbox checks passed 49, native auth checks passed 18, and the final native HTTP/CLI wire check passed once. The fresh whole-branch review of all seventeen commits from `95771b9..dd8005b` found five Important and two Minor defects; T-fs through T-fy retain each finding and its regression requirements. The one final fix wave, `cc9b38e`, addressed all seven findings. Its single scoped independent re-review approved the spec and quality, with zero new Critical, Important or Minor issues. Full review evidence is retained in T-ff comment 43. Full reports and the acceptance map are retained in T-ff comments 39 and 40; the independent review is also published there.

Final affected checks at `cc9b38e`: Linux ran 332 tests (331 passed, one existing integration-tier skip) in 87.452 seconds; native Windows passed 121 in 21.168 seconds; final-source native real HTTP/CLI passed once in 3.528 seconds. Full fix and scoped review reports are retained in T-ff comments 45 and 46; E-16 comment 47 preserves the final handoff and all seventeen controller rulings. The [handoff](../reviews/2026-10-23-durable-subscriptions-handoff.md) preserves the verification limits and all controller rulings. The branch is implemented and reviewed; integration and deployment remain separate.

## Overnight continuation, 2026-10-23

The owner subsequently authorized continued overnight implementation, integration, deployment and real dogfooding, with low model cost and push delivery preferred over repeated polling. This extends the earlier one-canary execution boundary. Existing unrelated main-checkout notes and active workers remain protected; repository Git push remains disabled.

Integration repairs are isolated on `fix/durable-integration`. The initial fresh Windows gate ran 1,862 tests and reproduced exactly thirteen known baseline failures, with no additional failures. Repair `c51a70a` corrected native worktree discovery and eliminated PowerShell from healthy cleanup identity reads, while repairing host-simulation fixtures. Both platforms passed the affected 304 tests with existing platform skips. Independent standards review found no documented violation; a possible placement Feature Envy refactor is deferred. Spec review found a reachable foreign-path fallback when WSL inventory is unavailable. Integration remains blocked until that regression is fixed and the corrected exact commit passes both platform gates. Earlier gate evidence must not certify a later commit.

The subsequent full Windows gate at `c51a70a` ran 1,871 tests with 43 existing skips and exposed one transient `PermissionError` in the rotation fixture's raw stop-file reader. The final repair uses the existing safe reader and preserves the rotation and nonce assertions, including one injected transient read error. The WSL gate was aborted after 124 jobs (1,716 tests and nine skips) because seven ASGI modules stalled under the network-restricted sandbox; one health selector timed out after 12 seconds inside it and passed in 0.057 seconds outside it. Its buffered disk-safety failure had no recoverable traceback; the isolated unchanged module passed all ten tests in 1.258 seconds outside the sandbox. These failed and aborted receipts are diagnostic evidence. Final WSL checks require the environment that supports the ASGI tests, and neither receipt authorizes deployment.

Deployment must use the clean pinned worktree and exact passing receipts. Stop only the validated hub listener, preserve detached runners, create a stopped-hub DB/WAL backup, rehearse migrations on a separate native-Windows copy, preserve existing durable/replay state, then move the pin and restart the task. Confirm new listener identity, health and notification controls. The production director executor remains disabled while its required capabilities are unsupported.

Efficiency work stays in E-16 with explicit acceptance criteria:

1. Wake dispatcher and landing from worker completion and committed eligibility/control changes. Coalesce hints separately from STOP, subscribe/register without lost wakes, and let the ledger decide eligibility. Preserve bounded recovery and adoption checks. Measure terminal-to-reap and reviewed-to-landing latency, idle ticks and builds per accepted SHA.
2. Keep durable notification receive open, using post-commit bus hints to replay from durable cursors. Subscribe before reading, wait outside transactions, revalidate session/ownership, and retain recovery on disconnect, overflow and restart. A transport delivery never acknowledges processing. Measure empty reads, reconnects and commit-to-offer latency.
3. Keep routine progress in deterministic summaries with digest wakes off. Model continuations consume bounded new decision evidence plus checkpoints; full reports and receipts remain available by reference. Track input, cached input and output separately, prompt bytes, wakes per decision and repeat builds. OS waits use zero model tokens; faster scheduling alone is not a token-saving claim.

Use one bounded real Drover task at a time with a narrow brief, named acceptance checks, no helper agents, and a short evidence report. Collect failures as issues immediately; reconcile an uncertain timed-out write before retrying. Initial efficiency-task writes and readbacks timed out during concurrent gates. A later supported readback found no matching task; retries created four drafts without duplication: T-fz for completion wakes, T-g0 for durable inbox receive, T-g1 for compact decision metrics, and T-g2 for task-creation timeouts under global Git load. Canary 799's failed readiness judgement remains a quality issue, not acceptance evidence. Do not manufacture task/run/landing receipts for work performed outside Drover.

External gate orchestration now blocks in a subprocess wait, retains an exact-source receipt locally, and attempts one supported `dogfood.gate-completed` publication. A failed publication leaves local evidence for reconciliation. No model is invoked for gate progress.

## Finite execution contract and live result, 2026-10-24

The owner clarified the default: plan the work, let Drover orchestrate finite builders, gates, dependencies and completion signals, briefly invoke Sol or Opus only for an unresolved decision, then report the outcome. Persistent interactive task agents are explicit exceptions. Controllers must not repeatedly query agents or coordinate them through side channels. T-g1 carries the operating-rule, role-configuration and durable-checkpoint work; this records the requirement, not completed runtime enforcement. The production director executor remains disabled while unsupported.

T-g2/run800 completed a genuine read-only Luna xhigh investigation at `736f05e`. Its four commands measured eager repository-policy loading (`empty=[]; calls_after_empty=1; known=['p@topic/x']; total_calls=2`) without claiming the underlying transient Git delay was proven. Drover pushed run completion and needs-owner; the controller evaluated the report once and accepted the findings through the owner status API. T-g2 is landed as accepted read-only work, with no code commit or CI receipt invented. Actual usage: 134 seconds, 133,048 input tokens including 108,544 cached, and 10,483 output tokens. T-g6 tracks implementation. Task model pins currently accept only backend/model, so explicit per-task reasoning effort remains a verified configuration gap in T-g1.

Dogfood issues: T-g3 records Windows' misleading CPU-derived load label and delayed admission recovery; T-g4 records double-encoded ledger SSE payloads; T-g5 records headless writer approval-policy overrides. The event consumer currently normalizes legacy SSE strings, waits on hints and rereads durable facts. Bus hints never certify acceptance or processing.

At `736f05e`, the full WSL gate passed 1,873 tests with 13 skips in 274.2 seconds. The full Windows gate ran 1,873 tests with 43 skips in 905.2 seconds and failed only in unchanged `test_runner.test_stop_request`: a transient status-file PermissionError caused a secondary file-in-use cleanup error. One unchanged-module retry passed 46 tests with eight skips in 27.5 seconds. Automatic approval review rejected publishing that composed evidence as a passing full gate; the original failed receipt was restored, no replacement was published, and neither main nor deployment moved. A deterministic injected read reproduced both original errors. The bounded test observer now retries only missing/sharing/partial-JSON observations within the existing deadline; persistent denial still times out and unrelated errors propagate. The real stop-request regression passes on both platforms. Fresh full gates are required for this subsequent source commit.
