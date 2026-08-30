#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
SUDO="${SUDO:-sudo}"

usage() {
  cat <<'EOF'
Usage: scripts/install_intraday_health_timer.sh [--dry-run]

Install and enable the read-only intraday health systemd timer.
EOF
}

DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
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

run() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '%q ' "$@"
    printf '\n'
  else
    "$@"
  fi
}

run "$SUDO" install -m 0644 "$ROOT/systemd/intraday-health.service" "$ROOT/systemd/intraday-health.timer" "$SYSTEMD_DIR/"
run "$SUDO" systemctl daemon-reload
run "$SUDO" systemctl enable --now intraday-health.timer

cat <<'EOF'
Verification commands:
systemctl status intraday-health.timer
systemctl list-timers | grep intraday-health
journalctl -u intraday-health.service --since "1 hour ago" --no-pager
cat data/intraday_health/$(date +%F)/live_intraday_health.json
EOF
