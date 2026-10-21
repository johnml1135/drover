$ErrorActionPreference = 'Stop'
# The gate runs both tiers; scripts/test.py runs the test modules in parallel and fails if any module fails.
$python = if ($IsWindows) { 'python' } else { 'python3' }
& $python scripts/test.py --tier integration
exit $LASTEXITCODE
