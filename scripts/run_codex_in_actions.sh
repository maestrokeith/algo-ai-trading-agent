#!/usr/bin/env bash
# Run Codex with a sandbox mode compatible with GitHub-hosted runners.

set -euo pipefail

prompt_path="${1:-/tmp/codex_prompt.txt}"
result_path="${2:-/tmp/codex_result.txt}"
workspace="${GITHUB_WORKSPACE:-$(pwd)}"

if [ ! -s "$prompt_path" ]; then
  echo "Codex prompt file is missing or empty: $prompt_path" >&2
  exit 2
fi

echo "CODEX_VERSION_BEGIN"
codex --version
echo "CODEX_VERSION_END"

sandbox_mode="${CODEX_SANDBOX_MODE:-workspace-write}"
fallback_reason="default local workspace-write sandbox"

if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
  sandbox_mode="${CODEX_GITHUB_ACTIONS_SANDBOX:-danger-full-access}"
  fallback_reason="GitHub-hosted runners block Bubblewrap network namespace setup: bwrap loopback RTM_NEWADDR Operation not permitted"
fi

case "$sandbox_mode" in
  read-only|workspace-write|danger-full-access)
    ;;
  *)
    echo "Unsupported Codex sandbox mode selected: $sandbox_mode" >&2
    exit 2
    ;;
esac

echo "CODEX_SANDBOX_MODE selected=${sandbox_mode}"
echo "CODEX_SANDBOX_FALLBACK_REASON ${fallback_reason}"
echo "CODEX_WORKSPACE ${workspace}"

codex exec \
  --cd "$workspace" \
  --sandbox "$sandbox_mode" \
  --output-last-message "$result_path" \
  - < "$prompt_path"
