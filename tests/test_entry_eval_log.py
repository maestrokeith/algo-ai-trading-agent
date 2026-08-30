"""entry_eval_log helpers."""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import pytest

from types import SimpleNamespace

from src.entry_eval_log import (
    infer_spread_position_cooldown_ok,
    log_entry_eval,
    log_execution_block,
    log_options_gate,
    option_delta_from_chain,
    trend_scan_route_label,
)
from src.strategy import TrendFollowingStrategy


def test_infer_all_ok_when_allowed() -> None:
    assert infer_spread_position_cooldown_ok(allowed=True, reason=None) == (True, True, True)


def test_infer_cooldown() -> None:
    s, p, c = infer_spread_position_cooldown_ok(
        allowed=False, reason="cooldown after stop loss (5 min < 30 min)"
    )
    assert s is True and p is True and c is False


def test_trend_scan_route_label() -> None:
    assert trend_scan_route_label(is_dynamic_added=False) == "trend_long"
    assert trend_scan_route_label(is_dynamic_added=True) == "momentum_breakout"


def test_log_execution_block_info(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="src.entry_eval_log")
    log_execution_block(
        symbol="NVDA",
        spread_pct=1.5,
        buying_power=25000.0,
        cooldown_ok=True,
        position_ok=False,
    )
    assert "EXECUTION_BLOCK" in caplog.text
    assert "symbol=NVDA" in caplog.text
    assert "spread=1.50" in caplog.text
    assert "bp=25000.00" in caplog.text


def test_infer_market_quality_spread() -> None:
    s, p, c = infer_spread_position_cooldown_ok(
        allowed=False, reason="market_quality: spread 1.00% > max 0.70%"
    )
    assert s is False


def _df_uptrend(n: int = 220) -> pd.DataFrame:
    t = np.arange(n, dtype=float)
    close = 100.0 + t * 0.05 + np.sin(t / 10.0) * 0.5
    high = close + 0.3
    low = close - 0.3
    vol = np.full(n, 1e7)
    return pd.DataFrame({"high": high, "low": low, "close": close, "volume": vol})


def test_entry_eval_components_smoke() -> None:
    cfg = {"strategy": {"trend_following": {"ma_fast": 10, "ma_slow": 50, "entry_mode": "momentum"}}}
    st = TrendFollowingStrategy(cfg)
    df = _df_uptrend()
    t, p, m, v = st.entry_eval_components_for_log("TEST", df, spread_pct=0.01, atr_pct_now=1.0)
    assert m is True
    assert t is True and p is True and v is True


def test_log_options_gate_matches_expected_shape(capsys: pytest.CaptureFixture[str]) -> None:
    """Greppable line shape per ops dashboards (example in ticket)."""
    log_options_gate(
        symbol="SPY",
        gross_exposure_pct=78.0,
        reduce_only=False,
        spread_pct=4.0,
        dte=21,
        delta=0.45,
        final=True,
        reason="ok",
    )
    out = capsys.readouterr().out.strip()
    assert (
        out
        == "OPTIONS_GATE symbol=SPY gross=0.78 reduce_only=False spread=0.04 dte=21 delta=0.45 final=True reason=ok"
    )


def test_option_delta_from_chain_abs() -> None:
    chain = [SimpleNamespace(symbol="SPY240119C00450000", delta=-0.45)]
    assert option_delta_from_chain("SPY240119C00450000", chain) == pytest.approx(0.45)


def test_log_entry_eval_prints(capsys: pytest.CaptureFixture[str]) -> None:
    log_entry_eval(
        symbol="QQQ",
        route="trend_long",
        trend=True,
        pullback=True,
        momentum=False,
        volatility=True,
        regime=True,
        spread=True,
        position=True,
        cooldown=True,
        final_signal=True,
        final_reason="ok",
    )
    out = capsys.readouterr().out
    assert "QQQ ENTRY_EVAL" in out and "route=trend_long" in out and "final=T" in out


def test_log_entry_eval_prints_allocator_followup_immediately(
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_entry_eval(
        symbol="XLF",
        route="trend_long",
        trend=True,
        pullback=True,
        momentum=True,
        volatility=True,
        regime=True,
        spread=True,
        position=True,
        cooldown=True,
        final_signal=True,
        final_reason="ok",
        allocator_followup={
            "symbol": "XLF",
            "route": "trend_long",
            "reason": "ok",
            "allocator_on": True,
            "action": "enqueue",
            "stage": "entry_eval",
            "score": 0.0,
        },
    )
    lines = capsys.readouterr().out.strip().splitlines()

    assert len(lines) == 6
    assert "XLF ENTRY_EVAL route=trend_long" in lines[0]
    assert "final=T reason=ok" in lines[0]
    assert lines[1] == "ENTRY_EVAL_PASS symbol=XLF route=trend_long reason=ok allocator_on=true"
    assert lines[2].startswith(
        "ENTRY_TO_ALLOCATOR_TRACE symbol=XLF route=trend_long decision_present=true "
    )
    assert lines[3] == (
        "ENTRY_TO_ALLOCATOR_FOLLOWUP_START symbol=XLF route=trend_long action=enqueue stage=entry_eval"
    )
    assert lines[4] == (
        "ALLOCATOR_ENQUEUE symbol=XLF route=trend_long reason=ok score=0.0000 "
        "allocator_on=true final=true stage=entry_eval"
    )
    assert lines[5] == (
        "ENTRY_TO_ALLOCATOR_FOLLOWUP_END symbol=XLF route=trend_long result=enqueue stage=entry_eval"
    )


def test_log_entry_eval_append_now_emits_allocator_transition_without_enqueue(
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_entry_eval(
        symbol="XLF",
        route="trend_long",
        trend=True,
        pullback=True,
        momentum=True,
        volatility=True,
        regime=True,
        spread=True,
        position=True,
        cooldown=True,
        final_signal=True,
        final_reason="ok",
        allocator_followup={
            "symbol": "XLF",
            "route": "trend_long",
            "reason": "ok",
            "allocator_on": True,
            "action": "append_now",
            "stage": "entry_eval",
            "decision_present": True,
            "decision_allowed": True,
            "order_request_present": True,
            "ohlcv_present": True,
        },
    )
    lines = capsys.readouterr().out.strip().splitlines()

    assert len(lines) == 4
    assert "XLF ENTRY_EVAL route=trend_long" in lines[0]
    assert lines[1] == "ENTRY_EVAL_PASS symbol=XLF route=trend_long reason=ok allocator_on=true"
    assert lines[2] == (
        "ENTRY_TO_ALLOCATOR_TRACE symbol=XLF route=trend_long decision_present=true "
        "decision_allowed=true order_request_present=true ohlcv_present=true "
        "allocator_on=true followup_emitted=false"
    )
    assert lines[3] == (
        "ENTRY_TO_ALLOCATOR_FOLLOWUP_START symbol=XLF route=trend_long action=append_now stage=entry_eval"
    )
