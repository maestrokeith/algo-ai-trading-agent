"""Tests for :mod:`src.exposure`."""

from __future__ import annotations

import pytest

from src.exposure import (
    ETF_SYMBOLS,
    INVERSE_ETFS,
    SYMBOL_SECTOR,
    THEME_MAP,
    ExposureSnapshot,
    compute_exposures,
    gross_notional_dollars_for_position,
    signed_options_delta_adjusted_dollars_for_position,
    stock_notional_dollars_for_position,
)

# Valid OCC US equity option symbol (6-digit date, C| P, 8-digit strike mils)
_OCC_OPT = "AAPL250117C00150000"
_OCC_OPT2 = "AAPL250220P00140000"


def test_compute_exposures_zero_equity() -> None:
    out = compute_exposures(0.0, [{"symbol": "SPY", "market_value": 1000.0}], {})
    assert out == ExposureSnapshot(0.0, 0.0, {}, {}, 0.0, 0.0)


def test_compute_exposures_empty_positions() -> None:
    out = compute_exposures(100_000.0, [], {})
    assert out.gross_pct == pytest.approx(0.0)
    assert out.net_pct == pytest.approx(0.0)
    assert out.etf_pct == pytest.approx(0.0)
    assert out.inverse_etf_pct == pytest.approx(0.0)
    assert out.sector_pct == {}
    assert out.theme_pct == {}


def test_compute_exposures_long_spy_qqq_sectors_themes_etfs() -> None:
    eq = 100_000.0
    positions = [
        {"symbol": "spy", "market_value": 30_000.0},
        {"symbol": "QQQ", "market_value": 20_000.0},
    ]
    sectors = {"SPY": "idx", "QQQ": "idx"}
    out = compute_exposures(eq, positions, sectors)
    assert out.gross_pct == pytest.approx(50.0)
    assert out.net_pct == pytest.approx(50.0)
    assert out.etf_pct == pytest.approx(50.0)
    assert out.inverse_etf_pct == pytest.approx(0.0)
    assert out.sector_pct["idx"] == pytest.approx(50.0)
    assert out.theme_pct["broad_index"] == pytest.approx(30.0)
    assert out.theme_pct["ai_growth"] == pytest.approx(20.0)


def test_compute_exposures_inverse_counts_etf_and_inverse() -> None:
    eq = 100_000.0
    out = compute_exposures(
        eq,
        [{"symbol": "SQQQ", "market_value": 10_000.0}],
        {},
    )
    assert out.gross_pct == pytest.approx(10.0)
    assert out.etf_pct == pytest.approx(10.0)
    assert out.inverse_etf_pct == pytest.approx(10.0)
    assert out.theme_pct["hedge"] == pytest.approx(10.0)


def test_compute_exposures_short_reduces_net_not_gross() -> None:
    eq = 100_000.0
    out = compute_exposures(
        eq,
        [{"symbol": "AAPL", "market_value": 5000.0, "side": "short"}],
        {"AAPL": "tech"},
    )
    assert out.gross_pct == pytest.approx(5.0)
    assert out.net_pct == pytest.approx(-5.0)
    assert out.sector_pct["tech"] == pytest.approx(5.0)


def test_constants_cover_known_etfs() -> None:
    assert "SPY" in ETF_SYMBOLS and "SQQQ" in ETF_SYMBOLS
    assert INVERSE_ETFS <= ETF_SYMBOLS
    assert THEME_MAP["NVDA"] == "ai_growth"


def test_symbol_sector_constant_buckets_sector_pct() -> None:
    eq = 100_000.0
    out = compute_exposures(
        eq,
        [{"symbol": "NVDA", "market_value": 10_000.0}],
        SYMBOL_SECTOR,
    )
    assert out.sector_pct["technology"] == pytest.approx(10.0)
    assert "unknown" not in out.sector_pct


def test_compute_exposures_default_sector_for_unmapped() -> None:
    eq = 100_000.0
    out = compute_exposures(
        eq,
        [{"symbol": "ZZZ", "market_value": 5_000.0}],
        {"AAPL": "technology"},
        default_sector="other",
    )
    assert out.sector_pct["other"] == pytest.approx(5.0)
    assert "unknown" not in out.sector_pct


def test_gross_uses_options_delta_notional() -> None:
    eq = 100_000.0
    position = {
        "symbol": _OCC_OPT,
        "market_value": 4_000.0,
        "side": "long",
        "options_delta_notional": 2_000.0,
    }
    out = compute_exposures(eq, [position], {})
    assert out.gross_pct == pytest.approx(2.0)
    assert gross_notional_dollars_for_position(position, abs_market_value=4_000.0) == pytest.approx(2_000.0)


def test_gross_options_delta_from_greeks() -> None:
    eq = 100_000.0
    # 0.5 * 1 contract * 100 * 200 underlying = 10,000
    position = {
        "symbol": _OCC_OPT,
        "market_value": 1_000.0,
        "side": "long",
        "qty": 1,
        "delta": 0.5,
        "underlying_last": 200.0,
    }
    out = compute_exposures(eq, [position], {})
    assert out.gross_pct == pytest.approx(10.0)
    assert gross_notional_dollars_for_position(position, abs_market_value=1_000.0) == pytest.approx(10_000.0)


def test_gross_options_falls_back_to_premium() -> None:
    eq = 100_000.0
    position = {"symbol": _OCC_OPT, "market_value": 1_200.0, "side": "long"}
    out = compute_exposures(eq, [position], {})
    assert out.gross_pct == pytest.approx(1.2)
    assert gross_notional_dollars_for_position(position, abs_market_value=1_200.0) == pytest.approx(1_200.0)


def test_gross_mixed_equity_and_option_deltas() -> None:
    eq = 100_000.0
    positions = [
        {"symbol": "SPY", "market_value": 30_000.0, "side": "long"},
        {
            "symbol": _OCC_OPT,
            "market_value": 1_000.0,
            "side": "long",
            "options_delta_notional": 5_000.0,
        },
    ]
    out = compute_exposures(
        eq,
        positions,
        {"SPY": "idx", _OCC_OPT: "other"},
    )
    assert out.gross_pct == pytest.approx(35.0)


def test_gross_is_stocks_plus_abs_net_option_delta() -> None:
    """gross = stock MV + abs(sum signed option delta); offsetting legs reduce option contribution."""
    eq = 100_000.0
    positions = [
        {"symbol": "SPY", "market_value": 30_000.0, "side": "long"},
        {
            "symbol": _OCC_OPT,
            "market_value": 1_000.0,
            "side": "long",
            "options_delta_notional": 5_000.0,
        },
        {
            "symbol": _OCC_OPT2,
            "market_value": 900.0,
            "side": "short",
            "options_delta_notional": 5_000.0,
        },
    ]
    out = compute_exposures(eq, positions, {})
    # Net option delta: +5000 - 5000 = 0 → gross is stock leg only
    assert out.gross_pct == pytest.approx(30.0)


def test_options_delta_adjusted_field_signed_used_directly() -> None:
    eq = 100_000.0
    position = {
        "symbol": _OCC_OPT,
        "market_value": 4_000.0,
        "side": "long",
        "options_delta_notional": 9_000.0,
        "options_delta_adjusted": -2_500.0,
    }
    out = compute_exposures(eq, [position], {})
    assert out.gross_pct == pytest.approx(2.5)
    assert signed_options_delta_adjusted_dollars_for_position(
        position, abs_market_value=4_000.0
    ) == pytest.approx(-2_500.0)
    assert stock_notional_dollars_for_position(position, abs_market_value=4_000.0) == pytest.approx(
        0.0
    )
