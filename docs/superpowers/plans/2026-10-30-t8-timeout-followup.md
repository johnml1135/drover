# T-8 timeout follow-up

The approved T-8 task is the design. Commit e82c7de already implements the two
review findings; preserve it. The remaining gate failure is the bounded Git
command check in `tests.test_pipeline.Safety`.

Windows may finish a subprocess during process creation, before Python checks
its timeout. Check elapsed monotonic time around the Git runner as well as
passing its timeout through. A completed command beyond the deadline must raise
`TimeoutExpired`, which the pipeline already reports as exit code 124. Preserve
the completed output on that exception. Calls without a deadline stay unchanged.

- [x] Add deterministic regressions in `tests/test_first_dispatch.py` for late
  completion, completion within the deadline, and no deadline. Observe the late
  completion regression fail before changing production code.
- [x] Update only `drover/placement.py` to check elapsed time after the runner
  returns. Keep the subprocess timeout for commands still running.
- [x] Run the timeout regressions, the failing pipeline safety check, and the
  targeted first-dispatch, dispatcher, seat, allowance, configuration, usage,
  launcher, and placement tests. Leave the full platform gate to Drover.
- [x] Review the scoped diff and leave changes uncommitted, with a commit message.

No dependencies, services, extra worktrees, or changes to the existing T-8
admission and home-resolution behavior are needed.

Verification: the deterministic regressions first failed with a missing timeout
and exit code 0 instead of 124. After the fix, 294 targeted tests passed across
`test_first_dispatch`, `test_dispatcher`, `test_seats`, `test_allowance`,
`test_config`, `test_usage`, `test_launch`, `test_platforms`, and
`test_pipeline.Safety`; all 15 `test_placement` tests passed separately.
`git diff --check` passed. Used the existing cached hub Python 3.13 environment
after the bare interpreter lacked MarkupSafe; installed nothing. Full WSL and
Windows gate results remain Drover's responsibility.
