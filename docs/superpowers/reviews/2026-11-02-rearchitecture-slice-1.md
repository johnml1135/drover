# Slice 1: real listing, fresh proof

`SLICE_BASE`: e23576d. Commits: 60fb0e1 (Task 8), d2bbc79 (Task 9), and Task 10's. The trace did not move before Task 10.

## Landing state

- `listing` runs once per repo per stage through `Listings`, on `LISTING_TIMEOUT_S`. The one other real listing is `reregister`'s, which verifies a repair.
- `remove` and `remove_output` take their row from the stage listing, with no git, and each takes one `prove`. `refused` is read before `reach`, and the row comes from `refused[0]`.
- `overlaps_worktrees(request, target, own, *, reach)` never lists: `Proof.reach` at removal, `Listings.reach()` at first pass.
- `discard_at` calls `Listings.removed`; `reregister` calls `Listings.repaired`.
- `entries`, `_entries` and `registered=` are gone, and the first two are in `architecture_removed_symbols.json`.
- R8 holds its five rows, R9 is live, R5 holds its one temporary row, and the R6 `_entries` row went.

## Changed trace rows

`--compare` against the record at d2bbc79 exits 0: no `towards_deletion`, one `neutral`, five `towards_keep`. Every scene is an applied run with a mid-stage change, and none deleted anything before or after.

| Scene | Path | Old | New | Direction |
|---|---|---|---|---|
| `revalidation_head` | `w` | keep, `revalidation_changed` | keep, `identity_changed` | towards_keep |
| `revalidation_locked_by_protect` | `w` | keep, `revalidation_changed` | keep, `identity_changed` | towards_keep |
| `revalidation_registration` | `w` | keep, `revalidation_changed` | keep, `identity_changed` | towards_keep |
| `revalidation_unsettled` | `w` | keep, `revalidation_changed` | keep, `identity_changed` | towards_keep |
| `output_registered_above` | `w/bin/x` | keep, `output_evidence_unavailable`, `disposable side marker changed` | keep, `registration_unknown` | towards_keep |
| `output_moved_after_status` | `w/bin/x` | keep, `output_evidence_unavailable`, `disposable side marker changed` | keep, `output_evidence_unavailable`, `disposable registration changed` | neutral |

The first four are section 10's mid-stage change. The last two are removals the proof refuses. `output_registered_above` is a pointer the listing lacks. The neutral row changes only the words of the rejection.

`registration_unknown` is a new reason, and joined `HOLDING` in `tests/disk_trace_compare.py` in the same commit. Without it the compare reads `output_registered_above` as `towards_deletion`.

No scene covers a target nesting in a drive-letter pointer, so section 10's third allowed change has no trace row. No live shape reaches it (section 16 O3).

## Command trace

There is no recorded command trace for removal. `placement_trace.json` holds no listing. By count, in `tests/test_disk_fresh_proof.py`: no `worktree list` at a removal, and one `prove` per removal, in stages 4 and 5.

## Timing, read-only, for owner check 8

`git_worktrees._read_admin` alone, from WSL, on `/mnt/h`, two runs in a row, 2026-11-02, WSL load average about 30 to 36.

| Repo | Admin directories | First run | Second run |
|---|---|---|---|
| drover | 46 | 0.21 s | 0.19 s |
| PanGloss | 51 | 0.23 s | 0.21 s |
| motif | 225 | 0.78 s | 0.83 s |
| Sum | 322 | 1.22 s | 1.23 s |

Section 2.5 estimated 7.2 s, so this is about one sixth of it. `prove` was not timed, by the brief. This covers the three files read for every directory and the main `HEAD`, and leaves out the target's `HEAD` and the tip. The cache was not dropped first, and the survey had read these files earlier, so the figure may be a warm one. Owner check 8 should read a live run's figure beside it.

## What a checkpoint must know

- `PROOF_TIMEOUT_S` is `2 * EVIDENCE_TIMEOUT_S`, 120 s. A stage's first proof spawns each repo's common directory inside that deadline, and each spawn may take 60 s. Past it the proof refuses, which keeps.
- A `changed` refusal of an unlisted target raises `snapshot changed`, as a row at `entry` does. The stray keeps its old row and its error row, and `quarantine_unlisted_registered` does not move. The other way, through `identity_changed`, loses that error row, which the compare reads as `towards_deletion`.
- `Listings.reach()` is beyond section 5.3's interface. It is how the first pass keeps a healthy repo's unit: a kept failure raises at the unit and gives its `output_evidence_unavailable` row. It costs about 20 microseconds per row per unit.
- An interrupted registration's `worktree repair` moves a listed pointer. `repaired` excuses the new place, but the row at the old place has no pointer, so every later removal in that stage refuses. It keeps, and the next stage lists afresh. No scene shows it.
- A disposable unit's `changed` row now says `disposable registration changed (<detail>)`, and `registration_unknown` rows on stage 4 carry `detail`, not `error`: `evaluate` turns any `error` into an error row, which would change the stamp.
- Handlers: no `except` changed. `RegistrationsUnknown` and `TimeoutError` are `OSError`, and both removal paths hold it.
