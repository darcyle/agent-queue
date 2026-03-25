#!/bin/bash
set -e

echo "========================================"
echo "Running Agent Queue Test Suite"
echo "========================================"

# Run pytest with verbose output
python -m pytest tests/ -v --tb=short 2>&1 | tee /tmp/agent-queue-test-output.txt

EXIT_CODE=$?

echo ""
echo "========================================"
echo "Test Suite Complete"
echo "Exit Code: $EXIT_CODE"
echo "========================================"

exit $EXIT_CODE
