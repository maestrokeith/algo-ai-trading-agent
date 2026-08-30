#!/usr/bin/env bash
# AutoOps Codex Auto-Fix: process GitHub issues with local Codex CLI using the laptop's existing ChatGPT login.

set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/process_codex_issues_local.sh [--paper|--live] [--dry-run] [--issue NUMBER] [--limit N] [--no-push] [--no-pr]

Poll open GitHub issues labeled "codex" and "auto-fix", run local `codex exec`,
validate changes, push a branch, and open a PR. This is local-only automation.

Options:
  --paper         Process only paper issues labeled environment:paper and processor:mac-paper.
  --live          Process only live issues labeled environment:live and processor:live-linux.
  --dry-run        Show planned work without running Codex, pushing, or opening PRs.
  --issue NUMBER  Process one specific issue number.
  --limit N       Process up to N issues. Default: 1.
  --no-push       Run Codex and validation locally, but do not push the branch.
  --no-pr         Push branch if allowed, but do not open a PR.
  -h, --help      Show this help.
USAGE
}

repo_root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$repo_root"

dry_run=0
issue_number=""
limit=1
no_push=0
no_pr=0
processor_label="${ALGO_CODEX_PROCESSOR_LABEL:-}"
requested_environment=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run)
      dry_run=1
      shift
      ;;
    --paper)
      requested_environment="paper"
      processor_label="processor:mac-paper"
      shift
      ;;
    --live)
      requested_environment="live"
      processor_label="processor:live-linux"
      shift
      ;;
    --issue)
      issue_number="${2:-}"
      if [ -z "$issue_number" ]; then
        echo "--issue requires a number" >&2
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
    --no-push)
      no_push=1
      shift
      ;;
    --no-pr)
      no_pr=1
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

labels_include() {
  local labels="$1"
  local needle="$2"
  grep -Fxq "$needle" <<<"$labels"
}

host_name() {
  hostname 2>/dev/null || echo unknown
}

detected_processor_label() {
  local os_name host
  if [ -n "$processor_label" ]; then
    printf '%s\n' "$processor_label"
    return
  fi
  os_name="$(uname -s 2>/dev/null || echo unknown)"
  host="$(host_name)"
  if [ "$os_name" = "Darwin" ]; then
    printf 'processor:mac-paper\n'
  elif [ "$os_name" = "Linux" ] && [ "$host" = "algosphere-live-host" ]; then
    printf 'processor:live-linux\n'
  else
    printf 'processor:unknown\n'
  fi
}

processor_environment() {
  case "$1" in
    processor:mac-paper) printf 'paper\n' ;;
    processor:fedora-live) printf 'live\n' ;;
    processor:live-linux) printf 'live\n' ;;
    *) printf 'unknown\n' ;;
  esac
}

issue_environment_label() {
  local labels="$1"
  if labels_include "$labels" "environment:paper"; then
    printf 'paper\n'
  elif labels_include "$labels" "environment:live"; then
    printf 'live\n'
  else
    printf 'unknown\n'
  fi
}

comment_issue() {
  local issue="$1"
  local body="$2"
  if [ "$dry_run" -eq 1 ]; then
    say "[dry-run] Would comment on issue #${issue}: ${body}"
    return 0
  fi
  gh issue comment "$issue" --body "$body"
}

add_issue_label() {
  local issue="$1"
  local label="$2"
  if [ "$dry_run" -eq 1 ]; then
    say "[dry-run] Would add label '${label}' to issue #${issue}"
    return 0
  fi
  gh issue edit "$issue" --add-label "$label" >/dev/null
}

fetch_issue_json() {
  local issue="$1"
  local output="$2"
  gh issue view "$issue" --json number,title,body,labels,url > "$output"
}

issue_labels() {
  local issue_json="$1"
  python - "$issue_json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
for label in payload.get("labels") or []:
    if isinstance(label, dict) and label.get("name"):
        print(label["name"])
    elif isinstance(label, str):
        print(label)
PY
}

issue_title() {
  local issue_json="$1"
  python - "$issue_json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    print((json.load(handle).get("title") or "").strip())
PY
}

issue_context() {
  local issue_json="$1"
  python - "$issue_json" <<'PY'
import json
import sys


def label_names(payload):
    names = []
    for label in payload.get("labels") or []:
        if isinstance(label, dict) and label.get("name"):
            names.append(str(label["name"]))
        elif isinstance(label, str):
            names.append(label)
    return names


def field_from_body(body, names):
    for line in body.splitlines():
        stripped = line.strip().lstrip("-").strip()
        for name in names:
            prefix = f"{name}:"
            if stripped.lower().startswith(prefix.lower()):
                return stripped[len(prefix):].strip()
            key = f"{name}="
            if stripped.lower().startswith(key.lower()):
                return stripped[len(key):].strip()
    return "unknown"


with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
labels = {label.lower() for label in label_names(payload)}
title = str(payload.get("title") or "")
body = payload.get("body") or ""
combined = f"{title}\n{body}".upper()
if "environment:paper" in labels or "[PAPER]" in combined or "ENVIRONMENT: PAPER" in combined or "ENVIRONMENT=PAPER" in combined:
    environment = "PAPER"
elif "environment:live" in labels or "[LIVE]" in combined or "ENVIRONMENT: LIVE" in combined or "ENVIRONMENT=LIVE" in combined:
    environment = "LIVE"
else:
    environment = "UNKNOWN"
print(f"Environment: {environment}")
print(f"Hostname: {field_from_body(body, ('Hostname', 'Host'))}")
print(f"Service name: {field_from_body(body, ('Service Name', 'Unit', 'Service'))}")
print(f"Failure source: {field_from_body(body, ('Failure Source',))}")
PY
}

list_candidate_issues() {
  if [ -n "$issue_number" ]; then
    printf '%s\n' "$issue_number"
    return 0
  fi
  if [ -n "$requested_environment" ] && [ -n "$processor_label" ]; then
    gh issue list \
      --state open \
      --label codex \
      --label auto-fix \
      --label "environment:${requested_environment}" \
      --label "$processor_label" \
      --limit 100 \
      --json number,labels \
      --jq '.[] | select(([.labels[].name] | index("needs-human-review") | not) and ([.labels[].name] | index("codex-pr-opened") | not)) | .number' |
      head -n "$limit"
    return 0
  fi
  gh issue list \
    --state open \
    --label codex \
    --label auto-fix \
    --limit 100 \
    --json number,labels \
    --jq '.[] | select(([.labels[].name] | index("needs-human-review") | not) and ([.labels[].name] | index("codex-pr-opened") | not)) | .number' |
    head -n "$limit"
}

acquire_lock() {
  local issue="$1"
  local lock="/tmp/algo_codex_issue_${issue}.lock"
  local existing_pid
  if ! (set -o noclobber; printf '%s\n' "$$" > "$lock") 2>/dev/null; then
    existing_pid="$(cat "$lock" 2>/dev/null || true)"
    if [[ "$existing_pid" =~ ^[0-9]+$ ]] && kill -0 "$existing_pid" 2>/dev/null; then
      say "Skipping issue #${issue}: active lock already exists at ${lock} pid=${existing_pid}"
      say "CODEX_RESULT issue=${issue} status=codex_running reason=active_lock"
      return 1
    fi
    say "Removing stale lock for issue #${issue}: ${lock}"
    rm -f "$lock"
    if ! (set -o noclobber; printf '%s\n' "$$" > "$lock") 2>/dev/null; then
      say "Skipping issue #${issue}: lock already exists at ${lock}"
      say "CODEX_RESULT issue=${issue} status=codex_running reason=lock_race"
      return 1
    fi
  fi
  active_locks+=("$lock")
  return 0
}

cleanup_locks() {
  local lock
  for lock in "${active_locks[@]:-}"; do
    if [ -f "$lock" ] && [ "$(cat "$lock" 2>/dev/null || true)" = "$$" ]; then
      rm -f "$lock"
    fi
  done
}

check_duplicate_state() {
  local issue="$1"
  local branch="$2"
  local issue_json="$3"
  local labels
  labels="$(issue_labels "$issue_json")"

  if labels_include "$labels" "codex-pr-opened"; then
    say "Skipping issue #${issue}: issue already has codex-pr-opened"
    return 1
  fi
  if ! labels_include "$labels" "codex" || ! labels_include "$labels" "auto-fix"; then
    say "Skipping issue #${issue}: issue must have both codex and auto-fix labels"
    return 1
  fi
  if labels_include "$labels" "needs-human-review"; then
    say "Skipping issue #${issue}: issue has needs-human-review"
    return 1
  fi
  if git show-ref --verify --quiet "refs/heads/${branch}"; then
    say "Skipping issue #${issue}: local branch ${branch} already exists"
    return 1
  fi
  if git ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1; then
    say "Skipping issue #${issue}: remote branch ${branch} already exists"
    return 1
  fi
  local existing_pr
  existing_pr="$(gh pr list --state open --head "$branch" --json url --jq '.[0].url // empty')"
  if [ -n "$existing_pr" ]; then
    say "CODEX_DUPLICATE_CHECK issue=${issue} result=duplicate source=exact_branch pr=unknown"
    say "Skipping issue #${issue}: open PR already exists: ${existing_pr}"
    return 1
  fi
  say "CODEX_DUPLICATE_CHECK issue=${issue} result=not_duplicate source=none pr=none"
  return 0
}

run_precheck() {
  local issue="$1"
  local branch="$2"
  local issue_json="$3"
  local precheck_output precheck_rc label close_issue comment
  set +e
  precheck_output="$(python scripts/codex_issue_precheck.py --issue-json "$issue_json" --issue "$issue" --branch "$branch" --repo-root "$repo_root" 2>&1)"
  precheck_rc=$?
  set -e
  printf '%s\n' "$precheck_output"
  if [ "$precheck_rc" -eq 0 ]; then
    return 0
  fi
  label="$(printf '%s\n' "$precheck_output" | sed -n 's/^CODEX_PRECHECK_LABEL label=//p' | head -n 1)"
  close_issue="$(printf '%s\n' "$precheck_output" | grep -c '^CODEX_PRECHECK_CLOSE close=true' || true)"
  comment="$(
    printf 'Codex precheck decided not to run Codex for issue #%s.\n\n' "$issue"
    printf 'Evidence:\n\n'
    printf '```text\n%s\n```\n' "$precheck_output"
  )"
  if [ -n "$label" ]; then
    add_issue_label "$issue" "$label" || true
  fi
  comment_issue "$issue" "$comment" || true
  if [ "$close_issue" -gt 0 ]; then
    if [ "$dry_run" -eq 1 ]; then
      say "[dry-run] Would close issue #${issue} after precheck"
    else
      gh issue close "$issue" --reason completed >/dev/null || true
    fi
  fi
  say "CODEX_RESULT issue=${issue} status=no_codex reason=precheck"
  return 1
}

check_issue_routing() {
  local issue="$1"
  local issue_json="$2"
  local labels processor expected_env issue_env host
  labels="$(issue_labels "$issue_json")"
  processor="$(detected_processor_label)"
  expected_env="$(processor_environment "$processor")"
  issue_env="$(issue_environment_label "$labels")"
  host="$(host_name)"

  say "ISSUE_ROUTING issue_number=${issue} environment=${issue_env} processor=${processor} hostname=${host}"

  if [ "$issue_env" = "unknown" ]; then
    say "ISSUE_SKIP issue_number=${issue} reason=missing_environment_label environment=${issue_env} processor=${processor} hostname=${host}"
    say "ISSUE_REJECTED issue_number=${issue} environment=${issue_env} processor=${processor} hostname=${host}"
    return 1
  fi
  if [ "$expected_env" = "unknown" ] || [ "$issue_env" != "$expected_env" ]; then
    say "ISSUE_SKIP issue_number=${issue} reason=environment_mismatch environment=${issue_env} processor=${processor} hostname=${host}"
    say "ISSUE_REJECTED issue_number=${issue} environment=${issue_env} processor=${processor} hostname=${host}"
    return 1
  fi
  if ! labels_include "$labels" "$processor"; then
    say "ISSUE_SKIP issue_number=${issue} reason=missing_processor_label environment=${issue_env} processor=${processor} hostname=${host}"
    say "ISSUE_REJECTED issue_number=${issue} environment=${issue_env} processor=${processor} hostname=${host}"
    return 1
  fi

  say "ISSUE_ACCEPTED issue_number=${issue} environment=${issue_env} processor=${processor} hostname=${host}"
  say "CODEX_ELIGIBLE issue_number=${issue} environment=${issue_env} processor=${processor} hostname=${host}"
  return 0
}

run_validation() {
  local log_path="$1"
  set +e
  {
    validation_rc=0
    echo "$ bash -n scripts/report_algo_failure_to_github.sh"
    bash -n scripts/report_algo_failure_to_github.sh
    rc=$?
    echo "[exit_code=$rc]"
    [ "$rc" -eq 0 ] || validation_rc=$rc

    echo "$ bash -n scripts/check_algo_health.sh"
    bash -n scripts/check_algo_health.sh
    rc=$?
    echo "[exit_code=$rc]"
    [ "$rc" -eq 0 ] || validation_rc=$rc

    echo "$ python -m py_compile scripts/analyze_algo_health_report.py"
    python -m py_compile scripts/analyze_algo_health_report.py
    rc=$?
    echo "[exit_code=$rc]"
    [ "$rc" -eq 0 ] || validation_rc=$rc

    echo "$ PYTHONPATH=. pytest tests/test_failure_reporter.py tests/test_algo_health_check.py tests/test_algo_health_analyzer.py tests/test_codex_pr_validation_workflow.py -v"
    PYTHONPATH=. pytest tests/test_failure_reporter.py tests/test_algo_health_check.py tests/test_algo_health_analyzer.py tests/test_codex_pr_validation_workflow.py -v
    rc=$?
    echo "[exit_code=$rc]"
    [ "$rc" -eq 0 ] || validation_rc=$rc

    echo "$ PYTHONPATH=. pytest tests/ -q"
    PYTHONPATH=. pytest tests/ -q
    rc=$?
    echo "[exit_code=$rc]"
    [ "$rc" -eq 0 ] || validation_rc=$rc

    exit "$validation_rc"
  } > "$log_path" 2>&1
  rc=$?
  set -e
  return "$rc"
}

process_issue() {
  local issue="$1"
  local branch="codex/issue-${issue}-auto-fix"
  local lock_acquired=0
  local issue_json prompt_path result_path validation_log pr_body title pr_url context_text

  say "Preparing issue #${issue}"
  if [ "$dry_run" -eq 1 ]; then
    say "[dry-run] Would use lock file /tmp/algo_codex_issue_${issue}.lock"
  else
    acquire_lock "$issue" || return 0
    lock_acquired=1
  fi

  issue_json="$(portable_mktemp "codex_issue_${issue}" ".json")"
  prompt_path="$(portable_mktemp "codex_prompt_${issue}" ".txt")"
  result_path="$(portable_mktemp "codex_result_${issue}" ".txt")"
  validation_log="$(portable_mktemp "codex_validation_${issue}" ".log")"

  if ! fetch_issue_json "$issue" "$issue_json"; then
    if [ "$dry_run" -eq 1 ]; then
      say "[dry-run] Could not fetch issue #${issue}; would otherwise build prompt and process branch ${branch}."
      return 0
    fi
    echo "Unable to fetch issue #${issue}" >&2
    return 1
  fi

  if ! check_issue_routing "$issue" "$issue_json"; then
    return 0
  fi

  title="$(issue_title "$issue_json")"
  context_text="$(issue_context "$issue_json")"
  if [ "$dry_run" -eq 1 ]; then
    run_precheck "$issue" "$branch" "$issue_json" || return 0
    say "[dry-run] Would check duplicate branch/PR state for ${branch}"
    say "[dry-run] Would add codex-running to issue #${issue}"
    say "[dry-run] Would create branch ${branch}"
    say "[dry-run] Would run: codex exec --cd ${repo_root} --sandbox workspace-write --output-last-message ${result_path} - < ${prompt_path}"
    say "[dry-run] Would run local validation, commit, push, and open a PR unless disabled."
    return 0
  fi

  if ! run_precheck "$issue" "$branch" "$issue_json"; then
    return 0
  fi

  if ! check_duplicate_state "$issue" "$branch" "$issue_json"; then
    say "CODEX_RESULT issue=${issue} status=no_change reason=duplicate_state"
    return 0
  fi

  if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "Worktree is dirty; refusing to run local Codex issue processor." >&2
    say "CODEX_RESULT issue=${issue} status=failed reason=dirty_worktree"
    return 0
  fi

  add_issue_label "$issue" "codex-running"
  comment_issue "$issue" "Local Codex processing started for issue #${issue}. This laptop workflow creates a PR only; it does not merge, deploy, restart services, or use broker credentials."
  say "AUTOOPS_CODEX_STARTED issue=${issue} dry_run=${dry_run}"

  python scripts/build_codex_issue_prompt.py --issue-json "$issue_json" --output "$prompt_path"

  git fetch origin main >/dev/null 2>&1 || true
  git checkout -B "$branch"

  say "CODEX_VERSION_BEGIN"
  codex --version
  say "CODEX_VERSION_END"
  say "CODEX_LOCAL_AUTH mode=chatgpt-login"
  say "CODEX_SANDBOX_MODE selected=workspace-write"
  codex exec \
    --cd "$repo_root" \
    --sandbox workspace-write \
    --output-last-message "$result_path" \
    - < "$prompt_path"

  if git diff --quiet && git diff --cached --quiet; then
    add_issue_label "$issue" "needs-human-review"
    comment_issue "$issue" "Local Codex completed without file changes for issue #${issue}. Marking for human review."
    say "CODEX_RESULT issue=${issue} status=no_change"
    return 0
  fi

  comment_issue "$issue" "Local Codex produced file changes for issue #${issue}. Validation is starting."

  if ! run_validation "$validation_log"; then
    add_issue_label "$issue" "codex-validation-failed"
    {
      echo "Local Codex validation failed for issue #${issue}. No PR was opened."
      echo
      echo '```'
      tail -n 120 "$validation_log"
      echo '```'
    } > /tmp/local_codex_validation_comment.md
    gh issue comment "$issue" --body-file /tmp/local_codex_validation_comment.md
    say "CODEX_RESULT issue=${issue} status=failed reason=validation_failed"
    return 0
  fi

  comment_issue "$issue" "Local Codex validation passed for issue #${issue}."
  git add -A
  git commit -m "Codex auto-fix for issue #${issue}"

  if [ "$no_push" -eq 1 ]; then
    say "--no-push set; leaving committed branch local: ${branch}"
    comment_issue "$issue" "Local Codex committed changes on branch ${branch}, but --no-push was set. No PR was opened."
    say "CODEX_RESULT issue=${issue} status=fix_local_only reason=no_push"
    return 0
  fi

  if ! git push --set-upstream origin "$branch"; then
    comment_issue "$issue" "Local Codex committed changes on branch ${branch}, but push failed. Manual action is required to push the branch and open a PR."
    say "CODEX_RESULT issue=${issue} status=fix_local_only reason=push_failed"
    return 0
  fi

  if [ "$no_pr" -eq 1 ]; then
    say "--no-pr set; branch pushed without opening PR: ${branch}"
    comment_issue "$issue" "Local Codex pushed branch ${branch}, but --no-pr was set. No PR was opened."
    say "CODEX_RESULT issue=${issue} status=fix_local_only reason=no_pr"
    return 0
  fi

  pr_body="$(portable_mktemp "codex_pr_${issue}" ".md")"
  {
    echo "Fixes #${issue}"
    echo
    echo "## Runtime Context"
    printf '%s\n' "$context_text"
    echo
    echo "## Summary"
    if [ -s "$result_path" ]; then
      cat "$result_path"
    else
      echo "Local Codex generated changes for this issue."
    fi
    echo
    echo "## Validation"
    echo '```'
    grep -E '^\$ |^\[exit_code=' "$validation_log" || true
    echo '```'
    echo
    echo "## Safety"
    echo "- PR requires human review before merge"
    echo "- no merge performed"
    echo "- no deploy performed"
    echo "- no live service restart performed"
    echo "- no live broker credentials used"
  } > "$pr_body"

  if ! pr_url="$(gh pr create \
    --head "$branch" \
    --base main \
    --title "Codex auto-fix: ${title}" \
    --body-file "$pr_body")"; then
    comment_issue "$issue" "Local Codex pushed branch ${branch}, but PR creation failed. Manual action is required to open the PR."
    say "CODEX_RESULT issue=${issue} status=fix_local_only reason=pr_create_failed"
    return 0
  fi
  add_issue_label "$issue" "codex-pr-opened"
  say "AUTOOPS_PR_CREATED issue=${issue} branch=${branch} url=${pr_url}"
  say "CODEX_RESULT issue=${issue} status=pr_created"
  comment_issue "$issue" "Local Codex opened PR for issue #${issue}: ${pr_url}"

  if [ "$lock_acquired" -eq 1 ]; then
    :
  fi
}

main() {
  active_locks=()
  trap cleanup_locks EXIT

  require_cmd git
  require_cmd gh
  require_cmd python
  if [ "$dry_run" -eq 0 ]; then
    require_cmd codex
  fi

  if [ -x scripts/post_merge_paper_apply.sh ] && { [ "${requested_environment:-}" = "paper" ] || [ "$(processor_environment "$(detected_processor_label)")" = "paper" ]; }; then
    if [ "$dry_run" -eq 1 ]; then
      scripts/post_merge_paper_apply.sh --dry-run --limit "$limit" || true
    else
      scripts/post_merge_paper_apply.sh --limit "$limit" || true
    fi
  fi

  read_lines_into_array issues list_candidate_issues
  if [ "${#issues[@]}" -eq 0 ]; then
    say "No eligible Codex auto-fix issues found."
    return 0
  fi

  processed=0
  local issue
  for issue in "${issues[@]}"; do
    [ -n "$issue" ] || continue
    process_issue "$issue"
    processed=$((processed + 1))
    if [ "$processed" -ge "$limit" ]; then
      break
    fi
  done
}

main "$@"
