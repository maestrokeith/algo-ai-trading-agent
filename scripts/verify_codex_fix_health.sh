#!/usr/bin/env bash
# Verify environment health after a merged Codex repair PR.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${ALGO_REPO_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
REPORT_TMPDIR="${TMPDIR:-/tmp}"
REPORT_TMPDIR="${REPORT_TMPDIR%/}"
REPORT_PATH="${ALGO_POSTFIX_HEALTH_REPORT_PATH:-${REPORT_TMPDIR}/algo_postfix_health_report_$$.md}"

dry_run=0
env_name=""
issue_number=""
pr_number=""

usage() {
  cat <<'USAGE'
Usage: scripts/verify_codex_fix_health.sh --env paper|live --issue NUMBER --pr NUMBER [--dry-run]

Run the environment-specific health checker after a merged Codex repair PR. If
actionable health failures remain, create a POSTFIX follow-up issue for another
repair attempt.
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
        paper|PAPER) env_name="PAPER" ;;
        live|LIVE) env_name="LIVE" ;;
        *)
          echo "--env requires paper or live" >&2
          exit 2
          ;;
      esac
      shift 2
      ;;
    --issue)
      issue_number="${2:-}"
      if [ -z "$issue_number" ]; then
        echo "--issue requires a number" >&2
        exit 2
      fi
      shift 2
      ;;
    --pr)
      pr_number="${2:-}"
      if [ -z "$pr_number" ]; then
        echo "--pr requires a number" >&2
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

if [ -z "$env_name" ] || [ -z "$issue_number" ] || [ -z "$pr_number" ]; then
  usage >&2
  exit 2
fi

say() {
  printf '%s\n' "$*"
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

env_lower() {
  printf '%s' "$env_name" | tr '[:upper:]' '[:lower:]'
}

processor_label_for_env() {
  case "$env_name" in
    PAPER) echo "processor:mac-paper" ;;
    LIVE) echo "processor:live-linux" ;;
    *) echo "processor:live-linux" ;;
  esac
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

report_for_env() {
  printf '/tmp/algo_health_report_%s.md' "$env_name"
}

live_health_summary() {
  local output="$1"
  local status issue
  status="$(printf '%s\n' "$output" | sed -n 's/^LIVE_HEALTH status=//p' | head -n 1)"
  issue="$(printf '%s\n' "$output" | sed -n 's/^LIVE_HEALTH issue=//p' | head -n 1)"
  if [ "$status" = "healthy" ]; then
    echo "severity=none"
    echo "condition=healthy"
    echo "detail=live stabilization healthy"
  else
    echo "severity=critical"
    echo "condition=${issue:-live health unhealthy}"
    echo "detail=${output//$'\n'/; }"
  fi
}

health_summary() {
  local report="$1"
  python - "$report" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

report = Path(sys.argv[1])
if not report.exists():
    print("severity=missing")
    print("condition=health report missing")
    print("detail=health report missing")
    raise SystemExit(0)

severity = ""
condition = ""
detail = ""
suppressed: list[str] = []
in_suppressed = False
in_code = False

for raw in report.read_text(encoding="utf-8", errors="replace").splitlines():
    line = raw.strip()
    if line.startswith("- Severity:"):
        severity = line.split(":", 1)[1].strip()
    elif line.startswith("- Detected Condition:"):
        condition = line.split(":", 1)[1].strip()
    elif line.startswith("- Detail:"):
        detail = line.split(":", 1)[1].strip()
    elif line == "## Suppressed Conditions":
        in_suppressed = True
        in_code = False
    elif in_suppressed and line.startswith("## "):
        in_suppressed = False
        in_code = False
    elif in_suppressed and line == "```":
        in_code = not in_code
    elif in_suppressed and in_code and line and line != "none":
        suppressed.append(line)

print(f"severity={severity or 'unknown'}")
print(f"condition={condition or 'unknown'}")
print(f"detail={detail or 'unknown'}")
print("suppressed=" + "\\n".join(suppressed))
PY
}

field_from_summary() {
  local summary="$1"
  local key="$2"
  printf '%s\n' "$summary" | sed -n "s/^${key}=//p" | head -n 1
}

remaining_failures_text() {
  local severity="$1"
  local condition="$2"
  local detail="$3"
  if [ "$severity" = "none" ] && [ "$condition" = "healthy" ]; then
    printf 'none\n'
  else
    printf '%s|%s|%s\n' "$severity" "$condition" "$detail"
  fi
}

postfix_fingerprint() {
  printf 'postfix:%s:issue-%s:pr-%s' "$(env_lower)" "$issue_number" "$pr_number"
}

issue_exists() {
  local fingerprint="$1"
  if ! have_cmd gh; then
    return 1
  fi
  gh issue list --state open --search "$fingerprint" --json number,title --limit 20 2>/dev/null | grep -q "$fingerprint"
}

create_followup_issue() {
  local report="$1"
  local remaining="$2"
  local fingerprint="$3"
  local host body_file processor_label
  host="$(hostname 2>/dev/null || echo unknown)"
  processor_label="$(processor_label_for_env)"
  body_file="$(portable_mktemp "postfix_health_${env_name}_${pr_number}" ".md")"
  {
    echo "Post-fix health verification found remaining actionable failures."
    echo
    echo "- Original Issue: #${issue_number}"
    echo "- Repair PR: #${pr_number}"
    echo "- Environment: ${env_name}"
    echo "- Hostname: ${host}"
    echo "- Health Report Path: ${report}"
    echo "- Fingerprint: ${fingerprint}"
    echo
    echo "## Remaining Actionable Failures"
    echo
    echo '```'
    printf '%s\n' "$remaining"
    echo '```'
    echo
    echo "Suppressed market-closed stale premarket artifacts should be ignored."
  } > "$body_file"

  if [ "$dry_run" -eq 1 ]; then
    say "Dry run: would create GitHub issue: POSTFIX [${env_name}] repair still unhealthy after PR #${pr_number}"
    say "Dry run fingerprint: ${fingerprint}"
    say "Dry run body: ${body_file}"
    return 0
  fi

  if ! have_cmd gh; then
    say "gh not available; postfix report written to ${body_file}"
    return 0
  fi
  if issue_exists "$fingerprint"; then
    say "Existing POSTFIX issue found for ${fingerprint}"
    return 0
  fi
  gh issue create \
    --title "POSTFIX [${env_name}] repair still unhealthy after PR #${pr_number}" \
    --body-file "$body_file" \
    --label "codex" \
    --label "auto-fix" \
    --label "algo-health" \
    --label "environment:$(env_lower)" \
    --label "$processor_label"
}

main() {
  cd "$ROOT"
  : >"$REPORT_PATH"

  local report summary severity condition detail remaining fingerprint live_output
  report="$REPORT_PATH"
  export ALGO_HEALTH_REPORT_PATH="$report"
  if [ "$env_name" = "LIVE" ] && [ -x ./scripts/check_live_health.sh ]; then
    say "$ ./scripts/check_live_health.sh --env live --dry-run"
    live_output="$(./scripts/check_live_health.sh --env live --dry-run 2>&1)"
    printf '%s\n' "$live_output" >>"$REPORT_PATH"
    summary="$(live_health_summary "$live_output")"
    report="$REPORT_PATH"
  else
    say "$ ./scripts/check_algo_health.sh --env $(env_lower) --dry-run"
    ./scripts/check_algo_health.sh --env "$(env_lower)" --dry-run >>"$REPORT_PATH" 2>&1
    summary="$(health_summary "$report")"
  fi
  severity="$(field_from_summary "$summary" "severity")"
  condition="$(field_from_summary "$summary" "condition")"
  detail="$(field_from_summary "$summary" "detail")"
  remaining="$(remaining_failures_text "$severity" "$condition" "$detail")"

  if [ "$severity" = "none" ] && [ "$condition" = "healthy" ]; then
    say "POST_FIX_VERIFICATION status=healthy env=$(env_lower)"
    return 0
  fi

  say "POST_FIX_VERIFICATION status=unhealthy env=$(env_lower)"
  fingerprint="$(postfix_fingerprint)"
  create_followup_issue "$report" "$remaining" "$fingerprint"
}

main "$@"
