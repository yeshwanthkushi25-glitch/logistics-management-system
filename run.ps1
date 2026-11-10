# =============================================================================
# Logistics Information Management System (Logistics IMS)
# Windows PowerShell Execution Runner
# Evaluates host environment, initializes database if missing, and starts server
# =============================================================================

[CmdletBinding()]
param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 5000,
    [switch]$RebuildDb = $false
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot

Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host "  Logistics IMS - Single-Command Host Execution Runner (PowerShell)" -ForegroundColor Cyan
Write-Host "=================================================================" -ForegroundColor Cyan

# 1. Verify Python Installation
Write-Host "[1/4] Checking Python environment..." -ForegroundColor Yellow
$PythonCmd = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $PythonCmd) {
    $PythonCmd = (Get-Command python3 -ErrorAction SilentlyContinue)
}
if (-not $PythonCmd) {
    Write-Error "Python 3.10+ executable was not found on PATH. Please install Python or activate your virtual environment."
    exit 1
}

$PyVersion = & $PythonCmd --version 2>&1
Write-Host "       Found Python: $PyVersion ($($PythonCmd.Source))" -ForegroundColor Green

# 2. Check Database Status & Initialize if Missing
Write-Host "[2/4] Verifying database status..." -ForegroundColor Yellow
$DbPath1 = Join-Path $ProjectRoot "data\logistics.db"
$DbPath2 = Join-Path $ProjectRoot "app\data\logistics.db"

$HasTables = $false
if (Test-Path $DbPath1) {
    $TableCount = & $PythonCmd -c "import sqlite3; conn = sqlite3.connect(r'$DbPath1'); c = conn.cursor(); c.execute('SELECT count(*) FROM sqlite_master WHERE type=\'table\' AND name=\'warehouses\''); print(c.fetchone()[0]); conn.close()" 2>$null
    if ($TableCount -and [int]$TableCount -gt 0) {
        $HasTables = $true
    }
}

$DbNeedsInit = $RebuildDb -or (-not $HasTables)

if ($DbNeedsInit) {
    Write-Host "       Initializing database schema and seeding demo dataset..." -ForegroundColor Cyan
    $env:PYTHONPATH = $ProjectRoot
    & $PythonCmd -m app.db.init_db --seed --db-path "$DbPath1"
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Database initialization failed with exit code $LASTEXITCODE."
        exit $LASTEXITCODE
    }
    Write-Host "       Database initialized and seeded successfully." -ForegroundColor Green
} else {
    Write-Host "       Database file located and verified (WAL mode ready)." -ForegroundColor Green
}

# 3. Configure Runtime Environment Variables
Write-Host "[3/4] Configuring runtime environment..." -ForegroundColor Yellow
$env:PYTHONPATH = $ProjectRoot
$env:FLASK_APP = "app.main:app"
$env:FLASK_ENV = "development"
$env:FLASK_RUN_HOST = $HostAddress
$env:FLASK_RUN_PORT = $Port

Write-Host "       PYTHONPATH: $env:PYTHONPATH" -ForegroundColor DarkGray
Write-Host "       FLASK_APP:  $env:FLASK_APP" -ForegroundColor DarkGray

# 4. Start Application Server
Write-Host "[4/4] Starting Logistics IMS Application Server..." -ForegroundColor Yellow
Write-Host ""
Write-Host "  Dashboard URL:      http://$HostAddress`:$Port/" -ForegroundColor Green -NoNewline
Write-Host "  (Interactive Web UI)" -ForegroundColor DarkGray
Write-Host "  Shipment Tracking:  http://$HostAddress`:$Port/tracking" -ForegroundColor Green
Write-Host "  Inventory Browser:  http://$HostAddress`:$Port/inventory" -ForegroundColor Green
Write-Host "  Routing Network:    http://$HostAddress`:$Port/network" -ForegroundColor Green
Write-Host "  Health Endpoint:    http://$HostAddress`:$Port/health" -ForegroundColor Green
Write-Host ""
Write-Host "Press Ctrl+C to terminate server." -ForegroundColor DarkYellow
Write-Host "-----------------------------------------------------------------" -ForegroundColor DarkGray

try {
    & $PythonCmd -m app.main
} catch {
    Write-Host "`nServer terminated." -ForegroundColor DarkYellow
}
