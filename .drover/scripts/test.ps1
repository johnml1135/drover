$ErrorActionPreference = 'Stop'
# The gate runs both tiers; scripts/test.py runs every integration module and fails if any module fails.

$originalProjectEnvironment = [Environment]::GetEnvironmentVariable('UV_PROJECT_ENVIRONMENT', 'Process')
$originalTestJobs = [Environment]::GetEnvironmentVariable('DROVER_TEST_JOBS', 'Process')
$originalPath = [Environment]::GetEnvironmentVariable('PATH', 'Process')

$uvCommand = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
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
$nodePathToPrepend = $null

if ($IsLinux -and $null -eq (Get-Command node -CommandType Application -ErrorAction SilentlyContinue)) {
    $nvmRoot = [Environment]::GetEnvironmentVariable('NVM_DIR', 'Process')
    if ([string]::IsNullOrWhiteSpace($nvmRoot)) {
        if ([string]::IsNullOrWhiteSpace($HOME)) {
            throw 'Node.js was not found on PATH, and the current user home directory could not be resolved for nvm.'
        }
        $nvmRoot = Join-Path $HOME '.nvm'
    }

    $nvmScript = Join-Path $nvmRoot 'nvm.sh'
    if (-not (Test-Path -LiteralPath $nvmScript -PathType Leaf)) {
        throw "Node.js was not found on PATH, and nvm was not available at '$nvmRoot'."
    }

    $bashCommand = Get-Command bash -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $bashCommand) {
        throw 'Node.js was not found on PATH, and bash is unavailable to resolve the configured nvm default.'
    }

    $nvmExitCode = 1
    $nodeDefaultPath = $null
    try {
        $LASTEXITCODE = $null
        $nvmResult = & $bashCommand.Source --noprofile --norc -c '. "$1/nvm.sh" --no-use; nvm which default' -- $nvmRoot 2>$null
        if ($null -ne $LASTEXITCODE) {
            $nvmExitCode = [int]$LASTEXITCODE
        }
        $nodeDefaultPath = (@($nvmResult) -join [Environment]::NewLine).Trim()
    } catch {
        if ($null -ne $LASTEXITCODE) {
            $nvmExitCode = [int]$LASTEXITCODE
        }
    }

    if ($nvmExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($nodeDefaultPath)) {
        throw "Node.js was not found on PATH, and the configured nvm default could not be resolved under '$nvmRoot'."
    }
    if (-not (Test-Path -LiteralPath $nodeDefaultPath -PathType Leaf)) {
        throw "The configured nvm default did not resolve to an installed Node.js file: '$nodeDefaultPath'."
    }

    $nodePreflightExitCode = 1
    try {
        $LASTEXITCODE = $null
        & $nodeDefaultPath --version *> $null
        if ($null -ne $LASTEXITCODE) {
            $nodePreflightExitCode = [int]$LASTEXITCODE
        }
    } catch {
        if ($null -ne $LASTEXITCODE) {
            $nodePreflightExitCode = [int]$LASTEXITCODE
        }
    }
    if ($nodePreflightExitCode -ne 0) {
        throw "The installed Node.js resolved from the configured nvm default could not run successfully: '$nodeDefaultPath'."
    }

    $nodePathToPrepend = Split-Path -Parent $nodeDefaultPath
}

try {
    [Environment]::SetEnvironmentVariable('UV_PROJECT_ENVIRONMENT', $projectEnvironment, 'Process')
    [Environment]::SetEnvironmentVariable('DROVER_TEST_JOBS', $testJobs, 'Process')
    if ($null -ne $nodePathToPrepend) {
        $pathSeparator = [System.IO.Path]::PathSeparator
        if ([string]::IsNullOrEmpty($originalPath)) {
            $testPath = $nodePathToPrepend
        } else {
            $testPath = "$nodePathToPrepend$pathSeparator$originalPath"
        }
        [Environment]::SetEnvironmentVariable('PATH', $testPath, 'Process')
    }

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
    [Environment]::SetEnvironmentVariable('PATH', $originalPath, 'Process')
}

exit $uvExitCode
