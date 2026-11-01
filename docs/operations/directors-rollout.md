# Directors in panes (owner runbook)

Run these steps in order. Nothing here is done by the build; each step is the owner's. Stop at the first step that fails.

1. **Install the Codex hook.** Only for a Codex director. Add `python3 <path to drover/pane_status_hook.py> --agent codex`
   as the command for each lifecycle event in the hooks of the director seat's `CODEX_HOME` (inside WSL), so a pane shows
   the agent's real state. A Claude director needs nothing: Drover passes the hook per launch.
2. **Allow the hub from WSL.** For a director placed in WSL, set `[hub] api_listen_wsl = true` in `config.toml` and restart
   the hub, so the pane's agent can reach the notification API.
3. **Turn panes on.** Set `[panes] enabled = true`, and `workspace` if the first Herdr workspace is not the one to use.
   Restart the hub. Herdr may stay closed: Drover then holds director launches and carries on with everything else.
4. **Register one role and its execution.** Create the director role, then store its `director_execution` setting as
   `{"<role id>": {"backend", "model", "seat", "max_tokens", "cwd", "placement"}}`. The backend, model and seat name the
   director seat, `cwd` is the checkout the director works in, and `placement` is `windows` for Claude and `wsl` for Codex.
5. **Enable execution.** Set `director_execution_enabled` to `true`. It stays `false` until you set it.
6. **Verify, in this order.**
   - The hub reaches your Herdr server from its scheduled task: the director pane opens in the Drover workspace.
   - A Claude director starts, is woken once for new work, and writes its handoff file at `[director] handoff_tokens`,
     after which a fresh session continues from it.
   - A Codex director reports its status through `herdr.exe` with `WSLENV`, and its context shows in the pane footer.
   - A compaction stop ends the session and the director resumes where it stopped.
   - With Herdr closed, dispatch, review and merging carry on.

To roll back, set `director_execution_enabled` to `false`, then `[panes] enabled = false`, and restart the hub.
