#!/usr/bin/env bash
# Live-only stabilization loop. Creates repair issues; never restarts or trades.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${ALGO_REPO_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
ATTEMPT_STATE_DIR="${ALGO_LIVE_STABILIZATION_STATE_DIR:-/tmp/algo_live_stabilization}"
MAX_ATTEMPTS_PER_DAY="${ALGO_LIVE_MAX_REPAIR_ATTEMPTS_PER_DAY:-1}"

dry_run=0

usage() {
  cat <<'USAGE'
Usage: scripts/run_live_stabilization_loop.sh [--dry-run]
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run)
      dry_run=1
      shift
      ;;
    --env)
      if [ "${2:-}" != "live" ] && [ "${2:-}" != "LIVE" ]; then
        echo "run_live_stabilization_loop only supports --env live" >&2
        exit 2
      fi
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

host_name() {
  hostname 2>/dev/null || echo unknown
}

os_name() {
  uname -s 2>/dev/null || echo unknown
}

today_key() {
  date +%Y%m%d
}

field() {
  local text="$1"
  local key="$2"
  printf '%s\n' "$text" | sed -n "s/^LIVE_HEALTH ${key}=//p" | head -n 1
}

fingerprint_for() {
  local reason="$1"
  printf 'live-stabilization:live:%s' "$reason" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9:._-' '-'
}

issue_exists() {
  local fingerprint="$1"
  if ! have_cmd gh; then
    return 1
  fi
  gh issue list --state open --search "$fingerprint" --json number,title --limit 20 2>/dev/null | grep -q "$fingerprint"
}

attempt_file() {
  mkdir -p "$ATTEMPT_STATE_DIR"
  printf '%s/live_%s.count' "$ATTEMPT_STATE_DIR" "$(today_key)"
}

attempt_count() {
  local file
  file="$(attempt_file)"
  if [ -f "$file" ]; then
    cat "$file" 2>/dev/null || echo 0
  else
    echo 0
  fi
}

record_attempt() {
  local count file
  file="$(attempt_file)"
  count="$(attempt_count)"
  count=$((count + 1))
  if [ "$dry_run" -eq 0 ]; then
    printf '%s\n' "$count" >"$file"
  fi
}

create_issue() {
  local summary="$1"
  local health_output="$2"
  local fingerprint="$3"
  local body_file host service_state broker premarket
  host="$(host_name)"
  service_state="$(field "$health_output" "service_state")"
  broker="$(field "$health_output" "broker")"
  premarket="$(field "$health_output" "premarket")"
  body_file="$(portable_mktemp live_stabilization .md)"
  {
    echo "Live stabilization detected an unsafe or unhealthy live runtime condition."
    echo
    echo "- environment=live"
    echo "- hostname=${host}"
    echo "- service_state=${service_state}"
    echo "- broker_account_check=${broker}"
    echo "- premarket_artifact_status=${premarket}"
    echo "- fingerprint=${fingerprint}"
    echo
    echo "## Detected Failure Summary"
    echo
    echo '```'
    printf '%s\n' "$health_output"
    echo '```'
    echo
    echo "## Latest Relevant Logs"
    echo
    echo '```'
    journalctl -u "${ALGO_LIVE_SERVICE:-algo.service}" --since "${ALGO_LIVE_HEALTH_JOURNAL_SINCE:-30 minutes ago}" --no-pager 2>&1 | tail -n 120 || true
    echo '```'
    echo
    echo "Safety instruction: Do not change trading logic, sizing, risk controls, allocator behavior, broker execution, or options live behavior."
  } >"$body_file"

  if [ "$dry_run" -eq 1 ]; then
    echo "Dry run: would create GitHub issue: LIVE_STABILIZATION [LIVE] unstable: ${summary}"
    echo "Dry run fingerprint: ${fingerprint}"
    return 0
  fi
  if ! have_cmd gh; then
    echo "gh not available; live stabilization body written to ${body_file}"
    return 0
  fi
  if issue_exists "$fingerprint"; then
    echo "Existing live stabilization issue found for ${fingerprint}"
    return 0
  fi
  gh issue create \
    --title "LIVE_STABILIZATION [LIVE] unstable: ${summary}" \
    --body-file "$body_file" \
    --label "codex" \
    --label "auto-fix" \
    --label "algo-health" \
    --label "environment:live" \
    --label "processor:live-linux" \
    --label "live-stabilization" \
    --label "needs-human-review" >/dev/null
}

main() {
  cd "$ROOT"
  if [ "$(os_name)" != "Linux" ] || [ "$(host_name)" != "algosphere-live-host" ]; then
    echo "LIVE_STABILIZATION status=wrong_host"
    echo "LIVE_STABILIZATION host=$(host_name)"
    return 2
  fi

  local output status reason fingerprint count
  if [ "$dry_run" -eq 1 ]; then
    output="$("$ROOT/scripts/check_live_health.sh" --env live --dry-run)"
  else
    output="$("$ROOT/scripts/check_live_health.sh" --env live)"
  fi
  printf '%s\n' "$output"
  status="$(field "$output" "status")"
  reason="$(field "$output" "issue")"
  if [ "$status" = "healthy" ]; then
    echo "LIVE_STABILIZATION status=stable"
    return 0
  fi

  count="$(attempt_count)"
  if [ "$count" -ge "$MAX_ATTEMPTS_PER_DAY" ]; then
    echo "LIVE_STABILIZATION status=needs_human_review"
    return 0
  fi
  fingerprint="$(fingerprint_for "$reason")"
  create_issue "$reason" "$output" "$fingerprint"
  record_attempt
  echo "LIVE_STABILIZATION status=repair_issue_created"
}

main "$@"
