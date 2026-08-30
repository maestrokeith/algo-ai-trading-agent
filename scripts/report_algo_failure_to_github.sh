#!/usr/bin/env bash
# AutoOps Failure Reporter: Level 1 read-only failure detection and GitHub issue reporting.
set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${ALGO_REPO_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
REPORT_PATH="${ALGO_FAILURE_REPORT_PATH:-/tmp/algo_failure_report.md}"
SINCE="${ALGO_FAILURE_JOURNAL_SINCE:-30 minutes ago}"
PYTEST_TIMEOUT="${ALGO_FAILURE_PYTEST_TIMEOUT:-120}"

DRY_RUN=0
ENV_ARG=""
UNIT_ARG=""

usage() {
  cat <<'EOF'
Usage: scripts/report_algo_failure_to_github.sh [--dry-run] [--env paper|live] [LIVE|PAPER] [systemd-unit]

Examples:
  scripts/report_algo_failure_to_github.sh --dry-run
  scripts/report_algo_failure_to_github.sh --dry-run --env paper
  scripts/report_algo_failure_to_github.sh LIVE algo.service
  scripts/report_algo_failure_to_github.sh PAPER paper.service
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
      if [[ -z "$UNIT_ARG" ]]; then
        UNIT_ARG="$1"
        shift
      else
        echo "Unexpected argument: $1" >&2
        usage >&2
        exit 2
      fi
      ;;
  esac
done

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

have_cmd() {
  command -v "$1" >/dev/null 2>&1
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

default_unit() {
  case "$1" in
    LIVE) echo "algo.service" ;;
    PAPER) echo "${ALGO_PAPER_SERVICE:-paper.service}" ;;
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

append_section() {
  local file="$1"
  local title="$2"
  {
    echo
    echo "## $title"
    echo
    cat
  } >>"$file"
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

collect_systemctl_status() {
  local unit="$1"
  if have_cmd systemctl; then
    systemctl status "$unit" --no-pager 2>&1 || true
  else
    echo "systemctl unavailable"
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

scan_failure_text() {
  local text_file="$1"
  grep -Eai \
    'Traceback|Exception|ERROR|CRITICAL|FATAL|auth|unauthorized|allocator exception|order placement|option chain|premarket|artifact stale|accepted=0|catalyst_ranked_symbols=0|catalysts=0|rankings=0|No candidates|No evaluations|Replay failure|Broker failure|broker failure|CORE_REBUILD_CHURN_GUARD_ERROR|TRADE_ATTRIBUTION_CORRUPT_ARTIFACT|JSONDecodeError|json.decoder.JSONDecodeError|PAPER_REVIEW_LOG_MISSING' \
    "$text_file" \
    | grep -Ev '(^PAPER_OPTIONS_HEALTH |^PAPER_OPTIONS_STABILIZATION |^### PAPER OPTIONS LOG: |fingerprint=paper-options:critical_options_errors|PAPER_OPTIONS \[PAPER\] unstable: critical_options_errors|POSTFIX \[PAPER_OPTIONS\].*critical_options_errors)' \
    || true
}

classify_issue() {
  local env_name="$1"
  local unit="$2"
  local state="$3"
  local failed_state="$4"
  local findings_file="$5"

  local severity="none"
  local failure_type=""

  if [[ "$failed_state" == "failed" || "$state" == "failed" || "$state" == "inactive" ]]; then
    if [[ "$env_name" == "LIVE" ]]; then
      severity="critical"
      failure_type="service down"
    else
      severity="high"
      failure_type="paper service down"
    fi
  fi

  if [[ "$env_name" == "PAPER" ]] && [[ "$(grep -Eai 'TRADE_ATTRIBUTION_CORRUPT_ARTIFACT|json\.decoder\.JSONDecodeError|JSONDecodeError.*trade_attribution|Invalid control character.*trade_attribution' "$findings_file" | wc -l | tr -d '[:space:]')" -ge 2 ]]; then
    severity="high"
    failure_type="trade attribution corrupt artifact"
  elif [[ "$env_name" == "PAPER" ]] && [[ "$(grep -Eai 'CORE_REBUILD_CHURN_GUARD_ERROR' "$findings_file" | wc -l | tr -d '[:space:]')" -ge 2 ]]; then
    severity="high"
    failure_type="core rebuild churn guard error"
  elif grep -Eaiq 'Traceback|CRITICAL|FATAL|broker failure|Broker failure|auth|unauthorized|allocator exception|order placement|startup failure' "$findings_file"; then
    severity="critical"
    failure_type="$(grep -Eai 'Traceback|CRITICAL|FATAL|broker failure|Broker failure|auth|unauthorized|allocator exception|order placement|startup failure' "$findings_file" | head -n 1 | sed 's/^[[:space:]]*//; s/[[:space:]]*$//; s/[^A-Za-z0-9_ .:-]/ /g' | cut -c1-80)"
  elif grep -Eaiq 'paper-options-diagnostics|options diagnostics|Replay failure|replay failure|scanner exception|premarket readiness failed|missing rankings|missing catalysts|missing event feed|artifact stale' "$findings_file"; then
    if [[ "$severity" == "none" || "$severity" == "medium" || "$severity" == "research" ]]; then
      severity="high"
      failure_type="$(grep -Eai 'paper-options-diagnostics|options diagnostics|Replay failure|replay failure|scanner exception|premarket readiness failed|missing rankings|missing catalysts|missing event feed|artifact stale' "$findings_file" | head -n 1 | sed 's/^[[:space:]]*//; s/[[:space:]]*$//; s/[^A-Za-z0-9_ .:-]/ /g' | cut -c1-80)"
    fi
  elif grep -Eaiq 'PAPER_REVIEW_LOG_MISSING|accepted=0|catalyst_ranked_symbols=0|catalysts=0|rankings=0|No candidates|No evaluations|no option evaluations|no replay artifacts|no health artifacts' "$findings_file"; then
    if [[ "$severity" == "none" ]]; then
      severity="medium"
      failure_type="$(grep -Eai 'PAPER_REVIEW_LOG_MISSING|accepted=0|catalyst_ranked_symbols=0|catalysts=0|rankings=0|No candidates|No evaluations|no option evaluations|no replay artifacts|no health artifacts' "$findings_file" | head -n 1 | sed 's/^[[:space:]]*//; s/[[:space:]]*$//; s/[^A-Za-z0-9_ .:-]/ /g' | cut -c1-80)"
    fi
  elif grep -Eaiq 'acceptance rate collapse|coverage degraded|coverage collapse|excessive spread|excessive RVOL|rejection outcome|performance degradation' "$findings_file"; then
    if [[ "$severity" == "none" ]]; then
      severity="research"
      failure_type="$(grep -Eai 'acceptance rate collapse|coverage degraded|coverage collapse|excessive spread|excessive RVOL|rejection outcome|performance degradation' "$findings_file" | head -n 1 | sed 's/^[[:space:]]*//; s/[[:space:]]*$//; s/[^A-Za-z0-9_ .:-]/ /g' | cut -c1-80)"
    fi
  fi

  if [[ -z "$failure_type" ]]; then
    failure_type="no reportable failure"
  fi
  printf '%s|%s\n' "$severity" "$failure_type"
}

fingerprint_for() {
  local env_name="$1"
  local severity="$2"
  local failure_type="$3"
  if [[ "$env_name" == "PAPER" && "$failure_type" == "trade attribution corrupt artifact" ]]; then
    printf 'paper:trade_attribution_corrupt_artifact\n'
    return
  fi
  if [[ "$env_name" == "PAPER" && "$failure_type" == "core rebuild churn guard error" ]]; then
    printf 'paper:core_rebuild_churn_guard_error\n'
    return
  fi
  printf 'autofail:%s:%s:%s' "$env_name" "$severity" "$failure_type" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9:._-' '-'
}

issue_exists() {
  local fingerprint="$1"
  local issues
  if ! have_cmd gh; then
    return 1
  fi
  issues="$(gh issue list --state open --search "$fingerprint" --json number,title --limit 20 2>/dev/null)" || return 1
  if printf '%s\n' "$issues" | grep -q "$fingerprint"; then
    return 0
  fi
  if [[ "$fingerprint" == autofail:paper:* ]] && printf '%s\n' "$issues" | grep -q "autofail:live:"; then
    return 1
  fi
  if [[ "$fingerprint" == autofail:live:* ]] && printf '%s\n' "$issues" | grep -q "autofail:paper:"; then
    return 1
  fi
  printf '%s\n' "$issues" | grep -Eq '"number"[[:space:]]*:'
}

create_issue() {
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
    echo "gh not available; report written to $body_file"
    return 0
  fi

  if issue_exists "$fingerprint"; then
    echo "Existing issue found for $fingerprint"
    return 0
  fi

  gh issue create \
    --title "$title" \
    --body-file "$body_file" \
    --label "auto-fix" \
    --label "codex" \
    --label "algo-failure" \
    --label "environment:$(printf '%s' "$env_name" | tr '[:upper:]' '[:lower:]')" \
    --label "$processor_label" \
    --label "severity:$severity"
  echo "AUTOOPS_ISSUE_CREATED env=${env_name} fingerprint=${fingerprint}"
}

write_env_report() {
  local env_name="$1"
  local unit="$2"
  local user_id
  user_id="$(env_user "$env_name")"
  local tmpdir
  tmpdir="${TMPDIR:-/tmp}"
  tmpdir="${tmpdir%/}"
  local env_report="${tmpdir}/algo_failure_report_${env_name}_$$.md"
  local journal_file="${tmpdir}/algo_failure_journal_${env_name}_$$.log"
  local findings_file="${tmpdir}/algo_failure_findings_${env_name}_$$.log"
  local timestamp host branch commit status_txt state failed_state severity failure_type fingerprint title

  timestamp="$(date_utc)"
  host="$(hostname 2>/dev/null || echo unknown)"
  branch="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
  commit="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  status_txt="$(git -C "$ROOT" status --short 2>/dev/null || true)"
  if [[ "$env_name" == "PAPER" ]]; then
    state="$(paper_service_state "$unit")"
    failed_state="$(paper_service_failed_state "$unit")"
  else
    state="$(service_state "$unit")"
    failed_state="$(service_failed_state "$unit")"
  fi

  collect_journal "$unit" "$env_name" >"$journal_file"
  scan_failure_text "$journal_file" >"$findings_file"
  IFS='|' read -r severity failure_type < <(classify_issue "$env_name" "$unit" "$state" "$failed_state" "$findings_file")
  fingerprint="$(fingerprint_for "$env_name" "$severity" "$failure_type")"

  {
    echo "# AUTOFAIL [$env_name] $failure_type $(date_day)"
    echo
    echo "- Environment: $env_name"
    echo "- environment=$(printf '%s' "$env_name" | tr '[:upper:]' '[:lower:]')"
    echo "- Severity: $severity"
    echo "- Failure Type: $failure_type"
    echo "- Detection Timestamp: $timestamp"
    echo "- Host: $host"
    echo "- Hostname: $host"
    echo "- User: $user_id"
    echo "- Unit: $unit"
    echo "- Service Name: $unit"
    echo "- Failure Source: failure-reporter"
    echo "- Repo: $ROOT"
    echo "- Git Branch: $branch"
    echo "- Git Commit: $commit"
    echo "- Fingerprint: $fingerprint"
    echo
    echo "## Runtime Context"
    echo
    echo "- Environment: $env_name"
    echo "- Hostname: $host"
    echo "- Service Name: $unit"
    echo "- Failure Source: failure-reporter"
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
    echo "## System Information"
    echo
    echo '```'
    uptime 2>&1 || true
    df -h 2>&1 || true
    free -h 2>&1 || true
    echo '```'
    echo
    if [[ "$env_name" == "PAPER" && "$(uname -s 2>/dev/null || true)" == "Darwin" ]]; then
      echo "## Paper Service Status"
    else
      echo "## systemctl status"
    fi
    echo
    echo '```'
    if [[ "$env_name" == "PAPER" && "$(uname -s 2>/dev/null || true)" == "Darwin" ]]; then
      echo "unit=$unit active_state=$state failed_state=$failed_state launchd_label=${ALGO_PAPER_LAUNCHD_LABEL:-} process_pattern=${ALGO_PAPER_PROCESS_PATTERN:-algo_loop.py --paper}"
    else
      collect_systemctl_status "$unit"
    fi
    echo '```'
    echo
    echo "## Recent Logs"
    echo
    echo '```'
    cat "$journal_file"
    echo '```'
    echo
    echo "## Matched Failure Signals"
    echo
    echo '```'
    cat "$findings_file"
    echo '```'
    echo
    echo "## Diagnostics"
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
    run_readonly "pytest" timeout "$PYTEST_TIMEOUT" env PYTHONPATH=. pytest tests/ -q
    echo '```'
    echo
    echo "## Recommended Next Action"
    echo
    if [[ "$severity" == "research" ]]; then
      echo "Open a research task to inspect performance degradation. Do not change trading rules without review."
    elif [[ "$severity" == "none" ]]; then
      echo "No action required."
    else
      echo "Investigate logs and diagnostics. Create a Codex issue/PR only after confirming root cause. Do not restart services or change trading controls from this reporter."
    fi
  } >"$env_report"

  {
    echo
    echo "---"
    cat "$env_report"
  } >>"$REPORT_PATH"

  if [[ "$severity" == "none" ]]; then
    echo "No reportable failure detected for $env_name"
    return 0
  fi

  title="AUTOFAIL [$env_name] $failure_type $(date_day)"
  create_issue "$title" "$env_report" "$env_name" "$severity" "$fingerprint"
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
    write_env_report "$detected_env" "${UNIT_ARG:-$(default_unit "$detected_env")}"
  else
    write_env_report "$ENV_ARG" "${UNIT_ARG:-$(default_unit "$ENV_ARG")}"
  fi
  echo "Report written to $REPORT_PATH"
}

main "$@"
