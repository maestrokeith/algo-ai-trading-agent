# AutoOps

AutoOps is the self-healing operations subsystem for Algo. It is not a rename of
the Algo trading platform. Algo remains the trading engine; AutoOps is the
operational loop around health checks, issue routing, Codex fixes, validation,
guarded merge, and safe recovery.

## Operating Loop

AutoOps is designed around this flow:

```text
health check -> GitHub issue -> Codex fix -> PR validation -> guarded auto-merge
-> deploy/restart -> post-deploy verification
```

Every stage must preserve trading safety. Health checks and drills are read-only
unless a specific command explicitly documents otherwise. Live broker credentials
must not be loaded by AutoOps status or dry-run drill commands.

## Components

### Health Monitor

Implemented:

- `scripts/check_algo_health.sh`
- `deploy/systemd/algo-health-check.service`
- `deploy/systemd/algo-health-check.timer`
- Read-only service, journal, premarket, dynamic scanner, replay, and paper
  options diagnostics.
- Emits the AutoOps event namespace through the new status/drill command:
  `AUTOOPS_HEALTH_CHECK`.

Roadmap:

- Promote more silent-flow checks from research metrics into health conditions.
- Add per-environment health summaries for dashboards.

### Failure Reporter

Implemented:

- `scripts/report_algo_failure_to_github.sh`
- Creates a Markdown evidence report from service state, journal excerpts, repo
  status, and safe validation commands.
- Supports dry-run mode.
- Emits `AUTOOPS_ISSUE_CREATED` after successful issue creation.

Roadmap:

- Add structured JSON sidecar output for downstream tooling.

### GitHub Issue Creation

Implemented:

- Health and failure reporters can create GitHub issues with environment labels.
- Duplicate suppression exists in reporter scripts through fingerprints.
- Self-heal can create/update issues from captured research metrics.

Roadmap:

- Consolidate issue fingerprint rendering across all reporters.
- Add a single AutoOps issue schema for evidence blocks.

### Codex Auto-Fix

Implemented:

- `.github/workflows/codex-auto-fix.yml`
- `scripts/process_codex_issues_local.sh`
- Issues labeled for Codex can be routed to automated fix branches and PRs.
- The local processor emits `AUTOOPS_CODEX_STARTED` and `AUTOOPS_PR_CREATED`.

Roadmap:

- Emit `AUTOOPS_CODEX_STARTED` and `AUTOOPS_PR_CREATED` consistently from all
  GitHub-hosted and local processor paths.
- Add richer issue-to-PR traceability in comments.

### PR Validation

Implemented:

- `.github/workflows/codex-pr-validation.yml`
- `scripts/validate_codex_pr.sh`
- Runs safe validation, updates validation comments, and applies
  `codex-validation-passed` or `codex-validation-failed`.
- Emits `AUTOOPS_VALIDATION_PASSED` and `AUTOOPS_VALIDATION_FAILED` in workflow
  logs after label updates.

Roadmap:

- Publish a compact validation artifact for AutoOps status.

### Guarded Auto-Merge

Implemented:

- `.github/workflows/codex-auto-merge.yml`
- Merges only eligible Codex PRs with validation-passed labels and no blocking
  labels.
- Does not restart live services from GitHub Actions.
- Emits `AUTOOPS_AUTO_MERGED` after successful guarded merge.

Roadmap:

- Add a merge audit artifact keyed by PR number.

### Safe Deploy

Implemented:

- Live deploy/restart is not automated from GitHub Actions.
- Paper post-merge apply exists separately in `scripts/post_merge_paper_apply.sh`.
- `./bin/algo autoops drill --dry-run` simulates deployment and emits
  `AUTOOPS_DEPLOY_STARTED` and `AUTOOPS_DEPLOYED` with dry-run markers.

Roadmap:

- Add guarded local deploy command with dirty-repo, market-open, and service
  restart guards.
- Require explicit confirmation flags for any non-dry-run deploy.

### Post-Deploy Verification

Implemented:

- Existing health scripts verify service state and recent logs.
- AutoOps dry-run drill simulates verification with `AUTOOPS_VERIFY_STARTED` and
  `AUTOOPS_VERIFIED`.

Roadmap:

- Add a structured post-deploy verifier that checks systemd active state, recent
  Tracebacks, and expected flow logs.
- Create follow-up GitHub issues automatically when verification fails.

### AutoOps Drill

Implemented:

- `./bin/algo autoops drill --dry-run`
- `./bin/algo autoops drill --dry-run --failure <type>`
- `./bin/algo autoops drill --paper-only --confirm --failure <type>`
- Verifies required scripts and workflows exist.
- Checks GitHub CLI availability locally if installed, without requiring network.
- Writes a history record to `data/autoops/history/YYYY-MM-DDTHHMMSS.json`.
- Emits:
  - `AUTOOPS_DRILL_START`
  - `AUTOOPS_HEALTH_CHECK`
  - `AUTOOPS_ISSUE_CREATED`
  - `AUTOOPS_CODEX_STARTED`
  - `AUTOOPS_PR_CREATED`
  - `AUTOOPS_VALIDATION_PASSED`
  - `AUTOOPS_AUTO_MERGED`
  - `AUTOOPS_DEPLOY_STARTED`
  - `AUTOOPS_DEPLOYED`
  - `AUTOOPS_VERIFY_STARTED`
  - `AUTOOPS_VERIFIED`
  - `AUTOOPS_RECOVERY_COMPLETE`
  - `AUTOOPS_DRILL_SUCCESS`

Non-dry-run drill is intentionally guarded and currently refuses to proceed
unless `--confirm` and `--paper-only` are supplied. It still does not perform a
live deploy.

Dry-run failure injection is synthetic. It never stops services, edits broker or
trading config, creates GitHub issues, or modifies trading state. Supported
failure types:

- `service_down`
- `premarket_missing`
- `broker_auth_failed`
- `stale_market_data`
- `allocator_silent_drop`
- `order_submit_failed`
- `paper_options_diagnostics_failed`
- `validation_failed`
- `github_issue_failed`
- `codex_processor_failed`
- `auto_merge_blocked`

Injected drills emit `AUTOOPS_FAILURE_INJECTED`, `AUTOOPS_DIAGNOSED`,
`AUTOOPS_RECOVERY_PLAN`, and `AUTOOPS_DRILL_SUCCESS`. History records include
`failure_type`, `diagnosis`, `recovery_plan`, and `improved`.

AutoOps v2 adds a confirmed paper recovery drill:

```bash
./bin/algo autoops drill --paper-only --confirm --failure allocator_silent_drop
```

The confirmed drill is restricted to the paper environment on macOS. It refuses
live/Fedora execution before creating any GitHub issue. When allowed, it creates
a real GitHub issue labeled `autoops-drill`, `paper`, and `codex`, triggers the
existing local Codex issue processor, requires a PR with
`codex-validation-passed`, requires the guarded auto-merge to have completed,
and then runs paper-only verification:

```bash
PYTHONPATH=. pytest tests/test_autoops*.py -v
./bin/algo paper-options-diagnostics --user paper_bot --symbol QQQ
scripts/check_algo_health.sh --dry-run PAPER
```

It never calls live broker APIs and never restarts the live service.

## Status Command

Run:

```bash
./bin/algo autoops status
```

The status command is read-only. It shows:

- health reporter status
- failure reporter status
- required workflow/script presence
- GitHub CLI availability
- latest AutoOps issue/PR if available through read-only `gh` list commands
- validation labels on the latest PR if available
- systemd active state through `systemctl is-active`

It does not call broker APIs, load live broker credentials, create GitHub
objects, deploy code, or restart services.

## Report Command

Run:

```bash
./bin/algo autoops report
```

The report command summarizes local drill history from `data/autoops/history`:

- total drills
- successful drills
- failed drills
- success percentage
- average recovery time from successful drills
- last successful drill
- last failed drill

Each drill history record captures timestamp, host, environment, drill mode,
duration, whether issue/PR/validation/merge/deploy/verification stages occurred,
success, and failure reason.

## Event Names

Canonical AutoOps event names:

- `AUTOOPS_HEALTH_CHECK`
- `AUTOOPS_ISSUE_CREATED`
- `AUTOOPS_CODEX_STARTED`
- `AUTOOPS_PR_CREATED`
- `AUTOOPS_VALIDATION_PASSED`
- `AUTOOPS_VALIDATION_FAILED`
- `AUTOOPS_AUTO_MERGED`
- `AUTOOPS_DEPLOY_STARTED`
- `AUTOOPS_DEPLOYED`
- `AUTOOPS_VERIFY_STARTED`
- `AUTOOPS_VERIFIED`
- `AUTOOPS_RECOVERY_COMPLETE`
- `AUTOOPS_DRILL_START`
- `AUTOOPS_DRILL_SUCCESS`
- `AUTOOPS_DRILL_FAILED`

These names are an operations event namespace only; they do not rename Algo.
