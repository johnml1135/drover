<#
Registers the scheduled task \DroverCompaction: the overnight WSL compaction, nightly at 03:30, elevated, as this user.
It runs as this user because the VHDX path comes from HKCU and the state directory from this profile. A night missed
stays missed: a catch-up run at a daytime boot would stop live work. Run this once, elevated. The owner runs it; the
build slice does not. -Root is the deployed checkout: elevated code must not run from a worktree agents can write to.
#>
param(
    [string]$Root = 'H:\repos\drover.deploy',                # the deployed checkout that holds the drover package
    [string]$StateDir = $(if ($env:DROVER_HOME) { $env:DROVER_HOME } else { Join-Path $env:USERPROFILE '.drover' }),
    [Parameter(Mandatory)][string]$Python    # a real python.exe; the WindowsApps alias fails under Task Scheduler
)
$ErrorActionPreference = 'Stop'
$user = "$env:USERDOMAIN\$env:USERNAME"
# cmd.exe pins DROVER_HOME, as the hub's task pins its paths, so the job reads the hub's state directory.
$cmdLine = "/d /c `"set `"DROVER_HOME=$StateDir`"&& `"$Python`" -m drover.compaction >> `"$StateDir\compaction-out.log`" 2>&1`""
$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument $cmdLine -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -Daily -At 03:30
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'DroverCompaction' -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description 'Drover overnight WSL disk compaction' -Force
Write-Host "Registered \DroverCompaction: nightly at 03:30 as $user, running $Python -m drover.compaction in $Root, state in $StateDir."
