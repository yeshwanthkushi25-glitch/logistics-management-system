#!/usr/bin/env bash
# =============================================================================
# Logistics Information Management System (Logistics IMS)
# Unix/Linux/macOS Bash Automated Test Runner
# Executes test suites with verbosity and exit code propagation
# =============================================================================

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$PROJECT_ROOT"

echo "================================================================="
echo "  Logistics IMS - Automated Test Runner (Bash)"
echo "================================================================="

if command -v python3 &>/dev/null; then
    PY_CMD="python3"
elif command -v python &>/dev/null; then
    PY_CMD="python"
else
    echo "Error: Python was not found on PATH."
    exit 1
fi

TARGET="${1:-tests/}"
shift || true

echo "Executing: $PY_CMD -m pytest $TARGET -v $@"
echo "-----------------------------------------------------------------"

$PY_CMD -m pytest "$TARGET" -v "$@"
TEST_EXIT=$?

echo "-----------------------------------------------------------------"
if [ $TEST_EXIT -eq 0 ]; then
    echo -e "\033[1;32mAll tests PASSED successfully.\033[0m"
else
    echo -e "\033[1;31mTest suite FAILED with exit code: $TEST_EXIT\033[0m"
fi

exit $TEST_EXIT
