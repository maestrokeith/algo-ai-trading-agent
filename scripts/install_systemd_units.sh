#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="$ROOT/deploy/systemd"
UNIT_DST="/etc/systemd/system"
HEALTH_SCRIPT="$ROOT/scripts/check_algo_health.sh"

install_unit() {
  local name="$1"
  install -m 0644 "$UNIT_SRC/$name" "$UNIT_DST/$name"
}

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Run as root: sudo ./scripts/install_systemd_units.sh" >&2
  exit 2
fi

install_unit algo.service
install_unit algo-health-check.service
install_unit algo-health-check.timer
install_unit algo-boot-health.service

systemctl daemon-reload
systemctl enable algo.service
systemctl enable algo-health-check.timer
systemctl enable algo-boot-health.service

if command -v semanage >/dev/null 2>&1; then
  if semanage fcontext -l | grep -F "$HEALTH_SCRIPT" | grep -q "bin_t"; then
    restorecon -v "$HEALTH_SCRIPT" || true
  else
    echo "WARNING: persistent SELinux fcontext rule for $HEALTH_SCRIPT with bin_t was not found" >&2
  fi
else
  echo "WARNING: semanage unavailable; cannot verify persistent SELinux fcontext rule" >&2
fi

(cd "$ROOT" && ./bin/algo boot-health) || true

cat <<EOF

Verification commands:
  systemctl is-enabled algo.service
  systemctl is-active algo.service
  systemctl is-enabled algo-health-check.timer
  systemctl is-active algo-health-check.timer
  systemctl is-enabled algo-boot-health.service
  systemd-analyze verify $UNIT_DST/algo.service $UNIT_DST/algo-health-check.service $UNIT_DST/algo-health-check.timer $UNIT_DST/algo-boot-health.service
  ./bin/algo boot-health
  ./bin/algo boot-health --json
EOF
