"""Tests for paper options end-to-end validation."""
from __future__ import annotations

from datetime import datetime, timezone

from src.options_paper_validation import (
    PaperValidationBroker,
    paper_validation_config,
    sample_validation_chain,
    validate_options_paper_e2e,
)
from src.options_position_manager import options_state_path


def test_validate_options_paper_e2e_success(tmp_path) -> None:
    now = datetime(2026, 6, 5, 14, 0, tzinfo=timezone.utc)
    cfg = paper_validation_config("QQQ")
    chain = sample_validation_chain("QQQ", now)
    broker = PaperValidationBroker(chain)

    report = validate_options_paper_e2e(
        cfg,
        broker=broker,
        symbol="QQQ",
        user_id="u1",
        data_dir=tmp_path,
        now=now,
        chain_candidates=chain,
    )

    assert report.passed is True
    assert report.order_symbol == chain[0].symbol
    assert [s.name for s in report.steps] == [
        "paper_mode",
        "option_scan",
        "contract_selection",
        "order_submission",
        "entry_persistence",
        "exit_handling",
    ]
    assert broker.submitted_orders
    req = broker.submitted_orders[0]
    assert req.symbol == chain[0].symbol
    assert req.side == "buy"
    assert req.limit_price is not None
    assert options_state_path("u1", data_dir=tmp_path).exists()


def test_validate_options_paper_e2e_blocks_non_paper(tmp_path) -> None:
    now = datetime(2026, 6, 5, 14, 0, tzinfo=timezone.utc)
    cfg = paper_validation_config("QQQ")
    cfg["broker"]["paper"] = False
    broker = PaperValidationBroker(sample_validation_chain("QQQ", now))
    broker.paper = False

    report = validate_options_paper_e2e(
        cfg,
        broker=broker,
        symbol="QQQ",
        user_id="u1",
        data_dir=tmp_path,
        now=now,
    )

    assert report.passed is False
    assert report.steps[0].name == "paper_mode"
    assert report.steps[0].passed is False
    assert not broker.submitted_orders


def test_validate_options_paper_e2e_fails_without_chain(tmp_path) -> None:
    cfg = paper_validation_config("QQQ")
    broker = PaperValidationBroker([])

    report = validate_options_paper_e2e(
        cfg,
        broker=broker,
        symbol="QQQ",
        user_id="u1",
        data_dir=tmp_path,
        chain_candidates=[],
    )

    assert report.passed is False
    assert [s.name for s in report.steps] == ["paper_mode", "option_scan"]
    assert report.steps[-1].detail == "0 candidate(s) available"
