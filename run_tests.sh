#!/bin/bash
# run_tests.sh
# This script runs the automated test suite.

# Activate the virtual environment if it exists, otherwise use system python
if [ -d "venv" ]; then
    source venv/bin/activate
fi

# Ensure pytest is installed
if ! command -v pytest &> /dev/null; then
    echo "Installing pytest..."
    pip install -r requirements.txt
fi

echo "Running CalorieTracker Test Suite..."
PYTHONPATH=. pytest tests/ -v
