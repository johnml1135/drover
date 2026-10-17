<#
Allows inbound TCP 11434 (native Windows Ollama, OLLAMA_HOST=0.0.0.0:11434) from the WSL network only.
NEEDS ELEVATION to apply. Prints the rule it would create; pass -Apply to create it.
Scoped like Add-HubFirewallRule.ps1: to the vEthernet (WSL*) adapter alias plus the local port, not to a subnet
(the WSL NAT subnet changes between boots, the adapter does not). Re-run if the adapter is renamed.
#>
param([switch]$Apply)
$ErrorActionPreference = 'Stop'
$name = 'Drover Ollama (WSL)'
$alias = Get-NetAdapter | Where-Object { $_.Name -like 'vEthernet (WSL*' } | Select-Object -First 1
if (-not $alias) { throw "No vEthernet (WSL*) adapter found. Start WSL first." }
$cmd = "New-NetFirewallRule -DisplayName '$name' -Direction Inbound -Protocol TCP " +
       "-LocalPort 11434 -InterfaceAlias '$($alias.Name)' -Action Allow -Profile Any"
Write-Host $cmd
# Ollama binds every interface, so an unscoped allow rule (the Ollama installer can add one) would expose it to the LAN.
$broad = @(Get-NetFirewallRule -Direction Inbound -Action Allow -Enabled True -ErrorAction SilentlyContinue |
    Where-Object { $_.DisplayName -like '*ollama*' -and $_.DisplayName -ne $name })
foreach ($r in $broad) {
    Write-Warning "Existing allow rule '$($r.DisplayName)' may not be scoped to the WSL adapter. Review or disable it."
}
if (-not $Apply) { Write-Host "Dry run. Re-run elevated with -Apply to create the rule."; exit 0 }
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw "Run from an elevated PowerShell." }
if (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue) { Remove-NetFirewallRule -DisplayName $name }
Invoke-Expression $cmd
