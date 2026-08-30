#!/usr/bin/env bash
# One-shot paper-options repair loop. It creates at most one issue per run.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${ALGO_REPO_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"

env_name="PAPER"
dry_run=0
max_attempts="${PAPER_OPTIONS_MAX_REPAIR_ATTEMPTS_PER_DAY:-3}"
state_dir="${PAPER_OPTIONS_STABILIZATION_STATE_DIR:-$ROOT/data/paper_options_stabilization}"
health_script="${PAPER_OPTIONS_HEALTH_SCRIPT:-$ROOT/scripts/check_paper_options_health.sh}"
invoke_processor=0
postfix_pr=""
issue_number=""

usage() {
  cat <<'USAGE'
Usage: scripts/run_paper_options_stabilization_loop.sh [--env paper] [--dry-run] [--max-attempts N] [--postfix-pr PR] [--issue NUMBER] [--invoke-processor]

Run one paper-options stabilization tick. Live mode is rejected. The script may
create a GitHub issue for Codex repair, but it never merges, deploys, or restarts
services.
USAGE
}

say() {
  printf '%s\n' "$*"
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
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

sanitize() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9._:-' '-'
}

field_from_health() {
  local output="$1"
  local key="$2"
  printf '%s\n' "$output" | sed -n "s/^PAPER_OPTIONS_HEALTH ${key}=//p" | head -n 1
}

first_issue() {
  local output="$1"
  local issue
  issue="$(field_from_health "$output" "issue")"
  printf '%s' "${issue:-unknown_paper_options_instability}"
}

today_utc() {
  date -u +"%Y-%m-%d"
}

attempt_file() {
  printf '%s/attempts_%s' "$state_dir" "$(today_utc)"
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

increment_attempt_count() {
  local count file
  file="$(attempt_file)"
  count="$(attempt_count)"
  if ! [[ "$count" =~ ^[0-9]+$ ]]; then
    count=0
  fi
  mkdir -p "$state_dir"
  printf '%s\n' "$((count + 1))" >"$file"
}

fingerprint_for() {
  local summary="$1"
  if [ -n "$postfix_pr" ]; then
    printf 'paper-options:postfix:pr-%s:%s' "$postfix_pr" "$(sanitize "$summary")"
  else
    printf 'paper-options:%s' "$(sanitize "$summary")"
  fi
}

issue_exists() {
  local fingerprint="$1"
  if ! have_cmd gh; then
    return 1
  fi
  gh issue list --state open --search "$fingerprint" --json number,title,body --limit 20 2>/dev/null | grep -q "$fingerprint"
}

latest_paper_log_excerpt() {
  local log_path
  log_path="$(printf '%s\n' "$health_output" | sed -n 's/^PAPER_OPTIONS_HEALTH log=//p' | head -n 1)"
  if [ -n "$log_path" ] && [ "$log_path" != "none" ] && [ -f "$log_path" ]; then
    tail -n 160 "$log_path"
  else
    echo "No paper options log file found."
  fi
}

create_repair_issue() {
  local summary="$1"
  local health_file="$2"
  local fingerprint="$3"
  local title body_file host
  host="$(hostname 2>/dev/null || echo unknown)"
  body_file="$(portable_mktemp "paper_options_stabilization" ".md")"
  if [ -n "$postfix_pr" ]; then
    title="POSTFIX [PAPER_OPTIONS] still unstable after PR #${postfix_pr}"
  else
    title="PAPER_OPTIONS [PAPER] unstable: ${summary}"
  fi
  {
    if [ -n "$postfix_pr" ]; then
      echo "Paper options remain unstable after repair PR #${postfix_pr}."
      [ -n "$issue_number" ] && echo "Original issue: #${issue_number}"
    else
      echo "Paper options stabilization detected an unhealthy paper-only options state."
    fi
    echo
    echo "environment=paper"
    echo "host=${host}"
    echo "fingerprint=${fingerprint}"
    echo
    echo "## Health Output"
    echo
    echo '```'
    cat "$health_file"
    echo '```'
    echo
    echo "## Latest Paper Options Logs"
    echo
    echo '```'
    latest_paper_log_excerpt
    echo '```'
    echo
    echo "## Safety Rule"
    echo
    echo "No live trading changes. Do not change live options behavior, broker execution, allocator behavior, risk controls, stock entry/exit logic, or position sizing."
    echo
    echo "## Validation"
    echo
    echo '```bash'
    echo "bash -n scripts/check_paper_options_health.sh"
    echo "bash -n scripts/run_paper_options_stabilization_loop.sh"
    echo "PYTHONPATH=. pytest tests/test_paper_options_health.py -v"
    echo "PYTHONPATH=. pytest tests/test_postfix_health_verification.py -v"
    echo '```'
  } >"$body_file"

  if [ "$dry_run" -eq 1 ]; then
    say "Dry run: would create GitHub issue: ${title}"
    say "Dry run fingerprint: ${fingerprint}"
    say "Dry run body: ${body_file}"
    return 0
  fi

  if ! have_cmd gh; then
    say "gh not available; paper-options issue body written to ${body_file}"
    return 0
  fi
  if issue_exists "$fingerprint"; then
    say "Existing PAPER_OPTIONS issue found for ${fingerprint}"
    return 0
  fi
  gh issue create \
    --title "$title" \
    --body-file "$body_file" \
    --label "codex" \
    --label "auto-fix" \
    --label "algo-health" \
    --label "environment:paper" \
    --label "processor:mac-paper" \
    --label "paper-options" >/dev/null
  increment_attempt_count
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run)
      dry_run=1
      shift
      ;;
    --env)
      case "${2:-}" in
        paper|PAPER) env_name="PAPER" ;;
        live|LIVE)
          echo "paper-options stabilization is paper-only; live is rejected" >&2
          exit 2
          ;;
        *)
          echo "--env requires paper" >&2
          exit 2
          ;;
      esac
      shift 2
      ;;
    --max-attempts)
      max_attempts="${2:-}"
      if ! [[ "$max_attempts" =~ ^[0-9]+$ ]] || [ "$max_attempts" -lt 1 ]; then
        echo "--max-attempts requires a positive integer" >&2
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
    --postfix-pr)
      postfix_pr="${2:-}"
      [ -n "$postfix_pr" ] || {
        echo "--postfix-pr requires a PR number" >&2
        exit 2
      }
      shift 2
      ;;
    --issue)
      issue_number="${2:-}"
      [ -n "$issue_number" ] || {
        echo "--issue requires a number" >&2
        exit 2
      }
      shift 2
      ;;
    --invoke-processor)
      invoke_processor=1
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

cd "$ROOT"
mkdir -p "$state_dir"

health_file="$(portable_mktemp "paper_options_health_output" ".log")"
set +e
"$health_script" --env paper >"$health_file" 2>&1
health_rc=$?
set -e
health_output="$(cat "$health_file")"
printf '%s\n' "$health_output"

status="$(field_from_health "$health_output" "status")"
stable_ticks="$(field_from_health "$health_output" "stable_ticks")"
required_ticks="$(field_from_health "$health_output" "required_stable_ticks")"
stable_ticks="${stable_ticks:-0}"
required_ticks="${required_ticks:-3}"

if [ "$status" = "healthy" ] && [ "$stable_ticks" -ge "$required_ticks" ]; then
  say "PAPER_OPTIONS_STABILIZATION status=stable"
  exit 0
fi

if [ "$status" = "healthy" ]; then
  say "PAPER_OPTIONS_STABILIZATION status=warming_up stable_ticks=${stable_ticks} required_stable_ticks=${required_ticks}"
  exit 0
fi

summary="$(first_issue "$health_output")"
fingerprint="$(fingerprint_for "$summary")"
attempts="$(attempt_count)"
if ! [[ "$attempts" =~ ^[0-9]+$ ]]; then
  attempts=0
fi
if [ "$attempts" -ge "$max_attempts" ]; then
  say "PAPER_OPTIONS_STABILIZATION status=needs_human_review attempts=${attempts} max_attempts=${max_attempts}"
  exit 1
fi

create_repair_issue "$summary" "$health_file" "$fingerprint"
if [ -n "$postfix_pr" ]; then
  say "PAPER_OPTIONS_STABILIZATION status=postfix_unhealthy issue=${summary}"
else
  say "PAPER_OPTIONS_STABILIZATION status=repair_issue_ready issue=${summary}"
fi

if [ "$invoke_processor" -eq 1 ]; then
  if [ "$dry_run" -eq 1 ]; then
    say "[dry-run] Would invoke scripts/process_codex_issues_local.sh --env paper"
  else
    scripts/process_codex_issues_local.sh --limit 1
  fi
fi

exit "$health_rc"
