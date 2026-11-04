# Slice 7 report: `run_output`

## What the slice changed

Task 47 (022a748, 48261e0): a run's output snapshot is now `drover.run_output.RunOutput(base, scope_files, worktree)`, a frozen record with `to_event`, `from_event` and `owns`. `LaunchSteps._output_snapshot` returns it and `_spawn` stores `to_event()`, which writes the same three keys as before, so older code reading new events still works. `dispatcher_finish` and `outside_land` read stored events through `from_event`. `run_output` imports no Drover module. GLOSSARY gains section 15's terms.

## Review outcomes

One combined task and slice-exit reviewer (lighter process): spec ✅; two Important findings, both fixed in 48261e0 and checked by the controller:
- `from_event` raised on non-dict values and odd field types. It now returns None for a non-dict, a missing, empty or non-str base, a non-list `scope_files` or a non-str `worktree`; non-str items in a list are dropped; a one-key `{'base': ...}` reads as `RunOutput(base, (), None)`.
- No test pinned the finish refusing a run whose `owns()` is False. `FinishChecksOwnership` now covers a non-owning run and an unreadable stored output (both `needs-owner`, no commit) and an owning one.

Parked Minor: the glossary wording drifts slightly from section 15.

## Exit checks

| Check | Command | Result |
|---|---|---|
| Importers of `output_snapshot` | `scripts/test.py` both tiers over the importer set | default 92 + 136 OK; integration 124 (2 skipped) + 183 OK |
| Trace | `trace_flake.py` | SAME |
| Full suite at the ship commit | `scripts/test.py -j 4`, both tiers | recorded at ship |
