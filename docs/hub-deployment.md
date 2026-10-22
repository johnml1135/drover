# Running the hub from a pinned deployment

Landing advances the main checkout. If the live hub runs from that checkout, every landing changes its source
under the running server: modules not yet imported load new code beside old, and detached children (disk
cleanup, runners, gates) start on whatever is on disk at that moment. A stray file there also parks landing
(DirtyCheckout). So the hub runs from a separate **deployment worktree** that only the owner moves.

## How code is found

- The hub has no editable install and no `.pth`: the scheduled task runs `uv run --project <HubDir> python -m hub`
  with the working directory set to `<HubDir>`'s parent, so `hub` and `drover` import from the current directory.
  Only the venv (`<HubDir>/.venv`, dependencies only) is per `--project`.
- Children take their code root from the loaded package: `runner.detach`, `placement.cleanup_enqueue`, the cleanup
  worker and the CI/launch paths use `sys.executable` plus `Path(__file__).parents[1]` (as `sys.path[0]` or
  `PYTHONPATH`). A hub started from the deployment therefore spawns only deployment code. No hard-coded repo path
  exists in code.

## Layout

    <repos>/drover            main checkout: Drover lands here, unaffected
    <repos>/drover.deploy     detached `git worktree` of the same repo, with its own hub/.venv

## One-time setup (owner)

    python scripts/deploy_hub.py --deploy <repos>/drover.deploy --force      # creates the worktree at main's tip
    uv sync --project <repos>/drover.deploy/hub
    powershell -File <repos>/drover.deploy/hub/scripts/Install-DroverHub.ps1 -HubDir <repos>/drover.deploy/hub
    # (re-registers the task with the deployment as working directory)

## Each deploy

    python scripts/deploy_hub.py --deploy <deploy> --receipt <gate-receipt.json> [--restart]

- The commit (default: tip of `main`) must be on `main`, and the receipt must be a passing gate receipt for exactly
  that sha (`{"sha": ..., "passed": true}`, the CI receipt format); `--force` skips both checks.
- The deploy worktree is moved with `git checkout --detach`; a dirty deploy worktree is refused.
- Without `--restart` the remaining steps are printed. With `--restart` it runs `uv sync` first (a failure leaves the
  old hub running), kills only the pid(s) listening on `--port` (default 8765) with `taskkill /PID` and no `/T`, then
  starts the `--task` (default `DroverHub`). Detached cleanups and runners keep running.
- Paths come from `--source`, `--deploy` (or `DROVER_HUB_DEPLOY`), `--port`, `--task`, `--uv`.

## Safety checks on `--restart`

- The task must exist; otherwise it stops with the migration steps below.
- Every pid listening on the UI port must be `python -m hub` run from the source or deploy checkout; anything else is
  never stopped. `uv sync` runs only after that check passes.
- A failed `taskkill` aborts before the task is started. After stopping, it waits up to `--wait` seconds (default 30)
  for the port to free, then starts the task.
- The deploy worktree must be clean, untracked files included.

## Migrating a hand-started hub (`Start-Process python -m hub`) to the task

1. Create the deploy worktree and `uv sync` it (one-time setup above).
2. Stop the hand-started hub: kill only its pid listening on the UI port (`taskkill /PID <pid> /F`, never `/T`).
3. Register the task from the deployment: `Install-DroverHub.ps1 -HubDir <deploy>\hub`.
4. `Start-ScheduledTask -TaskName DroverHub`; check `/health`.

## Logs

The task wraps uv in `cmd.exe /c` so stdout and stderr append to `<state dir>\hub-out.log` and `hub-err.log`
(the state dir is the script's `-Root`). These files are not rotated; truncate them while the hub is stopped.
