"""Tests for :mod:`src.sell_logging`."""

from __future__ import annotations

import json
import logging

import pytest

from src.sell_logging import log_sell, sell_log_reason_for_engine_exit


def test_log_sell_emits_json_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    log_sell("xlp", "stop_loss", {"user_id": "u1", "qty": 10})
    assert len(caplog.records) == 1
    rec = caplog.records[0]
    assert rec.levelname == "INFO"
    assert "[sell]" in rec.getMessage()
    payload = json.loads(rec.getMessage().split("[sell] ", 1)[1])
    assert payload["symbol"] == "XLP"
    assert payload["reason"] == "stop_loss"
    assert payload["user_id"] == "u1"
    assert payload["qty"] == 10


def test_log_sell_unknown_reason_warns(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    log_sell("spy", "not_a_real_reason", {})
    warns = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("unknown reason" in r.getMessage() for r in warns)
    infos = [r for r in caplog.records if r.levelname == "INFO"]
    line = json.loads(infos[-1].getMessage().split("[sell] ", 1)[1])
    assert line["reason"] == "signal_flip"


@pytest.mark.parametrize(
    ("engine_val", "expected"),
    [
        ("stop_loss", "stop_loss"),
        ("option_stop_loss", "stop_loss"),
        ("tp", "take_profit"),
        ("trail", "take_profit"),
        ("partial_take_profit", "take_profit"),
        ("time_bars", "time_exit"),
        ("option_max_hold_days", "time_exit"),
        ("signal_exit", "signal_flip"),
        ("risk_cap_rebalance", "exposure_limit"),
        ("overweight_trim", "rebalance_trim"),
    ],
)
def test_sell_log_reason_for_engine_exit(engine_val: str, expected: str) -> None:
    assert sell_log_reason_for_engine_exit(engine_val) == expected
