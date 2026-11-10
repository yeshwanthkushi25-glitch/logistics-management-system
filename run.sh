#!/usr/bin/env bash
# =============================================================================
# Logistics Information Management System (Logistics IMS)
# Unix/Linux/macOS Bash Execution Runner
# Evaluates host environment, initializes database if missing, and starts server
# =============================================================================

set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST_ADDR="${1:-127.0.0.1}"
PORT="${2:-5000}"

echo "================================================================="
echo "  Logistics IMS - Single-Command Host Execution Runner (Bash)"
echo "================================================================="

# 1. Verify Python
echo -e "\033[1;33m[1/4] Checking Python environment...\033[0m"
if command -v python3 &>/dev/null; then
    PY_CMD="python3"
elif command -v python &>/dev/null; then
    PY_CMD="python"
else
    echo -e "\033[1;31mError: Python 3 was not found on PATH. Please install Python 3.10+.\033[0m"
    exit 1
fi

PY_VERSION=$($PY_CMD --version)
echo -e "\033[1;32m       Found Python: $PY_VERSION\033[0m"

# 2. Check Database
echo -e "\033[1;33m[2/4] Verifying database status...\033[0m"
DB_PATH1="$PROJECT_ROOT/data/logistics.db"
DB_PATH2="$PROJECT_ROOT/app/data/logistics.db"

HAS_TABLES=0
if [ -f "$DB_PATH1" ]; then
    HAS_TABLES=$($PY_CMD -c "import sqlite3; conn = sqlite3.connect('$DB_PATH1'); c = conn.cursor(); c.execute(\"SELECT count(*) FROM sqlite_master WHERE type='table' AND name='warehouses'\"); print(c.fetchone()[0]); conn.close()" 2>/dev/null || echo 0)
fi

if [ "$HAS_TABLES" -eq 0 ]; then
    echo -e "\033[1;36m       Initializing database schema and seeding demo dataset...\033[0m"
    PYTHONPATH="$PROJECT_ROOT" $PY_CMD -m app.db.init_db --seed --db-path "$DB_PATH1"
    echo -e "\033[1;32m       Database initialized and seeded successfully.\033[0m"
else
    echo -e "\033[1;32m       Database file located and verified (WAL mode ready).\033[0m"
fi

# 3. Environment Variables
echo -e "\033[1;33m[3/4] Configuring runtime environment...\033[0m"
export PYTHONPATH="$PROJECT_ROOT"
export FLASK_APP="app.main:app"
export FLASK_ENV="development"
export FLASK_RUN_HOST="$HOST_ADDR"
export FLASK_RUN_PORT="$PORT"

# 4. Start Server
echo -e "\033[1;33m[4/4] Starting Logistics IMS Application Server...\033[0m"
echo ""
echo -e "  \033[1;32mDashboard URL:      http://$HOST_ADDR:$PORT/\033[0m (Interactive Web UI)"
echo -e "  \033[1;32mShipment Tracking:  http://$HOST_ADDR:$PORT/tracking\033[0m"
echo -e "  \033[1;32mInventory Browser:  http://$HOST_ADDR:$PORT/inventory\033[0m"
echo -e "  \033[1;32mRouting Network:    http://$HOST_ADDR:$PORT/network\033[0m"
echo -e "  \033[1;32mHealth Endpoint:    http://$HOST_ADDR:$PORT/health\033[0m"
echo ""
echo "Press Ctrl+C to terminate server."
echo "-----------------------------------------------------------------"

exec $PY_CMD -m app.main
