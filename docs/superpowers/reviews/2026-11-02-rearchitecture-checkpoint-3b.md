# Rearchitecture checkpoint after slice 3b

Range e23576d..ea5f404: slices 1, 2, 3a and 3b, then this checkpoint's fixes.

## Test runs

| Run | At | Result |
|---|---|---|
| Default tier, `-j 4` | 572dcb4 | 4078 tests, 28 skipped, 275 modules, 247.7 s, 1 failed module |
| Integration tier, `-j 4` | 572dcb4 | 208 tests, 14 skipped, 262.8 s, 0 failed |
| Default tier, `-j 4` | ea5f404 | 4092 tests, 28 skipped, 275 modules, 206.8 s, 0 failed |
| Integration tier, `-j 4` | ea5f404 | 208 tests, 14 skipped, 213.1 s, 0 failed |

The failed module at 572dcb4 was `test_cleanup_throttle`: a test injected `TimeoutExpired` into `cleanup_identity` after Task 16 narrowed the catch to `OSError`. It and three other stale injections are restated as `TimeoutError`.

Census at 572dcb4: 275 modules, none failed, 3853 spawns against 3832 at baseline. Of 266 modules with a baseline row, 8 differ. `test_decisions` rose from 74 to 106 for Task 13's real-git tests, which each need their own committed ignore file. The others fell. `test_usage` has since gone in-process, so `IMPURE_CEILING` is 63.

## Reviews

All three failed first. Each finding was fixed in e89069c (production), b0ca9c4 (tests) and ea5f404 (docs), or settled by a ruling; one scoped re-review passed.

| Reviewer | Important | Outcome |
|---|---|---|
| Completeness | Popen kwargs untested past the adapter; run-file waits pinned at 1 site of 17 | Adapter test, AST guard over every run-file site |
| Scope | Slice 3a's record of the marker sweep stale | Rewritten; spec Appendix C lists every design-changing ruling |
| Efficiency | `_join` waited before joining readers (10-50 ms a POSIX run); a chmod on every POSIX write; `IMPURE` at 64; new modules over 2 s | Readers first; temp created at its mode; `test_usage` in-process; two proof modules ruled at 3 s alone |

## Exceptions recorded

- `tests/disk_decision_trace.json` changed outside slice 1 once, at 7f07166: `cache_command_times_out.applied`'s error wording only. Ruled as wording.
- Two spawns stay outside `process` without the console flag, `ci:449` and `disk_cleanup:400`, as section 5.1 and ruling 13 allow.
- `fakeci.answering` / `answer_spawns` remains a second fake shape; carried to Task 28 or 45.
- The branch ships only whole: rc 124 at `landing_batch.py:174` and `origin.py:46` is misread until Tasks 24 and 25.
