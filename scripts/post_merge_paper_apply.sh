#!/usr/bin/env bash
# Apply a merged paper Codex PR on the local paper host, then run smoke checks.

set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/post_merge_paper_apply.sh [--dry-run] [--pr NUMBER] [--limit N]

Pull main after a merged PAPER Codex PR, restart the local paper service, and
run paper smoke checks. This script is local-host only and never restarts live.

Options:
  --dry-run     Show what would happen without pulling, restarting, or creating issues.
  --pr NUMBER  Apply one merged PR.
  --limit N    Scan/apply up to N merged paper PRs. Default: 1.
  -h, --help   Show this help.
USAGE
}

repo_root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$repo_root"

dry_run=0
pr_number=""
limit=1
paper_service="${ALGO_PAPER_SERVICE:-paper.service}"
state_dir="${ALGO_CODEX_POST_MERGE_STATE_DIR:-data/local_codex_post_merge}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run)
      dry_run=1
      shift
      ;;
    --pr)
      pr_number="${2:-}"
      if [ -z "$pr_number" ]; then
        echo "--pr requires a number" >&2
        exit 2
      fi
      shift 2
      ;;
    --limit)
      limit="${2:-}"
      if ! [[ "$limit" =~ ^[0-9]+$ ]] || [ "$limit" -lt 1 ]; then
        echo "--limit requires a positive integer" >&2
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

read_lines_into_array() {
  local array_name="$1"
  shift
  local line
  eval "$array_name=()"
  while IFS= read -r line; do
    eval "$array_name+=(\"\$line\")"
  done < <("$@")
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required command not found: $1" >&2
    exit 2
  fi
}

comment_pr() {
  local pr="$1"
  local body="$2"
  if [ "$dry_run" -eq 1 ]; then
    say "[dry-run] Would comment on PR #${pr}: ${body}"
    return 0
  fi
  gh pr comment "$pr" --body "$body"
}

comment_issue() {
  local issue="$1"
  local body="$2"
  [ -n "$issue" ] || return 0
  if [ "$dry_run" -eq 1 ]; then
    say "[dry-run] Would comment on issue #${issue}: ${body}"
    return 0
  fi
  gh issue comment "$issue" --body "$body"
}

issue_from_pr_json() {
  local json_path="$1"
  python - "$json_path" <<'PY'
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    pr = json.load(handle)
text = "\n".join([pr.get("title") or "", pr.get("body") or ""])
match = re.search(r"(?:Fixes|Closes|Resolves)\s+#(\d+)", text, re.IGNORECASE)
print(match.group(1) if match else "")
PY
}

is_paper_pr_json() {
  local json_path="$1"
  python - "$json_path" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    pr = json.load(handle)
labels = {item.get("name", "") for item in pr.get("labels") or []}
text = "\n".join([pr.get("title") or "", pr.get("body") or ""]).upper()
print("true" if "environment:paper" in labels or "[PAPER]" in text else "false")
PY
}

list_merged_codex_prs() {
  if [ -n "$pr_number" ]; then
    printf '%s\n' "$pr_number"
    return 0
  fi
  gh pr list \
    --state merged \
    --search 'head:codex/' \
    --limit 50 \
    --json number,labels \
    --jq '.[] | select([.labels[].name] | index("codex-validation-passed")) | .number' |
    head -n "$limit"
}

run_smoke_checks() {
  local log_path="$1"
  set +e
  {
    smoke_rc=0
    echo "$ ./bin/algo summary latest --user paper_bot"
    ./bin/algo summary latest --user paper_bot
    rc=$?
    echo "[exit_code=$rc]"
    [ "$rc" -eq 0 ] || smoke_rc=$rc
    echo
    echo "$ scripts/check_algo_health.sh --dry-run PAPER"
    scripts/check_algo_health.sh --dry-run PAPER
    rc=$?
    echo "[exit_code=$rc]"
    [ "$rc" -eq 0 ] || smoke_rc=$rc
    exit "$smoke_rc"
  } > "$log_path" 2>&1
  rc=$?
  set -e
  return "$rc"
}

restart_paper_service() {
  if command -v systemctl >/dev/null 2>&1; then
    systemctl restart "$paper_service"
    return $?
  fi
  if [ "$(uname -s 2>/dev/null || true)" = "Darwin" ]; then
    local launchd_label="${ALGO_PAPER_LAUNCHD_LABEL:-}"
    local process_pattern="${ALGO_PAPER_PROCESS_PATTERN:-algo_loop.py --paper}"
    if [ -n "$launchd_label" ] && command -v launchctl >/dev/null 2>&1; then
      local target="gui/$(id -u)/${launchd_label}"
      if launchctl print "$target" >/dev/null 2>&1; then
        say "Restarting paper service with launchd label ${launchd_label}."
        launchctl kickstart -k "$target" 2>/dev/null || {
          launchctl stop "$target" 2>/dev/null || true
          launchctl start "$target"
        }
        return $?
      fi
      say "launchd label ${launchd_label} is not loaded; cannot restart it automatically."
    else
      say "ALGO_PAPER_LAUNCHD_LABEL is not configured; cannot restart paper launchd service automatically."
    fi
    say "skipping paper service restart and continuing smoke checks"
    say "Fallback: start paper manually or configure launchd. Suggested process check: pgrep -f '${process_pattern}'"
    return 0
  fi
  echo "systemctl not found; cannot restart ${paper_service}" >&2
  return 1
}

create_paper_failure_issue() {
  local pr="$1"
  local smoke_log="$2"
  local body_file
  body_file="$(portable_mktemp "paper_post_merge_failure_${pr}" ".md")"
  {
    echo "Paper post-merge apply failed after Codex PR #${pr}."
    echo
    echo "Environment: PAPER"
    echo "Severity: high"
    echo
    echo "Diagnostics:"
    echo '```'
    tail -n 160 "$smoke_log"
    echo '```'
  } > "$body_file"
  if [ "$dry_run" -eq 1 ]; then
    say "[dry-run] Would create paper failure issue for PR #${pr}"
    return 0
  fi
  gh issue create \
    --title "AUTOFAIL [PAPER] post-merge smoke failed after PR #${pr}" \
    --label "algo-failure" \
    --label "environment:paper" \
    --label "severity:high" \
    --body-file "$body_file" >/dev/null
}

apply_pr() {
  local pr="$1"
  local pr_json issue paper state_file smoke_log
  pr_json="$(portable_mktemp "post_merge_pr_${pr}" ".json")"
  gh pr view "$pr" --json number,title,body,labels,mergedAt,headRefName,url > "$pr_json"

  if [ "$(is_paper_pr_json "$pr_json")" != "true" ]; then
    say "Skipping PR #${pr}: not a PAPER PR"
    return 0
  fi

  state_file="${state_dir}/pr_${pr}.done"
  if [ -f "$state_file" ]; then
    say "Skipping PR #${pr}: already applied locally (${state_file})"
    return 0
  fi

  issue="$(issue_from_pr_json "$pr_json")"
  smoke_log="$(portable_mktemp "paper_post_merge_smoke_${pr}" ".log")"

  say "Applying merged paper PR #${pr}"
  if [ "$dry_run" -eq 1 ]; then
    say "[dry-run] Would git checkout main && git pull --ff-only"
    say "[dry-run] Would restart paper service: ${paper_service}"
    say "[dry-run] Would run paper smoke checks"
    return 0
  fi

  if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "Worktree is dirty; refusing paper post-merge apply." >&2
    return 1
  fi

  git checkout main
  git pull --ff-only

  restart_paper_service

  if run_smoke_checks "$smoke_log"; then
    mkdir -p "$state_dir"
    date -Iseconds > "$state_file"
    comment_pr "$pr" "Paper post-merge apply succeeded locally. Pulled main, restarted ${paper_service}, and paper smoke checks passed."
    comment_issue "$issue" "Paper post-merge apply succeeded for PR #${pr}."
  else
    create_paper_failure_issue "$pr" "$smoke_log"
    {
      echo "Paper post-merge apply failed for PR #${pr}."
      echo
      echo '```'
      tail -n 120 "$smoke_log"
      echo '```'
    } > /tmp/paper_post_merge_comment.md
    gh pr comment "$pr" --body-file /tmp/paper_post_merge_comment.md
    comment_issue "$issue" "Paper post-merge apply failed for PR #${pr}; opened a PAPER failure issue."
    return 1
  fi
}

main() {
  require_cmd git
  require_cmd gh
  require_cmd python

  read_lines_into_array prs list_merged_codex_prs
  if [ "${#prs[@]}" -eq 0 ]; then
    say "No merged Codex paper PRs need local apply."
    return 0
  fi

  local processed=0
  local pr
  for pr in "${prs[@]}"; do
    [ -n "$pr" ] || continue
    apply_pr "$pr"
    processed=$((processed + 1))
    if [ "$processed" -ge "$limit" ]; then
      break
    fi
  done
}

main "$@"
