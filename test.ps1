# =============================================================================
# Logistics Information Management System (Logistics IMS)
# Windows PowerShell Automated Test Runner
# Executes test suites with verbosity and exit code propagation
# =============================================================================

[CmdletBinding()]
param(
    [string]$Target = "tests/",
    [switch]$Coverage = $false,
    [switch]$FailFast = $false,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs
)

$ProjectRoot = $PSScriptRoot
$env:PYTHONPATH = $ProjectRoot

Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host "  Logistics IMS - Automated Test Runner (PowerShell)" -ForegroundColor Cyan
Write-Host "=================================================================" -ForegroundColor Cyan

# 1. Locate Python
$PythonCmd = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $PythonCmd) {
    $PythonCmd = (Get-Command python3 -ErrorAction SilentlyContinue)
}
if (-not $PythonCmd) {
    Write-Error "Python executable not found on PATH."
    exit 1
}

# 2. Build Arguments
$PytestArgs = @("-m", "pytest", $Target, "-v")

if ($FailFast) {
    $PytestArgs += "-x"
}

if ($Coverage) {
    $PytestArgs += @("--cov=app", "--cov-report=term-missing")
}

if ($ExtraArgs) {
    $PytestArgs += $ExtraArgs
}

Write-Host "Executing command: python $($PytestArgs -join ' ')" -ForegroundColor Yellow
Write-Host "-----------------------------------------------------------------" -ForegroundColor DarkGray

& $PythonCmd $PytestArgs
$TestExitCode = $LASTEXITCODE

Write-Host "-----------------------------------------------------------------" -ForegroundColor DarkGray
if ($TestExitCode -eq 0) {
    Write-Host "All tests PASSED successfully." -ForegroundColor Green
} else {
    Write-Host "Test suite FAILED with exit code: $TestExitCode" -ForegroundColor Red
}

exit $TestExitCode
