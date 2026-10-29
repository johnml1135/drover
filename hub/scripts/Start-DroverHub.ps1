param(
    [Parameter(Mandatory = $true)][string]$HubDir,
    [Parameter(Mandatory = $true)][string]$Uv
)
$ErrorActionPreference = 'Stop'
for ($seconds = 0; $seconds -le 60; $seconds++) {
    if (Test-Path -LiteralPath $HubDir -PathType Container) {
        Set-Location -LiteralPath (Split-Path -Parent $HubDir)
        & $Uv run --project $HubDir python -m hub
        exit $LASTEXITCODE
    }
    if ($seconds -lt 60) { Start-Sleep -Seconds 1 }
}
[Console]::Error.WriteLine("DroverHub: $HubDir unavailable after 60 seconds; giving up for Task Scheduler restart.")
exit 1
