"""Tests for :mod:`src.portfolio` (allocator / replacement / rebalance helpers)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.portfolio.allocator import (
    flush_ranked_trend_long_entry_queue,
    run_post_scan_capital_allocator,
)
from src.portfolio.rebalance import (
    rfc_effective_spread_pct,
    rfc_fallback_open_mid_from_bars,
    rfc_position_qty_floor_for_sell,
    rfc_reference_mid_for_quote,
)
from src.portfolio.replacement import preflight_replacement_gates_on_dispatch
from src.signal_ranking import (
    SIGNAL_RANKING_MODE_COMPOSITE,
    SIGNAL_RANKING_MODE_STRENGTH,
)


def test_preflight_none_when_not_at_replacement() -> None:
    r = preflight_replacement_gates_on_dispatch(
        port_replace=True,
        max_port_positions=10**9,  # unbounded: no "at cap" new-name
        n_eligible_active=5,
        sym_u="NVDA",
        current_position_keys={},
        tracked={},
        eligible_active=[],
        positions=[],
        get_bars=None,
        engine=MagicMock(),
        rep_sub={},
        decision_tl=None,
        notional_tl=100.0,
        strength_jitter_max=0.0,
        replacement_threshold=0.5,
        allow_equal_replacement=False,
        cycle_replacements_done=0,
    )
    assert r is None


def test_rfc_position_qty_floor_for_sell_uses_broker_pos() -> None:
    pos = [
        {"symbol": "AAPL", "qty": 10, "market_value": 2000.0},
    ]
    assert rfc_position_qty_floor_for_sell(1, "AAPL", pos) == 10


def test_rfc_effective_spread_uses_015_when_stale() -> None:
    q = MagicMock()
    q.is_stale = MagicMock(return_value=True)
    assert rfc_effective_spread_pct(q, stale_hint=True, stale_quote_max_age=1.0) == pytest.approx(0.15)


def test_flush_ranked_respects_alpha_select_top_k() -> None:
    calls: list[str] = []

    def _d(row: dict) -> bool:
        calls.append(str(row.get("sym_u", "")))
        return True

    _rows = [
        {"sym_u": "A", "strength_eff": 0.5, "composite_score": 0.0},
        {"sym_u": "B", "strength_eff": 0.9, "composite_score": 0.0},
    ]
    flush_ranked_trend_long_entry_queue(
        _rows,
        max_take=2,
        sector_etfs=frozenset(),
        ranking_mode=SIGNAL_RANKING_MODE_STRENGTH,
        log_entry_skip=lambda *a, **k: None,
        dt=datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
        symbol_for_skip="P",
        verbose=False,
        dispatch_row=_d,
        config={"alpha": {"select_top_k": 1}},
    )
    assert calls == ["B"]


def test_flush_ranked_trend_long_calls_dispatch_per_chosen() -> None:
    calls: list[str] = []

    def _d(row: dict) -> bool:
        calls.append(str(row.get("sym_u", "")))
        return True

    _rows = [
        {
            "sym_u": "A",
            "strength_eff": 0.9,
            "composite_score": 0.0,
        },
    ]
    flush_ranked_trend_long_entry_queue(
        _rows,
        max_take=1,
        sector_etfs=frozenset(),
        ranking_mode=SIGNAL_RANKING_MODE_STRENGTH,
        log_entry_skip=lambda *a, **k: None,
        dt=datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
        symbol_for_skip="P",
        verbose=False,
        dispatch_row=_d,
    )
    assert "A" in calls


def test_flush_ranked_winner_allocation_marks_top_row() -> None:
    calls: list[dict] = []

    def _d(row: dict) -> bool:
        calls.append(row)
        return True

    _rows = [
        {"sym_u": "A", "strength_eff": 0.5, "composite_score": 0.0},
        {"sym_u": "B", "strength_eff": 0.9, "composite_score": 0.0},
    ]
    flush_ranked_trend_long_entry_queue(
        _rows,
        max_take=2,
        sector_etfs=frozenset(),
        ranking_mode=SIGNAL_RANKING_MODE_STRENGTH,
        log_entry_skip=lambda *a, **k: None,
        dt=datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
        symbol_for_skip="P",
        verbose=False,
        dispatch_row=_d,
        winner_allocation_enabled=True,
        winner_top_n=1,
        winner_size_multiplier=1.5,
    )
    # Strength: higher strength_eff is better → B is rank 1.
    b_row = [r for r in calls if r.get("sym_u") == "B"][0]
    assert b_row.get("in_top_signals") is True
    assert b_row.get("winner_size_multiplier") == pytest.approx(1.5)
    a_row = [r for r in calls if r.get("sym_u") == "A"][0]
    assert a_row.get("in_top_signals") is None


def test_flush_ranked_retries_top_after_trim_when_first_dispatch_fails() -> None:
    calls: list[str] = []
    trims: list[str] = []

    def _trim(top: str) -> bool:
        trims.append(top)
        return True

    def _d(row: dict) -> bool:
        su = str(row.get("sym_u", ""))
        calls.append(su)
        if su == "SPY" and calls.count("SPY") == 1:
            return False
        return True

    flush_ranked_trend_long_entry_queue(
        [
            {"sym_u": "SPY", "strength_eff": 1.0, "composite_score": 0.0},
            {"sym_u": "IWM", "strength_eff": 0.5, "composite_score": 0.0},
        ],
        max_take=2,
        sector_etfs=frozenset(),
        ranking_mode=SIGNAL_RANKING_MODE_STRENGTH,
        log_entry_skip=lambda *a, **k: None,
        dt=datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
        symbol_for_skip="P",
        verbose=False,
        dispatch_row=_d,
        trim_weakest_for_blocked_top=_trim,
    )
    assert trims == ["SPY"]
    assert calls == ["SPY", "SPY", "IWM"]


def test_flush_ranked_skips_retry_when_trim_returns_false() -> None:
    calls: list[str] = []

    def _trim(_top: str) -> bool:
        return False

    def _d(row: dict) -> bool:
        calls.append(str(row.get("sym_u", "")))
        return False

    flush_ranked_trend_long_entry_queue(
        [
            {"sym_u": "SPY", "strength_eff": 1.0, "composite_score": 0.0},
            {"sym_u": "IWM", "strength_eff": 0.5, "composite_score": 0.0},
        ],
        max_take=2,
        sector_etfs=frozenset(),
        ranking_mode=SIGNAL_RANKING_MODE_STRENGTH,
        log_entry_skip=lambda *a, **k: None,
        dt=datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
        symbol_for_skip="P",
        verbose=False,
        dispatch_row=_d,
        trim_weakest_for_blocked_top=_trim,
    )
    assert calls == ["SPY", "IWM"]


def test_run_post_scan_capital_allocator_noop_on_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("INFO", logger="src.portfolio.allocator"):
        assert (
            run_post_scan_capital_allocator(
                [],
                broker=MagicMock(),
                engine=MagicMock(),
                config={},
                dt=datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
                positions=[],
                tracked={},
                current_positions={},
                eligible_active=[],
                account_equity=100.0,
                available_cash=25.0,
                ca_cfg={
                    "enabled": True,
                    "max_positions": 1,
                    "symbol_cap": 0.25,
                    "min_trade_size": 1,
                    "min_realloc_leg": 300.0,
                    "rotate_trim_fraction": 0.3,
                },
                user_id="u1",
                data_dir="/tmp",
                stale_quote_max_age=60.0,
                strength_jitter_max=0.0,
                et_date_iso=None,
                cycle_risk_state={},
                verbose=False,
                exit_context=None,
                reg_score_bp=None,
                reg_cond_bp=None,
                entry_full_invest_flag=False,
            )
            == 25.0
        )
    assert "ALLOCATOR_PASS_START queued=0" in caplog.text
    assert "ALLOCATOR_PASS_SKIP reason=no_candidates queued=0" in caplog.text


def test_run_post_scan_capital_allocator_logs_nonempty_dedupe_skip(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("INFO", logger="src.portfolio.allocator"):
        assert (
            run_post_scan_capital_allocator(
                [{"score": 1.0}],
                broker=MagicMock(),
                engine=MagicMock(),
                config={},
                dt=datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
                positions=[],
                tracked={},
                current_positions={},
                eligible_active=[],
                account_equity=100.0,
                available_cash=25.0,
                ca_cfg={
                    "enabled": True,
                    "max_positions": 1,
                    "symbol_cap": 0.25,
                    "min_trade_size": 1,
                    "min_realloc_leg": 300.0,
                    "rotate_trim_fraction": 0.3,
                },
                user_id="u1",
                data_dir="/tmp",
                stale_quote_max_age=60.0,
                strength_jitter_max=0.0,
                et_date_iso=None,
                cycle_risk_state={},
                verbose=False,
                exit_context=None,
                reg_score_bp=None,
                reg_cond_bp=None,
                entry_full_invest_flag=False,
            )
            == 25.0
        )
    assert "ALLOCATOR_PASS_START queued=1" in caplog.text
    assert "ALLOCATOR_PASS_SKIP reason=dedupe_removed_all queued=1" in caplog.text
    assert "ALLOCATOR_INPUT" not in caplog.text


def test_run_post_scan_capital_allocator_returns_cash_on_execute_error() -> None:
    """Plan/execute failure keeps prior *available_cash* (fallback)."""
    from unittest.mock import patch

    with patch(
        "src.portfolio.allocator.execute_capital_allocator_pass",
        side_effect=RuntimeError("execute boom"),
    ):
        out = run_post_scan_capital_allocator(
            [{"sym_u": "A", "symbol": "A", "strength_eff": 0.5, "composite_score": 1.0}],
            broker=MagicMock(),
            engine=MagicMock(),
            config={},
            dt=datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100.0,
            available_cash=30.0,
            ca_cfg={
                "enabled": True,
                "max_positions": 1,
                "symbol_cap": 0.25,
                "min_trade_size": 1,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
            },
            user_id="u1",
            data_dir="/tmp",
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso=None,
            cycle_risk_state={},
            verbose=False,
            exit_context=None,
            reg_score_bp=None,
            reg_cond_bp=None,
            entry_full_invest_flag=False,
        )
    assert out == 30.0


def test_run_post_scan_capital_allocator_truncates_to_top_n_by_composite() -> None:
    """``max_signals_per_loop`` applies after dedupe before :func:`execute_capital_allocator_pass`."""
    from unittest.mock import patch

    _captured: list[list] = []

    def _cap_sig(*, signals: list, **kw: object) -> None:  # type: ignore[no-untyped-def]
        _captured.append(list(signals))

    with patch("src.portfolio.allocator.execute_capital_allocator_pass", side_effect=_cap_sig):
        run_post_scan_capital_allocator(
            [
                {"sym_u": "A", "composite_score": 1.0, "strength_eff": 0.3},
                {"sym_u": "B", "composite_score": 9.0, "strength_eff": 0.9},
                {"sym_u": "C", "composite_score": 2.0, "strength_eff": 0.2},
            ],
            broker=MagicMock(),
            engine=MagicMock(),
            config={
                "portfolio": {
                    "signal_ranking": {
                        "max_signals_per_loop": 1,
                        "ranking_mode": SIGNAL_RANKING_MODE_COMPOSITE,
                    }
                }
            },
            dt=datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100.0,
            available_cash=30.0,
            ca_cfg={
                "enabled": True,
                "max_positions": 1,
                "symbol_cap": 0.25,
                "min_trade_size": 1,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
            },
            user_id="u1",
            data_dir="/tmp",
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso=None,
            cycle_risk_state={},
            verbose=False,
            exit_context=None,
            reg_score_bp=None,
            reg_cond_bp=None,
            entry_full_invest_flag=False,
        )
    assert len(_captured) == 1
    assert [r.get("sym_u") for r in _captured[0]] == ["B"]


def test_run_post_scan_passes_locked_buying_power_to_allocate() -> None:
    """Live loop passes a snapshot BP for planning; scan-loop *available_cash* may differ."""
    _cash_kw: list[float] = []

    def _capture_execute(**kw: object) -> None:
        _cash_kw.append(float(kw["cash"]))  # type: ignore[arg-type]

    broker = MagicMock()
    broker.get_buying_power = MagicMock(return_value=400.0)
    with patch(
        "src.portfolio.allocator.execute_capital_allocator_pass",
        side_effect=_capture_execute,
    ):
        with patch(
            "src.portfolio.allocator.scaled_buying_power_for_lane",
            return_value=400.0,
        ):
            run_post_scan_capital_allocator(
                [{"sym_u": "Z", "composite_score": 2.0, "strength_eff": 0.6}],
                broker=broker,
                engine=MagicMock(),
                config={},
                dt=datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
                positions=[],
                tracked={},
                current_positions={},
                eligible_active=[],
                account_equity=100_000.0,
                available_cash=50.0,
                locked_buying_power=2500.0,
                ca_cfg={
                    "enabled": True,
                    "max_positions": 5,
                    "symbol_cap": 0.25,
                    "min_trade_size": 100,
                    "min_realloc_leg": 300.0,
                    "rotate_trim_fraction": 0.3,
                },
                user_id="u1",
                data_dir="/tmp",
                stale_quote_max_age=60.0,
                strength_jitter_max=0.0,
                et_date_iso=None,
                cycle_risk_state={},
                verbose=False,
                exit_context=None,
                reg_score_bp=None,
                reg_cond_bp=None,
                entry_full_invest_flag=False,
            )
    assert _cash_kw == [2500.0]
