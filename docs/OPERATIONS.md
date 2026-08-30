# Operations

All times in this document are US/Eastern. The systemd timer examples assume
the host timezone is `America/New_York` and the checkout lives at
`/opt/algosphere/algo-ai-trading-agent`. If either differs, edit `deploy/systemd/*.service` and
`deploy/systemd/*.timer` before installing.

## Quick Start

Local Codex processor validation test.

Set up a new node from the repo root:

```bash
cd /opt/algosphere/algo-ai-trading-agent
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
PYTHONPATH=. pytest tests/ -v
./bin/algo install-node --dry-run --user live_bot
./bin/algo install-node --user live_bot
```

The node installer performs dependency checks, repo path validation,
`config/users.yaml` and `.env`/environment validation, Python import checks,
a pytest smoke test, systemd unit installation, `systemctl daemon-reload`,
timer enablement, and post-install verification with
`systemctl list-timers 'algosphere*'`.
It installs every unit in `deploy/systemd/`; if an `algo.service` template is
present there, it is installed together with the AlgoSphere service and timer
units.

Available installer flags:

- `--dry-run` prints systemd install and enable commands without changing
  systemd state.
- `--enable-replay` enables `algosphere-ops-replay-summary.timer`.
- `--skip-tests` skips the pytest smoke test.
- `--user live_bot` selects the configured trading user to validate.

Use the narrower timer installer only after node prerequisites have already
been checked:

```bash
./bin/algo install-ops-timers --dry-run
./bin/algo install-ops-timers
```

Enable the optional replay timer:

```bash
./bin/algo install-node --enable-replay --user live_bot
```

Check installed timers:

```bash
systemctl list-timers 'algosphere-*'
systemctl status algosphere-premarket.timer
systemctl status algosphere-ops-research-metrics-begin.timer
systemctl status algosphere-ops-research-metrics-end.timer
systemctl status algosphere-ops-daily-summary.timer
```

## Operations Schedule

| Time ET | Mode | Task | Service | Timer | Command |
| --- | --- | --- | --- | --- | --- |
| 05:15-09:25 every 12 min | Automatic | Premarket Collection | `algosphere-premarket.service` | `algosphere-premarket.timer` | `python scripts/run_premarket_collection.py` |
| 09:20 | Automatic | Premarket Readiness Check | `algosphere-ops-premarket-ready.service` | `algosphere-ops-premarket-ready.timer` | `./bin/algo premarket-ready` |
| 09:25 | Automatic | Begin-Day Research Metrics Capture | `algosphere-ops-research-metrics-begin.service` | `algosphere-ops-research-metrics-begin.timer` | `./bin/algo capture-metrics --begin-day --live` |
| 09:30 | Automatic | Live algo startup | `algo.service` | host deployment dependent | deployment service starts live loop |
| 09:35 | Automatic | Startup Validation | `algosphere-ops-startup-validation.service` | `algosphere-ops-startup-validation.timer` | `./bin/algo ops startup-validation --user live_bot --journal-unit algo.service` |
| 16:05 | Automatic | End-Day Research Metrics Capture | `algosphere-ops-research-metrics-end.service` | `algosphere-ops-research-metrics-end.timer` | `./bin/algo capture-metrics --end-day --live` |
| 16:15 | Automatic | Daily Summary | `algosphere-ops-daily-summary.service` | `algosphere-ops-daily-summary.timer` | `./bin/algo ops daily-summary --user live_bot` |
| 16:25 | Automatic | Profitability Attribution and Catalyst Reports | `algosphere-ops-postmarket-analytics.service` | `algosphere-ops-postmarket-analytics.timer` | `./bin/algo ops postmarket-analytics --user live_bot` |
| 16:35 | Optional automatic | Replay Analysis | `algosphere-ops-replay-summary.service` | `algosphere-ops-replay-summary.timer` | `./bin/algo ops replay-summary --user live_bot` |
| 16:45 | Automatic | Research Feedback | `algosphere-ops-research-feedback.service` | `algosphere-ops-research-feedback.timer` | `./bin/algo ops research-feedback --user live_bot` |
| Sat 09:00 | Automatic | Weekly Research Feedback | `algosphere-ops-weekly-research-feedback.service` | `algosphere-ops-weekly-research-feedback.timer` | `./bin/algo ops weekly-research-feedback --user live_bot` |

`algosphere-ops-replay-summary.timer` is installed by
`./bin/algo install-ops-timers`, but it is enabled only when the installer is
run with `--enable-replay`.

## Automatic Actions

The automatic jobs are read-only with respect to live trading behavior. They
refresh premarket artifacts, validate artifact readiness, inspect startup logs,
and generate local reports.

## Research Metrics Capture

The research metrics capture command writes begin-day and end-day JSON/Markdown
snapshots under `data/research_metrics/YYYY-MM-DD/`. It is read-only with
respect to trading behavior and does not change thresholds, allocator behavior,
execution behavior, or risk gates.

Manual commands:

```bash
./bin/algo capture-metrics --begin-day --live
./bin/algo capture-metrics --end-day --live
./bin/algo capture-metrics --begin-day --paper
./bin/algo capture-metrics --end-day --paper
./bin/algo capture-metrics --end-day --live --dry-run
```

The report includes service/git context, account summary helper output,
positions helper output, open orders helper output, dynamic scanner selections
and rejections, `ENTRY_EVAL` pass/fail counts by route, allocator traces,
allocator actions and reject reasons, order submissions and confirmations,
quote/spread/ATR/range/price/catalyst reject symbols, exceptions, and the same
missing-flow diagnostics used by `./bin/algo self-heal`.

## Paper Dynamic History Experiment

The dynamic-candidate daily-history experiment is paper-only and opt-in. It is
controlled by:

```yaml
dynamic_universe:
  paper_min_history_bars_experiment:
    enabled: false
    min_bars: 50
```

When disabled, paper keeps the configured dynamic requirement and core/trend
symbols keep the normal non-SQQQ requirement: `max(200, strategy slow MA)`.
When enabled, only paper-mode dynamic candidates use `min_bars`. Live mode
ignores this paper experiment.

Live scanner-selected `DYNAMIC_ONLY` candidates have a separate read-only
control:

```yaml
dynamic_universe:
  live_min_history_bars: 50
```

This applies only to live `DYNAMIC_ONLY` candidates. Core, scoring,
trend-long, `CORE_WITH_DYNAMIC_SIGNAL`, paper, and options paths keep their
existing history requirements. Live dynamic history checks log:

```text
DYNAMIC_HISTORY_REQUIREMENT symbol=ASTN mode=live candidate_type=dynamic_only required_bars=50 available_bars=87
```

Dynamic scanner rejection funnel lines log the scanner stage and reason:

```text
DYNAMIC_REJECT_FUNNEL reason=below_min_price symbol=ASTN stage=scanner
```

Live dynamic weak-catalyst execution cooldown is configured with:

```yaml
dynamic_universe:
  weak_catalyst_execution_cooldown_minutes: 10
```

It starts only after the live execution guard rejects a dynamic buy with
`weak_catalyst_dynamic_non_exceptional_live`. During the cooldown, repeated
dynamic buy dispatch for that symbol is skipped; trend-long/core, paper, sells,
stop-losses, exits, and strong-catalyst dynamic candidates are not blocked.
Cooldown lifecycle logs:

```text
DYNAMIC_EXECUTION_COOLDOWN_START symbol=CORD reason=weak_catalyst_dynamic_non_exceptional_live minutes=10
DYNAMIC_EXECUTION_COOLDOWN_ACTIVE symbol=CORD reason=weak_catalyst_dynamic_non_exceptional_live
DYNAMIC_EXECUTION_COOLDOWN_EXPIRED symbol=CORD reason=elapsed
```

Active experiment passes log:

```text
DYNAMIC_HISTORY_EXPERIMENT symbol=ASTN got=87 need=50 default_need=200 mode=paper
```

## Level 1 Failure Reporting

Level 1 automated failure reporting is read-only. It may detect failures,
collect diagnostics, generate health reports, and create GitHub issues. It must
not restart services, fix code, merge PRs, change trading logic, change risk or
allocation settings, enable live options, place trades, cancel orders, or modify
orders.

The reporter covers both environments:

- LIVE: `user=live_bot`, `algo.service`, production timers, live premarket
  pipeline, live diagnostics, live portfolio.
- PAPER: `user=paper_bot`, paper services, paper options engine, paper
  diagnostics, paper replay framework, paper portfolio.

Run manually from the repo root:

```bash
scripts/report_algo_failure_to_github.sh --dry-run
scripts/report_algo_failure_to_github.sh --dry-run LIVE algo.service
scripts/report_algo_failure_to_github.sh --dry-run PAPER paper.service
```

Without an environment argument, the reporter checks both LIVE and PAPER. With
only a systemd unit argument, it infers PAPER if the unit name contains `paper`;
otherwise it treats the unit as LIVE. Every run writes
`/tmp/algo_failure_report.md`; dry-run mode writes the report but does not call
`gh issue create`.

Failure severities:

- `severity:critical`: live service down, service failed state, process crash,
  unhandled traceback, repeated exceptions, broker auth/connection failure,
  order placement exception, allocator crash, fatal startup validation failure.
- `severity:high`: paper service down, premarket readiness failure, missing or
  stale rankings/catalysts/event feed, scanner exception, paper options
  diagnostics failure, replay failure, options chain diagnostics failure,
  shadow portfolio inconsistency.
- `severity:medium`: silent failures such as repeated `accepted=0`, no dynamic
  candidates for several sessions, no rankings/catalysts, paper options enabled
  but no evaluations, no replay artifacts, or missing health artifacts.
- `severity:research`: degradation and quality concerns such as dynamic
  acceptance collapse, news coverage collapse, excessive spread/RVOL
  rejections, rejected candidates later becoming winners, or catalyst coverage
  deterioration.

GitHub issues created by the reporter use titles like:

- `AUTOFAIL [LIVE] allocator exception 2026-06-11`
- `AUTOFAIL [LIVE] premarket readiness failed 2026-06-11`
- `AUTOFAIL [PAPER] options diagnostics failed 2026-06-11`
- `AUTOFAIL [PAPER] replay validation failed 2026-06-11`

Issue labels:

- `auto-fix`
- `codex`
- `algo-failure`
- `environment:live` or `environment:paper`
- `severity:critical`, `severity:high`, `severity:medium`, or
  `severity:research`

The issue body includes Environment, Severity, Failure Type, Detection
Timestamp, Host, Git Commit, diagnostics, recent logs, and a recommended next
action. The reporter searches open GitHub issues for a stable fingerprint before
creating a new issue; if a duplicate exists it prints `Existing issue found`.
If nothing reportable is detected it prints `No reportable failure detected`.

Systemd integration is optional. Install
`deploy/systemd/algo-failure-reporter@.service` with the other unit files, then
add an `OnFailure` hook to monitored services:

```ini
[Unit]
OnFailure=algo-failure-reporter@%n.service
```

For explicit environment routing, use a drop-in that passes the environment and
unit:

```ini
[Unit]
OnFailure=algo-failure-reporter@algo.service
```

The template service executes:

```bash
/opt/algosphere/algo-ai-trading-agent/scripts/report_algo_failure_to_github.sh %i
```

Validation:

```bash
PYTHONPATH=. pytest tests/test_failure_reporter.py -v
scripts/report_algo_failure_to_github.sh --dry-run LIVE algo.service
scripts/report_algo_failure_to_github.sh --dry-run PAPER paper.service
```

## Level 4 Live Self-Healing Loop

The self-healing command reads research metrics capture output, creates or
updates a deduplicated GitHub issue, and routes that issue to the local Codex
processor. It does not auto-merge, deploy, pull main, restart services, or
verify post-restart health. It is intended for wiring/runtime regressions, not
strategy threshold tuning.

Run manually from the repo root:

```bash
./bin/algo self-heal --live --dry-run
./bin/algo self-heal --live
```

Paper routing is available for paper-only incidents:

```bash
./bin/algo self-heal --paper --dry-run
```

The preferred input is the latest
`data/research_metrics/YYYY-MM-DD/end_day_live.json` or
`begin_day_live.json` report. The command inspects
`logs.missing_flow_diagnostics` for these critical live conditions:

- `DYNAMIC_SCAN selected` without `DYNAMIC_ENTRY_EVAL_START`,
  `DYNAMIC_ENTRY_EVAL_DROPPED`, or `ENTRY_EVAL`.
- `ENTRY_EVAL_PASS` without `ENTRY_TO_ALLOCATOR_TRACE`.
- `ENTRY_TO_ALLOCATOR` without allocator actions or reject reasons.
- `ORDER_SUBMITTED` without fill, status, or position confirmation.

If no metrics report is present, the command falls back to recent service logs
for the same missing-flow conditions and service tracebacks.

Issues use titles like `[LIVE] Self-heal: dynamic scanner selected symbols
without entry eval` and include the time window, grep command, matching logs,
expected flow, actual missing step, and a stable fingerprint. The command
searches open issues for that fingerprint before creating another issue; a
duplicate receives a new comment instead.

Required tools and credentials:

- `gh` authenticated with access to create issues and comments.
- Local `codex` CLI authentication for `scripts/process_codex_issues_local.sh`.
- `git` access for the local Codex issue processor branch/PR flow.
- `journalctl` access only for fallback log inspection when no metrics report
  exists.

Auto-deploy is intentionally disabled. The command stops after issue
create/update and Codex routing.
Secrets are redacted from generated issue bodies.

## Level 1.5 Silent Health Monitoring

Level 1.5 health monitoring detects cases where services remain active but the
platform is no longer functioning normally. It is also reporting-only. It does
not restart services, edit configs, change trading logic, alter risk controls,
place orders, cancel orders, open pull requests, or auto-fix code.

Difference from Level 1:

- Level 1 failure reporting handles crashes, failed units, unhandled
  tracebacks, broker failures, and hard operational failures.
- Level 1.5 health monitoring handles silent degradation: no scanner
  candidates, stale artifacts, no catalyst coverage, inactive paper options,
  missing replay outputs, and research-level quality deterioration.

Run manually from the repo root:

```bash
scripts/check_algo_health.sh --dry-run
scripts/check_algo_health.sh --dry-run LIVE
scripts/check_algo_health.sh --dry-run PAPER
```

Without an environment argument, the health checker checks both LIVE and PAPER.
Every run writes `/tmp/algo_health_report.md`; dry-run mode performs all checks
and generates the report without creating GitHub issues.

Coverage:

- LIVE: `user=live_bot`, `algo.service`, live diagnostics, live premarket
  pipeline, live dynamic scan artifacts, live health artifacts.
- PAPER: `user=paper_bot`, paper diagnostics, paper options engine, replay
  framework, paper dynamic scan artifacts, paper replay artifacts.

Health issue titles use:

- `HEALTH [LIVE] catalyst generation unhealthy`
- `HEALTH [LIVE] dynamic scanner producing no candidates`
- `HEALTH [PAPER] options engine inactive`
- `HEALTH [PAPER] replay artifacts missing`

Health issue labels:

- `algo-health`
- `environment:live` or `environment:paper`
- `severity:medium` or `severity:research`

Health severities:

- `severity:medium`: premarket artifacts missing or stale, rankings/catalysts
  at zero, dynamic scanner accepted zero candidates, paper options diagnostics
  failing, no recent option evaluations, replay artifacts missing or stale.
- `severity:research`: acceptance rate collapse, catalyst or news coverage
  degradation, excessive spread/RVOL filtering, unusually high scanner
  rejection rates, or rejected candidates later becoming strong winners.

Duplicate protection uses a stable `health:<environment>:<severity>:<condition>`
fingerprint. If an open GitHub issue already contains the fingerprint, the
checker prints `Existing health issue found` and does not create another issue.
If no condition is detected it prints `No health issue detected`.

Install scheduled health monitoring:

```bash
sudo cp deploy/systemd/algo-health-check.service /etc/systemd/system/
sudo cp deploy/systemd/algo-health-check.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now algo-health-check.timer
systemctl list-timers 'algo-health-check.timer'
```

The timer runs every 30 minutes via `OnUnitActiveSec=30min`. The service calls:

```bash
/opt/algosphere/algo-ai-trading-agent/scripts/check_algo_health.sh
```

Troubleshooting:

- Run `scripts/check_algo_health.sh --dry-run LIVE` to inspect live health
  without creating issues.
- Run `scripts/check_algo_health.sh --dry-run PAPER` to inspect paper options
  and replay health without creating issues.
- Inspect `/tmp/algo_health_report.md` for the latest combined report.
- Inspect `journalctl -u algo-health-check.service --since today --no-pager`
  for scheduled checker output.
- If duplicate suppression is unexpected, search open issues for the printed
  `health:` fingerprint.

Validation:

```bash
bash -n scripts/check_algo_health.sh
PYTHONPATH=. pytest tests/test_algo_health_check.py -v
scripts/check_algo_health.sh --dry-run
```

## Post-Fix Health Verification

After a Codex repair PR is merged, run a post-fix health verification for the
affected environment. This closes the loop from `issue -> Codex PR -> validation
-> merge -> health verification` without changing trading behavior.
Lifecycle: issue -> Codex PR -> validation -> merge -> health verification.

Run manually from the environment host:

```bash
scripts/verify_codex_fix_health.sh --env live --issue 120 --pr 45 --dry-run
scripts/verify_codex_fix_health.sh --env paper --issue 120 --pr 45 --dry-run
```

The verifier calls:

```bash
./scripts/check_algo_health.sh --env <env> --dry-run
```

It then inspects the environment health report. Suppressed market-closed stale
premarket artifacts are ignored, while remaining actionable failures such as
`service down` still fail verification.

Healthy output:

```text
POST_FIX_VERIFICATION status=healthy env=live
```

Unhealthy output:

```text
POST_FIX_VERIFICATION status=unhealthy env=live
```

When unhealthy and not in `--dry-run`, the verifier opens a follow-up issue:

- `POSTFIX [LIVE] repair still unhealthy after PR #45`
- `POSTFIX [PAPER] repair still unhealthy after PR #45`

Labels:

- `codex`
- `auto-fix`
- `algo-health`
- `environment:live` or `environment:paper`

The follow-up issue includes the original issue number, repair PR number,
environment, hostname, health report path, remaining actionable failures, and a
note that suppressed market-closed stale premarket artifacts should be ignored.
Duplicate protection uses `postfix:<environment>:issue-<issue>:pr-<pr>`.

Validation:

```bash
bash -n scripts/verify_codex_fix_health.sh
PYTHONPATH=. pytest tests/test_postfix_health_verification.py -v
```

## Live Stabilization Loop

Live stabilization is stricter than paper automation. It is live-only, runs only
on Fedora/Linux host `algosphere-live-host`, and is limited to infrastructure,
diagnostic, and safety repair issue creation.
Live fixes must remain infrastructure/diagnostic/safety-only unless explicitly
reviewed.

Manual dry-run:

```bash
scripts/check_live_health.sh --env live --dry-run
scripts/run_live_stabilization_loop.sh --dry-run
```

The live health checker prints machine-readable lines:

```text
LIVE_HEALTH status=healthy|unhealthy
LIVE_HEALTH issue=<reason>
LIVE_HEALTH stable_ticks=<n>
LIVE_HEALTH required_stable_ticks=<n>
LIVE_HEALTH host=<hostname>
LIVE_HEALTH environment=live
```

Live health checks include host validation, `algo.service` active state,
premarket artifact freshness when actionable, weekend market-closed stale
artifact suppression, broker account and buying-power read checks, open-orders
read checks, crash-loop markers, repeated allocator/order submission errors,
paper-only execution path markers, and unexpected live options execution
markers. Suppressed weekend stale premarket artifacts do not count as unhealthy.
Service-down, broker failures, crash loops, paper/live mismatches, and unsafe
live options routes remain unhealthy.

The stabilization loop creates issues titled:

```text
LIVE_STABILIZATION [LIVE] unstable: <summary>
```

Labels:

- `codex`
- `auto-fix`
- `algo-health`
- `environment:live`
- `live-stabilization`
- `needs-human-review`

Safety limits:

- Do not restart `algo.service` automatically.
- Do not auto-deploy.
- Do not auto-merge live PRs.
- Do not enable live options.
- Do not change trading rules, sizing, risk controls, allocator behavior, or
  broker execution.
- Do not place or cancel orders.
- Max live repair attempts per day defaults to `1`; after that the loop prints
  `LIVE_STABILIZATION status=needs_human_review`.

After a live repair PR is merged, verify health:

```bash
scripts/verify_codex_fix_health.sh --env live --issue <issue> --pr <pr>
```

If live is still unhealthy, post-fix verification creates:

```text
POSTFIX [LIVE] repair still unhealthy after PR #<pr>
```

Human review is required whenever the loop reaches the daily attempt cap, a live
fix would affect trading behavior, live options behavior is involved, broker
execution behavior is implicated, or post-fix verification remains unhealthy.

Validation:

```bash
bash -n scripts/check_live_health.sh
bash -n scripts/run_live_stabilization_loop.sh
PYTHONPATH=. pytest tests/test_live_health.py -v
```

## Level 1.6 Health Research Analyzer

Level 1.6 enriches Level 1.5 health issues with root-cause analysis. It remains
reporting-only and research-only: it does not change scanner thresholds, risk
settings, allocation settings, options state, orders, services, pull requests,
or production processes.

Implementation:

- `scripts/check_algo_health.sh` remains the scheduler and GitHub issue
  orchestrator.
- `scripts/analyze_algo_health_report.py` parses recent journal logs and
  runtime artifacts, then appends a `Root-Cause Analysis` section to every
  environment report before issue creation.
- Dry-run mode still performs all checks and writes the enriched
  `/tmp/algo_health_report.md` without creating GitHub issues.

Root-cause sections include:

- Rejection Summary: counts of unstable quote/spread rejects, gain cap rejects,
  below-min-price rejects, below-min-average-volume rejects, RVOL rejects,
  entry-alignment rejects, bad quote rejects, catalyst/news score rejects, ATR
  guards, and other scanner filters.
- Representative Symbols: up to five example symbols per top rejection reason,
  including available values such as spread, gain, price, min price, RVOL, and
  catalyst/news score.
- Filter Quality Interpretation: a plain-English classification such as
  `likely healthy filtering`, `possible over-filtering`,
  `likely data-quality issue`, `likely news/catalyst coverage issue`,
  `likely options pipeline inactivity`, or `likely replay pipeline issue`.
- Suggested Next Action: a specific diagnostic action that avoids changing live
  trading behavior from the monitor.
- Data Quality Signals: provider status, NewsAPI rate-limit status, Alpaca
  raw counts, SEC counts when available, artifact freshness, rankings, catalysts,
  and catalyst-ranked symbol counts.
- Trading Activity Context: summary/replay availability and any available trade
  or PnL fields from local summary artifacts.

High rejection rate is not automatically treated as a bug. A high rejection
rate by itself is classified as `severity:research` unless it is paired with
operational symptoms such as repeated zero accepted candidates, zero rankings,
zero catalysts, stale artifacts, provider failures, known exceptions, or paper
options inactivity. Many high-rejection sessions are healthy filtering when the
rejected universe is dominated by microcaps, wide spreads, unstable quotes,
extreme gains, prices below the configured minimum, or bad quotes.

LIVE root-cause focus:

- Premarket artifact freshness and coverage.
- Dynamic scanner rejection mix.
- Provider quality and news/catalyst coverage.
- Trading activity and allocator/scanner symptoms.

PAPER root-cause focus:

- Dynamic scanner rejection mix.
- Paper options diagnostics and option evaluation activity.
- Replay artifacts and replay validation availability.
- Paper summary context.

Validate the analyzer:

```bash
bash -n scripts/check_algo_health.sh
PYTHONPATH=. pytest tests/test_algo_health_check.py tests/test_algo_health_analyzer.py -v
scripts/check_algo_health.sh --dry-run
rg -n "Root-Cause Analysis|Rejection Summary|Representative Symbols|Suggested Next Action" /tmp/algo_health_report.md
```

## Level 2 Local Codex PR Workflow

Level 2 now uses zero API cost local automation on the laptop. The local
processor polls GitHub issues, builds the same safe Codex prompt, runs
`codex exec` using the already logged-in local ChatGPT authentication, commits
changes on a branch, pushes it, and opens a PR. It is engineering automation
only: no auto-merge, no live restart or deploy, no production secret access, no
runtime state changes, and no service manipulation.

Active local processor:

- `scripts/process_codex_issues_local.sh`

Shared prompt builder:

- `scripts/build_codex_issue_prompt.py`

Legacy GitHub Actions auto-fix workflow:

- `.github/workflows/codex-auto-fix.yml`
- Legacy name: Level 2 Codex Auto-Fix PR Workflow.
- This workflow should remain disabled in GitHub Actions and should not be used
  for normal Codex repairs.
- The local processor does not call this workflow.
- `OPENAI_API_KEY` is not required for the local processor.
- OPENAI_API_KEY is not required in zero API cost local mode.
- Do not add an `OPENAI_API_KEY` dependency to the local processor; it uses the
  laptop's ChatGPT login through `codex exec`.

PR validation remains enabled:

- `.github/workflows/codex-pr-validation.yml`
- Codex PRs opened from local branches still run the safe validation workflow in
  GitHub Actions.

Issue selection:

- The issue must be open and have both labels: `codex` and `auto-fix`.
- The issue must have exactly one environment ownership label:
  `environment:paper` or `environment:live`.
- The issue must have the matching processor ownership label:
  `processor:mac-paper` for paper/Mac, or `processor:live-linux` for
  live/Fedora.
- Issues with `needs-human-review` are ignored.
- Issues with `codex-pr-opened` are ignored.

Environment-routed ownership:

- Mac paper processor: handles only issues labeled `environment:paper` and
  `processor:mac-paper`.
- Fedora live processor on `algosphere-live-host`: handles only issues labeled
  `environment:live` and `processor:live-linux`.
- Paper-options issues must also carry `paper-options` and must never be
  processed on Fedora.
- Live issues must never be processed on the Mac.

The processor emits routing diagnostics before doing Codex work:

```text
ISSUE_ROUTING issue_number=... environment=paper processor=processor:mac-paper hostname=...
ISSUE_ACCEPTED issue_number=... environment=paper processor=processor:mac-paper hostname=...
ISSUE_SKIP issue_number=... reason=missing_environment_label ...
ISSUE_SKIP issue_number=... reason=missing_processor_label ...
ISSUE_SKIP issue_number=... reason=environment_mismatch ...
ISSUE_REJECTED issue_number=... environment=live processor=processor:mac-paper hostname=...
```

Troubleshooting routing:

- `missing_environment_label`: add `environment:paper` or `environment:live`.
- `missing_processor_label`: add `processor:mac-paper` for paper/Mac or
  `processor:live-linux` for live/Fedora.
- `environment_mismatch`: the issue belongs to the other host; do not override
  this to force processing.
- Research or health issues do not run unless a human explicitly adds
  `auto-fix`.

Manual local usage:

```bash
scripts/process_codex_issues_local.sh --dry-run
scripts/process_codex_issues_local.sh --dry-run --issue 120
scripts/process_codex_issues_local.sh --issue 120
scripts/process_codex_issues_local.sh --limit 3
scripts/process_codex_issues_local.sh --issue 120 --no-push
scripts/process_codex_issues_local.sh --issue 120 --no-pr
```

Dry-run mode lists what would happen without running Codex, pushing branches, or
opening pull requests.

Optional user timer installation:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/user/algo-local-codex-processor.service ~/.config/systemd/user/
cp deploy/systemd/user/algo-local-codex-processor.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now algo-local-codex-processor.timer
systemctl --user list-timers algo-local-codex-processor.timer
```

The timer runs every 30 minutes as the laptop user and calls:

```bash
/opt/algosphere/algo-ai-trading-agent/scripts/process_codex_issues_local.sh --limit 1
```

It is user-level only, does not run in GitHub Actions, does not touch live or
paper trading services, and does not restart anything.

Safety boundaries:

- The local processor creates a branch named
  `codex/issue-<issue-number>-auto-fix`.
- It opens a pull request titled `Codex auto-fix: <issue title>`.
- It never merges the pull request.
- It does not deploy code.
- It does not restart live or paper services.
- It does not read `/etc/algo.env`.
- It does not use broker credentials or Alpaca keys.
- It does not place, cancel, or modify orders.
- The prompt instructs Codex not to change trading risk limits, sizing,
  allocation percentages, entry/exit strategy behavior, options enablement,
  spread filters, volatility filters, or safety gates.

Eligible issues:

- Python exceptions and tracebacks.
- Import errors.
- CLI failures.
- Reporter or health-check bugs.
- Test failures.
- Missing diagnostics.
- Docs related to a detected failure.

Not eligible without human review:

- Strategy performance tuning.
- Research conclusions.
- Threshold changes.
- Risk setting changes.
- Allocation changes.
- Live options enablement.
- Safety-gate loosening.

If Codex determines that an issue needs trading behavior changes, the prompt
instructs it to stop and request human review instead of changing
trading logic. The workflow labels such cases with `needs-human-review` when no
code changes are produced.

Duplicate protection:

- Checks for an existing branch named `codex/issue-<issue-number>-auto-fix`.
- Checks for an open PR from that branch.
- Checks for an open PR referencing the source issue.
- If a duplicate exists, it comments on the issue and exits cleanly.

Issue comments and labels:

- Adds `codex-running` when automation starts.
- Comments when Codex starts.
- Comments when Codex produces changes.
- Comments when validation passes or fails.
- Adds `codex-validation-failed` if validation fails.
- Adds `codex-pr-opened` when a pull request is created.
- Adds `needs-human-review` if the issue cannot be safely fixed automatically.

Validation performed by the workflow:

```bash
bash -n scripts/report_algo_failure_to_github.sh
bash -n scripts/check_algo_health.sh
python -m py_compile scripts/analyze_algo_health_report.py
PYTHONPATH=. pytest tests/test_failure_reporter.py tests/test_algo_health_check.py tests/test_algo_health_analyzer.py tests/test_codex_pr_validation_workflow.py -v
PYTHONPATH=. pytest tests/ -q
```

If validation fails, the processor comments with the failure summary, adds
`codex-validation-failed`, and does not open a pull request.

How to trigger:

1. Open the GitHub issue created by the failure reporter or health monitor.
2. Confirm the issue is a software bug or diagnostics/docs task.
3. Add both labels: `codex` and `auto-fix`.
4. Do not add `needs-human-review`.
5. Wait for the local laptop processor or run it manually.
6. Review the generated PR manually before merge.

How to disable:

- Remove either the `codex` or `auto-fix` label from the issue.
- Add `needs-human-review`.
- Stop the user timer with
  `systemctl --user disable --now algo-local-codex-processor.timer`.
- Keep the legacy GitHub Actions Codex Auto-Fix workflow disabled.

Why merge and deploy stay manual:

- The trading platform has live capital, broker credentials, and safety gates
  outside the GitHub runner.
- Codex may propose code, tests, and diagnostics, but a human must review
  trading impact before merge.
- Deployment and service restarts remain operator-controlled.

Validate workflow coverage:

```bash
PYTHONPATH=. pytest tests/test_local_codex_issue_processor.py tests/test_codex_pr_validation_workflow.py -v
```

## Level 3 Codex PR Validation

Level 3 validates pull requests created by the Level 2 local Codex processor.
It is validation-only: no auto-merge, no deploy, no live restart, no production
secret access, no live broker credentials, and no trading loop execution.

Workflow file:

- `.github/workflows/codex-pr-validation.yml`

Helper:

- `scripts/validate_codex_pr.sh`

When it runs:

- On pull request `opened`, `synchronize`, `reopened`, and
  `ready_for_review`.
- Full Codex validation runs only when the PR branch starts with `codex/`, the
  PR has label `codex-pr-opened`, or the PR title starts with
  `Codex auto-fix:`.
- Non-Codex PRs are ignored by this workflow.

Validation performed:

```bash
python -m pip install -U pip
pip install -r requirements.txt
bash -n scripts/report_algo_failure_to_github.sh
bash -n scripts/check_algo_health.sh
python -m py_compile scripts/analyze_algo_health_report.py
PYTHONPATH=. pytest tests/test_failure_reporter.py tests/test_algo_health_check.py tests/test_algo_health_analyzer.py tests/test_codex_auto_fix_workflow.py tests/test_codex_pr_validation_workflow.py tests/test_local_codex_issue_processor.py -v
PYTHONPATH=. pytest tests/ -q
```

Safe paper/dev diagnostics are also attempted when commands are present:

```bash
./bin/algo premarket-ready
./bin/algo summary latest --user paper_bot
./bin/algo paper-options-diagnostics --user paper_bot --symbol QQQ
scripts/check_algo_health.sh --dry-run PAPER
```

These diagnostics are optional and non-blocking in GitHub Actions. If
GitHub-hosted runners lack market data, broker credentials, or local artifacts,
`scripts/validate_codex_pr.sh` records `SKIPPED ... missing local artifacts`
instead of failing the workflow. Validation should fail only for shell syntax
failures, Python compile failures, or unit test failures. The workflow must not
use live credentials, read `/etc/algo.env`, call live order APIs, or restart
services.

PR comments:

- The workflow writes `Codex PR Validation: PASS` or
  `Codex PR Validation: FAIL`.
- It includes commands executed, a concise result summary, skipped diagnostics
  or non-blocking diagnostic failures, and the safety statement.
- It updates the previous validation comment when one exists to avoid comment
  spam.

Validation labels:

- PASS adds `codex-validation-passed`.
- PASS removes `codex-validation-failed` if present.
- FAIL adds `codex-validation-failed`.
- The workflow does not remove `needs-human-review`.

How to rerun validation:

- Push another commit to the Codex PR branch.
- Close and reopen the PR.
- Mark a draft PR ready for review.
- Use GitHub Actions rerun for the failed workflow attempt.

Human review is still required because validation only proves that tests and
safe diagnostics passed in a GitHub-hosted runner. It does not prove trading
impact is acceptable, does not deploy, and does not verify live broker state.

Validate Level 3 workflow coverage:

```bash
PYTHONPATH=. pytest tests/test_codex_pr_validation_workflow.py -v
```

## Level 4 Guarded Codex Auto-Merge

Level 4 can automatically merge eligible Codex PRs after validation passes. It
does not deploy live code and it never restarts live services.

Workflow file:

- `.github/workflows/codex-auto-merge.yml`

Triggers:

- Pull request review submission.
- Pull request label/synchronize/reopen/ready events.
- Completed check suites.
- Completed `Codex PR Validation` workflow runs.

Merge eligibility:

- PR has label `codex-validation-passed`.
- PR branch starts with `codex/`.
- PR is not a draft.
- PR has no merge conflicts.
- PR does not have `codex-validation-failed`.
- PR does not have `needs-human-review`.

GitHub-hosted workflow behavior:

- Uses `gh pr merge --squash --delete-branch` only after all guards pass.
- Does not call `systemctl`.
- Does not restart paper or live services.
- Does not read `/etc/algo.env`.
- Does not use broker credentials.
- Does not deploy.

Live behavior:

- Auto-merge is allowed after the guard passes.
- The workflow comments: `Merged. Live restart is manual.`
- Operators must handle any live deploy/restart manually after review.

Paper behavior:

- Auto-merge is allowed after the guard passes.
- GitHub Actions comments that local paper post-merge apply is required.
- The local laptop processor runs `scripts/post_merge_paper_apply.sh --limit 1`
  before polling new Codex issues.
- The local apply script pulls `main`, restarts only the configured paper
  service (`ALGO_PAPER_SERVICE`, default `paper.service`), and runs paper smoke
  checks:

```bash
./bin/algo summary latest --user paper_bot
scripts/check_algo_health.sh --dry-run PAPER
```

If paper smoke checks fail, the local script opens a GitHub issue labeled:

- `algo-failure`
- `environment:paper`
- `severity:high`

Manual paper apply:

```bash
scripts/post_merge_paper_apply.sh --dry-run --pr 123
scripts/post_merge_paper_apply.sh --pr 123
```

Safety boundaries:

- Never restart live.
- Never call `systemctl restart algo.service`.
- Never deploy live.
- Never use broker credentials.
- Never use `/etc/algo.env` in GitHub Actions.
- Human review remains required for live operational restart/deploy decisions.

Validate Level 4 workflow coverage:

```bash
PYTHONPATH=. pytest tests/test_codex_auto_merge.py -v
```

Premarket collection writes or refreshes:

- `data/premarket/latest_event_feed.json`
- `data/premarket/latest_rankings.json`
- `data/premarket/latest_catalysts.json`
- `data/premarket/provider_diagnostics_latest.json`
- `data/premarket/social_sentiment_latest.json` when social diagnostics run

When a run produces ranked catalysts, premarket collection also preserves the
latest non-empty snapshots:

- `data/premarket/previous_non_empty_event_feed.json`
- `data/premarket/previous_non_empty_rankings.json`
- `data/premarket/previous_non_empty_catalysts.json`

Empty current runs still overwrite `latest_*.json` so readiness stays honest,
but they do not erase `previous_non_empty_*.json`.

NewsAPI is optional for premarket collection. The default configuration keeps
`premarket_intelligence.newsapi.enabled: false` because the free tier may
rate-limit or reject broader premarket queries. Leave it disabled on nodes
without a paid NewsAPI subscription; Alpaca News and SEC filings remain active.
To enable NewsAPI explicitly, set:

```yaml
premarket_intelligence:
  newsapi:
    enabled: true
```

Social sentiment collection is also optional and read-only. The default
configuration keeps `premarket_intelligence.social.enabled: false`. When enabled,
the Reddit provider uses the official Reddit OAuth API only; it does not scrape
Reddit HTML. Set `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, and
`REDDIT_USER_AGENT` before enabling it. Missing credentials skip cleanly with
`reason=reddit_credentials_missing`. Twitter-style social collection is disabled
by default and reports `reason=twitter_disabled`. Social sentiment is written for
diagnostics/research only and is not fed into live entries, exits, sizing,
allocation, or order placement.

```yaml
premarket_intelligence:
  social:
    enabled: true
    reddit:
      enabled: true
    twitter:
      enabled: false
```

Daily operations outputs are written under:

- `reports/daily/YYYY-MM-DD/premarket_readiness.txt`
- `reports/daily/YYYY-MM-DD/startup_validation.txt`
- `reports/daily/YYYY-MM-DD/daily_summary.txt`
- `reports/daily/YYYY-MM-DD/catalyst_stats.txt`
- `reports/daily/YYYY-MM-DD/profitability_attribution.txt`
- `reports/daily/YYYY-MM-DD/replay_summary.txt`
- `reports/daily/YYYY-MM-DD/research_feedback.txt`
- `reports/daily/YYYY-MM-DD/weekly_research_feedback.txt`

Research feedback reports and dashboards are written under:

- `reports/research_feedback/YYYY-MM-DD.md`
- `reports/research_feedback/YYYY-MM-DD_dashboard.json`
- `reports/research_feedback/YYYY-MM-DD_dashboard.html`
- `reports/research_feedback/week_YYYY-MM-DD.md`
- `reports/research_feedback/week_YYYY-MM-DD_dashboard.json`
- `reports/research_feedback/week_YYYY-MM-DD_dashboard.html`

Historical catalyst outcome research databases are written under:

- `data/research/catalyst_outcomes/YYYY-MM-DD_live_bot.json`
- `data/research/catalyst_outcomes/YYYY-MM-DD_live_bot.txt`

Per-job logs are written under `data/logs/`:

- `data/logs/ops_premarket_ready_YYYY-MM-DD.log`
- `data/logs/ops_startup_validation_YYYY-MM-DD.log`
- `data/logs/ops_daily_summary_YYYY-MM-DD.log`
- `data/logs/ops_catalyst_stats_YYYY-MM-DD.log`
- `data/logs/ops_profitability_attribution_YYYY-MM-DD.log`
- `data/logs/ops_replay_summary_YYYY-MM-DD.log`
- `data/logs/ops_research_feedback_YYYY-MM-DD.log`
- `data/logs/ops_weekly_research_feedback_YYYY-MM-DD.log`

Some analytics scripts also write structured JSON artifacts under `data/`, for
example `data/profitability_attribution/daily/` and
`data/replay_market_session/`.

Dynamic entry rejection explainability is read-only and writes alongside
research metrics:

```bash
./bin/algo dynamic-entry-rejection-report --date YYYY-MM-DD --user live_bot
```

Outputs:

- `data/research_metrics/YYYY-MM-DD/dynamic_entry_rejections.md`
- `data/research_metrics/YYYY-MM-DD/dynamic_entry_rejections.json`

Use this when the dynamic scanner selects symbols but entry/risk gates block
them later. The report buckets blockers into trend, volume, EMA slope,
portfolio cap, replacement, cooldown, momentum-rank, gain-threshold, and
no-decision reasons. Adaptive dynamic RVOL, EMA slope tolerance, and
young-position replacement override remain configurable gates; they do not
disable spread, quote, trend, portfolio cap, stop-loss, or exit protections.

## Manual Actions

Manual commands from the repo root:

```bash
./bin/algo premarket-ready
./bin/algo ops premarket-ready --date 2026-06-07 --user live_bot
./bin/algo ops startup-validation --date 2026-06-07 --user live_bot --journal-unit algo.service
./bin/algo ops daily-summary --date 2026-06-07 --user live_bot
./bin/algo ops postmarket-analytics --date 2026-06-07 --user live_bot
./bin/algo ops replay-summary --date 2026-06-07 --user live_bot
./bin/algo ops research-feedback --date 2026-06-07 --user live_bot
./bin/algo ops weekly-research-feedback --date 2026-06-07 --user live_bot
./bin/algo research-feedback 2026-06-07 --user live_bot
./bin/algo research-feedback --date 2026-06-07 --user live_bot
./bin/algo catalyst-outcomes --date 2026-06-07 --user live_bot
./bin/algo catalyst-outcomes --date latest --user live_bot
```

Direct analytics commands that work without setting `PYTHONPATH`:

```bash
python scripts/show_catalyst_stats.py
python scripts/generate_profitability_attribution_report.py --date 2026-06-07 --user live_bot
python scripts/replay_market_session.py --date 2026-06-07 --user live_bot --broker-mock
python scripts/generate_research_feedback.py 2026-06-07 --user live_bot
python scripts/generate_research_feedback.py --date 2026-06-07 --user live_bot
python scripts/generate_research_feedback.py 2026-06-07 --user live_bot --weekly
python scripts/generate_catalyst_outcomes.py --date 2026-06-07 --user live_bot
```

For direct research feedback generation, `--date` overrides a positional date
when both are provided.

Commands that require `PYTHONPATH=.` when run directly:

```bash
PYTHONPATH=. pytest tests/ -v
PYTHONPATH=. python scripts/run_premarket_collection.py --force
```

Prefer `./bin/algo ...` for operational commands because the wrapper runs from
the repo root and sets up the expected script path.

## Validation Commands

Before the open:

```bash
./bin/algo premarket-ready
./bin/algo ops premarket-ready --date 2026-06-07 --user live_bot
```

After `algo.service` starts:

```bash
./bin/algo ops startup-validation --date 2026-06-07 --user live_bot --journal-unit algo.service
journalctl -u algo.service --since '2026-06-07 09:30:00' --no-pager | grep PREMARKET_STARTUP_ARTIFACTS
```

After market close:

```bash
./bin/algo ops daily-summary --date 2026-06-07 --user live_bot
./bin/algo ops postmarket-analytics --date 2026-06-07 --user live_bot
./bin/algo ops replay-summary --date 2026-06-07 --user live_bot
./bin/algo ops research-feedback --date 2026-06-07 --user live_bot
./bin/algo catalyst-outcomes --date 2026-06-07 --user live_bot
```

Paper-options diagnostics:

```bash
./bin/algo paper-options-diagnostics --user paper_bot --symbol QQQ
```

This is mock-only and should show `OPTIONS_CONFIG enabled=true mode=paper_only`,
`ENTRY_EVAL`, `OPTION_CHAIN_LOADED`, `OPTION_FILTER_SUMMARY`, and either
`OPTION_BEST_REJECTED` or `OPTION_SELECTED`. Do not use
`scripts/replay_live_cycle.py` for options validation; replay is allocator-only
and logs `options_disabled_by_replay_live_cycle`.

Runtime option diagnostics are logging-only and do not change routing or order
behavior. Useful tags:

```bash
journalctl -u algo.service --since today --no-pager | grep -E 'OPTION_ROUTE_CHECK|OPTION_ROUTE_SKIPPED|OPTION_SCAN_START|OPTION_SCAN_SUMMARY|OPTION_SIGNAL|OPTION_SELECTED|OPTION_ENTRY_BLOCKED|OPTION_ORDER_INTENT|OPTION_ORDER_SUBMITTED|OPTION_POSITION_OPENED'
```

The tags trace route eligibility, skip classification, signal eligibility,
contract scan start/end, selected contract, blocked entry reason, order intent,
broker submission, and paper position tracking. `OPTION_ROUTE_SKIPPED` uses
the normalized reasons `entry_eval_false`, `underlying_not_allowed`,
`require_top_signal_failed`, `environment_blocked`, `daily_cap`, `cooldown`,
`gross_exposure`, `no_contract_found`, `selector_rejected_all`,
`fallback_to_stock`, and `stock_route_selected`.

### Paper Options Stabilization Loop

Paper options are paper-only and run on the Mac paper host. The stabilization
loop is a one-shot repair tick: it checks paper-options health, creates a Codex
repair issue when unhealthy, and exits. It does not auto-merge, deploy, restart
services, or touch live/Fedora trading.

Run the health check manually on the Mac:

```bash
scripts/check_paper_options_health.sh --env paper
```

Stable output requires no critical paper-options errors, passing diagnostics,
paper-only safety confirmation, and the configured number of consecutive healthy
ticks, default `3`:

```text
PAPER_OPTIONS_HEALTH status=healthy
PAPER_OPTIONS_HEALTH stable_ticks=3
PAPER_OPTIONS_HEALTH required_stable_ticks=3
```

Run one stabilization tick:

```bash
scripts/run_paper_options_stabilization_loop.sh --env paper
```

If unhealthy, the loop creates a GitHub issue labeled `codex`, `auto-fix`,
`algo-health`, `environment:paper`, `processor:mac-paper`, and `paper-options` with title
`PAPER_OPTIONS [PAPER] unstable: <summary>`. The existing local Codex issue
processor can then produce a repair PR. The loop caps repair attempts at
`PAPER_OPTIONS_MAX_REPAIR_ATTEMPTS_PER_DAY` (default `3`) and uses a
paper-options fingerprint to avoid duplicate issues for the same root cause.

After a merged repair PR, verify paper options with:

```bash
scripts/run_paper_options_stabilization_loop.sh --env paper --postfix-pr 123 --issue 131
```

If still unhealthy, it creates a follow-up issue titled
`POSTFIX [PAPER_OPTIONS] still unstable after PR #123`. If stable, it prints
`PAPER_OPTIONS_STABILIZATION status=stable`.

Inspect recent paper-options logs:

```bash
tail -n 200 data/review/$(date +%F)/paper_full.log | grep -E 'OPTION_|PAPER_OPTIONS'
```

Safety rules before any future live promotion: paper evidence must be stable,
paper-only diagnostics must pass repeatedly, and any live options enablement
requires a separate reviewed change. Do not promote by editing this loop.

Open-order diagnostics are read-only. Use live credentials for `live_bot` and
paper credentials for `paper_bot`:

```bash
PYTHONPATH=. python scripts/show_open_orders.py --mode live
PYTHONPATH=. python scripts/show_open_orders.py --mode paper --user paper_bot
```

The command prints the selected mode, user, and base URL type, then a stable
`symbol side qty status submitted_at` table. It does not print API secrets.

Check generated outputs:

```bash
find reports/daily/2026-06-07 -maxdepth 1 -type f -print
find data/logs -maxdepth 1 -name 'ops_*_2026-06-07.log' -print
```

## Deployment Commands

Dry run:

```bash
./bin/algo install-node --dry-run --user live_bot
./bin/algo install-ops-timers --dry-run
```

Install and enable default production node timers:

```bash
./bin/algo install-node --user live_bot
```

Install and enable default timers only:

```bash
./bin/algo install-ops-timers
```

Install and enable the optional replay timer:

```bash
./bin/algo install-node --enable-replay --user live_bot
```

Reload after editing unit files manually:

```bash
sudo systemctl daemon-reload
sudo systemctl restart algosphere-premarket.service
sudo systemctl restart algosphere-premarket.timer
sudo systemctl restart algosphere-ops-premarket-ready.timer
sudo systemctl restart algosphere-ops-startup-validation.timer
sudo systemctl restart algosphere-ops-daily-summary.timer
sudo systemctl restart algosphere-ops-postmarket-analytics.timer
sudo systemctl restart algosphere-ops-replay-summary.timer
```

## Troubleshooting

Dynamic momentum catalyst gain filter:

- Dynamic scanner max price defaults to `dynamic_universe.max_price: 150`.
- Normal dynamic candidates still use `dynamic_universe.max_day_gain_pct`.
- Catalyst-backed candidates can use `dynamic_universe.catalyst_boost.max_gain_pct_catalyst` after quote, spread, price, and liquidity checks pass.
- The override is scanner-only and does not change sizing, allocation, order placement, or exits.
- Inspect `DYNAMIC_GAIN_FILTER_LIMITS`, `DYNAMIC_CATALYST_RELAXED_GATE`, `DYNAMIC_SCORE_SOURCE`, and `DYNAMIC_OVERRIDE_DECISION` in `algo.service` logs.

Inspect timer schedules:

```bash
systemctl list-timers 'algosphere-*'
systemctl cat algosphere-premarket.service
systemctl cat algosphere-premarket.timer
systemctl cat algosphere-ops-postmarket-analytics.timer
```

Inspect service status and logs:

```bash
systemctl status algosphere-premarket.service
systemctl status algosphere-ops-premarket-ready.service
systemctl status algosphere-ops-startup-validation.service
journalctl -u algosphere-premarket.service --since today --no-pager
journalctl -u algosphere-premarket.service -n 120 --no-pager | grep -A5 -B5 "provider=alpaca"
journalctl -u algosphere-ops-startup-validation.service --since today --no-pager
journalctl -u algo.service --since today --no-pager | grep PREMARKET_STARTUP_ARTIFACTS
```

`algosphere-premarket.service` requires the same Alpaca credentials as
`algo.service` and loads them from `EnvironmentFile=/etc/algo.env`. Verify the
installed unit with `systemctl cat algosphere-premarket.service`. After updating
the unit, run:

```bash
sudo systemctl daemon-reload
sudo systemctl restart algosphere-premarket.service
journalctl -u algosphere-premarket.service -n 120 --no-pager | grep -A5 -B5 "provider=alpaca"
```

Expected Alpaca provider diagnostics include `selected=live`,
`live_key_present=true`, `credentials_present=true`, and `request_sent=true`.

Validate artifacts and reports:

```bash
ls -l data/premarket/latest_event_feed.json data/premarket/latest_rankings.json data/premarket/latest_catalysts.json
ls -l data/premarket/provider_diagnostics_latest.json data/premarket/news_diagnostics_latest.json data/premarket/social_sentiment_latest.json data/premarket/previous_non_empty_*.json
./bin/algo premarket-ready
python -m json.tool data/premarket/provider_diagnostics_latest.json
python -m json.tool data/premarket/news_diagnostics_latest.json
python -m json.tool data/premarket/social_sentiment_latest.json
ls -l reports/daily/2026-06-07/
ls -l data/logs/ops_*_2026-06-07.log
```

Run read-only provider diagnostics for one symbol:

```bash
./bin/algo news-diagnostics --provider alpaca --symbol AAPL --hours 24 --limit 10
./bin/algo news-diagnostics --provider newsapi --symbol AAPL --hours 24 --limit 10
./bin/algo news-diagnostics --provider sec --symbol AAPL --hours 24 --limit 10
```

The command writes `data/premarket/news_diagnostics_latest.json` and does not
submit orders or alter trading decisions.

Run read-only social sentiment diagnostics:

```bash
./bin/algo social-sentiment --symbols AAPL,NVDA,PLTR --hours 24
```

The command writes `data/premarket/social_sentiment_latest.json`. Reddit rows
include mention counts, unique author counts, bullish/bearish/neutral counts,
sentiment and velocity scores, top post titles, and source breakdowns. Pump/spam
guards cap each author's contribution, ignore very new accounts when Reddit
provides account age, require a minimum unique-author count for nonzero scores,
and filter obvious pump language unless the symbol is confirmed elsewhere.

Common failure checks:

- If premarket readiness fails, inspect `data/premarket/latest_*.json` and
  `journalctl -u algosphere-premarket.service --since today --no-pager`.
- If Alpaca credentials are missing in scheduled premarket collection, verify
  `systemctl cat algosphere-premarket.service` includes
  `EnvironmentFile=/etc/algo.env`, then run `sudo systemctl daemon-reload` and
  `sudo systemctl restart algosphere-premarket.service`. Confirm the journal
  shows `selected=live`, `live_key_present=true`, `credentials_present=true`,
  and `request_sent=true` near `provider=alpaca`.
- If readiness reports `status=fresh_empty`, the latest artifacts are current
  but contain zero catalyst-ranked symbols. Inspect
  `data/premarket/provider_diagnostics_latest.json` and the provider status
  lines printed by `./bin/algo premarket-ready`.
- If NewsAPI returns 429, confirm `PREMARKET_PROVIDER_STATUS provider=newsapi`
  shows `http_status=429 rate_limited=true`, then wait for the provider quota
  window to recover or disable NewsAPI on nodes without a paid subscription by
  setting `premarket_intelligence.newsapi.enabled: false`. When disabled,
  NewsAPI diagnostics report `reason=newsapi_disabled`; `earnings_overnight`
  reports `reason=depends_on_newsapi_disabled` because that path uses NewsAPI.
  Alpaca News and SEC filings continue to run. Use
  `./bin/algo news-diagnostics --provider newsapi --symbol AAPL` to verify
  credentials, request status, counts, and sample titles for a single symbol.
- If Alpaca shows HTTP 200 with zero articles, confirm
  `PREMARKET_PROVIDER_STATUS provider=alpaca http_status=200 raw_count=0`.
  This means the request succeeded but Alpaca returned no matching articles for
  the current universe/window; compare against `previous_non_empty_*.json` for
  the last ranked snapshot. Use
  `./bin/algo news-diagnostics --provider alpaca --symbol AAPL` to isolate the
  provider response.
- If SEC filings look missing, run
  `./bin/algo news-diagnostics --provider sec --symbol AAPL` and inspect
  `raw_count`, `filtered_count`, and `reason`.
- If Reddit social sentiment is empty, run
  `./bin/algo social-sentiment --symbols AAPL,NVDA,PLTR --hours 24` and inspect
  `data/premarket/social_sentiment_latest.json`. `reason=reddit_credentials_missing`
  means the Reddit OAuth env vars are not configured. `reason=no_mentions` or
  `reason=no_filtered_mentions` means the provider ran but returned no usable
  posts after spam and relevance filters.
- If startup validation fails, confirm `algo.service` started and emitted a
  `PREMARKET_STARTUP_ARTIFACTS` log line after 09:30 ET.
- If daily reports are missing, run the matching `./bin/algo ops ...` command
  manually with the trading date and inspect `data/logs/`.
- If direct script execution fails with imports, retry through `./bin/algo` or
  set `PYTHONPATH=.` for scripts that require it.

## Daily Operator Checklist

- Before 09:20 ET, confirm `algosphere-premarket.timer` has run recently:
  `systemctl list-timers 'algosphere-premarket.timer'`.
- At 09:20 ET, confirm `./bin/algo premarket-ready` exits 0.
- At 09:30 ET, confirm `algo.service` is running.
- At 09:35 ET, confirm startup validation found
  `PREMARKET_STARTUP_ARTIFACTS status=fresh`.
- After 16:15 ET, confirm `reports/daily/YYYY-MM-DD/daily_summary.txt` exists.
- After 16:25 ET, confirm catalyst and profitability reports exist.
- After 16:35 ET, if replay is enabled, confirm `replay_summary.txt` exists.
- After 16:45 ET, confirm `reports/research_feedback/YYYY-MM-DD.md` exists.
- On Saturday, confirm `reports/research_feedback/week_YYYY-MM-DD.md` exists.
- Review `data/logs/ops_*_YYYY-MM-DD.log` for nonzero exit codes.

## Daily Analytics

Use an explicit trading date for reproducible reports:

```bash
./bin/algo summary 2026-06-07 --user live_bot
python scripts/generate_profitability_attribution_report.py --date 2026-06-07 --user live_bot
python scripts/replay_market_session.py --date 2026-06-07 --user live_bot --broker-mock
python scripts/generate_research_feedback.py 2026-06-07 --user live_bot
python scripts/generate_catalyst_outcomes.py --date 2026-06-07 --user live_bot
./bin/algo dynamic-rejection-report --date 2026-06-07 --user live_bot
./bin/algo dynamic-gate-research --date 2026-06-07 --user live_bot
```

The combined summary, profitability attribution, and replay scripts also
support `latest` when local artifacts exist:

```bash
./bin/algo summary latest --user live_bot
python scripts/generate_profitability_attribution_report.py --date latest --user live_bot
python scripts/replay_market_session.py --date latest --user live_bot --broker-mock
python scripts/generate_research_feedback.py latest --user live_bot
./bin/algo catalyst-outcomes --date latest --user live_bot
./bin/algo dynamic-rejection-report --date latest --user live_bot
./bin/algo dynamic-gate-research --date latest --user live_bot
```

`latest` resolves from local artifacts under `data/`. For the combined summary
and profitability attribution, this includes daily attribution, profitability,
daily summary, order history, and replay report files. For full market-session
replay, it resolves from dynamic scan history snapshots and prior
`data/replay_market_session` summaries. For catalyst outcomes, it resolves from
existing catalyst outcome research databases, dynamic scan history, trade
attribution, and current premarket artifacts.

The catalyst outcome database is research-only. It records dynamic, news, and
catalyst candidates plus later local outcomes for review; it does not change
entry filters, exits, sizing, allocation, or order placement.

## Scoring Prefilter Bar Requirements

The scoring prefilter uses MA20, MA50, MA200, and 20-day average volume inputs.
When a symbol has fewer than 200 daily bars, diagnostics identify the missing
indicator, for example `missing_indicators=ma200,ma50_gt_ma200`.

Defaults preserve the existing behavior for both core and dynamic candidates:

```yaml
scoring:
  min_history_bars:
    core: 200
    dynamic: 200
    enable_dynamic_override: false
```

To test a shorter dynamic-only requirement without changing core trend names,
set `enable_dynamic_override: true` and lower `dynamic`. This is config-gated;
with the default `false`, dynamic momentum candidates still require 200 bars.

Dynamic scanner rejection research is also read-only. Rejected candidates are
persisted in `data/dynamic_scan_history/*.json` with timestamp, symbol, price,
gain, relative volume, spread, news score, catalyst score, rejection reason, and
same-day high/return when later local bars are available. The rejection report
is written to:

- `reports/research_feedback/dynamic_rejections_YYYY-MM-DD_live_bot.md`
- `reports/research_feedback/dynamic_rejections_YYYY-MM-DD_live_bot.json`

Use it after the session to find rejected symbols that later moved +5%, +10%,
or +20%:

```bash
./bin/algo dynamic-rejection-report --date 2026-06-07 --user live_bot
./bin/algo dynamic-rejection-report --date latest --user live_bot
```

Dynamic gate research explains what blocked dynamic momentum names after the
scanner found them. It parses only local dynamic scan history and logs, then
writes:

- `data/research/dynamic_gate_research/YYYY-MM-DD_live_bot.json`
- `data/research/dynamic_gate_research/YYYY-MM-DD_live_bot.txt`

Run it after a session to summarize downstream gates:

```bash
./bin/algo dynamic-gate-research --date 2026-06-07 --user live_bot
./bin/algo dynamic-gate-research --date latest --user live_bot
```

The same dynamic gate report includes entry-alignment forward-return research
for symbols rejected by `need 5m breakout OR new intraday high OR strong green
1m OR opening-range breakout`. When local intraday bars are available, it
reports 15-minute, 30-minute, 60-minute, and end-of-day average return, median
return, win rate, and best/worst examples. Use `--bars-dir` to point at an
alternate local bar cache without changing trading behavior.

Dynamic selected-entry gap diagnostics are logging-only. They do not change
selection, entry filters, sizing, allocation, risk, or order placement. During
entry scanning, selected dynamic symbols emit `DYNAMIC_SELECTED_ENTRY_TRACE`
with dynamic-set, effective-universe, scoring-top-N, scoring gate, dynamic
bypass, route candidate, selected count, and rank fields when available. Before
pre-entry `continue` paths, the loop emits `DYNAMIC_SELECTED_ENTRY_DROP` with a
stage, reason, and detail. Immediately before dynamic entry evaluation/logging,
it emits `DYNAMIC_SELECTED_ENTRY_EVAL_START`.

Use the read-only gap report after a session to explain selected dynamic symbols
that entered `DYNAMIC_UNIVERSE` but never reached `ENTRY_EVAL`:

```bash
./bin/algo dynamic-entry-gap-report --date latest --user paper_bot
```

Allocator dispatch dynamic RVOL diagnostics are logging-only. In paper/replay
contexts, dispatch emits `DISPATCH_DYNAMIC_RVOL_CHECK` around the dynamic
relative-volume validation. The line records the symbol, route, source,
relative volume, base and effective RVOL floors, override state, news/catalyst
metadata, scanner effective floor when present, entry route, decision flag, and
dispatch result. When expected override metadata is absent, dispatch also emits
`DISPATCH_DYNAMIC_METADATA_MISSING` with `missing_fields` and `available_keys`.
If dispatch skips the action for `dynamic_relative_volume`, it emits
`DISPATCH_DYNAMIC_RVOL_SKIP_DETAIL` with the threshold used and why the override
did or did not apply.

Use the allocator/dispatch mismatch report after a paper session to see whether
scanner or entry-eval RVOL override metadata made it to dispatch:

```bash
./bin/algo allocator-dispatch-mismatch-report --date latest --user paper_bot
```

Allocator sizing diagnostics are also logging-only. During allocator planning,
each candidate emits `ALLOCATOR_SIZE_TRACE` before the minimum-deploy check.
The trace records rank, route/source, dynamic flag, score, account equity,
cash, gross headroom, raw target notional, sleeve/cap/headroom stages, final
trade size, `minimum_cash_to_deploy`, `min_realloc_leg`, and whether the
candidate was skipped by the deployment floor. Use it to explain cases such as
INTC being sized to `1312.50` before being rejected by a `3470` deploy floor.

The read-only sizing report parses those traces and identifies the first stage
that clipped the candidate and the first stage where size fell below the deploy
floor:

```bash
./bin/algo dynamic-allocator-sizing-report --date latest --user paper_bot
```

The dynamic allocator minimum-deploy experiment is paper-only and opt-in. When
enabled, dynamic candidates in paper mode use `min_realloc_leg` instead of
`minimum_cash_to_deploy` for the allocator deployment-floor check. Live mode,
core/trend-long candidates, and default configuration keep the normal
`minimum_cash_to_deploy` behavior. Do not enable a live equivalent without
separate forward evidence.

```yaml
dynamic_universe:
  paper_dynamic_min_deploy_experiment:
    enabled: false
    use_min_realloc_leg: true
```

When active, allocator logs include:

```text
DYNAMIC_MIN_DEPLOY_EXPERIMENT symbol=INTC mode=paper dynamic_candidate=true original_floor=3468.00 experiment_floor=1200.00 trade_size=1312.50 would_have_skipped_default=true
```

## Premarket Readiness

Before the open, verify premarket artifacts:

```bash
./bin/algo premarket-ready
```

The command checks:

- `data/premarket/latest_event_feed.json`
- `data/premarket/latest_rankings.json`
- `data/premarket/latest_catalysts.json`

It exits nonzero if artifacts are missing, stale, unreadable, or have no
catalyst-ranked symbols.
# Post-Reboot Boot Health

`algo-boot-health.service` runs once after reboot. It waits for
`network-online.target`, runs after `algo.service` has started, delays briefly
with `ALGO_BOOT_HEALTH_DELAY_SEC`, then executes:

```bash
/usr/bin/python3.12 -m src.boot_health
```

Manual checks:

```bash
./bin/algo boot-health
./bin/algo boot-health --json
./bin/algo boot-health --quiet
```

Exit codes:

- `0`: ready for live trading
- `1`: one or more readiness checks failed
- `2`: the boot-health command itself failed unexpectedly

Reports are written atomically to:

```bash
reports/boot_health/latest.json
reports/boot_health/latest.md
```

Each report includes UTC and America/New_York timestamps, hostname, boot ID,
uptime, Git commit, live/paper environment, every check result, overall
readiness, and any repair actions.

Repair mode is intentionally narrow:

```bash
./bin/algo boot-health --repair
```

Allowed repairs are starting `algo.service` or `algo-health-check.timer` only
when already enabled, resetting stale failed status for currently healthy units,
running the regular health check, creating the report directory, and restoring
the specific `scripts/check_algo_health.sh` SELinux context only when the
persistent `bin_t` file-context rule already exists.

Repair mode must not enable live trading, change credentials, modify trading
configuration, place orders, disable SELinux, delete NetworkManager profiles,
restart NetworkManager, reboot the machine, unmask arbitrary services, relabel
the project tree, or repeatedly restart the algo.

Operator checklist:

```bash
./bin/algo boot-health
systemctl is-active algo.service
systemctl is-active algo-health-check.timer
systemctl --failed
journalctl -u algo-boot-health.service -b --no-pager
journalctl -u algo.service -b -n 100 --no-pager
```

Timer verification:

```bash
systemctl is-enabled algo-health-check.timer
systemctl is-active algo-health-check.timer
systemctl list-timers algo-health-check.timer --all
```

Systemd logs:

```bash
journalctl -u algo-boot-health.service -b --no-pager
journalctl -u algo-health-check.service -b --no-pager
journalctl -u algo.service -b -n 100 --no-pager
```

SELinux troubleshooting: keep SELinux enforcing. Verify the health-check script
label and restore only that file when the persistent rule exists:

```bash
ls -Z /opt/algosphere/algo-ai-trading-agent/scripts/check_algo_health.sh
semanage fcontext -l | grep -F /opt/algosphere/algo-ai-trading-agent/scripts/check_algo_health.sh
restorecon -v /opt/algosphere/algo-ai-trading-agent/scripts/check_algo_health.sh
```

NetworkManager wait-online troubleshooting:

```bash
nmcli general
nmcli -t -f CONNECTIVITY general
ip route show default
getent hosts api.alpaca.markets
nmcli connection show --active
```

`cherry` should be the active Wi-Fi profile. `Bridge br0` should not be active
or stuck connecting. `serial-getty@ttyS0.service` should remain `masked`.

When readiness fails, inspect `reports/boot_health/latest.md` first, then the
boot-health and algo journals. Use `--repair` only for the constrained actions
above. If Alpaca checks fail, verify credentials in `/etc/algo.env` and network
connectivity; do not edit trading config during boot recovery. If heartbeat
fails during a regular market session, inspect recent `algo.service` logs for
cycle or heartbeat output before deciding whether a manual service start is
appropriate.

# Profitability Controls

AlgoSphere has explicit operating modes under `trading_control.mode`:

- `live`: existing live behavior; real orders are allowed only with normal safety checks.
- `paper`: paper broker/execution; signals and positions are processed normally.
- `shadow`: production decisions run, but broker submission is blocked and shadow order objects are returned.
- `entries-disabled`: safe exits may continue, but new buy entries are blocked with `ENTRY_BLOCKED_MODE_ENTRIES_DISABLED`.

Manual mode examples:

```bash
./bin/algo live --mode entries-disabled
./bin/algo live --mode shadow
./bin/algo live --mode live
```

Startup logs include:

```text
TRADING_MODE mode=<mode> live_orders_allowed=<true|false> new_entries_allowed=<true|false>
STRATEGY_STATE route=<route> state=<LIVE|SHADOW|DISABLED>
```

Every route must have a production state in `trading_control.strategy_states`.
Experimental or insufficiently validated routes default to `SHADOW`. Promotion
to `LIVE` is manual only.

Operator workflow:

```bash
./bin/algo day-review --date YYYY-MM-DD --user live_bot
./bin/algo duplicate-forensics --date YYYY-MM-DD --user live_bot
./bin/algo trading-audit --date YYYY-MM-DD --user live_bot
./bin/algo profitability-report --from YYYY-MM-DD --to YYYY-MM-DD --user live_bot
./bin/algo news-edge-report --from YYYY-MM-DD --to YYYY-MM-DD
./bin/algo strategy-readiness
```

`day-review` is the first report to run after a session. It uses the canonical
lifecycle layer and writes `reports/day_review/YYYY-MM-DD.json` and `.md`.
Exit code `0` means the report was generated and lifecycle status is `CLEAN` or
`PARTIAL`; exit code `1` means the report generated but lifecycle status is
`CONTAMINATED`, `UNRECONCILED`, or a readiness gate failed; exit code `2` means
the command failed.

`duplicate-forensics` writes `reports/duplicate_forensics/YYYY-MM-DD.json`
and `.md`. It is read-only and shows duplicate order/fill source rows,
canonical identity keys, byte-identical duplicates, cumulative snapshots, and
whether replay/mock rows leaked into live attribution.

`trading-audit` writes:

```bash
reports/trading_audit/YYYY-MM-DD.json
reports/trading_audit/YYYY-MM-DD.md
```

It reconciles candidates, entry decisions, submissions, broker status, fills,
positions, exits, and P&L. It does not count submitted orders as fills, alerts
as trades, or open attempts as positions.

Authoritative lifecycle sources:

- entry decisions: `data/trade_attribution/daily/<date>_<user>.json`, `candidates`
- submitted orders and broker acknowledgements: `orders`, de-duplicated by broker order ID, client order ID, then logical order ID
- order snapshots: raw `orders` rows only; snapshots are not trades
- fills: broker activity/fill ID when present; otherwise deterministic cumulative fill deltas per canonical order
- positions: derived from unique fills by canonical position identity
- closed positions: unique exit fills matched to canonical open positions
- daily aggregates and summaries: not authoritative for lifecycle counts

Trading dates for orders, fills, opens, and closes use broker event time
normalized to `America/New_York`. Ingestion time, report generation time, file
mtime, and local update time are not trading-date sources.

Lifecycle integrity status:

- `CLEAN`: canonical counts reconcile and no replay contamination exists
- `PARTIAL`: core lifecycle is clean but optional context is unavailable
- `CONTAMINATED`: replay/mock/shadow records exist in live sources
- `UNRECONCILED`: orders, fills, positions, or exits do not link
- `INSUFFICIENT_DATA`: lifecycle is valid but outcomes cannot be measured

Replay contamination prevention blocks new live attribution rows with
`LIVE_DATA_CONTAMINATION_BLOCKED` when environment/origin/identifier evidence
shows replay, mock, shadow, paper, or test data. Historical quarantine is
non-destructive and writes manifests under:

```text
data/quarantine/replay_contamination/YYYY-MM-DD/
```

Forward-outcome enrichment uses decision time as time zero and local supported
bars only. Missing bars remain `OUTCOME_UNAVAILABLE_MISSING_BARS`; unavailable
option quotes remain unavailable rather than modeled as real option P&L.

Adaptive relaxation is production-safe by default:

```text
ADAPTIVE_RELAXATION production_auto_apply=false
```

Low trade frequency is informational only and does not loosen production
filters. Zero-trade days are permitted.

`profitability-report` separates gross and net expectancy, spread/slippage
costs, MFE/MAE, MFE capture, latency, and route/symbol/exit/loss-attribution
breakdowns. Small samples are marked with sample-size warnings and must not be
treated as statistically reliable.

`news-edge-report` compares ingested news records against trade linkage when
available. Successful ingestion alone is not treated as predictive value.
Recommendations are limited to `KEEP`, `DOWNWEIGHT`, `SHADOW_ONLY`, `DISABLE`,
or `INSUFFICIENT_DATA`.

Loss attribution is deterministic. Losing trades are classified into one
primary cause such as `BAD_SIGNAL`, `LATE_ENTRY`, `BAD_OPTION_CONTRACT`,
`WIDE_SPREAD`, `SLIPPAGE`, `EXIT_GIVEBACK`, `RUNTIME_FAILURE`,
`UNRECONCILED`, or `INSUFFICIENT_DATA`. Unreconciled trades are never reported
as normal strategy losses.

MFE is maximum favorable excursion after entry. MAE is maximum adverse
excursion after entry. MFE capture ratio is:

```text
realized profit / maximum favorable excursion
```

Zero-MFE and losing trades are handled without division-by-zero.

Spread and slippage review:

```bash
./bin/algo profitability-report --from YYYY-MM-DD --to YYYY-MM-DD --user live_bot --json
./bin/algo trading-audit --date YYYY-MM-DD --user live_bot --json
```

Contract quality gates are configured under `contract_quality`; poor contracts
must be rejected instead of used as a fallback. Every candidate contract can be
audited for spread, volume, open interest, delta, strike distance, DTE, quote
age, and expected slippage.

Experiments are bounded and manual:

```bash
./bin/algo experiment list
./bin/algo experiment report <experiment-id>
```

Each experiment may change only one variable, run only in replay or shadow
mode, and requires explicit sample and degradation limits.

To restore live entries safely:

```bash
./bin/algo trading-audit --date YYYY-MM-DD --user live_bot
./bin/algo profitability-report --from YYYY-MM-DD --to YYYY-MM-DD --user live_bot
./bin/algo strategy-readiness
./bin/algo live --mode live
```

Do not restore live entries when audit data is unreconciled, sample size is too
small, runtime failures affected results, or required quote/news/fill fields
are unavailable.
