from __future__ import annotations

from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT = PROJECT_ROOT / "docs" / "options_allowed_underlyings_issue_110.md"


def test_issue_110_report_exists_and_documents_required_review() -> None:
    report = REPORT.read_text(encoding="utf-8")

    assert "Issue 110" in report
    assert "No trading logic changes were made" in report
    assert "liquidity/open-interest evidence is absent" in report
    for symbol in ("ORCL", "MU", "ANET", "MRVL", "TSM", "AVGO", "GOOGL"):
        assert symbol in report


def test_default_options_allowlists_include_expanded_liquid_universe() -> None:
    config = yaml.safe_load((PROJECT_ROOT / "config" / "default.yaml").read_text(encoding="utf-8"))

    required = {
        "SPY",
        "QQQ",
        "NVDA",
        "AAPL",
        "AMZN",
        "SMH",
        "ORCL",
        "BABA",
        "INDA",
        "GOOG",
        "MU",
        "TSM",
        "ANET",
        "MRVL",
        "INTC",
    }
    allowed_underlyings = config["options"]["allowed_underlyings"]
    allowed_symbols = config["options"]["allowed_symbols"]

    assert required.issubset(set(allowed_underlyings))
    assert allowed_symbols == allowed_underlyings
    assert len(allowed_underlyings) == len(set(allowed_underlyings))
