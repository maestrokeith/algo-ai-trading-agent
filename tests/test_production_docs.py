"""Tests for production process documentation."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_production_deployment_checklist_covers_required_steps() -> None:
    text = (ROOT / "docs" / "production_deployment_checklist.md").read_text(encoding="utf-8")

    for heading in (
        "## Tests",
        "## Preflight",
        "## Restart Procedure",
        "## Validation Procedure",
        "## Abort Criteria",
    ):
        assert heading in text
    for command in (
        "PYTHONPATH=. pytest tests/ -v",
        "python scripts/preflight_live_safety.py --project-root .",
        "python scripts/run_alpaca_loop.py --live",
        "python scripts/generate_premarket_health_report.py --live --user-label default",
    ):
        assert command in text


def test_release_management_covers_rollback_requirements() -> None:
    text = (ROOT / "docs" / "release_management.md").read_text(encoding="utf-8")

    for heading in (
        "## Version Tracking",
        "## Stable Release Tags",
        "## Release Notes",
        "## Rollback Command",
        "## Rollback Validation",
    ):
        assert heading in text
    for phrase in (
        "git tag -a prod-YYYYMMDD-N",
        "git reset --hard prod-YYYYMMDD-N",
        "python scripts/show_release_status.py",
        "docs/releases/prod-YYYYMMDD-N.md",
    ):
        assert phrase in text
