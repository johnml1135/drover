# Installing Drover Hub (owner steps)

Run everything from PowerShell on Windows as **yourself**. Only the two firewall steps need an elevated shell
("Run as administrator"). Every script prints what it does; `Install-Drover.ps1 -WhatIf` changes nothing.

Prerequisites: `uv` at `%USERPROFILE%\.local\bin\uv.exe`, Tailscale signed in (with HTTPS certificates enabled for
the tailnet), WSL with the distro named in `paths.wsl_distro` in the config (default `Ubuntu`), Ollama for Windows with
`OLLAMA_HOST=0.0.0.0:11434` (the wizard can show where its model store should go).

## 1. Preview (no elevation)

    cd <your-drover-checkout>\hub\scripts
    .\Install-Drover.ps1 -WhatIf

Read the six steps it lists. Nothing is created and the wizard is not started.

## 2. Install (no elevation)

    .\Install-Drover.ps1

In order: the setup wizard (`python -m drover init`), only when `%USERPROFILE%\.drover\config.toml` is missing (or
`$env:DROVER_HOME\config.toml`); the state dir from the config (default `%USERPROFILE%\.drover`) with an owner-only
ACL; the token `<state dir>\hub-token` (kept if it exists); `uv sync` of the hub venv; the per-user scheduled task
`DroverHub` (at logon, restart every minute on failure); `tailscale serve` for the UI (skipped when the config says
`hub.tailscale = false`). Add `-SkipTailscale` to leave the last step for later, `-Defaults` to run the wizard without
questions, or `-Answers FILE.toml`. If you chose a state dir other than the default, the script also sets the user
environment variable `DROVER_HOME` to it so Drover finds its config. Re-run the wizard any time:
`python -m drover init` (the old file is backed up). Every key is in `drover.example.toml` at the repo root.

## 3. Hub firewall rule (ELEVATED, only if the WSL API listener is wanted)

The WSL-facing API (`hub.api_port`, default 8766) is off by default (ADR 0002): workers talk to the hub through files. Skip this unless
you turn `api_listen_wsl` on.

    .\Add-HubFirewallRule.ps1            # prints the rule
    .\Add-HubFirewallRule.ps1 -Apply     # from an elevated shell

## 4. Ollama firewall rule (ELEVATED)

Allows inbound 11434 from the WSL virtual adapter only. Start WSL first so the adapter exists.

    .\Add-OllamaFirewallRule.ps1         # prints the rule and warns about unscoped Ollama rules
    .\Add-OllamaFirewallRule.ps1 -Apply  # from an elevated shell

If it warns about an existing Ollama allow rule that is not scoped to the adapter, disable that rule in
Windows Defender Firewall: Ollama listens on every interface.

## 5. Start

    Start-ScheduledTask -TaskName DroverHub

Open `http://127.0.0.1:8765/health` (or the tailnet URL). The hub finds `claude` and `codex` in WSL at start and when
WSL first comes up; the Health tab shows the paths it stored.

## 6. Turn dispatch on

A fresh ledger is **PAUSED**: nothing is dispatched. When you are ready, use **Turn on...** in the page header or on
the Health tab and confirm. **Pause** is immediate and does not stop runs already started. Imported tasks stay
`proposed` until you approve them in the Inbox.

## Undo

    Unregister-ScheduledTask -TaskName DroverHub -Confirm:$false
    tailscale serve reset
    Remove-NetFirewallRule -DisplayName 'Drover Ollama (WSL)'   # elevated; also 'Drover Hub API (WSL)' if added

The state dir (ledger, token, runs, config, repos) is left in place. `C:\drover` from an earlier install is no longer
used; copy `ledger.db` to the state dir if you want to keep its history (stop the hub first).
