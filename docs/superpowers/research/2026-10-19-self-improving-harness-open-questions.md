# Self-improving harness: open-question research

Date: 2026-10-19. Task: T-bw. Source revision: `b7979e3dfd4f4e9a016d7801450a59b10fc6cc87`.

The immediate priorities are storing replay inputs, distinguishing operational outcomes from truth labels, and defining tokens consistently. Existing metadata does not justify issue-class routing. **S8 remains authoritative: routing by predicted issue class is deferred and stays an open question.** This report proposes documentation amendments; it changes no runtime policy.

## Evidence and method

No `AGENTS.md` exists in this worktree, its checked ancestor directories, or the supplied read checkout `C:\Users\johnm\.drover\read-checkouts\375eebdc09f7c7e8a968d6d9`. The named design document is also absent from both checkouts. I read the authoritative alternative: `tasks.id='T-bo'`, its `goal` containing the complete design and owner-decision material. References to spec sections below refer to that text. T-bq's acceptance explicitly records S5, T-bt records S6, and T-bw records S8. The docs task's appended original questionnaire contains recommendations, so it must not override those accepted decisions.

Ledger observation L1 was captured at **2026-10-19 20:31:17 UTC** from `C:\Users\johnm\.drover\ledger.db` and its WAL. Both files were copied into task scratch, with SHA-256 checked before copying, on the copies, and after copying; all three agreed. SQLite opened only the scratch copy, with `mode=ro`; `PRAGMA quick_check` returned `ok`. No live SQLite connection, migration, or write to hub state was made. This respects the distinction in `drover/ledger/db.py:26` and `docs/adr/0001-windows-hub-owns-ledger.md:19`. The hub GET attempts failed with sandbox `Operation not permitted`; this did not prevent analysis of the offline copy.

L1 identity:

```text
ledger.db      12293d6353ebb8b8e5a0fb306f8919b248a2458a26cd534d43fe580ef2fce220
ledger.db-wal  9ee7409ed5687902494eea5f244554d0eaa0acf260d218a3bd8809a8ce841fb9
schema versions: 1–5
rows: tasks 479; runs 661; task_events 11,461; decisions 468; questions 11; comments 0
first/last event: 2026-10-17T16:45:25Z / 2026-10-19T20:31:12Z
```

Ledger findings below identify tables and row IDs, with reproducible queries at the end. Owner-facing source locators are the [tasks API](http://127.0.0.1:8765/api/v1/tasks), [events API](http://127.0.0.1:8765/api/v1/events), and [decisions API](http://127.0.0.1:8765/api/v1/decisions). These URLs are locators, not claims of successful HTTP retrieval. Their implementation is `hub/api.py:253`, `hub/api.py:325`, and `hub/api.py:372`. Scratch contains the observation manifest and extracted JSON, but this report retains the results needed for review without those temporary files.

## 1. Decider location, system context, and examples

**Drover contains a client, not the server.** `drover/decide.py:166` resolves `DROVER_OLLAMA_URL`, then effective `decider.url`, then the Windows gateway from WSL or `127.0.0.1:11434`. WSL tries localhost second unless an explicit URL was supplied (`drover/decide.py:186`). The hub's Ollama probe defaults to localhost (`hub/settings.py:26`). Config layers are documented at `drover.example.toml:5`; defaults are `url=''`, primary `nimble-v3`, shadow/long-state `nimble` (`drover/config.py:73`). The inspected Windows state config has no explicit decider URL. That does not establish the running process's environment or overlays.

The source location for the public server is [ollama/ollama, server/routes.go, SystemOneHandler](https://github.com/ollama/ollama/blob/v0.35.0/server/routes.go#L789), backed by [decision/systemone.go](https://github.com/ollama/ollama/blob/v0.35.0/decision/systemone.go#L27). **The deployed binary's version, local checkout/fork path, exact model system prompt, and model digest are unverified.** This task grants repository read access to Drover only. To establish deployment equivalence, a follow-up needs read access to the deployed Ollama source/fork, its revision/build identity, and model metadata (version/tags and `show` output for `nimble-v3`, including system/template). No local Ollama source path can be inferred from an HTTP base URL. This is an access/evidence limit, not a reason to guess a checkout location.

What the accessible upstream source establishes, separately from the unverified deployment:

- The tagged v0.35.0 compiler converts state and the question schema into a user-message payload with `context`, `schema`, and a requested-field selector. It maps allowed answers to letter candidates and scores those candidates. It does not invent task-specific examples. See [compiler](https://github.com/ollama/ollama/blob/v0.35.0/decision/systemone.go#L41).
- The handler prepends the model's stored system message when present, applies the model chat template with thinking disabled, and enforces the loaded context limit. The system text is model metadata; its literal content is not a universal prompt hard-coded by Drover. See [renderer invocation](https://github.com/ollama/ollama/blob/v0.35.0/server/routes.go#L843).
- The current upstream API question has `type`, `instructions`, and `criteria`, with no dedicated examples or message-history field; the request likewise has no user-supplied system-message channel. See [API types](https://github.com/ollama/ollama/blob/main/api/types.go#L558). Current upstream has different execution paths for GGUF and other formats, so the tagged compiler is not proof of the installed binary's path. See [current handler](https://github.com/ollama/ollama/blob/main/server/routes.go#L959).

**Drover has no per-question examples channel today.** `build_request` sends one question's instructions and, for choice/score only, criteria (`drover/decide.py:212`). A worked example can already be serialized as text inside that question's instructions; choice descriptions or score levels can also carry explanatory text. That is ordinary prompt content, not a native few-shot conversation. A structured `examples` field and its renderer would require a client/server extension. Keep v1 on existing text fields pending deployment verification.

Two implications for spec §6:

1. Most configured questions are `noul`; appending only to a `criteria` property would have no effect because Drover does not send it for that type. Version the instructions too, and apply the 1,500-token cap to **all added per-question text**, not merely choice criteria (`decide-criteria.json:23`, `drover/decide.py:213`).
2. Existing shadow comparison changes the **model**, reusing the same question and criteria. It does not compare old and new instructions automatically (`drover/decide.py:363`, `drover/decide.py:405`). T-bv needs an explicit baseline/candidate question-version mechanism before calling it a criteria experiment.

## 2. Token semantics and fresh-token formulas

`Usage` is the normalization point (`drover/launch.py:283`). The dispatcher copies its three counters into the run unchanged (`drover/dispatcher.py:349`); `ops.finish_run` persists them without reinterpretation (`drover/ledger/ops.py:625`). Consequently `runs.tokens_in` has backend-specific meaning.

| Backend/event | `runs.tokens_in` | `runs.tokens_cached` | Accumulation |
|---|---|---|---|
| Claude `assistant` | `message.usage.input_tokens`: ordinary input, excluding both cache reads and cache creation | `cache_read_input_tokens` | Sum unique message IDs until a final usage aggregate arrives. |
| Claude `result` | `usage.input_tokens` | `usage.cache_read_input_tokens` | Replace the provisional totals, rather than adding the final aggregate again. `modelUsage` replaces per-model totals when supplied. |
| Codex `turn.completed` | `usage.input_tokens`: total input including the cached subset | `usage.cached_input_tokens` | Add each completed turn's counters. No Claude-style final aggregate replacement or turn deduplication. |

Evidence: `drover/launch.py:306`, `drover/launch.py:332`, `drover/launch.py:345`. Independent repository confirmation: `docs/USAGE-STATS.md:52` explicitly says Codex input includes cache reads; `drover/stats.py:97` adds cached reads only for Claude when constructing total tokens. These are the stream formats this revision consumes; other producer formats require a new normalization audit.

Let `I=runs.tokens_in`, `O=runs.tokens_out`, `R=runs.tokens_cached`, and `W=Claude cache-creation input`. There are two definitions to distinguish:

| Measure | Claude | Codex |
|---|---|---|
| Spec's current **runs-column proxy**, ordinary uncached input + output | `I + O` | `I - R + O` |
| Recommended **fresh computation**, all non-cache-read input + output | `I + W + O` | `I - R + O` |

**Never subtract `R` from Claude's `I`: that subtracts cache reads twice.** Cache creation is newly processed input; the strict fresh-computation measure includes it. The spec's proxy omits it because `runs` has no cache-write column. Retain that proxy only under an explicit name if the owner wants continuity; do not silently call it the complete fresh-token total.

Claude `W` is retained per model as `tokens_cache_write`, including final `cacheCreationInputTokens`, in `Usage.tokens_by_model` (`drover/launch.py:323`, `drover/launch.py:335`). The dispatcher passes that to run stats (`drover/dispatcher.py:344`). Where complete, sum per-model `tokens_in + tokens_cache_write + tokens_out`; do not also add the aggregate run totals. A missing write counter means **unknown/incomplete coverage**, not proof of zero writes. Historical aggregate-only runs cannot be exactly repaired. Codex's per-model fallback uses the run's model (`drover/stats.py:94`).

Example: Claude `I=10, R=5, W=7, O=4` has 21 fresh-computation tokens and 26 total tokens; its runs-column proxy is 14. Codex `I=100, R=80, O=10` has 30 fresh tokens and 110 total tokens. `context` is the latest request's size, not cumulative consumption: Claude includes reads/writes there; Codex stores the latest turn's input (`drover/launch.py:314`, `drover/launch.py:347`).

For measurements, preserve missing counters and flag invalid `R>I` on Codex rather than silently clamping them. Local gate runs are a separate population, not zero-cost worker observations. Token arithmetic measures usage, not money or allowance: backend cache pricing and account calibration remain separate. The current tuner itself uses `I+O` across backends (`drover/tuner.py:139`, `drover/tuner.py:155`), so a new fresh-token view must not be mistaken for that existing cost proxy.

## 3. Claim-time issue-class prediction: exploratory 30-task check

**Result: insufficient evidence for routing.** The simple metadata baseline below matched **8/24 assessable tasks (33.3% micro precision)**. Six of 30 had insufficient root-cause diagnostics. Across all predictions, the confirmed-correct yield is **8/30 (26.7%)**; unknowns are neither asserted wrong nor counted correct. Prediction coverage was 30/30; assessable-label coverage was 24/30. No sampled case establishes the counterfactual that a stronger starting model would have helped.

L1 contains no `issue_labels` table or triage-label events; `task_links` has only 12 pairs. Related-task class labels therefore contribute no existing training feature. Existing decision outcomes are not a substitute taxonomy: `gate_failure_class` labels are next-run gate success, not verified root causes (`drover/dispatcher.py:542`). Current routing keys use role and size plus placement/allowance constraints, not issue class (`drover/route.py:327`).

Method:

- Eligible population: 68 tasks with an ended nonlocal `role='worker'` run, any ended run with outcome in `{gate-fail, blocked, error, failed, timeout, hold}`, and a recorded transition to `claimed`.
- Select the first 30 after sorting by the ascending SHA-256 hex digest of the UTF-8 task ID. This reproducible sample deliberately studies failure-bearing tasks; it is not representative of all 479 tasks or successful first passes. It contains 17 Drover and 13 Motif tasks.
- Predict the first observable obstacle after first claim, using only repo/kind/scope. Rule order: docs kind or entirely documentation scope → `spec-gap`; otherwise Drover → `harness`; otherwise any `.cs/.csproj/.sln/.slnx` scope → `env`; otherwise abstain. Documentation means paths beginning `docs/`, or `docs`, `GLOSSARY.md`, `README.md`, `LICENSE`, `LICENSE-NOTE.md`.
- Reverse later `task-updated` `[old,new]` changes to recover claim-time feature values. Kind did not exist before schema V4 (2026-10-17 19:50:51Z); for T-a, T-7 and T-6 treat it as unavailable, not evidence available then. Their predictions still follow scope or repo.
- One researcher hand-labelled each first gate failure, review HOLD, or explicit blocker after claim, from ledger event detail. `env` includes observed sandbox/resource/timeouts without claiming a verified deeper cause; plain exit-1 or assertion lists without sufficient diagnosis remain `unknown`. Mixed cases use the first stated obstacle and note the secondary issue. These are provisional analyst labels, not owner validation.
- This baseline was formulated during retrospective inspection, not preregistered or tested on an independent holdout. Its precision is descriptive and potentially optimistic. No model was fitted or invoked to produce predictions.

Per predicted class: `env` 3/8 assessable (37.5%; 13 predictions), `harness` 3/9 (33.3%; 10 predictions), `spec-gap` 2/7 (28.6%; 7 predictions). True labels comprise env 8, spec-gap 7, harness 5, owner-decision 3, permission 1, unknown 6. Multi-episode task histories could support different later labels; this experiment is explicitly about the first obstacle.

Evidence is L1 `task_events.id` below, also locatable from each [task page](http://127.0.0.1:8765/tasks/T-a) by substituting its task ID. Claim IDs identify feature cutoffs; outcome event IDs identify the separate labelling evidence.

| Task | Repo/kind | Claim event | Prediction | Hand label | Outcome evidence |
|---|---|---:|---|---|---|
| T-a | drover/implementation | 322 | spec-gap | spec-gap | event 423: Missing control identity and schema contract |
| T-61 | motif/mechanical | 3808 | env | env | event 3862: Read-only Git metadata; also scope gap |
| T-4v | motif/mechanical | 8604 | env | harness | event 9763: Windows path embedded in WSL Git path |
| T-1b | drover/implementation | 10170 | harness | spec-gap | event 10461: Required integration files excluded |
| T-6g | motif/mechanical | 4899 | env | unknown | event 5348: Exit 1; no diagnostic excerpt |
| T-be | drover/docs | 7646 | spec-gap | env | event 8006: Windows gate idle timeout |
| T-7o | motif/mechanical | 8430 | env | unknown | event 9250: Exit 1; no diagnostic excerpt |
| T-ce | drover/docs | 7650 | spec-gap | env | event 8323: Windows gate idle timeout |
| T-c4 | drover/fix | 8067 | harness | env | event 8565: Windows gate idle timeout |
| T-1h | drover/implementation | 4460 | harness | harness | event 4780: Service loop spins while barrier parked |
| T-59 | motif/mechanical | 8745 | env | unknown | event 9918: Exit 1; no diagnostic excerpt |
| T-c6 | drover/implementation | 10655 | harness | unknown | event 11018: Several test assertions; cause unverified |
| T-bb | drover/fix | 4409 | harness | harness | event 4793: Cleanup misses cross-host activity/nested repos |
| T-7 | drover/implementation | 28 | harness | owner-decision | event 73: Existing edits need ownership resolution |
| T-7l | motif/fix | 4156 | env | env | event 4252: EROFS mutex and absent tool |
| T-4k | motif/mechanical | 8528 | env | unknown | event 9846: Exit 1; no diagnostic excerpt |
| T-b | drover/implementation | 1146 | spec-gap | owner-decision | event 1374: Protected existing outputs need ownership resolution |
| T-bf | drover/fix | 5787 | harness | spec-gap | event 5910: Required test updates excluded |
| T-6m | motif/mechanical | 4910 | env | env | event 5104: EROFS mutex and absent tool |
| T-ca | drover/docs | 6316 | spec-gap | spec-gap | event 6619: Ranking requirement impossible at score ceiling |
| T-cx | drover/fix | 9644 | harness | harness | event 10123: Unittest wrapper rejects discover arguments |
| T-6w | motif/mechanical | 10215 | env | unknown | event 11436: Gate failed; no cause in event |
| T-c8 | motif/implementation | 5928 | env | owner-decision | event 6051: Worker requested design approval |
| T-1d | drover/implementation | 4436 | harness | permission | event 4451: Tool write/exec not granted |
| T-c2 | drover/fix | 5828 | harness | spec-gap | event 5885: Required parser/dispatcher files excluded |
| T-6c | motif/mechanical | 3916 | env | harness | event 4122: Candidate not prepared on main; scope mismatch |
| T-7n | motif/mechanical | 8284 | env | spec-gap | event 9093: Stale test rejects current accepted behavior |
| T-6p | motif/mechanical | 4096 | env | spec-gap | event 4211: Required embedded profile excluded |
| T-8i | drover/docs | 7754 | spec-gap | env | event 8623: Windows gate idle timeout |
| T-6 | drover/implementation | 105 | spec-gap | env | event 137: Read-only temp prevents review verification |

Open question only: after validated labels and better failure excerpts exist, can temporally prior related-task labels predict anything beyond repo/platform? Evaluate on later tasks, split by episode and time, and report per-class precision/abstention. `model-capability` requires evidence of the same plan succeeding at a higher tier with environmental changes ruled out; repo or a later escalation alone cannot supply that label. **No issue-class routing overlay is proposed or implemented.**

## 4. Replay inventory and timeline

**Replay-ready historical decisions: zero.** L1 has 468 decision rows, all `model='nimble-v3'`, `shadow=1`, but no `decision_states` table. The client hashes the state; the dispatcher persists only that hash and selected answer fields (`drover/decide.py:338`, `drover/dispatcher.py:282`). A hash cannot reconstruct the question input. Missing task/run links and input snapshots also prevent a reliable retrospective join to claim-time metadata. Do not promise replay of all existing labels after a migration.

Raw rows by **ISO week of asking**, with labels observed at L1 capture:

| Question | W42, Oct 12–18: asked / settled | W43, Oct 19–25: asked / settled | Total asked / settled | Non-null model answer + settled outcome |
|---|---:|---:|---:|---:|
| `retry_or_escalate` | 43 / 42 | 126 / 87 | 169 / 129 | 75 |
| `gate_failure_class` | 8 / 7 | 95 / 64 | 103 / 71 | 35 |
| `plan_wrong` | 43 / 0 | 125 / 0 | 168 / 0 | 0 |
| `landed_diff_stale` | 9 / 0 | 19 / 0 | 28 / 0 | 0 |
| `task_size` | 0 / 0 | 0 / 0 | 0 / 0 | 0 |
| `task_risk` | 0 / 0 | 0 / 0 | 0 / 0 | 0 |
| `harness_pick` | 0 / 0 | 0 / 0 | 0 / 0 | 0 |
| `note_admit` | 0 / 0 | 0 / 0 | 0 / 0 | 0 |
| `lane_stuck` | 0 / 0 | 0 / 0 | 0 / 0 | 0 |
| `batch_culprit` | 0 / 0 | 0 / 0 | 0 / 0 | 0 |
| `branch_superseded` | 0 / 0 | 0 / 0 | 0 / 0 | 0 |

Neither week is complete: observation begins Saturday Oct 17 and ends Monday Oct 19 at 20:31 UTC. The available decision window is **2.13938 days**, from Oct 17 17:10:35 UTC. These counts must not be described as mature weekly traffic. Since `outcome_at` does not exist, the table groups labels by **decision timestamp**, not the week they settled. Seven zero-row names are configured questions (`decide-criteria.json:1`); this does not mean their call paths can never run.

A deliberately simple projection divides counts by 2.13938 days. It assumes unchanged workload, label yield and lags; it is a planning scenario, not an observed full-week rate:

| Question | Asked/week, extrapolated | Labelled rows/week, extrapolated | Existing raw settled count ≥30? | From new state capture: days to 30 labelled rows at that yield |
|---|---:|---:|---|---:|
| `retry_or_escalate` | 553.0 | 422.1 | Yes, already 129 | 0.50 |
| `gate_failure_class` | 337.0 | 232.3 | Yes, already 71 | 0.90 |
| `plan_wrong` | 549.7 | 0 | No | No finite ETA under current settling code |
| `landed_diff_stale` | 91.6 | 0 | No | No finite ETA under current settling code |
| All seven zero-row questions, individually | 0 | 0 | No | No finite ETA from observed traffic |

The first two ETAs are optimistic raw-row scenarios, **not** dates when validated replay will be available. If `plan_wrong` settling is added and 50% of future asks yield usable settled outcomes, 30 would take about **0.76 days plus settling lag**; at 50% for `landed_diff_stale`, about **4.58 days plus lag**. Those are hypothetical yields, not measured ones. `batch_culprit` and `branch_superseded` require qualifying operational events as well as settling code; time alone cannot generate them.

Formula for each question, after input capture and reliable settling begin:

```text
eligible_rate_per_week = asks_per_week * usable_settlement_fraction
weeks_for_30 = max(0, 30 - existing_eligible_replay_pairs) / eligible_rate_per_week
shadow_duration >= max(2 weeks, time to 30 eligible settled outcomes)
```

For the frozen historical ledger, `existing_eligible_replay_pairs=0`. Zero rate means no finite ETA. Add observed settlement delay once `outcome_at` exists. At half the traffic the estimates double; at one quarter they quadruple. The approved shadow minimum remains two weeks even when 30 rows accumulate faster (spec §6).

**Why 200 non-null outcomes are not 200 truth-labelled replay pairs:**

- `retry_or_escalate`: 129 outcomes include 25 audit-only `needless` cases with no target label. Among non-null model answers, 65 have a comparable `label`; 10 are audit-only. At that provisional comparable yield, 30 new rows take **0.99 days**, before lag. The other 54 settled rows lack a model answer. Map `noul=true` to `escalate`, false to `retry`; do not compare booleans directly to those strings (`decide-criteria.json:23`, `drover/dispatcher.py:545`).
- `gate_failure_class`: 71 outcomes are `{correct:false}` (52) or `{correct:true}` (19). Only 35 have non-null model answers, representing 31 unique state hashes. Their provisional yield gives **1.83 days** to 30 rows, but the outcome means only that the next gate passed, not that the predicted failure class was true. It cannot score a new class prediction against ground truth. Independent class labels are still needed (`drover/dispatcher.py:549`).
- Across all 468 asks, 226 model answers are JSON null. The recorder drops fallback reasons, so these cannot all be called timeouts. Gate-class asks have only 70 distinct state hashes among 103 rows, meaning repeated cases could leak between replay splits. Group duplicates and task episodes before the newest-30% holdout split, and keep the holdout unavailable during proposal selection.

The real timeline is therefore: land input capture and joins (T-bp), land/define outcomes (T-bx plus label auditing), then accumulate eligible pairs per question. The raw label rates suggest collection may be fast for retry decisions, while verified gate-class truth and zero-traffic questions have **no supported calendar ETA**. Thirty examples is a collection threshold, not proof of statistical power; paired testing depends on discordant pairs, with validation and heldout results reported separately.

## 5. Owner-minute sanity check

**Keep the accepted S5 priors; event timing cannot calibrate active owner effort.** The accepted rules in T-bq are approve 0.5 minutes, answer 2, status override 3, and a grill/docket round equal to **0.5 minutes per question plus all question words / 238 words per minute**. The original spec §4 and Appendix A still say five minutes per round; those statements need replacing. For five questions totalling 1,190 displayed words, the S5 estimate is `5*0.5 + 1190/238 = 7.5 minutes`.

Observed L1 evidence:

| Observation | Timing/count | Implication |
|---|---|---|
| Explicit owner approvals (`status`, reason `approved`) | 410 | Current 0.5-minute rule implies 205 estimated minutes; no active-time measurement validates that number. |
| Large approval batch | 220 approvals in 10 seconds, Oct 18 03:08:37–03:08:47 UTC, events 3049–3268 | Applying the prior implies 110 minutes. Timestamp spacing measures submission, not earlier reading/thinking. |
| Consecutive approval gaps | 350/409 gaps are zero; among the 391 gaps ≤300 seconds, p50=0s and p90=1s | Per-item interarrival is unsuitable for fitting reading or decision time. |
| Creation → approval latency | n=410; p50=4,752s (79.2m); p90=124,434s (34.6h) | This is queue latency, not effort. Imported/batched tasks further distort it. |
| Other owner status changes | 39 | Includes bulk repairs/re-ready operations; not necessarily 39 distinct human interruptions. Example events 48, 87, 191, 212 include infrastructure/brief repairs. |
| Questions/comments | 11 questions, all open; 0 answers; 0 comments | No sample supports fitting the 2-minute answer prior. |
| Owner dispatch changes | 13 enable, 12 pause | Real owner actions outside the three fixed rates; treatment needs explicit configuration/coverage. |

Sources: L1 `task_events` and `questions`; question timestamps and status are defined at `drover/ledger/schema.py:46`, answer events at `drover/ledger/ops.py:857`. The ledger actor indicates the credential/path attributed to an action, not proof the owner personally spent that time. Many lead/API actions are attributed to dispatcher; there is no basis for deriving their human duration from timestamps.

Store `est_minutes`, `estimate_method/rule_version`, rate values, word count, question count, and a batch/round identifier. Keep actionable latency separately; never substitute it for estimated minutes. Charge a question once through either the round formula or the individual-answer rule, and retain per-item action counts separately from per-session interruptions. Comments and toggles need an explicit rate or an `unestimated` marker, not an invented default. S5 fixes the initial values; this research provides no evidence to replace them.

Optional future calibration: collect a small voluntary owner-reported active-duration sample of individual and batch sessions, including time spent reading before submission. Compare medians by action and batch size. This is a research recommendation, not new browser instrumentation or a change to the approved rule table. S6's baseline/alerts start **2026-10-26**; three weeks end **2026-11-16**. Do not reset that date from this report's capture date (T-bt acceptance).

## 6. Hermes self-evolution: gates and holdout

The separate [NousResearch/hermes-agent-self-evolution PLAN.md](https://github.com/NousResearch/hermes-agent-self-evolution/blob/main/PLAN.md) describes an offline optimizer that proposes PRs. These are plan requirements, not evidence that every gate is implemented or producing improvements.

Its candidate pipeline requires all pytest tests, a 20-task TBLite check, and task-specific fitness; the top three then face full 100-task TBLite and YC-Bench fast_test. Benchmarks reject regressions rather than supply optimization fitness. TerminalBench2 is also described for final validation. Text-size limits, semantic preservation, frozen interfaces and session-boundary deployment protect behavior/caching. Human review remains required. ([Benchmark and constraint sections](https://github.com/NousResearch/hermes-agent-self-evolution/blob/main/PLAN.md#benchmarks-as-fitness-signals))

Skill evaluation proposes 10 train / 5 validation / 5–10 holdout examples; tool selection proposes 60/20/20. It compares baseline and candidate on holdout and requests statistical-significance checks; PRs include all split scores and costs. Those small sets do not establish strong evidence by themselves. The plan does not specify a sealed-holdout access protocol, repeated-testing correction, or a universal numerical significance rule. ([Skill dataset section](https://github.com/NousResearch/hermes-agent-self-evolution/blob/main/PLAN.md#phase-1-skill-evolution-via-dspygepa-core-capability), [tool section](https://github.com/NousResearch/hermes-agent-self-evolution/blob/main/PLAN.md#phase-2-tool-description-evolution))

## Proposed spec amendments for the owner

1. **§3/§4, token definitions:** specify both the legacy runs-column proxy (Claude `I+O`, Codex `I-R+O`) and the recommended complete fresh-computation measure (Claude adds cache-write tokens). Name the selected metric, preserve missing-data coverage, and require numerical fixtures for both streams. Do not silently change the tuner's existing allowance-cost proxy.
2. **§6, lesson transport:** version instructions as well as choice/score criteria; append examples there for `noul`. Enforce the 1,500-token cap across the full question text. Keep a dedicated examples channel as a follow-up requiring deployed-server source/version verification.
3. **§6, comparison mechanism:** distinguish a shadow model from a candidate criteria version. Specify baseline/candidate question snapshots and their model/template identities; existing `shadow_compare_model` alone does not compare criteria versions.
4. **§2/§5/§6, replay eligibility:** distinguish a stored state, an operational outcome, a verified label and a comparable replay pair. Record `outcome_at`, fallback and input-version provenance. Exclude null answers from correctness denominators; map boolean escalation answers to action labels. Do not use next-run success as gate-class ground truth or claim historical replay from hashes.
5. **§6, evaluation:** group repeated states/task episodes before the temporal 30% holdout, freeze its membership, prohibit tuning against it, and report paired counts/discordances and uncertainty. Retain the two-week AND 30-eligible-outcome shadow requirement; zero-traffic questions remain unestimated. Include all rejected candidates and guardrail results, following Hermes's distinction between fitness and regression gates.
6. **§2.4/§6/§9, S8:** keep issue labels for diagnosis; remove the scheduled issue-class routing slice/claim-time overlay from committed implementation scope and preserve only an open research question. This sample is 33.3% precision on assessable failures, not an experiment authorization or a promise to resume after four weeks.
7. **§4 and Appendix A, S5:** replace flat five-minute grill rounds with `0.5*questions + words/238`; store rule version/counts and distinguish queue latency, active-time estimates, per-item actions and batch interruptions. Document handling of comments/toggles and avoid double charging docket answers.
8. **§4/§7/§8, accepted decisions:** preserve baseline/alert start Oct 26 and three-week review Nov 16 (S6); keep tuner and retro proposals in the single harness epic E-v (S1/T-c6), rather than copying older E-t references. External-work observations feed owner-load coverage; they are not automatically measured owner minutes (S7/T-c7).

## Reproduction notes

Run only against a hub-provided read-only export or a stable offline copy on a WSL-local filesystem, not against the live DrvFs database. No package install or service start is necessary. Snapshot hashes above identify this report's dataset; a later ledger will have different counts.

Core inventory and weekly query (SQLite `%G/%V` requires modern SQLite; Python's `datetime.isocalendar()` gives the same ISO grouping):

```sql
SELECT question_name, strftime('%G-W%V', at) AS asked_week,
       COUNT(*) AS asked,
       SUM(outcome IS NOT NULL) AS settled_at_capture,
       SUM(outcome IS NOT NULL AND json_type(answer) <> 'null') AS answered_settled
FROM decisions GROUP BY question_name, asked_week;

SELECT question_name, COUNT(DISTINCT state_hash) AS states,
       SUM(json_type(answer) = 'null') AS null_answers
FROM decisions GROUP BY question_name;

SELECT kind, actor, COUNT(*) FROM task_events GROUP BY kind, actor;
SELECT status, COUNT(*) FROM questions GROUP BY status;
SELECT name FROM sqlite_master WHERE type='table';
```

Sample population query; then sort returned task IDs by `hashlib.sha256(id.encode()).hexdigest()` and take 30:

```sql
SELECT t.id FROM tasks t
WHERE EXISTS (SELECT 1 FROM runs r WHERE r.task_id=t.id
              AND r.role='worker' AND r.backend<>'local' AND r.ended IS NOT NULL)
  AND EXISTS (SELECT 1 FROM runs r WHERE r.task_id=t.id AND r.ended IS NOT NULL
              AND r.outcome IN ('gate-fail','blocked','error','failed','timeout','hold'))
  AND EXISTS (SELECT 1 FROM task_events e WHERE e.task_id=t.id AND e.kind='status'
              AND json_extract(e.data,'$.to')='claimed');
```

For each selected task, take the lowest event ID transitioning to claimed, reverse later task-updated diffs to recover repo/kind/scope, and use the prediction rule in §3. Hand-label the first subsequent gate-done with `g.ok=false`, review-done HOLD, or replan-request with an explicit blocker; where absent, inspect the failed run-finished event. Claim and labelling event IDs are recorded in the table, so the selected evidence can be inspected independently:

```sql
SELECT id, task_id, kind, at, data FROM task_events
WHERE task_id=:task ORDER BY id;
SELECT * FROM runs WHERE task_id=:task ORDER BY id;
```

Owner checks use `kind='status' AND actor='owner' AND json_extract(data,'$.reason')='approved'`. Join the task's created timestamp for approval latency; subtract consecutive approval timestamps for gaps. Quantiles above use the sorted value at `floor(0.9*(n-1))` for p90 and the ordinary median for p50. The batch is bounded by event IDs 3049–3268.

## Verification

On 2026-10-19, the approved WSL Python ran these focused existing tests: `tests.test_stats.Stats.test_cached_input_is_counted_once_for_each_backend` and `tests.test_stats.Stats.test_claude_cache_writes_survive_completed_and_interrupted_streams`: **2 tests, OK**. A separate in-memory parser check confirmed Claude message deduplication/final aggregate replacement/cache-write retention and Codex turn accumulation. Inventory SQL, weekly counts, all 30 sample rows and their precision arithmetic were rechecked against L1. All 40 repository file:line citations resolve. Only this report is changed; no full suite or exact-SHA gate receipt is claimed.
