<#
Registers the per-user scheduled task 'DroverHub': runs at logon, restarts on
failure every 1 minute up to 999 times. Does not need elevation.
Owner runs this; the build slice does not.
#>
param(
    [string]$HubDir = (Split-Path -Parent $PSScriptRoot),
    [string]$Uv = "$env:USERPROFILE\.local\bin\uv.exe",
    [string]$Root = (Join-Path $env:USERPROFILE '.drover')    # the state directory (Install-Drover.ps1 passes the config's)
)
if (-not $env:POWERSHELL_TELEMETRY_OPTOUT) { $env:POWERSHELL_TELEMETRY_OPTOUT = '1' }
if (-not $env:DOTNET_CLI_TELEMETRY_OPTOUT) { $env:DOTNET_CLI_TELEMETRY_OPTOUT = '1' }
if (-not $env:MSBUILDTELEMETRYOPTOUT) { $env:MSBUILDTELEMETRYOPTOUT = '1' }
if (-not $env:TESTINGPLATFORM_TELEMETRY_OPTOUT) { $env:TESTINGPLATFORM_TELEMETRY_OPTOUT = '1' }
if (-not $env:AVALONIA_TELEMETRY_OPTOUT) { $env:AVALONIA_TELEMETRY_OPTOUT = '1' }
if (-not $env:DOTNET_NOLOGO) { $env:DOTNET_NOLOGO = '1' }
$ErrorActionPreference = 'Stop'
$bootstrap = Join-Path $env:LOCALAPPDATA 'Drover/Start-DroverHub.ps1'
New-Item -ItemType Directory -Force (Split-Path -Parent $bootstrap) | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Start-DroverHub.ps1') -Destination $bootstrap -Force
$cmdLine = "/d /c `"powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$bootstrap`" -HubDir `"$HubDir`" -Uv `"$Uv`" >> `"$Root\hub-out.log`" 2>> `"$Root\hub-err.log`"`""
$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument $cmdLine -WorkingDirectory $env:USERPROFILE
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
New-Item -ItemType Directory -Force $Root | Out-Null
icacls $Root /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" | Out-Null
Register-ScheduledTask -TaskName 'DroverHub' -Action $action -Trigger $trigger `
    -Settings $settings -Description 'Drover Hub (ledger, API, website, watchdog)' -Force
Write-Host "Hub output: $Root\hub-out.log and hub-err.log. Registered DroverHub. Start now with: Start-ScheduledTask -TaskName DroverHub"
