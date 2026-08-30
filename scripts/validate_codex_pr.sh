#!/usr/bin/env bash
# Safe validation for Codex-generated pull requests.
set -u
set -o pipefail

ROOT="${ALGO_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
RUN_FULL_SUITE="${CODEX_PR_RUN_FULL_SUITE:-1}"
LOG_PATH="${CODEX_PR_VALIDATION_LOG:-/tmp/codex_pr_validation.log}"

cd "$ROOT" || {
  echo "Repo root not found: $ROOT" >&2
  exit 1
}

: >"$LOG_PATH"
VALIDATION_RC=0

run_required() {
  local label="$1"
  shift
  {
    echo "$ $*"
    "$@"
    local rc=$?
    echo "[exit_code=$rc]"
    if [[ "$rc" -ne 0 && "$VALIDATION_RC" -eq 0 ]]; then
      VALIDATION_RC="$rc"
    fi
    echo
  } >>"$LOG_PATH" 2>&1
}

run_optional() {
  local label="$1"
  shift
  {
    echo "$ $*"
    if [[ ! -x "$1" && "$1" == ./* ]]; then
      echo "SKIPPED $label: command not executable or not present"
      echo "[exit_code=skipped]"
      echo
      return
    fi
    "$@"
    local rc=$?
    echo "[exit_code=$rc]"
    if [[ "$rc" -ne 0 ]]; then
      echo "SKIPPED/FAILED $label: command returned $rc; this diagnostic is non-blocking in GitHub Actions."
    fi
    echo
  } >>"$LOG_PATH" 2>&1
}

run_optional_with_artifacts() {
  local label="$1"
  local artifact_path="$2"
  shift 2
  {
    echo "$ $*"
    if [[ ! -e "$artifact_path" ]]; then
      echo "SKIPPED $label: missing local artifacts at $artifact_path"
      echo "[exit_code=skipped]"
      echo
      return
    fi
    if [[ ! -x "$1" && "$1" == ./* ]]; then
      echo "SKIPPED $label: command not executable or not present"
      echo "[exit_code=skipped]"
      echo
      return
    fi
    "$@"
    local rc=$?
    echo "[exit_code=$rc]"
    if [[ "$rc" -ne 0 ]]; then
      echo "SKIPPED/FAILED $label: command returned $rc; this diagnostic is non-blocking in GitHub Actions."
    fi
    echo
  } >>"$LOG_PATH" 2>&1
}

run_required "failure reporter shell syntax" bash -n scripts/report_algo_failure_to_github.sh
run_required "health checker shell syntax" bash -n scripts/check_algo_health.sh
run_required "health analyzer compile" python -m py_compile scripts/analyze_algo_health_report.py
run_required "focused tests" env PYTHONPATH=. pytest tests/test_failure_reporter.py tests/test_algo_health_check.py tests/test_algo_health_analyzer.py tests/test_codex_auto_fix_workflow.py tests/test_codex_pr_validation_workflow.py tests/test_local_codex_issue_processor.py -v

if [[ "$RUN_FULL_SUITE" == "1" || "$RUN_FULL_SUITE" == "true" ]]; then
  run_required "full tests" env PYTHONPATH=. pytest tests/ -q
else
  {
    echo "$ PYTHONPATH=. pytest tests/ -q"
    echo "SKIPPED full tests: CODEX_PR_RUN_FULL_SUITE=$RUN_FULL_SUITE"
    echo "[exit_code=skipped]"
    echo
  } >>"$LOG_PATH"
fi

run_optional_with_artifacts "premarket readiness" data/premarket ./bin/algo premarket-ready
run_optional_with_artifacts "paper summary" data ./bin/algo summary latest --user paper_bot
run_optional_with_artifacts "paper options diagnostics" data ./bin/algo paper-options-diagnostics --user paper_bot --symbol QQQ
run_optional_with_artifacts "paper health dry-run" data scripts/check_algo_health.sh --dry-run PAPER

cat "$LOG_PATH"
exit "$VALIDATION_RC"
