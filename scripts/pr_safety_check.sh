#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

BROKER_MODE_UPPER="$(printf '%s' "${BROKER_MODE:-${ALPACA_BROKER_MODE:-MOCK}}" | tr '[:lower:]' '[:upper:]')"
BROKER_MOCK_FLAG="${PR_SAFETY_BROKER_MOCK:-1}"

if [[ "$BROKER_MODE_UPPER" == "LIVE" && "$BROKER_MOCK_FLAG" != "1" ]]; then
  echo "PR_SAFETY_BLOCKED unsafe live broker mode requires PR_SAFETY_BROKER_MOCK=1" >&2
  exit 2
fi

if [[ "${1:-}" == "--validate-broker-mode-only" ]]; then
  echo "PR_SAFETY_OK broker_mode=${BROKER_MODE_UPPER} broker_mock=${BROKER_MOCK_FLAG}"
  exit 0
fi

PYTHONPATH=. pytest tests/test_dynamic_universe.py -v
PYTHONPATH=. pytest tests/test_live_cycle.py -v
PYTHONPATH=. pytest tests/test_capital_allocator_loop.py -v
PYTHONPATH=. pytest tests/test_capital_allocator.py -v
PYTHONPATH=. pytest tests/test_execution.py -v
PYTHONPATH=. pytest tests/test_allocation_profile.py -v

if compgen -G "data/dynamic_scan_history/*.json" >/dev/null; then
  PYTHONPATH=. python scripts/replay_live_cycle.py --date latest --user live_bot --broker-mock
else
  echo "PR_SAFETY_REPLAY_SKIPPED no data/dynamic_scan_history/*.json"
fi
