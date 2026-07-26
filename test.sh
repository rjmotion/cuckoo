#!/bin/bash
# Everything that runs without a camera. Run after every edit.
set -euo pipefail
cd "$(dirname "$0")"
# The wire itself lives in pyunifiwire; during development it is on the path
# rather than installed.
export PYTHONPATH="${PYTHONPATH:-}${PYTHONPATH:+:}$(cd ../pyunifiwire/src && pwd)"
echo "== mypy =="
python3 -m mypy . 2>&1 | tail -20
echo "== pytest =="
python3 -m pytest 2>&1 | tail -20
