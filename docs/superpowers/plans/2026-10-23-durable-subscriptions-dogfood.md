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
| T-ff | Acceptance, final verification and whole-branch review | Acceptance source independently approved at 8361968; whole-branch review next |
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

The phase and issue tasks remain drafts while their work is already owned by the active controller and implementer. Their descriptions/comments carry progress and evidence; drafts prevent a second dispatcher from duplicating the same branch work. They are not fabricated running or landed receipts. Only the separate read-only canary was approved for automatic execution. Existing tasks, dispatch scope/settings, and T-b7 were not changed.

## Actual dogfood execution

Drover admitted **T-fn, run 799** on 2026-10-23 at 22:50 UTC. The run records `codex`, `gpt-6-luna`, `xhigh`, and WSL placement. Its access event records `write=false` and a read checkout pinned to `437645caa5fd9f06649b687e1f6fda5278311f23`. A runner process was recorded. It completed in 63 seconds with outcome `read-only-done` and moved to `needs-owner`. The readiness report missed the three confirmed defects; T-fo records that false-negative. Its findings were rejected and the task closed as cancelled with an explicit review reason, without an automatic paid rerun or owner question. The pipeline delivery portion passed.

This exercises Drover's existing task/worker pipeline. It does not demonstrate that the new supervised director adapter supports live model execution. The canary may report the pinned snapshot while fixes continue on the development branch.

## Verification recorded in the epic

The native Windows integration run at `437645c` completed 1,841 tests in 119 modules (148 jobs) in 955.7 seconds, with 42 skips. It failed with 17 assertion failures and one error across eleven jobs in nine modules. This is baseline-before-fix evidence, not a green final branch gate.

The architecture tests exposed the new director helper naming and Dispatcher constructor seam; both were fixed at bdca38d and passed the native affected-code gate. Inbox count assertions returned 2 versus 1 and 7 versus 4; T-fj records their source-identity diagnosis and public red-to-green regressions at cf04e1d. A separate exported source snapshot at fixed base `95771b9` reproduced all thirteen other failed selectors with matching signatures (twelve assertion failures and one error). These failures predate this branch in the native Windows environment; T-fm retains them as baseline debt rather than silently discarding them. The Windows notification API module ran zero tests and skipped once; no API pass is inferred from it.

Separately, three native Windows real-listener tests and one real HTTP/CLI flow passed. The isolated Ubuntu-to-private-Windows-loopback flow was refused; no listening scope or firewall was widened. Interactive director push and a compatible production executor remain unverified.

Progress comments on E-16, T-fe, T-ff and T-fn retain the observed run identity, failure counts, owners and next steps in Drover itself. Continue adding newly discovered issues there and link their fixes and exact verification commits before closing them.

Runtime fix commit `bdca38d` closed all three Important findings in an independent scoped re-review, with no new findings. The native affected-code gate passed 78 tests in 43.800 seconds, with no skips. Linux covering evidence records 262 passes and 19 skips, plus three real zero-model CI recovery tests, two disk tests, and a final 38-test migration/CI slice. Final owner Inbox compatibility and five missing public cross-component acceptance scenarios passed independent Task 6 review at 8361968, with zero Important findings. The acceptance map contains 76 loader-validated selectors across all fourteen spec scenarios; this is a coverage map, not a claim that all selectors ran again at the final SHA. Linux affected checks passed 140 tests, final API/auth/acceptance checks passed 44, native Inbox checks passed 49, native auth checks passed 18, and the final native HTTP/CLI wire check passed once. Whole-branch review remains pending. Full reports and the acceptance map are retained in T-ff comments 39 and 40; the independent review is also published there.
