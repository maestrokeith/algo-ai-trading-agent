#!/usr/bin/env bash
# Read-only live stabilization health check. Never restarts, deploys, or trades.

set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${ALGO_REPO_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
SERVICE="${ALGO_LIVE_SERVICE:-algo.service}"
SINCE="${ALGO_LIVE_HEALTH_JOURNAL_SINCE:-30 minutes ago}"
REQUIRED_STABLE_TICKS="${ALGO_LIVE_REQUIRED_STABLE_TICKS:-3}"
STABLE_STATE_FILE="${ALGO_LIVE_STABLE_STATE_FILE:-/tmp/algo_live_stable_ticks}"
PREMARKET_MAX_AGE_MINUTES="${ALGO_LIVE_PREMARKET_MAX_AGE_MINUTES:-720}"

dry_run=0
env_name=""
issues=()
details=()
service_state="unknown"
broker_result="not_run"
premarket_result="not_run"
latest_logs=""

usage() {
  cat <<'USAGE'
Usage: scripts/check_live_health.sh --env live [--dry-run]
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run)
      dry_run=1
      shift
      ;;
    --env)
      case "${2:-}" in
        live|LIVE) env_name="LIVE" ;;
        paper|PAPER)
          echo "check_live_health rejects --env paper" >&2
          exit 2
          ;;
        *)
          echo "--env requires live" >&2
          exit 2
          ;;
      esac
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

if [ "$env_name" != "LIVE" ]; then
  usage >&2
  exit 2
fi

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

portable_mktemp() {
  local prefix="$1"
  local suffix="${2:-}"
  local tmpdir="${TMPDIR:-/tmp}"
  local path
  path="$(mktemp -t "${prefix}.XXXXXX" 2>/dev/null)" || path="$(mktemp "${tmpdir%/}/${prefix}.XXXXXX" 2>/dev/null)"
  if [ -n "$suffix" ]; then
    mv "$path" "${path}${suffix}"
    path="${path}${suffix}"
  fi
  printf '%s\n' "$path"
}

add_issue() {
  issues+=("$1")
  details+=("$2")
}

host_name() {
  hostname 2>/dev/null || echo unknown
}

os_name() {
  uname -s 2>/dev/null || echo unknown
}

market_closed_reason() {
  local day
  day="$(date +%u 2>/dev/null || echo 0)"
  case "$day" in
    6|7) echo "weekend_market_closed" ;;
    *) echo "" ;;
  esac
}

age_minutes_for_file() {
  local path="$1"
  [ -f "$path" ] || {
    echo 999999
    return
  }
  python - "$path" <<'PY' 2>/dev/null || echo 999999
import os, sys, time
print(max(0, int((time.time() - os.path.getmtime(sys.argv[1])) // 60)))
PY
}

service_is_active() {
  if ! have_cmd systemctl; then
    service_state="unknown"
    add_issue "service_state_unknown" "systemctl unavailable for ${SERVICE}"
    return
  fi
  service_state="$(systemctl is-active "$SERVICE" 2>/dev/null || true)"
  if [ "$service_state" != "active" ]; then
    add_issue "service_down" "service=${SERVICE} active_state=${service_state}"
  fi
}

collect_logs() {
  if have_cmd journalctl; then
    latest_logs="$(journalctl -u "$SERVICE" --since "$SINCE" --no-pager 2>&1 || true)"
  else
    latest_logs="journalctl unavailable"
  fi
}

check_host() {
  local os host
  os="$(os_name)"
  host="$(host_name)"
  if [ "$os" != "Linux" ] || [ "$host" != "algosphere-live-host" ]; then
    add_issue "wrong_live_host" "os=${os} host=${host} expected=Linux/algosphere-live-host"
  fi
}

check_premarket() {
  local reason dir names name path age stale=0
  reason="$(market_closed_reason)"
  dir="$ROOT/data/premarket"
  names="latest_event_feed.json latest_rankings.json latest_catalysts.json"
  for name in $names; do
    path="$dir/$name"
    if [ ! -f "$path" ]; then
      add_issue "premarket_artifact_missing" "$name missing"
      premarket_result="missing"
      return
    fi
    age="$(age_minutes_for_file "$path")"
    if [ "$age" -gt "$PREMARKET_MAX_AGE_MINUTES" ]; then
      stale=1
      if [ -n "$reason" ]; then
        premarket_result="stale_suppressed:${reason}"
      else
        add_issue "premarket_artifact_stale" "$name age_minutes=${age} threshold=${PREMARKET_MAX_AGE_MINUTES}"
        premarket_result="stale"
        return
      fi
    fi
  done
  if [ "$stale" -eq 0 ]; then
    premarket_result="fresh"
  fi
}

check_broker_reads() {
  local summary_log open_orders_log
  summary_log="$(portable_mktemp live_health_summary .log)"
  open_orders_log="$(portable_mktemp live_health_open_orders .log)"
  if ! "$ROOT/bin/algo" summary latest --user live_bot >"$summary_log" 2>&1; then
    broker_result="account_read_failed"
    add_issue "broker_account_unreadable" "$(tail -n 3 "$summary_log" | tr '\n' ' ')"
    return
  fi
  if grep -Eiq 'unavailable|missing credentials|error|exception|failed' "$summary_log"; then
    broker_result="account_read_failed"
    add_issue "broker_account_unreadable" "$(tail -n 3 "$summary_log" | tr '\n' ' ')"
    return
  fi
  if ! python "$ROOT/scripts/show_open_orders.py" --mode live --user live_bot >"$open_orders_log" 2>&1; then
    broker_result="open_orders_read_failed"
    add_issue "broker_open_orders_unreadable" "$(tail -n 3 "$open_orders_log" | tr '\n' ' ')"
    return
  fi
  if grep -Eiq 'open_orders_unavailable|missing credentials|error|exception|failed' "$open_orders_log"; then
    broker_result="open_orders_read_failed"
    add_issue "broker_open_orders_unreadable" "$(tail -n 3 "$open_orders_log" | tr '\n' ' ')"
    return
  fi
  broker_result="account_buying_power_open_orders_readable"
}

check_logs_for_runtime_errors() {
  local allocator_count order_count crash_count
  allocator_count="$(printf '%s\n' "$latest_logs" | grep -Eci 'allocator (exception|error)|capital_allocator ERROR' || true)"
  order_count="$(printf '%s\n' "$latest_logs" | grep -Eci 'order submission error|order placement|submit_order.*(error|exception|failed)' || true)"
  crash_count="$(printf '%s\n' "$latest_logs" | grep -Eci 'Traceback|CRITICAL|FATAL|restart counter|start request repeated too quickly' || true)"
  if [ "$crash_count" -ge "${ALGO_LIVE_CRASH_LOOP_THRESHOLD:-2}" ]; then
    add_issue "crash_loop" "critical_log_count=${crash_count}"
  fi
  if [ "$allocator_count" -ge "${ALGO_LIVE_ALLOCATOR_ERROR_THRESHOLD:-2}" ]; then
    add_issue "repeated_allocator_errors" "allocator_error_count=${allocator_count}"
  fi
  if [ "$order_count" -ge "${ALGO_LIVE_ORDER_ERROR_THRESHOLD:-2}" ]; then
    add_issue "repeated_order_submission_errors" "order_error_count=${order_count}"
  fi
  if printf '%s\n' "$latest_logs" | grep -Eiq 'paper-only option entry helper|route=paper_options|paper-only execution'; then
    add_issue "paper_only_execution_path_live" "paper-only execution marker in live logs"
  fi
  if [ "${ALGO_LIVE_OPTIONS_APPROVED:-0}" != "1" ] && printf '%s\n' "$latest_logs" | grep -Eiq 'OPTION_ORDER_INTENT|OPTION_ORDER_SUBMITTED|OPTION_POSITION_OPENED|live options route'; then
    add_issue "live_options_unapproved" "live options execution marker without approval"
  fi
}

stable_ticks_current() {
  if [ -f "$STABLE_STATE_FILE" ]; then
    cat "$STABLE_STATE_FILE" 2>/dev/null || echo 0
  else
    echo 0
  fi
}

write_stable_ticks() {
  local value="$1"
  if [ "$dry_run" -eq 1 ]; then
    return
  fi
  printf '%s\n' "$value" >"$STABLE_STATE_FILE"
}

main() {
  cd "$ROOT" 2>/dev/null || {
    echo "Repo root not found: $ROOT" >&2
    exit 1
  }
  check_host
  service_is_active
  collect_logs
  check_premarket
  check_broker_reads
  check_logs_for_runtime_errors

  local status reason current_ticks stable_ticks host
  host="$(host_name)"
  if [ "${#issues[@]}" -eq 0 ]; then
    current_ticks="$(stable_ticks_current)"
    stable_ticks=$((current_ticks + 1))
    write_stable_ticks "$stable_ticks"
    if [ "$stable_ticks" -ge "$REQUIRED_STABLE_TICKS" ]; then
      status="healthy"
      reason="none"
    else
      status="unhealthy"
      reason="stabilizing"
    fi
  else
    stable_ticks=0
    write_stable_ticks 0
    status="unhealthy"
    reason="${issues[0]}"
  fi

  printf 'LIVE_HEALTH status=%s\n' "$status"
  printf 'LIVE_HEALTH issue=%s\n' "$reason"
  printf 'LIVE_HEALTH stable_ticks=%s\n' "$stable_ticks"
  printf 'LIVE_HEALTH required_stable_ticks=%s\n' "$REQUIRED_STABLE_TICKS"
  printf 'LIVE_HEALTH host=%s\n' "$host"
  printf 'LIVE_HEALTH environment=live\n'
  printf 'LIVE_HEALTH service_state=%s\n' "$service_state"
  printf 'LIVE_HEALTH broker=%s\n' "$broker_result"
  printf 'LIVE_HEALTH premarket=%s\n' "$premarket_result"
  if [ "${#issues[@]}" -gt 0 ]; then
    local i
    i=0
    while [ "$i" -lt "${#issues[@]}" ]; do
      printf 'LIVE_HEALTH detail=%s|%s\n' "${issues[$i]}" "${details[$i]}"
      i=$((i + 1))
    done
  fi
}

main "$@"
