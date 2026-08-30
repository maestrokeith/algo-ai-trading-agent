# Rollback And Release Management

This process defines how AlgoSphere production releases are tagged, documented, validated, and rolled back. It is designed to return the running system to a known-good version in under 5 minutes once an operator decides to roll back.

## Version Tracking

Before a release, record the current local version:

```bash
python scripts/show_release_status.py
git rev-parse HEAD
git status --short --branch
```

The release status output records:

- `branch`: expected to be `main` for production release work.
- `commit`: short immutable commit id.
- `version`: nearest git tag or commit description.
- `worktree`: `clean` or `dirty`.

Production releases require a clean worktree.

## Stable Release Tags

Use annotated tags for known-good production releases:

```bash
git tag -a prod-YYYYMMDD-N -m "prod-YYYYMMDD-N: short release summary"
```

Tag naming:

- `prod-YYYYMMDD-N` where `N` starts at `1` for the day.
- Example: `prod-20260605-1`.
- Tag only commits that have passed full pytest, live preflight, and operator validation.

Do not tag uncommitted changes or a dirty worktree.

## Release Notes

Create release notes before tagging. Use this template:

```markdown
# Release prod-YYYYMMDD-N

## Commit

- `<full_commit_sha>`

## Summary

- What changed

## Validation

- `PYTHONPATH=. pytest tests/ -v`
- `python scripts/preflight_live_safety.py --project-root .`
- `python scripts/generate_premarket_health_report.py --live --user-label default`

## Operational Notes

- Expected open orders:
- Expected exposure:
- Expected startup mode:

## Rollback Target

- Previous known-good tag:
```

Store release notes in `docs/releases/prod-YYYYMMDD-N.md` when preparing a production release.

## Release Procedure

1. Confirm `main` is checked out and clean:

```bash
git status --short --branch
python scripts/show_release_status.py
```

2. Run the deployment checklist in `docs/production_deployment_checklist.md`.
3. Create release notes under `docs/releases/`.
4. Tag the release:

```bash
git tag -a prod-YYYYMMDD-N -m "prod-YYYYMMDD-N: short release summary"
```

5. Start or restart the live loop only after validation passes.
6. Record the final release status in the operator log.

## Rollback Command

Use the previous known-good production tag from release notes or `git tag --list 'prod-*' --sort=-creatordate`.

```bash
git fetch --tags
git switch main
git reset --hard prod-YYYYMMDD-N
python scripts/show_release_status.py
PYTHONPATH=. pytest tests/ -v
python scripts/preflight_live_safety.py --project-root .
python scripts/run_alpaca_loop.py --live
```

Rollback constraints:

- Do not edit `.env` files.
- Do not edit broker credentials, account IDs, live keys, or systemd service files.
- Do not reset paper/live account state as part of rollback.
- Do not cancel open orders unless the incident runbook explicitly calls for it.

## Rollback Validation

After rollback:

- `python scripts/show_release_status.py` reports the expected known-good tag.
- Full pytest passes or the operator accepts a documented incident exception.
- Live preflight passes.
- Premarket health report is ready if rolling back before market open.
- The live loop starts once, under the expected `user_id`, in the expected broker mode.
- Open orders, positions, and exposure match the rollback expectation.

## Failure Handling

If rollback validation fails:

- Stop the live loop.
- Keep the broker account unchanged unless an explicit trade-risk incident requires intervention.
- Capture `python scripts/show_release_status.py`, failing command output, and the latest live loop logs.
- Escalate with the current tag, intended rollback tag, account mode, and validation failure reason.
