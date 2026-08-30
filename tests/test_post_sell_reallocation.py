"""Post-sell equal-split reallocation (``run_post_sell_reallocation``)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.portfolio.allocator import run_post_sell_reallocation


def _base_kw(
    cap_rows: list[dict],
) -> dict:
    return dict(
        broker=MagicMock(),
        engine=MagicMock(),
        config={
            "portfolio": {
                "signal_ranking": {"max_signals_per_loop": 10},
                "capital_allocator": {
                    "post_sell_reallocation": {
                        "enabled": True,
                        "require_equity_sell": True,
                        "min_freed_cash": 1.0,
                        "split_n": 2,
                    }
                },
            }
        },
        dt=datetime.now(timezone.utc),
        positions=[],
        tracked={},
        current_positions={},
        eligible_active=[],
        account_equity=100_000.0,
        ca_cfg={
            "min_realloc_leg": 1.0,
            "min_trade_size": 1.0,
            "max_positions": 7,
            "single_pass_per_cycle": False,
        },
        user_id="u1",
        data_dir="/tmp",
        stale_quote_max_age=300.0,
        strength_jitter_max=0.0,
        et_date_iso="2020-01-01",
        cycle_risk_state=None,
        verbose=False,
        exit_context=MagicMock(),
        reg_score_bp=3,
        reg_cond_bp="neutral",
        entry_full_invest_flag=False,
        gross_exposure_pct=50.0,
        entry_wave_strong_signal_count=0,
        symbol_sector={},
        theme_map={},
    )


def test_post_sell_realloc_disabled_noop() -> None:
    _rows = [
        {
            "sym_u": "AAA",
            "strength_eff": 0.5,
            "composite_score": 0.3,
        }
    ]
    kw = _base_kw(_rows)
    kw["config"] = {
        "portfolio": {
            "signal_ranking": {"max_signals_per_loop": 10},
            "capital_allocator": {"post_sell_reallocation": {"enabled": False}},
        }
    }
    r = run_post_sell_reallocation(True, 2000.0, _rows, **kw)
    assert r == 2000.0


def test_post_sell_realloc_requires_sell() -> None:
    cfg = {
        "portfolio": {
            "signal_ranking": {"max_signals_per_loop": 10},
            "capital_allocator": {
                "post_sell_reallocation": {
                    "enabled": True,
                    "require_equity_sell": True,
                }
            },
        }
    }
    r = run_post_sell_reallocation(
        False,
        2000.0,
        [{"sym_u": "A", "strength_eff": 0.2, "composite_score": 0.1}],
        **(_base_kw(
            [{"sym_u": "A", "strength_eff": 0.2, "composite_score": 0.1}]
        ) | {"config": cfg}),
    )
    assert r == 2000.0


@patch(
    "src.portfolio.allocator.scaled_buying_power_for_lane",
    return_value=8888.0,
)
@patch("src.portfolio.allocator.execute_capital_allocator_pass", autospec=True)
def test_post_sell_realloc_skipped_when_single_pass_second_execute(
    mock_ex: MagicMock, _mock_eff: MagicMock
) -> None:
    rows = [{"sym_u": "AAA", "symbol": "AAA", "strength_eff": 0.9, "composite_score": 4.0}]
    broker = MagicMock()
    broker.get_buying_power = MagicMock(return_value=7777.0)
    kw = _base_kw(rows)
    kw["broker"] = broker
    kw["ca_cfg"] = {
        **kw["ca_cfg"],
        "single_pass_per_cycle": True,
    }
    out = run_post_sell_reallocation(True, 1000.0, rows, **kw)
    mock_ex.assert_not_called()
    assert out == pytest.approx(8888.0)


@patch("src.portfolio.allocator.execute_capital_allocator_pass", autospec=True)
def test_post_sell_realloc_invokes_preallocated_buys(mock_ex: MagicMock) -> None:
    rows = [
        {
            "sym_u": "AAA",
            "symbol": "AAA",
            "strength_eff": 0.9,
            "composite_score": 4.0,
        },
        {
            "sym_u": "BBB",
            "symbol": "BBB",
            "strength_eff": 0.1,
            "composite_score": 0.1,
        },
    ]
    broker = MagicMock()
    broker.get_buying_power = MagicMock(return_value=5000.0)
    kw = _base_kw(rows)
    kw["broker"] = broker
    run_post_sell_reallocation(True, 1000.0, rows, **kw)
    assert mock_ex.called
    cargs, ckwargs = mock_ex.call_args
    assert ckwargs.get("preallocated_equal_split_buys") is not None
    assert ckwargs.get("preallocated_equal_split_buys")  # non-empty
    _ca = ckwargs.get("ca_cfg")
    assert _ca is not None
    assert _ca.get("require_net_sell_gte_buy") is False
