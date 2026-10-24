$ErrorActionPreference = 'Stop'
# Task checks precheck changed integration modules, then run the complete default tier; full runs all integration modules.

$originalProjectEnvironment = [Environment]::GetEnvironmentVariable('UV_PROJECT_ENVIRONMENT', 'Process')
$originalTestJobs = [Environment]::GetEnvironmentVariable('DROVER_TEST_JOBS', 'Process')
$originalPath = [Environment]::GetEnvironmentVariable('PATH', 'Process')
$originalProjectEnvironmentPresent = [Environment]::GetEnvironmentVariables('Process').Contains('UV_PROJECT_ENVIRONMENT')
$originalTestJobsPresent = [Environment]::GetEnvironmentVariables('Process').Contains('DROVER_TEST_JOBS')
$originalPathPresent = [Environment]::GetEnvironmentVariables('Process').Contains('PATH')

function Get-ChangedTestModules {
    param([string]$Json)

    if ([string]::IsNullOrWhiteSpace($Json)) {
        return [pscustomobject]@{ IsValid = $false; Modules = @() }
    }

    try {
        $changedFiles = ConvertFrom-Json -InputObject $Json -NoEnumerate -ErrorAction Stop
    } catch {
        return [pscustomobject]@{ IsValid = $false; Modules = @() }
    }

    if ($changedFiles -isnot [System.Array]) {
        return [pscustomobject]@{ IsValid = $false; Modules = @() }
    }

    $modules = [System.Collections.Generic.List[string]]::new()
    foreach ($filePath in $changedFiles) {
        if ($filePath -isnot [string] -or [string]::IsNullOrEmpty($filePath) -or
            $filePath.Contains('\') -or $filePath.Contains([char]0) -or
            [System.IO.Path]::IsPathRooted($filePath) -or $filePath -match '^[A-Za-z]:' -or
            $filePath -match '(^|/)\.{1,2}(/|$)' -or $filePath -match '(^/|/$|//)') {
            return [pscustomobject]@{ IsValid = $false; Modules = @() }
        }

        if ($filePath -cmatch '^tests/test_.*\.py$') {
            if ($filePath -cnotmatch '^tests/test_[A-Za-z0-9_]*\.py$') {
                return [pscustomobject]@{ IsValid = $false; Modules = @() }
            }

            $modulePath = Join-Path $repositoryRoot $filePath
            if (Test-Path -LiteralPath $modulePath -PathType Leaf) {
                $moduleName = 'tests.' + [System.IO.Path]::GetFileNameWithoutExtension($filePath)
                if (-not $modules.Contains($moduleName)) {
                    $modules.Add($moduleName)
                }
            }
        }
    }

    $modules.Sort([System.StringComparer]::Ordinal)
    return [pscustomobject]@{ IsValid = $true; Modules = $modules.ToArray() }
}

function Restore-ProcessEnvironmentVariable {
    param([string]$Name, [string]$Value, [bool]$WasPresent)

    if ($WasPresent) {
        [Environment]::SetEnvironmentVariable($Name, $Value, 'Process')
    } else {
        Remove-Item -LiteralPath "Env:$Name" -ErrorAction SilentlyContinue
    }
}

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
$uvArguments = @('run', '--project', 'hub', '--frozen', '--python', '3.13', 'python', 'scripts/test.py')
$ciKind = [Environment]::GetEnvironmentVariable('DROVER_CI_KIND', 'Process')
$changedTestModules = @()
$runIntegration = $true
$runDefault = $false

if ($ciKind -ceq 'checks') {
    $changedMetadata = Get-ChangedTestModules -Json ([Environment]::GetEnvironmentVariable('DROVER_CHANGED_FILES', 'Process'))
    if (-not $changedMetadata.IsValid) {
        Write-Warning 'DROVER_CHANGED_FILES is absent or malformed; running the complete integration tier.'
    } else {
        $changedTestModules = @($changedMetadata.Modules)
        if ($changedTestModules.Count -gt 0) {
            $runDefault = $true
        } else {
            $runIntegration = $false
            $runDefault = $true
        }
    }
}

$uvInvocations = [System.Collections.Generic.List[object]]::new()
if ($runIntegration) {
    $integrationArguments = @($uvArguments) + @('--tier', 'integration')
    if ($runDefault) {
        $integrationArguments += $changedTestModules
    }
    $uvInvocations.Add([string[]]$integrationArguments)
}
if ($runDefault) {
    $uvInvocations.Add([string[]](@($uvArguments) + @('--tier', 'default')))
}

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

    foreach ($arguments in $uvInvocations) {
        $LASTEXITCODE = $null
        try {
            & $uvPath @arguments
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
        if ($uvExitCode -ne 0) {
            break
        }
    }
} finally {
    Restore-ProcessEnvironmentVariable 'UV_PROJECT_ENVIRONMENT' $originalProjectEnvironment $originalProjectEnvironmentPresent
    Restore-ProcessEnvironmentVariable 'DROVER_TEST_JOBS' $originalTestJobs $originalTestJobsPresent
    Restore-ProcessEnvironmentVariable 'PATH' $originalPath $originalPathPresent
}

if ($MyInvocation.InvocationName -eq '.') {
    $global:LASTEXITCODE = $uvExitCode
    return
}

exit $uvExitCode
