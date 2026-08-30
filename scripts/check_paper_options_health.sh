#!/usr/bin/env bash
# Paper-only options stability check. Read-only: no trading, deploy, or restart.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${ALGO_REPO_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"

env_name="PAPER"
required_stable_ticks="${PAPER_OPTIONS_REQUIRED_STABLE_TICKS:-3}"
state_dir="${PAPER_OPTIONS_HEALTH_STATE_DIR:-$ROOT/data/paper_options_health}"
log_path="${PAPER_OPTIONS_LOG_PATH:-}"
symbol="${PAPER_OPTIONS_HEALTH_SYMBOL:-QQQ}"
user_id="${PAPER_OPTIONS_HEALTH_USER:-paper_bot}"

usage() {
  cat <<'USAGE'
Usage: scripts/check_paper_options_health.sh [--env paper] [--required-stable-ticks N] [--state-dir DIR] [--log-file PATH]

Read-only paper-options health check. Live mode is rejected.
USAGE
}

say() {
  printf '%s\n' "$*"
}

portable_mktemp() {
  local name="$1"
  local suffix="${2:-}"
  local tmpdir="${TMPDIR:-/tmp}"
  local path final_path
  tmpdir="${tmpdir%/}"
  path="$(mktemp "${tmpdir}/${name}.XXXXXX")"
  if [ -n "$suffix" ]; then
    final_path="${path}${suffix}"
    mv "$path" "$final_path"
    printf '%s\n' "$final_path"
  else
    printf '%s\n' "$path"
  fi
}

add_issue() {
  local issue="$1"
  issues+=("$issue")
  say "PAPER_OPTIONS_HEALTH issue=${issue}"
}

date_day() {
  if [ -n "${ALGO_HEALTH_DATE:-}" ]; then
    printf '%s\n' "$ALGO_HEALTH_DATE"
  else
    date +"%Y-%m-%d"
  fi
}

latest_paper_log() {
  python - "$ROOT" <<'PY' 2>/dev/null || true
from pathlib import Path
from datetime import date
import sys
import os

root = Path(sys.argv[1])
day = os.environ.get("ALGO_HEALTH_DATE") or date.today().isoformat()
path = root / "data" / "review" / day / "paper_full.log"
path.parent.mkdir(parents=True, exist_ok=True)
if path.is_file():
    print(path)
PY
}

count_pattern() {
  local pattern="$1"
  local file="$2"
  if [ -f "$file" ]; then
    { grep -Eai "$pattern" "$file" || true; } | wc -l | tr -d ' '
  else
    echo "0"
  fi
}

count_actionable_critical_options_errors() {
  local file="$1"
  if [ -f "$file" ]; then
    {
      grep -Eai 'Traceback|PAPER_OPTIONS_DIAGNOSTICS_FAILED|CRITICAL|FATAL|uncaught.*option|option.*uncaught|record_option_entry_exception' "$file" \
        | grep -Ev '(^PAPER_OPTIONS_HEALTH |^PAPER_OPTIONS_STABILIZATION |^### PAPER OPTIONS LOG: |fingerprint=paper-options:critical_options_errors|PAPER_OPTIONS \[PAPER\] unstable: critical_options_errors|POSTFIX \[PAPER_OPTIONS\].*critical_options_errors)' \
        || true
    } | wc -l | tr -d ' '
  else
    echo "0"
  fi
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --env)
      case "${2:-}" in
        paper|PAPER) env_name="PAPER" ;;
        live|LIVE)
          echo "check_paper_options_health is paper-only; live is rejected" >&2
          exit 2
          ;;
        *)
          echo "--env requires paper" >&2
          exit 2
          ;;
      esac
      shift 2
      ;;
    --required-stable-ticks)
      required_stable_ticks="${2:-}"
      if ! [[ "$required_stable_ticks" =~ ^[0-9]+$ ]] || [ "$required_stable_ticks" -lt 1 ]; then
        echo "--required-stable-ticks requires a positive integer" >&2
        exit 2
      fi
      shift 2
      ;;
    --state-dir)
      state_dir="${2:-}"
      [ -n "$state_dir" ] || {
        echo "--state-dir requires a path" >&2
        exit 2
      }
      shift 2
      ;;
    --log-file)
      log_path="${2:-}"
      [ -n "$log_path" ] || {
        echo "--log-file requires a path" >&2
        exit 2
      }
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

cd "$ROOT"

issues=()
mkdir -p "$state_dir"
stable_file="$state_dir/stable_ticks"
combined_file="$(portable_mktemp "paper_options_health_combined" ".log")"
diag_file="$(portable_mktemp "paper_options_health_diag" ".log")"

diag_rc=0
if [ "${PAPER_OPTIONS_HEALTH_SKIP_DIAGNOSTICS:-0}" = "1" ]; then
  echo "diagnostics skipped by PAPER_OPTIONS_HEALTH_SKIP_DIAGNOSTICS" >"$diag_file"
else
  if [ ! -x "$ROOT/bin/algo" ]; then
    diag_rc=127
    echo "bin/algo missing or not executable" >"$diag_file"
  else
    set +e
    "$ROOT/bin/algo" paper-options-diagnostics --user "$user_id" --symbol "$symbol" >"$diag_file" 2>&1
    diag_rc=$?
    set -e
  fi
fi

cat "$diag_file" >"$combined_file"
if [ -z "$log_path" ]; then
  log_path="$(latest_paper_log)"
fi
if [ -n "$log_path" ] && [ -f "$log_path" ]; then
  {
    echo
    echo "### PAPER OPTIONS LOG: $log_path"
    tail -n "${PAPER_OPTIONS_HEALTH_LOG_LINES:-400}" "$log_path"
  } >>"$combined_file"
fi

if [ "$diag_rc" -ne 0 ]; then
  add_issue "diagnostics_failed"
fi

if ! grep -Eaiq 'PASS paper options diagnostics|OPTIONS_CONFIG .*enabled=true.*mode=paper_only|OPTION_PIPELINE_STAGE .*options_route' "$combined_file"; then
  add_issue "paper_options_route_not_confirmed"
fi

if ! grep -Eaiq 'OPTION_SIGNAL|PASS paper options diagnostics|OPTION_PIPELINE_STAGE .*entry_eval_allowed' "$combined_file"; then
  add_issue "option_signal_generation_missing"
fi

if ! grep -Eaiq 'OPTION_CHAIN_LOADED|PASS paper options diagnostics|chain_source=mock|chain_source=broker' "$combined_file"; then
  add_issue "option_chain_path_missing"
fi

if ! grep -Eaiq 'OPTION_ORDER_INTENT|OPTION_ORDER_SUBMITTED|OPTION_POSITION_OPENED|OPTION_ENTRY_BLOCKED|OPTION_ROUTE_SKIPPED|OPTION_BEST_REJECTED|OPTION_SELECTED|PASS paper options diagnostics' "$combined_file"; then
  add_issue "option_order_or_valid_skip_missing"
fi

if grep -Eaiq 'LIVE_OPTION|live options execution|live option order|options_live_execution|mode=live.*OPTION_ORDER|OPTION_ORDER_SUBMITTED.*live' "$combined_file"; then
  add_issue "live_options_execution_attempted"
fi

critical_count="$(count_actionable_critical_options_errors "$combined_file")"
if [ "${critical_count:-0}" -gt 0 ]; then
  add_issue "critical_options_errors"
fi

unknown_count="$(count_pattern 'unknown option rejection|unknown_option|reject_reason=unknown|reason=unknown' "$combined_file")"
if [ "${unknown_count:-0}" -gt "${PAPER_OPTIONS_UNKNOWN_REASON_MAX:-1}" ]; then
  add_issue "repeated_unknown_option_rejection_reason"
fi

if [ "${#issues[@]}" -eq 0 ]; then
  previous="0"
  if [ -f "$stable_file" ]; then
    previous="$(cat "$stable_file" 2>/dev/null || echo 0)"
  fi
  if ! [[ "$previous" =~ ^[0-9]+$ ]]; then
    previous=0
  fi
  stable_ticks=$((previous + 1))
  printf '%s\n' "$stable_ticks" >"$stable_file"
  say "PAPER_OPTIONS_HEALTH status=healthy"
else
  stable_ticks=0
  printf '0\n' >"$stable_file"
  say "PAPER_OPTIONS_HEALTH status=unhealthy"
fi

say "PAPER_OPTIONS_HEALTH stable_ticks=${stable_ticks}"
say "PAPER_OPTIONS_HEALTH required_stable_ticks=${required_stable_ticks}"
say "PAPER_OPTIONS_HEALTH diagnostics_exit_code=${diag_rc}"
say "PAPER_OPTIONS_HEALTH log=${log_path:-none}"

if [ "${#issues[@]}" -eq 0 ]; then
  exit 0
fi
exit 1
