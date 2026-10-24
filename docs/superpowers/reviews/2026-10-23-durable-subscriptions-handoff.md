# Durable subscriptions implementation handoff

Branch: `design/durable-subscriptions`, worktree `H:/repos/drover.worktrees/wsl-worktrees`. Fixed review base: `95771b98507d505dbe97055a97dfa59d2f5a0af3`. Final source: `cc9b38eed79b5983105e33bb642dd69993fdb52e`. Final scoped review: **APPROVED, seven of seven findings addressed, zero new findings**. Integration and deployment remain separate. The [design](../specs/2026-10-23-durable-subscriptions-design.md), [plan](../plans/2026-10-23-durable-subscriptions.md) and [Drover tracking](../plans/2026-10-23-durable-subscriptions-dogfood.md) retain the contracts and live task mapping.

The implemented ledger supports exclusive ordinary-event ownership for ten independent directors, personal summaries, scoped sessions and processing receipts, shared durable actions, sealed accepted-result joins, broadcasts and STOP/skill controls. Dispatcher supervision retains uncertain launch identity and capacity, permits bounded verified replacement, and preserves ordinary worker lifecycle. Owner views, HTTP, CLI and replay expose these contracts.

Automatic director execution defaults off. The bundled production adapter reports unsupported until an executor can enforce caps and private authenticated credential processing. A real controlled subprocess proves checkpoint/unread recovery through authenticated ASGI, not paid-model execution or interactive push/resume.

## Work and issues in Drover

The live Plan contains epic **E-16** with twenty-five phase/issue records. T-ff owns final acceptance and review. T-fg/T-fh/T-fi record the runtime fixes; T-fj/T-fp/T-fq record Inbox, partial ownership and settings authority fixes. Their evidence is committed and independently task-reviewed; branch integration is pending. They remain draft tracking records because this controller already owns their external implementation; they are not fabricated running/landed receipts or duplicate automatic work.

Open follow-ups: T-fk compatible production executor (related existing T-18), T-fl supported Windows/WSL notification transport, T-fm pre-existing native Windows failures, T-fo review-canary false-negative, T-fr missing epic-plan edit API. Existing external-agent tracking gap T-c7 is linked from runtime work.

Real dogfood **T-fn/run799** used Luna xhigh in WSL, read-only at `437645c`, and completed in 63 seconds. Admission, checkout, runner and result delivery worked. Its report missed three confirmed defects; findings were rejected, T-fo retained the evidence, and the canary closed as cancelled with a reason and no automatic rerun. The existing worker pipeline was exercised; no new production director executor was claimed. T-b7 and global dispatch settings were not changed.

## Verification evidence

| Check | Result and scope |
| --- | --- |
| Native full integration at `437645c` | 1,841 tests, 42 skips, 119 modules/148 jobs, 955.7 seconds; 17 assertion failures + one error. Not a passing final full suite. |
| Fixed-base comparison at `95771b9` | All thirteen other failed selectors reproduced: twelve assertion failures + one error. Full comparison retained in T-fm comment35. Introduced architecture/Inbox failures were fixed separately. |
| Native runtime affected at `bdca38d` | 78 passed, no skips, 43.800 seconds. Independent rereview closed all three Important findings; T-fe comment38 preserves reports. |
| Linux acceptance affected | 140 passed; final auth/API/acceptance 44 passed at source `8361968`. |
| Native Inbox/auth at final source | Inbox 49 passed; auth 18 passed. |
| Native real HTTP/CLI at `8361968` | One passed in 3.135 seconds; isolated Windows-owned loopback listener and disposable database. Three earlier real-listener cases also passed. |
| Acceptance map | 76 loader-validated selectors cover all fourteen spec scenarios and named review races; this does not imply all selectors were rerun at the latest SHA. T-ff comment40 retains the complete map. |
| Final fix Linux/native/wire | At `cc9b38e`: 332 Linux tests (331 passed, one existing integration-tier skip), 121 native passes, one native actual HTTP/CLI pass. Scoped review approved seven of seven fixes, zero new findings; no final full-suite claim. |
| Independent Task6 source review | Approved; zero Critical/Important, one Minor fixture diagnostic noise. T-ff comment41 retains the review. |

T-ff comments36/39 retain the full-suite and final acceptance reports, with commands and output. Native Windows lacks plain `httpx`, so its notification API module ran zero tests/one skip; API/ASGI coverage used the existing Linux hub environment. Actual Ubuntu CLI access to the isolated Windows loopback listener was refused twice before mutation. No bind/firewall widening or dependency installation was used. Cross-WSL notification HTTP remains an unverified deployment gate.

## Permission defaults side task

The separate Luna review aligned ten existing Codex/Claude Windows/WSL configuration files and the future Herdr home generators. Codex uses on-request plus automatic approval review; Claude uses auto mode. Existing workspace/network profiles, allow/ask/deny rules and the restricted warden carveout remain. Generator commit: `8507f39`; 26 focused tests and independent parsed ten-file preservation checks passed. Already-running sessions were not restarted and may retain startup settings.

## Final whole-branch gate

Fresh single whole-branch review of `95771b9..dd8005b` found zero Critical, five Important and two Minor issues. Full report is retained in T-ff comment43; T-fs through T-fy track each finding. One complete fix wave is committed at `cc9b38e`; the single scoped independent rereview approved all seven findings with zero new Critical, Important or Minor issues. T-ff comment44 records the source and observed checks; comments45 and46 retain the full fix and scoped review reports. E-16 comment47 preserves this handoff and all seventeen rulings. Final affected Linux checks ran 332 tests (331 passed, one existing integration-tier skip) in 87.452 seconds. Native Windows checks passed 121 tests in 21.168 seconds; the final-source native real HTTP/CLI check passed once in 3.528 seconds. No product/test source changed after the affected checks. No deployment, merge or push is part of this handoff.

# Rulings I made

1. skill helper scripts are CRLF on mounted C:; run normalized copies in /tmp/drover-sdd-scripts, preserving source skills. If wrong: helper-script invocation needs correction; product code and source skills are unchanged.

2. semantic result-accepted/group-ready events must be reserved from legacy worker event API in Task4, and joins must validate actual accepted evidence in Task3; classifier itself is trusted-service taxonomy, not proof of acceptance. If wrong: a legacy worker client must move a reserved event submission to the authorized service path.

3. subscription precedence is task > epic > repo > owned-all, class override > explicit mode > profile, equal-scope mute > immediate > digest; matching filters union before precedence. Task3 extends group/descendants consistently. If wrong: subscription preferences and their expected results need a migration or precedence revision.

4. compacted digests archive full original body as internal audit event and durable resync coverage; Task2/4 acknowledge of compacted identity must explicitly resync, never acknowledge a successor. If wrong: audit storage or resynchronization semantics need adjustment; compacted identities cannot acknowledge successors.

5. acquire/control acknowledgement and actionless checkpoint/ack remain possible during storage/pause/control holds; ordinary invocation/new actions require admission and durable wake reservation. Durable accepted outbox intent survives release of execution lease, but not generation/ownership/issue authority loss. Task3 must avoid skill-ack/admission deadlock. If wrong: held-session/lease handling needs refinement while retaining the ban on new effects during holds.

6. Task3 supports synthesis groups only and explicitly rejects collection/unknown modes until a declared accepted collection lifecycle exists; parent acceptance remains manual under existing lifecycle. Spec permits collection completion only under such lifecycle and excludes unrelated registry. Cost if wrong: later lifecycle needs narrow adapter.

7. broadcasts use one source event plus retained per-role target/application records, integrated into existing offers/digests/processing/replay/wakes through shared recipient authority; ordinary V9 intent uniqueness remains. These are specified broadcast records, not a second outbox/scheduler. Cost if wrong: target reconciliation may need adapter adjustment.

8. linked synthesis parents remain unclaimable until all required linked groups are ready, using existing ready_tasks filtering while preserving status/approval/dependency/wait semantics. This implements specified group-to-parent progression without a new wait kind/scheduler. Cost if wrong: parent eligibility may need refinement; Task4 must expose group-held reason.

9. new accepted action authority pins current STOP control_epoch; execution requires that epoch. Legacy missing epoch is0. STOP/resume holds stale accepted intents until fresh authorized processing, preventing silent revival. Cost if wrong: callers must submit a fresh intent after controls; Task5 external barriers remain separate.

10. acquire records/returns current control epoch in director_leases; new actions require lease epoch=current epoch. Old leases retain actionless checkpoint/release and control ack, so control holds do not deadlock. Cost if wrong: clients must release/reacquire after epoch changes. Prevents stale leases minting current-epoch intents without observing controls.

11. failed child alone is retryable/waiting, with retained failure_reason and no new group wake. Cancelled/deleted/needs-owner or explicit recovery.refused/exhausted produces one coalesced group issue; recovered inputs clear obsolete issue. Reuse existing dispatcher/replan policy rather than duplicate retry evaluator. Cost if wrong: a silent failed child without explicit exhaustion signal stays waiting; health exposes reason.

12. Task4 may extend ledger ops target validation/event addressing for director/group fallback questions and comments — existing task/epic-only addressing makes owner answers fail despite Tasks2/3 creating these fallbacks — cost if wrong: narrow target hooks may need refinement. Free-text answers do not reset budgets, accept groups or resume controls; retain existing task/epic behavior and avoid fake task/epic foreign keys.

13. Task5 may add narrow optional ledger-aware guards in drover/ci.py and drover/origin.py beyond initial file list — actual group CI launcher/stop wait and final merge/push effects live there, so tick-only fences miss required safe points — cost if wrong: adjacent standalone caller compatibility may need refinement. Preserve no-ledger standalone behavior/worker sandbox; actual hub/landing callers must pass guards and regression tests prove barrier/reconciliation behavior.

14. append narrow V14 pin to existing gate_tickets for possibly-issued groupCI — retaining a row alone cannot retain capacity after TTL/restart; preserve ordinary expiration and release only on proved-unissued/terminal evidence — cost if wrong: uncertain orphan holds a slot until explicit reconciliation. Original runtime implementer owns fixround1.

15. user request to organize and dogfood now authorizes root-only liveE16 task/issue/comment management and one bounded readonlyLuna canary via existingHub — explicit later user authorization supersedes original no-live/model constraint for this scope; implementation/review tests remain isolated/no-model — cost if wrong: reversible tracking changes and63seconds model allowance usage. Planconstraint updated accordingly; no deploy/merge/globalsettings/Tb7 effects.

16. finalwholebranchreview will use the existingSDD spec+quality rubric expanded to repository standards and whole-branch acceptance — referenced requesting-code-review/code-reviewer.md is absent from skills roots and repo skill sources; equivalentreview need not stall authorizedimplementation — cost if wrong: missing template-specific check may require another targeted correction, mitigated by explicit14case/authority/recovery/standards lenses andfreshmostcapable reviewer.

17. Merged semantic completion uses a reserved `task.landed` fact emitted atomically from the existing `record_merged` receipt seam; receipt-free status remains an audit stage because status precedes receipt persistence. Cost if wrong: legacy semantic consumers may need a receipt-event adapter; original source identities and processed history remain intact.
