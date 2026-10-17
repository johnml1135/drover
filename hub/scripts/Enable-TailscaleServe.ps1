<#
Fronts the hub UI (127.0.0.1:<hub.ui_port>, default 8765) with tailnet-only HTTPS via tailscale serve.
Requires HTTPS certificates enabled for the tailnet. Owner runs this.
#>
param([string]$Port = '8765')
$ErrorActionPreference = 'Stop'
$st = tailscale status --json | ConvertFrom-Json
$login = $st.User."$($st.Self.UserID)".LoginName
if (-not $login) { throw "Cannot discover the Tailscale login; the hub would refuse remote UI." }
Write-Host "The hub will allow the Tailscale login: $login"
tailscale serve --bg --https=443 "http://127.0.0.1:$Port"
tailscale serve status
