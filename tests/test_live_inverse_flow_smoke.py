"""Smoke tests for bear / inverse live entry module (no broker I/O when paths are idle)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import pytz

from src.live.inverse_flow import BearInverseContext, run_bear_inverse_flow


@pytest.fixture
def et_now() -> datetime:
    et = pytz.timezone("America/New_York")
    return et.localize(datetime(2026, 4, 1, 10, 30, 0))


def test_run_bear_inverse_flow_returns_configured_universe_idle(
    et_now: datetime, tmp_path: Path
) -> None:
    """Non-bearish tick: no symbol loop; universe set still reflects config."""
    config = {
        "universe": {
            "bear_etfs": {
                "symbols": ["SQQQ", "SPXS"],
                "controlled_scaling": {"enabled": False},
            }
        }
    }
    exposure = SimpleNamespace(
        sector_pct={},
        gross_pct=0.0,
        net_pct=0.0,
        theme_pct={},
    )
    policy = SimpleNamespace(
        score=1,
        sqqq_notional_fraction=1.0,
        long_notional_fraction=1.0,
        sqqq_requires_severe_breakdown=False,
        long_require_ma_stack=False,
    )
    ctx = BearInverseContext(
        now=et_now,
        verbose=False,
        broker=MagicMock(),
        engine=MagicMock(),
        config=config,
        user_id="test_user",
        data_dir=tmp_path,
        account_equity=100_000.0,
        exposure_snapshot=exposure,
        allowed_symbols_for_stock_orders=None,
        open_order_symbols=set(),
        available_cash=50_000.0,
        stale_quote_max_age=60.0,
        regime_entry_policy=policy,
        regime_result=None,
        bear_inv_regime_mult=None,
        bearish_regime=False,
    )
    out = run_bear_inverse_flow(ctx, positions=[], tracked={}, current_positions={})
    assert out == {"SQQQ", "SPXS"}
    ctx.broker.get_bars.assert_not_called()


def test_run_bear_inverse_flow_skips_when_reduce_only(
    et_now: datetime, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = {
        "universe": {
            "bear_etfs": {
                "symbols": ["SQQQ", "SPXS"],
            }
        }
    }
    exposure = SimpleNamespace(
        sector_pct={},
        gross_pct=105.0,
        net_pct=0.0,
        theme_pct={},
    )
    policy = SimpleNamespace(
        score=1,
        sqqq_notional_fraction=1.0,
        long_notional_fraction=1.0,
        sqqq_requires_severe_breakdown=False,
        long_require_ma_stack=False,
    )
    ctx = BearInverseContext(
        now=et_now,
        verbose=False,
        broker=MagicMock(),
        engine=MagicMock(),
        config=config,
        user_id="test_user",
        data_dir=tmp_path,
        account_equity=100_000.0,
        exposure_snapshot=exposure,
        allowed_symbols_for_stock_orders=None,
        open_order_symbols=set(),
        available_cash=50_000.0,
        stale_quote_max_age=60.0,
        regime_entry_policy=policy,
        regime_result=None,
        bear_inv_regime_mult=None,
        bearish_regime=True,
        reduce_only=True,
    )
    out = run_bear_inverse_flow(ctx, positions=[], tracked={}, current_positions={})
    assert out == set()
    assert "reduce_only" in capsys.readouterr().out
