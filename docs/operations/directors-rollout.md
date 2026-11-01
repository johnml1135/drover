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

## Rolling back

A pane director must be stopped before panes go off. With panes off, or on an older hub, nothing can read its run, so the
hub never sees it end. Keep the hub running with panes on and Herdr open until step 5, and do not start during a compaction:
a row in `compaction` reads as alive without asking Herdr.

1. **Stop new launches.** Set `director_execution_enabled` to `false`. This starts nothing new and stops nothing: a running
   director keeps working and keeps getting wakes.
2. **Find what is open.** Run the check below. It only reads the ledger.
3. **Close each pane listed.** Let the director commit first if it can, then close the pane in Herdr, or run
   `herdr pane close <pane_id>`. `/exit` alone is not enough: the pane's shell stays, the hub reads that as unknown, and the
   run stays open.
4. **Wait for the run to end.** Run the check again until both lists are empty. The hub's next ticks do this,
   and a tick needs Herdr open: with Herdr closed every pane reads unknown and nothing ends.
5. **Turn panes off.** Set `[panes] enabled = false` and restart the hub. If step 4 was skipped, the hub raises one notice,
   "A director is still in a pane": turn panes back on and start again from step 2.

The check, in PowerShell, from the hub's checkout with the hub's Python, and with its `DROVER_HOME` if it has one:

```powershell
@'
import sqlite3
from drover import config
ledger = sqlite3.connect(f"file:{config.state_path('ledger.db').as_posix()}?mode=ro", uri=True)
print('runs:', ledger.execute("SELECT run_id, role_id FROM director_runs WHERE state<>'finished'").fetchall())
print('panes:', ledger.execute("SELECT pane_id, state FROM pane_agents WHERE started_by='drover' "
                               "AND state IN ('starting','live','rotating','compaction')").fetchall())
'@ | python -
```

### What an older hub does to a resident run

An older hub, like this one with panes off, has only the runner adapter. It opens the newer ledger without complaint and
ignores the pane tables, so nothing warns you. Roll back as above before deploying one. Deployed over an open pane run:

- The role is held on `consumer-liveness-uncertain`, because no runner status exists, and the run never finishes.
- The agent keeps working in its pane on a credential valid for up to 24 hours, and gets no wakes.
- When its session expires, the stop reaches no runner and the role sits at `termination-uncertain`.
- The open run keeps one slot of `parallel_cap` and one load on its seat, and its `pane_agents` row stays open.
- A compaction kills the agent and nothing resumes it.
- An older hub reads the outcomes `rotated`, `revived` and `compaction-fresh` as failures and spends a replacement on each.

To recover, deploy this hub again with `[panes] enabled = true`, follow the rollback above, and only then go back. Do not
clear the ledger by hand: a pane that is open again is read and ended like any other.
