# Deterministic contract for the actual bootstrap and installer; no real waits, launches, or scheduler changes.
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$start = Join-Path $repo 'hub/scripts/Start-DroverHub.ps1'
$install = Join-Path $repo 'hub/scripts/Install-DroverHub.ps1'
$global:checks = 0
$global:sleeps = 0
$global:readyAfter = 3
$global:uvExit = 0
function Test-Path { param($LiteralPath, $PathType) $global:checks++; return $global:checks -gt $global:readyAfter }
function Start-Sleep { param($Seconds) $global:sleeps += $Seconds }
function Set-Location { param($LiteralPath) Write-Output "CWD:$LiteralPath" }
function fake-uv { Write-Output "UV:$($args -join '|')"; $global:LASTEXITCODE = $global:uvExit }

$hub = 'H:/repos/drover.deploy/hub'
$output = & $start -HubDir $hub -Uv fake-uv
if ($LASTEXITCODE -ne 0 -or $global:checks -ne 4 -or $global:sleeps -ne 3 -or
    ($output -join "`n").Replace('\', '/') -notmatch 'CWD:H:/repos/drover.deploy' -or
    $output -notcontains "UV:run|--project|$hub|python|-m|hub") { throw "delayed drive: $output" }

$global:checks = 0; $global:sleeps = 0; $global:readyAfter = 1000
# Capture the wrapper's stderr while keeping its exit code visible to this process.
$oldError = [Console]::Error
$errorText = [IO.StringWriter]::new()
[Console]::SetError($errorText)
try { $output = & $start -HubDir $hub -Uv fake-uv; $status = $LASTEXITCODE }
finally { [Console]::SetError($oldError) }
if ($status -eq 0 -or $global:checks -ne 61 -or $global:sleeps -ne 60 -or $output -or
    -not $errorText.ToString().Contains($hub) -or $errorText.ToString() -notmatch '60 seconds.*restart') {
    throw "timeout: status=$status checks=$global:checks sleeps=$global:sleeps error=$errorText"
}

$global:checks = 0; $global:sleeps = 0; $global:readyAfter = 0; $global:uvExit = 7
$output = & $start -HubDir $hub -Uv fake-uv
if ($LASTEXITCODE -ne 7 -or $global:checks -ne 1 -or $global:sleeps -ne 0) { throw 'launch exit status lost' }

function New-ScheduledTaskAction { param($Execute, $Argument, $WorkingDirectory) @{execute=$Execute; argument=$Argument; cwd=$WorkingDirectory} }
function New-ScheduledTaskTrigger { param([switch]$AtLogOn, $User) @{logon=$AtLogOn.IsPresent; user=$User} }
function New-ScheduledTaskSettingsSet { param($RestartCount, $RestartInterval, [switch]$StartWhenAvailable, [switch]$AllowStartIfOnBatteries, [switch]$DontStopIfGoingOnBatteries, $ExecutionTimeLimit, $MultipleInstances) @{count=$RestartCount; interval=$RestartInterval.TotalMinutes} }
function icacls { $global:acl = $args }
function Register-ScheduledTask { param($TaskName, $Action, $Trigger, $Settings, $Description, [switch]$Force) $global:task = @{name=$TaskName; action=$Action; trigger=$Trigger; settings=$Settings} }
$temp = Join-Path ([IO.Path]::GetTempPath()) ('drover-install-test-' + [guid]::NewGuid())
$oldLocal = $env:LOCALAPPDATA; $oldProfile = $env:USERPROFILE; $oldDomain = $env:USERDOMAIN; $oldUser = $env:USERNAME
try {
    $env:LOCALAPPDATA = Join-Path $temp 'local'; $env:USERPROFILE = $temp
    $env:USERDOMAIN = 'domain'; $env:USERNAME = 'owner'
    $hubDir = Join-Path $temp 'deploy with spaces/hub'; $uv = Join-Path $temp 'uv tools/uv.exe'; $state = Join-Path $temp 'state with spaces'
    & $install -HubDir $hubDir -Uv $uv -Root $state | Out-Null
    $wrapper = Join-Path $env:LOCALAPPDATA 'Drover/Start-DroverHub.ps1'
    if ((Get-Content -LiteralPath $wrapper -Raw) -cne (Get-Content -LiteralPath $start -Raw) -or
        $global:task.name -ne 'DroverHub' -or -not $global:task.trigger.logon -or $global:task.trigger.user -ne 'domain\owner' -or
        $global:task.settings.count -ne 999 -or $global:task.settings.interval -ne 1 -or
        $global:task.action.execute -ne 'cmd.exe' -or $global:task.action.cwd -ne $temp -or
        -not $global:task.action.argument.Contains('"' + $wrapper + '"') -or
        -not $global:task.action.argument.Contains('"' + $hubDir + '"') -or
        -not $global:task.action.argument.Contains('"' + $uv + '"') -or
        -not $global:task.action.argument.Contains('"' + $state + '\hub-out.log"') -or
        -not $global:task.action.argument.Contains('"' + $state + '\hub-err.log"') -or
        ($global:acl -join '|') -ne "$state|/inheritance:r|/grant:r|owner:(OI)(CI)F") { throw 'installer contract failed' }
}
finally {
    $env:LOCALAPPDATA = $oldLocal; $env:USERPROFILE = $oldProfile; $env:USERDOMAIN = $oldDomain; $env:USERNAME = $oldUser
    Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Output 'PASS: delayed drive, timeout, exit status, and scheduled task configuration'
