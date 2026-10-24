$ErrorActionPreference = 'Stop'
# The gate runs both tiers; scripts/test.py runs every integration module and fails if any module fails.

$originalProjectEnvironment = [Environment]::GetEnvironmentVariable('UV_PROJECT_ENVIRONMENT', 'Process')
$originalTestJobs = [Environment]::GetEnvironmentVariable('DROVER_TEST_JOBS', 'Process')

$uvCommand = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue
if ($null -ne $uvCommand) {
    $uvPath = $uvCommand.Source
} elseif ($IsWindows) {
    $uvPath = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
} else {
    $uvPath = Join-Path $HOME '.local/bin/uv'
}

if ([string]::IsNullOrWhiteSpace($uvPath) -or -not (Test-Path -LiteralPath $uvPath -PathType Leaf)) {
    throw 'uv was not found on PATH or in the standard user-local .local/bin location.'
}

if ($null -ne $originalTestJobs) {
    if ($originalTestJobs -notmatch '^[0-9]+$' -or $originalTestJobs -notmatch '[1-9]') {
        throw 'DROVER_TEST_JOBS must be a positive integer.'
    }
    $testJobs = $originalTestJobs
} elseif ($IsWindows) {
    $testJobs = '8'
} else {
    $testJobs = '4'
}

$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$lockPath = Join-Path $repositoryRoot 'hub/uv.lock'
if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) {
    throw "Locked dependency file was not found: $lockPath"
}
$lockHash = (Get-FileHash -LiteralPath $lockPath -Algorithm SHA256).Hash.ToLowerInvariant()

if ($IsWindows) {
    $userCache = [Environment]::GetFolderPath('LocalApplicationData')
    if ([string]::IsNullOrWhiteSpace($userCache)) {
        throw 'Could not resolve the current Windows user local application-data directory.'
    }
    $environmentParent = Join-Path $userCache 'Drover/uv/project-environments/windows'
} elseif ($IsLinux) {
    if ([string]::IsNullOrWhiteSpace($HOME)) {
        throw 'Could not resolve the current WSL user home directory for the uv environment cache.'
    }
    $environmentParent = Join-Path $HOME '.cache/drover/uv/project-environments/linux'
} else {
    throw 'The CI test entrypoint supports native Windows and Linux/WSL only.'
}

$projectEnvironment = Join-Path $environmentParent "hub-$lockHash-python-3.13"
$uvExitCode = 1

try {
    [Environment]::SetEnvironmentVariable('UV_PROJECT_ENVIRONMENT', $projectEnvironment, 'Process')
    [Environment]::SetEnvironmentVariable('DROVER_TEST_JOBS', $testJobs, 'Process')

    $LASTEXITCODE = $null
    try {
        & $uvPath run --project hub --frozen --python 3.13 python scripts/test.py --tier integration
    } catch {
        # PowerShell can promote a native non-zero exit to a terminating error.
        # Keep uv's actual status in that case instead of replacing it with 1.
        if ($null -eq $LASTEXITCODE -or $LASTEXITCODE -eq 0) {
            throw
        }
    }

    if ($null -eq $LASTEXITCODE) {
        throw 'uv did not report an exit status.'
    }
    $uvExitCode = [int]$LASTEXITCODE
} finally {
    [Environment]::SetEnvironmentVariable('UV_PROJECT_ENVIRONMENT', $originalProjectEnvironment, 'Process')
    [Environment]::SetEnvironmentVariable('DROVER_TEST_JOBS', $originalTestJobs, 'Process')
}

exit $uvExitCode
