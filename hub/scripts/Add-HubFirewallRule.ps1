<#
Allows inbound TCP 8766 (hub API) from the WSL network only. NEEDS ELEVATION.
Prints the rule it would create; pass -Apply to create it.
#>
param([switch]$Apply)
$ErrorActionPreference = 'Stop'
$alias = Get-NetAdapter | Where-Object { $_.Name -like 'vEthernet (WSL*' } | Select-Object -First 1
if (-not $alias) { throw "No vEthernet (WSL*) adapter found. Start WSL first." }
# Scoped to the adapter alias (resolved now) plus the local port, not to a subnet: the WSL NAT
# subnet changes between boots, the adapter does not. Re-run this script if the adapter is renamed.
$cmd = "New-NetFirewallRule -DisplayName 'Drover Hub API (WSL)' -Direction Inbound -Protocol TCP " +
       "-LocalPort 8766 -InterfaceAlias '$($alias.Name)' -Action Allow -Profile Any"
Write-Host $cmd
if (-not $Apply) { Write-Host "Dry run. Re-run elevated with -Apply to create the rule."; exit 0 }
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw "Run from an elevated PowerShell." }
Invoke-Expression $cmd
