#!/usr/bin/env bash
# AutoOps Health Monitor: Level 1.5/1.6 read-only silent health monitoring and GitHub issue reporting.
set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${ALGO_REPO_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
ANALYZER_PATH="${ALGO_HEALTH_ANALYZER_PATH:-$SCRIPT_DIR/analyze_algo_health_report.py}"
REPORT_PATH="${ALGO_HEALTH_REPORT_PATH:-/tmp/algo_health_report.md}"
SINCE="${ALGO_HEALTH_JOURNAL_SINCE:-30 minutes ago}"
DYNAMIC_ACCEPTED_ZERO_THRESHOLD="${ALGO_HEALTH_ACCEPTED_ZERO_THRESHOLD:-1}"
PREMARKET_MAX_AGE_MINUTES="${ALGO_HEALTH_PREMARKET_MAX_AGE_MINUTES:-720}"
REPLAY_MAX_AGE_MINUTES="${ALGO_HEALTH_REPLAY_MAX_AGE_MINUTES:-1440}"
RECOVERABLE_RUNTIME_ERROR_THRESHOLD="${ALGO_HEALTH_RECOVERABLE_RUNTIME_ERROR_THRESHOLD:-2}"

DRY_RUN=0
ENV_ARG=""

usage() {
  cat <<'EOF'
Usage: scripts/check_algo_health.sh [--dry-run] [--env paper|live] [LIVE|PAPER]

Examples:
  scripts/check_algo_health.sh --dry-run
  scripts/check_algo_health.sh --dry-run --env paper
  scripts/check_algo_health.sh --dry-run LIVE
  scripts/check_algo_health.sh --dry-run PAPER
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --env)
      if [[ "${2:-}" != "paper" && "${2:-}" != "live" && "${2:-}" != "PAPER" && "${2:-}" != "LIVE" ]]; then
        echo "--env requires paper or live" >&2
        exit 2
      fi
      ENV_ARG="$(printf '%s' "$2" | tr '[:lower:]' '[:upper:]')"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    LIVE|live|PAPER|paper)
      ENV_ARG="$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]')"
      shift
      ;;
    *)
      echo "Unexpected argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

date_utc() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

date_day() {
  date +"%Y-%m-%d"
}

paper_review_dir() {
  echo "$ROOT/data/review/$(date_day)"
}

paper_full_log_path() {
  echo "$(paper_review_dir)/paper_full.log"
}

market_closed_reason() {
  local day_of_week
  day_of_week="$(date +%u 2>/dev/null || echo 0)"
  case "$day_of_week" in
    6|7) echo "weekend_market_closed" ;;
    *) echo "" ;;
  esac
}

env_user() {
  case "$1" in
    LIVE) echo "live_bot" ;;
    PAPER) echo "paper_bot" ;;
    *) echo "live_bot" ;;
  esac
}

processor_label_for_env() {
  case "$1" in
    PAPER) echo "processor:mac-paper" ;;
    LIVE) echo "processor:live-linux" ;;
    *) echo "processor:live-linux" ;;
  esac
}

env_unit() {
  case "$1" in
    LIVE) echo "${ALGO_HEALTH_LIVE_UNIT:-algo.service}" ;;
    PAPER) echo "${ALGO_HEALTH_PAPER_UNIT:-${ALGO_PAPER_SERVICE:-paper.service}}" ;;
    *) echo "algo.service" ;;
  esac
}

default_env() {
  local os_name host
  os_name="$(uname -s 2>/dev/null || echo unknown)"
  host="$(hostname 2>/dev/null || echo unknown)"
  if [[ "$os_name" == "Darwin" ]]; then
    echo "PAPER"
  elif [[ "$os_name" == "Linux" && "$host" == "algosphere-live-host" ]]; then
    echo "LIVE"
  else
    echo "LIVE"
  fi
}

run_readonly() {
  local label="$1"
  shift
  {
    echo "\$ $*"
    "$@" 2>&1
    local rc=$?
    echo "[exit_code=$rc]"
  } || true
}

collect_journal() {
  local unit="$1"
  local env_name="${2:-}"
  local os_name paper_dir paper_log
  os_name="$(uname -s 2>/dev/null || echo unknown)"
  if [[ "$env_name" == "PAPER" && "$os_name" == "Darwin" ]]; then
    paper_dir="$(paper_review_dir)"
    paper_log="$(paper_full_log_path)"
    mkdir -p "$paper_dir"
    if [[ -f "$paper_log" ]]; then
      cat "$paper_log"
    else
      echo "PAPER_REVIEW_LOG_MISSING path=$paper_log"
    fi
    return
  fi
  if have_cmd journalctl; then
    journalctl -u "$unit" --since "$SINCE" --no-pager 2>&1 || true
  else
    echo "journalctl unavailable"
  fi
}

service_state() {
  local unit="$1"
  if ! have_cmd systemctl; then
    echo "unknown"
    return
  fi
  systemctl is-active "$unit" 2>/dev/null || true
}

service_failed_state() {
  local unit="$1"
  if ! have_cmd systemctl; then
    echo "unknown"
    return
  fi
  systemctl is-failed "$unit" 2>/dev/null || true
}

paper_service_state() {
  local unit="$1"
  local os_name label pattern target
  os_name="$(uname -s 2>/dev/null || echo unknown)"
  if [[ "$os_name" != "Darwin" ]]; then
    service_state "$unit"
    return
  fi
  label="${ALGO_PAPER_LAUNCHD_LABEL:-}"
  if [[ -n "$label" ]] && have_cmd launchctl; then
    target="gui/$(id -u)/${label}"
    if launchctl print "$target" >/dev/null 2>&1; then
      echo "active"
      return
    fi
  fi
  if [[ -z "$label" && -z "${ALGO_PAPER_PROCESS_PATTERN:-}" ]]; then
    echo "unknown"
    return
  fi
  pattern="${ALGO_PAPER_PROCESS_PATTERN:-algo_loop.py --paper}"
  if have_cmd pgrep && pgrep -f "$pattern" >/dev/null 2>&1; then
    echo "active"
    return
  fi
  echo "inactive"
}

paper_service_failed_state() {
  local os_name
  os_name="$(uname -s 2>/dev/null || echo unknown)"
  if [[ "$os_name" != "Darwin" ]]; then
    service_failed_state "$1"
    return
  fi
  echo "unknown"
}

latest_file_for_user() {
  local dir="$1"
  local user_id="$2"
  [[ -d "$dir" ]] || return 1
  python - "$dir" "$user_id" <<'PY' 2>/dev/null
from pathlib import Path
import sys

directory = Path(sys.argv[1])
user_id = sys.argv[2]
matches = [p for p in directory.glob(f"*_{user_id}.json") if p.is_file()]
if not matches:
    raise SystemExit(1)
print(max(matches, key=lambda p: p.stat().st_mtime))
PY
}

age_minutes_for_file() {
  local path="$1"
  [[ -f "$path" ]] || {
    echo "999999"
    return
  }
  python - "$path" <<'PY' 2>/dev/null || echo "999999"
import os
import sys
import time

path = sys.argv[1]
print(max(0, int((time.time() - os.path.getmtime(path)) // 60)))
PY
}

json_count_value() {
  local path="$1"
  local key="$2"
  [[ -f "$path" ]] || {
    echo ""
    return
  }
  grep -Eo "\"$key\"[[:space:]]*:[[:space:]]*[0-9]+" "$path" 2>/dev/null \
    | head -n 1 \
    | grep -Eo '[0-9]+' \
    || true
}

json_array_count() {
  local path="$1"
  local key="$2"
  [[ -f "$path" ]] || {
    echo "0"
    return
  }
  python - "$path" "$key" <<'PY' 2>/dev/null || echo "0"
import json, sys
path, key = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as f:
    data = json.load(f)
value = data.get(key)
print(len(value) if isinstance(value, list) else 0)
PY
}

add_condition() {
  local file="$1"
  local severity="$2"
  local kind="$3"
  local detail="$4"
  printf '%s|%s|%s\n' "$severity" "$kind" "$detail" >>"$file"
}

add_suppressed_condition() {
  local file="$1"
  local kind="$2"
  local detail="$3"
  local reason="$4"
  printf '%s|%s|%s\n' "$kind" "$detail" "$reason" >>"$file"
}

scan_service_health() {
  local env_name="$1"
  local unit="$2"
  local conditions_file="$3"
  local state failed_state severity kind

  if [[ "$env_name" == "PAPER" ]]; then
    state="$(paper_service_state "$unit")"
    failed_state="$(paper_service_failed_state "$unit")"
  else
    state="$(service_state "$unit")"
    failed_state="$(service_failed_state "$unit")"
  fi
  if [[ "$failed_state" == "failed" || "$state" == "failed" || "$state" == "inactive" ]]; then
    if [[ "$env_name" == "LIVE" ]]; then
      severity="critical"
      kind="service down"
    else
      severity="high"
      kind="paper service down"
    fi
    add_condition "$conditions_file" "$severity" "$kind" "unit=$unit active_state=$state failed_state=$failed_state"
  fi
}

scan_premarket_health() {
  local env_name="$1"
  local conditions_file="$2"
  local suppressed_file="$3"
  local market_reason="$4"
  local premarket_dir="$ROOT/data/premarket"
  local names=("latest_event_feed.json" "latest_rankings.json" "latest_catalysts.json")

  for name in "${names[@]}"; do
    local path="$premarket_dir/$name"
    if [[ ! -f "$path" ]]; then
      add_condition "$conditions_file" "medium" "premarket artifacts missing" "$name missing"
      continue
    fi
    local age
    age="$(age_minutes_for_file "$path")"
    if [[ "$age" =~ ^[0-9]+$ && "$age" -gt "$PREMARKET_MAX_AGE_MINUTES" ]]; then
      if [[ -n "$market_reason" ]]; then
        add_suppressed_condition "$suppressed_file" "premarket artifacts stale" "$name age_minutes=$age threshold=$PREMARKET_MAX_AGE_MINUTES" "$market_reason"
      else
        add_condition "$conditions_file" "medium" "premarket artifacts stale" "$name age_minutes=$age threshold=$PREMARKET_MAX_AGE_MINUTES"
      fi
    fi
  done

  local rankings="$premarket_dir/latest_rankings.json"
  local catalysts="$premarket_dir/latest_catalysts.json"
  local events="$premarket_dir/latest_event_feed.json"
  if [[ -f "$rankings" ]]; then
    local ranked_symbols ranking_count
    ranked_symbols="$(json_count_value "$rankings" "catalyst_ranked_symbols")"
    ranking_count="$(json_count_value "$rankings" "rankings")"
    [[ "${ranked_symbols:-${ranking_count:-1}}" == "0" ]] && add_condition "$conditions_file" "medium" "catalyst generation unhealthy" "catalyst_ranked_symbols=0"
  fi
  if [[ -f "$catalysts" ]]; then
    local catalyst_count
    catalyst_count="$(json_count_value "$catalysts" "catalysts")"
    [[ "${catalyst_count:-1}" == "0" ]] && add_condition "$conditions_file" "medium" "catalyst generation unhealthy" "catalysts=0"
  fi
  if [[ -f "$events" ]]; then
    local event_count
    event_count="$(json_count_value "$events" "events")"
    [[ "${event_count:-1}" == "0" ]] && add_condition "$conditions_file" "research" "news coverage degraded" "events=0"
  fi
}

scan_dynamic_health() {
  local env_name="$1"
  local user_id="$2"
  local conditions_file="$3"
  local dynamic_file accepted rejected candidates rejection_rate

  dynamic_file="$(latest_file_for_user "$ROOT/data/dynamic_scan_history" "$user_id" || true)"
  if [[ -z "$dynamic_file" || ! -f "$dynamic_file" ]]; then
    add_condition "$conditions_file" "medium" "dynamic scanner producing no candidates" "dynamic scan artifact missing user=$user_id"
    return
  fi
  accepted="$(json_count_value "$dynamic_file" "accepted")"
  rejected="$(json_count_value "$dynamic_file" "rejected")"
  candidates="$(json_count_value "$dynamic_file" "candidates")"
  if [[ -z "$accepted" ]]; then
    accepted="$(json_array_count "$dynamic_file" "accepted")"
  fi
  if [[ "${accepted:-0}" -le "$DYNAMIC_ACCEPTED_ZERO_THRESHOLD" ]]; then
    add_condition "$conditions_file" "medium" "dynamic scanner producing no candidates" "accepted=${accepted:-0} path=$dynamic_file"
  fi
  if [[ "${candidates:-0}" -gt 0 && "${rejected:-0}" -gt 0 ]]; then
    rejection_rate=$(( 100 * rejected / candidates ))
    if [[ "$rejection_rate" -ge "${ALGO_HEALTH_REJECTION_RATE_RESEARCH_PCT:-90}" ]]; then
      add_condition "$conditions_file" "research" "unusual scanner rejection rate" "rejection_rate=${rejection_rate}% rejected=$rejected candidates=$candidates"
    fi
  fi
  if grep -Eaiq 'acceptance rate collapse|excessive spread|excessive RVOL|rejected.*winner' "$dynamic_file"; then
    add_condition "$conditions_file" "research" "dynamic acceptance quality degraded" "research signal in $dynamic_file"
  fi
}

scan_paper_options_health() {
  local conditions_file="$1"
  local journal_file="$2"
  local diag_file="/tmp/algo_health_paper_options_diag.log"
  local options_activity

  run_readonly "paper-options-diagnostics" "$ROOT/bin/algo" paper-options-diagnostics --user paper_bot --symbol QQQ >"$diag_file"
  if grep -q '\[exit_code=0\]' "$diag_file"; then
    :
  else
    add_condition "$conditions_file" "medium" "options engine inactive" "paper-options-diagnostics failure"
  fi

  options_activity="$(grep -Eai 'OPTION_ROUTE_CHECK|OPTION_SIGNAL|OPTION_ORDER_INTENT|OPTION_ORDER_SUBMITTED|OPTION_POSITION_OPENED|OPTION_ENTRY_BLOCKED' "$journal_file" | tail -n 1 || true)"
  if [[ -z "$options_activity" ]]; then
    add_condition "$conditions_file" "medium" "options engine inactive" "no options activity in recent paper logs"
  fi
}

scan_replay_health() {
  local user_id="$1"
  local conditions_file="$2"
  local replay_file age
  replay_file="$(latest_file_for_user "$ROOT/data/replay" "$user_id" || true)"
  if [[ -z "$replay_file" ]]; then
    replay_file="$(latest_file_for_user "$ROOT/data/replay_market_session" "$user_id" || true)"
  fi
  if [[ -z "$replay_file" || ! -f "$replay_file" ]]; then
    add_condition "$conditions_file" "medium" "replay artifacts missing" "no replay artifact user=$user_id"
    return
  fi
  age="$(age_minutes_for_file "$replay_file")"
  if [[ "$age" =~ ^[0-9]+$ && "$age" -gt "$REPLAY_MAX_AGE_MINUTES" ]]; then
    add_condition "$conditions_file" "medium" "replay artifacts stale" "age_minutes=$age threshold=$REPLAY_MAX_AGE_MINUTES path=$replay_file"
  fi
  if grep -Eaiq 'replay failure|validation failed|mismatch|allocator_input_missing' "$replay_file"; then
    add_condition "$conditions_file" "medium" "replay validation failed" "failure marker in $replay_file"
  fi
}

_runtime_first_seen() {
  local journal_file="$1"
  local pattern="$2"
  grep -Eai "$pattern" "$journal_file" | head -n 1 | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' || true
}

_runtime_last_seen() {
  local journal_file="$1"
  local pattern="$2"
  grep -Eai "$pattern" "$journal_file" | tail -n 1 | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' || true
}

_runtime_artifact_path() {
  local journal_file="$1"
  grep -Eaio 'data/trade_attribution/daily/[^[:space:]]+\.json|/[^[:space:]]*/data/trade_attribution/daily/[^[:space:]]+\.json' "$journal_file" \
    | head -n 1 \
    || true
}

scan_paper_recoverable_runtime_health() {
  local env_name="$1"
  local conditions_file="$2"
  local journal_file="$3"
  local threshold="${RECOVERABLE_RUNTIME_ERROR_THRESHOLD}"
  local pattern count first last path

  [[ "$env_name" == "PAPER" ]] || return 0
  [[ "$threshold" =~ ^[0-9]+$ ]] || threshold=2
  [[ "$threshold" -lt 2 ]] && threshold=2

  pattern='TRADE_ATTRIBUTION_CORRUPT_ARTIFACT|json\.decoder\.JSONDecodeError|JSONDecodeError.*trade_attribution|Invalid control character.*trade_attribution'
  count="$(grep -Eai "$pattern" "$journal_file" | wc -l | tr -d '[:space:]')"
  if [[ "${count:-0}" -ge "$threshold" ]]; then
    first="$(_runtime_first_seen "$journal_file" "$pattern")"
    last="$(_runtime_last_seen "$journal_file" "$pattern")"
    path="$(_runtime_artifact_path "$journal_file")"
    add_condition "$conditions_file" "high" "trade attribution corrupt artifact" \
      "fingerprint=paper:trade_attribution_corrupt_artifact occurrence_count=$count artifact_path=${path:-unknown} first_seen=${first:-unknown} last_seen=${last:-unknown}"
  fi

  pattern='CORE_REBUILD_CHURN_GUARD_ERROR'
  count="$(grep -Eai "$pattern" "$journal_file" | wc -l | tr -d '[:space:]')"
  if [[ "${count:-0}" -ge "$threshold" ]]; then
    first="$(_runtime_first_seen "$journal_file" "$pattern")"
    last="$(_runtime_last_seen "$journal_file" "$pattern")"
    add_condition "$conditions_file" "high" "core rebuild churn guard error" \
      "fingerprint=paper:core_rebuild_churn_guard_error occurrence_count=$count artifact_path=unknown first_seen=${first:-unknown} last_seen=${last:-unknown}"
  fi
}

scan_paper_review_log_health() {
  local env_name="$1"
  local conditions_file="$2"
  local os_name paper_dir paper_log

  [[ "$env_name" == "PAPER" ]] || return 0
  os_name="$(uname -s 2>/dev/null || echo unknown)"
  [[ "$os_name" == "Darwin" ]] || return 0
  paper_dir="$(paper_review_dir)"
  paper_log="$(paper_full_log_path)"
  mkdir -p "$paper_dir"
  if [[ ! -f "$paper_log" ]]; then
    add_condition "$conditions_file" "medium" "paper review log missing" "path=$paper_log"
  fi
}

top_condition() {
  local conditions_file="$1"
  if grep -q '^critical|' "$conditions_file"; then
    grep '^critical|' "$conditions_file" | head -n 1
  elif grep -q '^high|' "$conditions_file"; then
    grep '^high|' "$conditions_file" | head -n 1
  elif grep -q '^medium|' "$conditions_file"; then
    grep '^medium|' "$conditions_file" | head -n 1
  elif grep -q '^research|' "$conditions_file"; then
    grep '^research|' "$conditions_file" | head -n 1
  else
    echo "none|healthy|no health issue detected"
  fi
}

fingerprint_for() {
  local env_name="$1"
  local severity="$2"
  local kind="$3"
  if [[ "$env_name" == "PAPER" && "$kind" == "trade attribution corrupt artifact" ]]; then
    printf 'paper:trade_attribution_corrupt_artifact\n'
    return
  fi
  if [[ "$env_name" == "PAPER" && "$kind" == "core rebuild churn guard error" ]]; then
    printf 'paper:core_rebuild_churn_guard_error\n'
    return
  fi
  printf 'health:%s:%s:%s' "$env_name" "$severity" "$kind" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9:._-' '-'
}

issue_exists() {
  local fingerprint="$1"
  local issues
  if ! have_cmd gh; then
    return 1
  fi
  issues="$(gh issue list --state open --search "$fingerprint" --json number,title --limit 20 2>/dev/null || true)"
  printf '%s\n' "$issues" | grep -Fq -- "$fingerprint"
}

create_health_issue() {
  local title="$1"
  local body_file="$2"
  local env_name="$3"
  local severity="$4"
  local fingerprint="$5"
  local processor_label
  processor_label="$(processor_label_for_env "$env_name")"

  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "Dry run: would create GitHub issue: $title"
    echo "Dry run fingerprint: $fingerprint"
    return 0
  fi
  if ! have_cmd gh; then
    echo "gh not available; health report written to $body_file"
    return 0
  fi
  if issue_exists "$fingerprint"; then
    echo "Existing health issue found for $fingerprint"
    return 0
  fi
  gh issue create \
    --title "$title" \
    --body-file "$body_file" \
    --label "algo-health" \
    --label "environment:$(printf '%s' "$env_name" | tr '[:upper:]' '[:lower:]')" \
    --label "$processor_label" \
    --label "severity:$severity"
  echo "AUTOOPS_ISSUE_CREATED env=${env_name} fingerprint=${fingerprint}"
}

write_env_report() {
  local env_name="$1"
  local user_id unit timestamp host branch commit status_txt journal_file conditions_file suppressed_file env_report top severity kind detail fingerprint title
  local suppression_reason market_closed stale_premarket_artifacts_suppressed
  local tmp_base
  user_id="$(env_user "$env_name")"
  unit="$(env_unit "$env_name")"
  echo "AUTOOPS_HEALTH_CHECK env=${env_name} unit=${unit} dry_run=${DRY_RUN}"
  timestamp="$(date_utc)"
  host="$(hostname 2>/dev/null || echo unknown)"
  branch="$(git -c "safe.directory=$ROOT" -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
  commit="$(git -c "safe.directory=$ROOT" -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  status_txt="$(git -c "safe.directory=$ROOT" -C "$ROOT" status --short 2>/dev/null || true)"
  tmp_base="${TMPDIR:-/tmp}"
  journal_file="$(mktemp "$tmp_base/algo_health_journal_${env_name}.XXXXXX.log")"
  conditions_file="$(mktemp "$tmp_base/algo_health_conditions_${env_name}.XXXXXX.txt")"
  suppressed_file="$(mktemp "$tmp_base/algo_health_suppressed_${env_name}.XXXXXX.txt")"
  env_report="$(mktemp "$tmp_base/algo_health_report_${env_name}.XXXXXX.md")"
  : >"$conditions_file"
  : >"$suppressed_file"
  collect_journal "$unit" "$env_name" >"$journal_file"
  suppression_reason="$(market_closed_reason)"
  market_closed="false"
  if [[ -n "$suppression_reason" ]]; then
    market_closed="true"
  fi

  scan_service_health "$env_name" "$unit" "$conditions_file"
  scan_premarket_health "$env_name" "$conditions_file" "$suppressed_file" "$suppression_reason"
  scan_dynamic_health "$env_name" "$user_id" "$conditions_file"
  if [[ "$env_name" == "PAPER" ]]; then
    scan_paper_review_log_health "$env_name" "$conditions_file"
    scan_paper_options_health "$conditions_file" "$journal_file"
    scan_replay_health "$user_id" "$conditions_file"
    scan_paper_recoverable_runtime_health "$env_name" "$conditions_file" "$journal_file"
  fi

  top="$(top_condition "$conditions_file")"
  IFS='|' read -r severity kind detail <<<"$top"
  fingerprint="$(fingerprint_for "$env_name" "$severity" "$kind")"
  stale_premarket_artifacts_suppressed="false"
  if grep -q '^premarket artifacts stale|' "$suppressed_file"; then
    stale_premarket_artifacts_suppressed="true"
  fi

  {
    echo "# HEALTH [$env_name] $kind $(date_day)"
    echo
    echo "- Environment: $env_name"
    echo "- environment=$(printf '%s' "$env_name" | tr '[:upper:]' '[:lower:]')"
    echo "- Severity: $severity"
    echo "- Detected Condition: $kind"
    echo "- Detail: $detail"
    echo "- Detection Timestamp: $timestamp"
    echo "- Host: $host"
    echo "- Hostname: $host"
    echo "- User: $user_id"
    echo "- Unit: $unit"
    echo "- Service Name: $unit"
    echo "- Failure Source: health-monitor"
    echo "- market_closed=$market_closed"
    echo "- stale_premarket_artifacts_suppressed=$stale_premarket_artifacts_suppressed"
    if [[ -n "$suppression_reason" ]]; then
      echo "- suppression_reason=$suppression_reason"
    else
      echo "- suppression_reason=none"
    fi
    echo "- Git Branch: $branch"
    echo "- Git Commit: $commit"
    echo "- Fingerprint: $fingerprint"
    echo
    echo "## Runtime Context"
    echo
    echo "- Environment: $env_name"
    echo "- Hostname: $host"
    echo "- Service Name: $unit"
    echo "- Failure Source: health-monitor"
    echo "- market_closed=$market_closed"
    echo "- stale_premarket_artifacts_suppressed=$stale_premarket_artifacts_suppressed"
    if [[ -n "$suppression_reason" ]]; then
      echo "- suppression_reason=$suppression_reason"
    else
      echo "- suppression_reason=none"
    fi
    echo
    echo "## Git Status"
    echo
    if [[ -n "$status_txt" ]]; then
      echo '```'
      printf '%s\n' "$status_txt"
      echo '```'
    else
      echo "clean"
    fi
    echo
    echo "## All Detected Conditions"
    echo
    echo '```'
    cat "$conditions_file"
    echo '```'
    echo
    echo "## Suppressed Conditions"
    echo
    echo '```'
    if [[ -s "$suppressed_file" ]]; then
      cat "$suppressed_file"
    else
      echo "none"
    fi
    echo '```'
    echo
    echo "## Recent Diagnostics"
    echo
    echo '```'
    if [[ "$env_name" == "LIVE" ]]; then
      run_readonly "premarket-ready" "$ROOT/bin/algo" premarket-ready
      run_readonly "live-summary" "$ROOT/bin/algo" summary latest --user live_bot
    else
      run_readonly "paper-options-diagnostics" "$ROOT/bin/algo" paper-options-diagnostics --user paper_bot --symbol QQQ
      run_readonly "paper-summary" "$ROOT/bin/algo" summary latest --user paper_bot
      run_readonly "paper-replay" python scripts/replay_live_cycle.py --date latest --user paper_bot --broker-mock
    fi
    echo '```'
    echo
    echo "## Recent Relevant Logs"
    echo
    echo '```'
    grep -Eai 'PREMARKET|DYNAMIC|accepted=0|rankings=0|catalysts=0|OPTION_|Replay|replay|coverage|rejection|No candidates|No evaluations|CORE_REBUILD_CHURN_GUARD_ERROR|TRADE_ATTRIBUTION_CORRUPT_ARTIFACT|JSONDecodeError|PAPER_REVIEW_LOG_MISSING' "$journal_file" || true
    echo '```'
    echo
    if [[ -f "$ANALYZER_PATH" ]]; then
      python "$ANALYZER_PATH" \
        --root "$ROOT" \
        --environment "$env_name" \
        --user "$user_id" \
        --journal-file "$journal_file" \
        --severity "$severity" \
        --kind "$kind" \
        --detail "$detail" 2>&1 || true
      echo
    fi
    echo "## Recommended Next Action"
    echo
    if [[ "$severity" == "research" ]]; then
      echo "Review degradation data and open a research task. Do not change trading thresholds from this monitor."
    elif [[ "$severity" == "medium" ]]; then
      echo "Inspect artifacts, diagnostics, and recent logs for silent health degradation. This monitor does not restart services or change trading behavior."
    else
      echo "No health action required."
    fi
  } >"$env_report"

  {
    echo
    echo "---"
    cat "$env_report"
  } >>"$REPORT_PATH"

  if [[ "$severity" == "none" ]]; then
    echo "No health issue detected for $env_name"
    return 0
  fi
  title="HEALTH [$env_name] $kind $(date_day)"
  create_health_issue "$title" "$env_report" "$env_name" "$severity" "$fingerprint"
}

main() {
  cd "$ROOT" 2>/dev/null || {
    echo "Repo root not found: $ROOT" >&2
    exit 1
  }
  : >"$REPORT_PATH"
  if [[ -z "$ENV_ARG" ]]; then
    local detected_env
    detected_env="$(default_env)"
    write_env_report "$detected_env"
  else
    write_env_report "$ENV_ARG"
  fi
  echo "Health report written to $REPORT_PATH"
}

main "$@"
