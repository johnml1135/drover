<#
Registers the scheduled task \DroverCompaction: the overnight WSL compaction, nightly at 03:30, elevated, as this user.
It runs as this user because the VHDX path comes from HKCU and the state directory from this profile. A night missed
stays missed: a catch-up run at a daytime boot would stop live work. Run this once, elevated. The owner runs it; the
build slice does not.
#>
param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),    # the checkout that holds the drover package
    [string]$Python = (Get-Command python).Source
)
$ErrorActionPreference = 'Stop'
$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $Python -Argument '-m drover.compaction' -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -Daily -At 03:30
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'DroverCompaction' -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description 'Drover overnight WSL disk compaction' -Force
Write-Host "Registered \DroverCompaction: nightly at 03:30 as $user, running $Python -m drover.compaction in $Root."
