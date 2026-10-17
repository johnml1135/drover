# Accounts, allowance and history

Each `[[seats]]` entry identifies one account. Set `config_dir` for Claude's
`CLAUDE_CONFIG_DIR`, or `codex_home` for Codex's `CODEX_HOME`. Codex also accepts
`config_dir` for compatibility. `~` expands on the account's configured OS. The example config
contains Codex, Claude and Claude Personal in priority order; installations that
omit seats retain the legacy two-seat defaults.

The dispatcher reads Claude's zero-token headless `/usage` command with a
30-second timeout. Codex reads only the newest rollout's last `token_count`
rate limits (the Windows hub invokes its stdlib reader in the seat's WSL distro). Null windows clear an old observation. Raw allowance text stays in
`allowance_readings`; public order responses omit it. A format or command failure
raises an Inbox alert and records an unsuccessful observation. No statusline or
token-cost estimate supplies allowance.

Active accounts refresh every five minutes. Assignments require a reading with
a 60-second probe cache. Manual readings survive failed probes until their reset
or a successful observation replaces them. Idle accounts are read when routing
needs them. Admission checks with no assignable work do not probe accounts, and
run completion uses the most recent observation. Command output is decoded as UTF-8
on both Windows and WSL. On Windows without IANA timezone data, the parser uses the configured
WSL distro's `/usr/share/zoneinfo` files; if unavailable it alerts and accepts
manual ISO timestamps with an explicit offset.

```console
python -m drover.usage order --json
python -m drover.usage set claude seven_day 40 2026-10-20T04:00:00-04:00
python -m drover.stats --json
python -m drover.stats --json --since 2026-10-01 --task T-1
```

WSL clients use the hub API (`DROVER_HUB_URL`, `DROVER_HUB_TOKEN`, or the state's
`hub-token` file). Without `DROVER_HUB_URL`, clients discover the Windows gateway
under WSL NAT. `--db` is for a local test ledger or Windows. The stats reader opens
SQLite in read-only mode, without migrations or settings writes. The API
exposes `GET /api/v1/stats`, `GET /api/v1/usage/order`, and `POST /api/v1/usage`.

Accounts above their reserve are tried in priority order. An account projected
to waste at least `seats_policy.waste_threshold_pct` percentage points of its
weekly allowance moves ahead. Burn is the change in measured percentage over
the last 24 hours within the same reset window; one observation does not imply
a zero burn rate. Account capacity and repo platform restrictions still apply.
The default director uses `[director] seat, model, effort`; blank seat/model
retain the flagged seat and `models.director`. It may consume its reserve, but
borrowing a different Claude account creates an owner question. Only an explicit
"Approve borrowing" answer permits that account for that task's director work.

Run statistics remain in `run_stats` with their task and run. Counters include
sessions, handoffs, compactions, additional attempts per role (retries), model
changes between attempts (escalations), and work repeated after a failed gate or
review (rework). Token totals use the backend's model breakdown when available,
and otherwise the run's model. `tokens_total` includes cached reads once (Codex input already includes them). Unknown peaks remain null. CPU peaks are sampled
process-tree utilisation (100% equals one CPU), RAM is resident/peak job memory,
and disk bytes are measured physical IO on Linux or job IO on Windows. Very short
runs or processes that exit between samples can have incomplete peak telemetry.
Existing runs are migrated with their known tokens and timings; unavailable
historical telemetry stays unknown.

Hourly UTC buckets contain current allowance and burn per window, host resource
facts, queue counts and active workers. Tokens and landings are additive counters
written at run completion and landing transitions, so refreshing a gauge or
deleting a task cannot erase throughput. The dispatcher tick keeps hourly data
35 days, folds older buckets into daily data, and keeps daily history 365 days.
Daily flow counters sum; gauges retain the latest sampled value and scalar
resource min/max ranges. Run/task rows have no automatic expiry. Run logs expire
after 30 days; action records and gate receipts remain. The Stats tab plots both
hourly and daily data using Plotly.js 2.35.2. Historical series are host-wide;
`--task` filters permanent run and task details.

Archive hides a task from default lists and claims; restore it from Stats.
Archive and deletion refuse in-flight work. Deletion requires an authenticated
owner and explicit confirmation, removes the task, its runs, dependencies,
comments and questions, and retains anonymous aggregate statistics and append-only
audit events. The API token cannot archive or delete tasks.
