#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
SUDO="${SUDO:-sudo}"
DRY_RUN=0
ENABLE_REPLAY=0

usage() {
  cat <<'EOF'
Usage: scripts/install_ops_timers.sh [--dry-run] [--enable-replay]

Install and enable AlgoSphere systemd timers for daily read-only operations.

  --dry-run        Print the commands without changing systemd state.
  --enable-replay  Enable the optional post-close replay summary timer.
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

run() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '%q ' "$@"
    printf '\n'
  else
    "$@"
  fi
}

run "$SUDO" install -m 0644 "$ROOT"/deploy/systemd/*.service "$ROOT"/deploy/systemd/*.timer "$SYSTEMD_DIR"/
run "$SUDO" systemctl daemon-reload
run "$SUDO" systemctl enable --now "${timers[@]}"

if [[ "$ENABLE_REPLAY" -ne 1 ]]; then
  echo "Optional replay timer installed but not enabled. Re-run with --enable-replay to enable algosphere-ops-replay-summary.timer."
fi
