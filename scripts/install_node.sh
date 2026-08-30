#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
SUDO="${SUDO:-sudo}"
PY="${PYTHON:-python3}"
DRY_RUN=0
ENABLE_REPLAY=0
SKIP_TESTS=0
TRADING_USER="live_bot"

usage() {
  cat <<'EOF'
Usage: scripts/install_node.sh [--dry-run] [--enable-replay] [--skip-tests] [--user USER]

Validate and install a production AlgoSphere node.

  --dry-run        Print install/enable commands without changing systemd state.
  --enable-replay  Enable the optional post-close replay summary timer.
  --skip-tests     Skip the pytest smoke test.
  --user USER      Trading user to validate in config/users.yaml. Default: live_bot.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --enable-replay)
      ENABLE_REPLAY=1
      shift
      ;;
    --skip-tests)
      SKIP_TESTS=1
      shift
      ;;
    --user)
      TRADING_USER="${2:?--user requires a value}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

log() {
  echo "INSTALL_NODE $*"
}

run() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '%q ' "$@"
    printf '\n'
  else
    "$@"
  fi
}

require_file() {
  if [[ ! -f "$1" ]]; then
    echo "Missing required file: $1" >&2
    exit 1
  fi
}

require_dir() {
  if [[ ! -d "$1" ]]; then
    echo "Missing required directory: $1" >&2
    exit 1
  fi
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

log "checking dependencies"
require_command "$PY"
require_command systemctl
require_command install

log "validating repo at $ROOT"
require_file "$ROOT/bin/algo"
require_file "$ROOT/config/default.yaml"
require_file "$ROOT/config/users.yaml"
require_file "$ROOT/requirements.txt"
require_dir "$ROOT/scripts"
require_dir "$ROOT/deploy/systemd"

log "validating config and environment for user=$TRADING_USER"
ALGOSPHERE_ROOT="$ROOT" ALGOSPHERE_USER="$TRADING_USER" "$PY" - <<'PY'
from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

root = Path(os.environ["ALGOSPHERE_ROOT"])
user_id = os.environ["ALGOSPHERE_USER"]
users_path = root / "config" / "users.yaml"
env_path = root / ".env"

payload = yaml.safe_load(users_path.read_text(encoding="utf-8")) or {}
users = payload.get("users") or []
user = next((row for row in users if isinstance(row, dict) and row.get("id") == user_id), None)
if user is None:
    print(f"config/users.yaml does not define user {user_id!r}", file=sys.stderr)
    raise SystemExit(1)

env_keys = set(os.environ)
if env_path.exists():
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        env_keys.add(line.split("=", 1)[0].strip().removeprefix("export "))

missing = [
    key
    for key in (user.get("alpaca_key_env"), user.get("alpaca_secret_env"))
    if not key or key not in env_keys
]
if missing:
    print(
        f"Missing env vars for {user_id!r}: {', '.join(str(key) for key in missing)}. "
        "Set them in the environment or .env.",
        file=sys.stderr,
    )
    raise SystemExit(1)
PY

log "checking Python imports"
PYTHONPATH="$ROOT" "$PY" - <<'PY'
from __future__ import annotations

import src.config_loader
import src.user_manager
import src.app.live_cycle
import scripts.run_ops_workflow
PY

if [[ "$SKIP_TESTS" -eq 1 ]]; then
  log "skipping pytest smoke test"
else
  log "running pytest smoke test"
  PYTHONPATH="$ROOT" "$PY" -m pytest tests/test_ops_workflow.py tests/test_premarket_readiness.py -q
fi

log "collecting systemd units"
mapfile -t unit_files < <(
  find "$ROOT/deploy/systemd" -maxdepth 1 -type f \( -name '*.service' -o -name '*.timer' \) | sort
)
if [[ -f "$ROOT/deploy/systemd/algo.service" ]]; then
  log "algo.service template found and will be installed"
else
  log "algo.service template not present; install will include AlgoSphere timers/services only"
fi

timers=(
  algosphere-premarket.timer
  algosphere-ops-premarket-ready.timer
  algosphere-ops-startup-validation.timer
  algosphere-ops-research-metrics-begin.timer
  algosphere-ops-daily-summary.timer
  algosphere-ops-research-metrics-end.timer
  algosphere-ops-postmarket-analytics.timer
  algosphere-ops-research-feedback.timer
  algosphere-ops-weekly-research-feedback.timer
)
if [[ "$ENABLE_REPLAY" -eq 1 ]]; then
  timers+=(algosphere-ops-replay-summary.timer)
fi

log "installing systemd unit files"
run "$SUDO" install -m 0644 "${unit_files[@]}" "$SYSTEMD_DIR"/
run "$SUDO" systemctl daemon-reload
run "$SUDO" systemctl enable --now "${timers[@]}"

log "post-install verification"
run systemctl list-timers 'algosphere*'
for timer in "${timers[@]}"; do
  run systemctl is-enabled "$timer"
done

if [[ "$ENABLE_REPLAY" -ne 1 ]]; then
  log "optional replay timer installed but not enabled; rerun with --enable-replay to enable it"
fi

log "complete"
