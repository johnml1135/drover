<#
Installs Drover Hub for the current user, in order:
  1. the configuration: runs `python -m drover init` (the setup wizard) when config.toml is missing
  2. the state directory (default %USERPROFILE%\.drover, from the config) with an owner-only ACL
  3. the hub token (<state dir>\hub-token), created only if missing, owner-only
  4. uv sync of the hub venv
  5. the per-user scheduled task 'DroverHub' (runs at logon, restarts on failure) via Install-DroverHub.ps1
  6. tailscale serve for the UI via Enable-TailscaleServe.ps1 (skipped when the config says hub.tailscale = false)
Does NOT need elevation. Prints each step; -WhatIf prints what would happen and changes nothing (it never starts the
wizard). -Defaults runs the wizard with --defaults; -Answers FILE passes --answers.
The Windows Firewall rules (Add-HubFirewallRule.ps1, Add-OllamaFirewallRule.ps1) are separate and need elevation.
Dispatch stays PAUSED after install; the owner turns it on in the website.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$Root,                       # the state directory (default: from the config; the wizard's first answer)
    [string]$HubDir = (Split-Path -Parent $PSScriptRoot),
    [string]$Uv = "$env:USERPROFILE\.local\bin\uv.exe",
    [switch]$SkipTailscale,
    [switch]$Defaults,
    [string]$Answers
)
if (-not $env:POWERSHELL_TELEMETRY_OPTOUT) { $env:POWERSHELL_TELEMETRY_OPTOUT = '1' }
if (-not $env:DOTNET_CLI_TELEMETRY_OPTOUT) { $env:DOTNET_CLI_TELEMETRY_OPTOUT = '1' }
if (-not $env:MSBUILDTELEMETRYOPTOUT) { $env:MSBUILDTELEMETRYOPTOUT = '1' }
if (-not $env:TESTINGPLATFORM_TELEMETRY_OPTOUT) { $env:TESTINGPLATFORM_TELEMETRY_OPTOUT = '1' }
if (-not $env:AVALONIA_TELEMETRY_OPTOUT) { $env:AVALONIA_TELEMETRY_OPTOUT = '1' }
if (-not $env:DOTNET_NOLOGO) { $env:DOTNET_NOLOGO = '1' }
$ErrorActionPreference = 'Stop'
$user = $env:USERNAME
$repoRoot = Split-Path -Parent $HubDir          # `drover` imports from here
$defaultHome = Join-Path $env:USERPROFILE '.drover'

# python -m drover <args> from the checkout, with uv's own Python (stdlib only, no project environment needed)
function Invoke-Drover([string[]]$DroverArgs) {
    Push-Location $repoRoot
    try {
        $env:PYTHONPATH = $repoRoot
        & $Uv run --no-project python -m drover @DroverArgs
        if ($LASTEXITCODE -ne 0) { throw "python -m drover $($DroverArgs -join ' ') failed ($LASTEXITCODE)" }
    } finally { Pop-Location }
}
function Get-DroverValue([string]$Key) {
    Push-Location $repoRoot
    try { $env:PYTHONPATH = $repoRoot; (& $Uv run --no-project python -m drover config --get $Key | Select-Object -Last 1) }
    finally { Pop-Location }
}

function Step([int]$n, [string]$text) { Write-Host ("[{0}/6] {1}" -f $n, $text) }

# Where config.toml is: -Root (a state dir you name), else $env:DROVER_HOME, else %USERPROFILE%\.drover
$configHome = if ($Root) { $Root } elseif ($env:DROVER_HOME) { $env:DROVER_HOME } else { $defaultHome }
$configPath = Join-Path $configHome 'config.toml'
Step 1 "configuration ($configPath)"
if (Test-Path $configPath) {
    Write-Host "      config exists; leaving it alone (re-run: python -m drover init)"
} elseif ($PSCmdlet.ShouldProcess($configPath, 'run the setup wizard: python -m drover init')) {
    $wizard = @('init')
    if ($Defaults) { $wizard += '--defaults' }
    if ($Answers) { $wizard += @('--answers', $Answers) }
    if ($Root) { $wizard += @('--state-dir', $Root) }
    Invoke-Drover $wizard
    if (-not (Test-Path $configPath)) {
        throw "The wizard did not write $configPath (you chose another state dir). Set DROVER_HOME to the state dir you chose, e.g. setx DROVER_HOME <dir>, open a new shell and run Install-Drover.ps1 again (or pass -Root <dir>)."
    }
}
$env:DROVER_HOME = $configHome      # so `python -m drover config --get` below reads this config

if (-not $Root) { $Root = if (Test-Path $Uv) { Get-DroverValue 'paths.state_dir' } else { $configHome } }
$tokenPath = Join-Path $Root 'hub-token'

Step 2 "state directory with an owner-only ACL ($Root)"
if ($PSCmdlet.ShouldProcess($Root, 'create directory and restrict ACL to the current user')) {
    New-Item -ItemType Directory -Force $Root | Out-Null
    icacls $Root /inheritance:r /grant:r "${user}:(OI)(CI)F" | Out-Null
}
if ($configHome -ne $defaultHome) {
    if ($PSCmdlet.ShouldProcess('DROVER_HOME', "set the user environment variable to $configHome so Drover finds its config")) {
        [Environment]::SetEnvironmentVariable('DROVER_HOME', $configHome, 'User')
    }
}

Step 3 "hub token ($tokenPath)"
if (Test-Path $tokenPath) {
    Write-Host "      token exists; leaving it alone"
} elseif ($PSCmdlet.ShouldProcess($tokenPath, 'create a random token file readable only by the current user')) {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $token = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    [IO.File]::WriteAllText($tokenPath, "$token`n")
    icacls $tokenPath /inheritance:r /grant:r "${user}:F" | Out-Null
}

Step 4 "uv sync of the hub venv ($HubDir)"
if (-not (Test-Path $Uv)) { Write-Warning "uv not found at $Uv; pass -Uv with its path" }
if ($PSCmdlet.ShouldProcess($HubDir, "$Uv sync --project")) {
    & $Uv sync --project $HubDir
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed ($LASTEXITCODE)" }
}

Step 5 "per-user hub and report-only disk schedules"
if ($PSCmdlet.ShouldProcess('DroverHub', 'register the scheduled task (Install-DroverHub.ps1)')) {
    & (Join-Path $PSScriptRoot 'Install-DroverHub.ps1') -HubDir $HubDir -Uv $Uv -Root $Root
}

if ($PSCmdlet.ShouldProcess('DroverDiskTracker and DroverDiskCleanup', 'register report-only disk schedules')) {
    Invoke-Drover @('disk-tracker', 'install')
}

Step 6 "tailscale serve for the UI"
$useTailscale = $true
if ((Test-Path $Uv) -and (Test-Path $configPath)) { $useTailscale = (Get-DroverValue 'hub.tailscale') -ne 'false' }
$uiPort = if ((Test-Path $Uv) -and (Test-Path $configPath)) { Get-DroverValue 'hub.ui_port' } else { '8765' }
if ($SkipTailscale) {
    Write-Host "      skipped (-SkipTailscale)"
} elseif (-not $useTailscale) {
    Write-Host "      skipped (hub.tailscale = false in the config)"
} elseif ($PSCmdlet.ShouldProcess('tailscale serve', "front http://127.0.0.1:$uiPort with tailnet HTTPS (Enable-TailscaleServe.ps1)")) {
    & (Join-Path $PSScriptRoot 'Enable-TailscaleServe.ps1') -Port $uiPort
}

Write-Host "Done. Start the hub with: Start-ScheduledTask -TaskName DroverHub"
Write-Host "Dispatch is PAUSED until the owner turns it on in the website (Health tab or header)."
