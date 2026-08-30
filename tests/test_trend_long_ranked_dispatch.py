"""Tests for trend_long_ranked_dispatch."""

from __future__ import annotations

from typing import Any

from unittest.mock import MagicMock, patch

import pytest

from src.trend_long_ranked_dispatch import dispatch_trend_long_after_buying_power


def test_dispatch_at_cap_new_symbol_runs_replacement_eval_when_port_replace_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At cap, replacement flag off: still call evaluate_portfolio_replacement; skip is from gates, not config."""
    monkeypatch.setattr("src.trend_long_ranked_dispatch.load_tracked", lambda *a, **k: {})
    monkeypatch.setattr("src.trend_long_ranked_dispatch.portfolio_brain_enabled", lambda _c: False)

    skips: list[str] = []

    def _capture(dt: Any, sym: Any, msg: Any, **_k: Any) -> None:
        skips.append(str(msg))

    broker = MagicMock()
    broker.get_positions.return_value = [{"symbol": "QQQ", "qty": 1}]
    regime_result = MagicMock(score=4, condition="bullish")
    engine = MagicMock()

    decision = MagicMock()
    decision.entry_signal = None
    decision.allowed = True
    decision.order_request = MagicMock()
    decision.position_sizing = MagicMock(notional=100.0, shares=1)

    row_tl = {
        "symbol": "NVDA",
        "sym_u": "NVDA",
        "decision": decision,
        "df": MagicMock(empty=False),
        "quote": MagicMock(spread_pct=0.05),
        "notional": 100.0,
        "trend_long_ok": True,
        "entry_regime_score": 4,
    }

    with patch(
        "src.trend_long_ranked_dispatch.evaluate_portfolio_replacement_for_dispatch",
        return_value=(None, "rotation not justified (test)"),
    ) as pev:
        with patch("src.trend_long_ranked_dispatch.route_to_options_executor") as ro:
            with patch("src.trend_long_ranked_dispatch.route_to_stock_executor") as rs:
                dispatch_trend_long_after_buying_power(
                    row_tl,
                    dt=MagicMock(strftime=lambda *a, **k: ""),
                    broker=broker,
                    config={"options": {"enabled": False}},
                    engine=engine,
                    verbose=False,
                    account_equity=100_000.0,
                    positions=[],
                    regime_result=regime_result,
                    bearish_regime=False,
                    pct_above_50d_universe=None,
                    allowed_symbols_for_stock_orders=None,
                    max_port_positions=1,
                    port_replace=False,
                    port_allow_add=False,
                    eligible_active=["QQQ"],
                    strength_jitter_max=0.0,
                    rep_sub={},
                    replace_if_weakest_older_than=None,
                    current_positions={"QQQ": {}},
                    user_id="test",
                    data_dir="/tmp",
                    option_chain_for_underlying=lambda *a, **k: [],
                    log_entry_skip=_capture,
                )
    ro.assert_not_called()
    rs.assert_not_called()
    pev.assert_called_once()
    assert not any("enable portfolio.enable_replacement" in m for m in skips), skips
    assert any("rotation not justified" in m for m in skips), skips


@pytest.fixture()
def minimal_row_tl() -> dict:
    decision = MagicMock()
    decision.entry_signal = None
    decision.allowed = True
    decision.order_request = MagicMock()
    decision.position_sizing = MagicMock()
    decision.position_sizing.notional = 100.0
    decision.position_sizing.shares = 1
    return {
        "symbol": "AAPL",
        "sym_u": "AAPL",
        "decision": decision,
        "df": MagicMock(),
        "quote": MagicMock(),
        "notional": 100.0,
        "trend_long_ok": True,
        "entry_regime_score": None,
    }


def test_dispatch_skips_when_already_holding_without_allow_add(
    monkeypatch: pytest.MonkeyPatch, minimal_row_tl: dict
) -> None:
    monkeypatch.setattr("src.trend_long_ranked_dispatch.load_tracked", lambda *a, **k: {})

    broker = MagicMock()
    broker.get_positions.return_value = []
    positions_list: list = []
    regime_result = MagicMock(score=4, condition="bullish")

    with patch("src.trend_long_ranked_dispatch.route_to_options_executor") as ro:
        with patch("src.trend_long_ranked_dispatch.route_to_stock_executor") as rs:
            dispatch_trend_long_after_buying_power(
                minimal_row_tl,
                dt=MagicMock(strftime=lambda *a, **k: ""),
                broker=broker,
                config={"options": {"enabled": False}},
                engine=MagicMock(),
                verbose=False,
                account_equity=100_000.0,
                positions=positions_list,
                regime_result=regime_result,
                bearish_regime=False,
                pct_above_50d_universe=None,
                allowed_symbols_for_stock_orders=None,
                max_port_positions=10,
                port_replace=False,
                port_allow_add=False,
                eligible_active=[],
                strength_jitter_max=0.0,
                rep_sub={},
                replace_if_weakest_older_than=None,
                current_positions={"AAPL": {"notional": 1000.0}},
                user_id="test",
                data_dir="/tmp",
                option_chain_for_underlying=lambda *a, **k: [],
                log_entry_skip=lambda *a, **k: None,
            )
    ro.assert_not_called()
    rs.assert_not_called()


def test_dispatch_allows_strong_incremental_add_when_already_holding_without_allow_add(
    monkeypatch: pytest.MonkeyPatch,
    minimal_row_tl: dict,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setattr("src.trend_long_ranked_dispatch.load_tracked", lambda *a, **k: {})
    monkeypatch.setattr("src.trend_long_ranked_dispatch.portfolio_brain_enabled", lambda _c: False)

    minimal_row_tl["strength_eff"] = 0.91
    minimal_row_tl["notional"] = 5_000.0
    minimal_row_tl["decision"].entry_signal = MagicMock(strength=0.91, metadata={})
    broker = MagicMock()
    broker.get_positions.return_value = []
    positions_list: list = []
    regime_result = MagicMock(score=4, condition="bullish")
    engine = MagicMock()
    engine.strategy.effective_max_atr_pct_for_entry.return_value = 10.0
    engine.execution.build_order.return_value = None

    with patch("src.trend_long_ranked_dispatch.route_to_options_executor") as ro:
        with patch("src.trend_long_ranked_dispatch.route_to_stock_executor") as rs:
            dispatch_trend_long_after_buying_power(
                minimal_row_tl,
                dt=MagicMock(strftime=lambda *a, **k: ""),
                broker=broker,
                config={
                    "options": {"enabled": False},
                    "portfolio": {
                        "add_on": {
                            "enabled": True,
                            "min_signal_strength": 0.85,
                            "incremental_add_pct": 0.01,
                        }
                    },
                },
                engine=engine,
                verbose=False,
                account_equity=100_000.0,
                positions=positions_list,
                regime_result=regime_result,
                bearish_regime=False,
                pct_above_50d_universe=None,
                allowed_symbols_for_stock_orders=None,
                max_port_positions=10,
                port_replace=False,
                port_allow_add=False,
                eligible_active=["AAPL"],
                strength_jitter_max=0.0,
                rep_sub={},
                replace_if_weakest_older_than=None,
                current_positions={"AAPL": {"notional": 1000.0}},
                user_id="test",
                data_dir="/tmp",
                option_chain_for_underlying=lambda *a, **k: [],
                log_entry_skip=lambda *a, **k: None,
            )
    ro.assert_not_called()
    rs.assert_called_once()
    assert "incremental add allowed" in capsys.readouterr().out


def test_dispatch_skips_when_portfolio_brain_blocks_symbol(
    monkeypatch: pytest.MonkeyPatch, minimal_row_tl: dict
) -> None:
    monkeypatch.setattr("src.trend_long_ranked_dispatch.load_tracked", lambda *a, **k: {})

    heavy = [{"symbol": "SPY", "qty": 1, "market_value": 50_000.0}]
    broker = MagicMock()
    broker.get_positions.return_value = list(heavy)
    positions_list: list = []
    regime_result = MagicMock(score=4, condition="bullish")
    cfg = {
        "options": {"enabled": False},
        "portfolio": {"portfolio_brain": {"enabled": True}},
        "risk": {"max_bucket_allocation_pct": 0.30, "max_symbol_allocation_pct": 0.50},
        "risk_buckets": {"mega_cap_beta": ["SPY", "AAPL"]},
    }

    skips: list[tuple[Any, ...]] = []

    def _capture_skip(*a, **k) -> None:
        skips.append(a + tuple(k.items()))

    with patch("src.trend_long_ranked_dispatch.route_to_options_executor") as ro:
        with patch("src.trend_long_ranked_dispatch.route_to_stock_executor") as rs:
            dispatch_trend_long_after_buying_power(
                minimal_row_tl,
                dt=MagicMock(strftime=lambda *a, **k: ""),
                broker=broker,
                config=cfg,
                engine=MagicMock(),
                verbose=False,
                account_equity=100_000.0,
                positions=positions_list,
                regime_result=regime_result,
                bearish_regime=False,
                pct_above_50d_universe=None,
                allowed_symbols_for_stock_orders=None,
                max_port_positions=10,
                port_replace=False,
                port_allow_add=True,
                eligible_active=[],
                strength_jitter_max=0.0,
                rep_sub={},
                replace_if_weakest_older_than=None,
                current_positions={},
                user_id="test",
                data_dir="/tmp",
                option_chain_for_underlying=lambda *a, **k: [],
                log_entry_skip=_capture_skip,
            )
    ro.assert_not_called()
    rs.assert_not_called()
    assert skips, "expected portfolio_brain skip log"
    assert any("portfolio_brain" in str(s) for s in skips[0])


def test_dispatch_not_skipped_when_holding_with_allow_add(
    monkeypatch: pytest.MonkeyPatch, minimal_row_tl: dict
) -> None:
    """Held name + allow_add must reach stock routing (not early return); options off → no options route."""
    monkeypatch.setattr("src.trend_long_ranked_dispatch.load_tracked", lambda *a, **k: {})

    broker = MagicMock()
    broker.get_positions.return_value = []
    positions_list: list = []
    regime_result = MagicMock(score=4, condition="bullish")
    engine = MagicMock()
    engine.strategy.effective_max_atr_pct_for_entry.return_value = 10.0
    engine.execution.build_order.return_value = None

    with patch("src.trend_long_ranked_dispatch.route_to_options_executor") as ro:
        with patch("src.trend_long_ranked_dispatch.route_to_stock_executor") as rs:
            dispatch_trend_long_after_buying_power(
                minimal_row_tl,
                dt=MagicMock(strftime=lambda *a, **k: ""),
                broker=broker,
                config={"options": {"enabled": False}},
                engine=engine,
                verbose=False,
                account_equity=100_000.0,
                positions=positions_list,
                regime_result=regime_result,
                bearish_regime=False,
                pct_above_50d_universe=None,
                allowed_symbols_for_stock_orders=None,
                max_port_positions=10,
                port_replace=False,
                port_allow_add=True,
                eligible_active=["AAPL"],
                strength_jitter_max=0.0,
                rep_sub={},
                replace_if_weakest_older_than=None,
                current_positions={"AAPL": {"notional": 1000.0}},
                user_id="test",
                data_dir="/tmp",
                option_chain_for_underlying=lambda *a, **k: [],
                log_entry_skip=lambda *a, **k: None,
            )
    ro.assert_not_called()
    rs.assert_called_once()


def test_replacement_skips_tiny_weakest_market_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """At replacement sell, block rotating out a sub-floor market-value lot (1-share style leftovers)."""
    monkeypatch.setattr(
        "src.trend_long_ranked_dispatch.load_tracked",
        lambda *a, **k: {
            "MSFT": {
                "qty": 10,
                "entry_time": "2020-01-01T00:00:00+00:00",
                "signal_strength": 0.1,
            },
        },
    )
    monkeypatch.setattr("src.trend_long_ranked_dispatch.portfolio_brain_enabled", lambda _c: False)

    skips: list[tuple[Any, ...]] = []

    def _capture_skip(*a, **k) -> None:
        skips.append(a + tuple(k.items()))

    positions_list = [{"symbol": "MSFT", "qty": 10, "market_value": 50.0}]
    broker = MagicMock()
    broker.get_positions.return_value = positions_list
    broker.get_latest_quote.return_value = MagicMock(
        reference_mid=lambda _fb: 100.0,
        spread_pct=0.01,
        bid=99.0,
        ask=101.0,
        skip_spread_check=False,
    )
    regime_result = MagicMock(score=4, condition="bullish")
    engine = MagicMock()
    engine.strategy.effective_max_atr_pct_for_entry.return_value = 10.0
    engine.execution.build_order.return_value = MagicMock()

    decision = MagicMock()
    decision.allowed = True
    decision.order_request = MagicMock()
    decision.position_sizing = MagicMock(notional=5000.0, shares=10)
    decision.entry_signal = MagicMock(strength=2.0, stop_pct=2.0, metadata={})

    row_tl = {
        "symbol": "NVDA",
        "sym_u": "NVDA",
        "decision": decision,
        "df": MagicMock(empty=False),
        "quote": MagicMock(spread_pct=0.01),
        "notional": 5000.0,
        "trend_long_ok": True,
        "entry_regime_score": 4,
    }

    def _call_execute(_sig: Any, execute_stock: Any) -> None:
        execute_stock()

    with patch(
        "src.trend_long_ranked_dispatch.bucket_allocation_allows",
        return_value=(True, None),
    ):
        with patch(
            "src.trend_long_ranked_dispatch.evaluate_strength_based_portfolio_swap",
            return_value=(["MSFT"], None),
        ):
            with patch(
                "src.trend_long_ranked_dispatch.route_to_options_executor"
            ) as ro:
                with patch(
                    "src.trend_long_ranked_dispatch.route_to_stock_executor",
                    side_effect=_call_execute,
                ) as rs:
                    dispatch_trend_long_after_buying_power(
                        row_tl,
                        dt=MagicMock(strftime=lambda *a, **k: ""),
                        broker=broker,
                        config={"options": {"enabled": False}},
                        engine=engine,
                        verbose=False,
                        account_equity=100_000.0,
                        positions=positions_list,
                        regime_result=regime_result,
                        bearish_regime=False,
                        pct_above_50d_universe=None,
                        allowed_symbols_for_stock_orders=None,
                        max_port_positions=2,
                        port_replace=True,
                        port_allow_add=False,
                        eligible_active=["MSFT", "NVDA"],
                        strength_jitter_max=0.0,
                        rep_sub={"min_hold_minutes": 0, "min_market_value_to_replace_usd": 750},
                        replace_if_weakest_older_than=None,
                        max_position_age_bars=None,
                        current_positions={},
                        user_id="test",
                        data_dir="/tmp",
                        option_chain_for_underlying=lambda *a, **k: [],
                        log_entry_skip=_capture_skip,
                        replacement_threshold=0.25,
                        allow_equal_replacement=False,
                    )
    ro.assert_not_called()
    rs.assert_called_once()
    assert any("tiny position" in str(s) for s in skips), skips
    broker.submit_order.assert_not_called()


def test_dispatch_partial_replacement_trims_weakest_and_caps_incoming_buy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rotate_partial_replacement: one sell leg (tranche), update tracker, buy min(planned, proceeds)."""
    monkeypatch.setattr(
        "src.trend_long_ranked_dispatch.load_tracked",
        lambda *a, **k: {
            "MSFT": {
                "qty": 10,
                "entry_time": "2020-01-01T00:00:00+00:00",
                "signal_strength": 0.1,
            },
        },
    )
    monkeypatch.setattr("src.trend_long_ranked_dispatch.portfolio_brain_enabled", lambda _c: False)
    removed: list[str] = []
    monkeypatch.setattr(
        "src.trend_long_ranked_dispatch.remove_tracked",
        lambda s, **k: removed.append(str(s)),
    )
    updated: list[tuple[str, int]] = []
    monkeypatch.setattr(
        "src.trend_long_ranked_dispatch.update_tracked",
        lambda sym, **k: updated.append(
            (str(sym).upper(), int(k.get("qty") or 0)),
        ),
    )
    adds: list[tuple[str, int]] = []
    monkeypatch.setattr(
        "src.trend_long_ranked_dispatch.add_tracked",
        lambda sym, qty, *a, **k: adds.append((str(sym).upper(), int(qty))),
    )

    positions_list = [
        {"symbol": "MSFT", "qty": 10, "market_value": 10_000.0},
        {"symbol": "QQQ", "qty": 1, "market_value": 500.0},
    ]
    broker = MagicMock()
    broker.get_positions.return_value = positions_list
    broker.get_latest_quote.return_value = MagicMock(
        reference_mid=lambda _fb: 100.0,
        spread_pct=0.01,
        bid=99.0,
        ask=101.0,
        skip_spread_check=False,
    )
    broker.submit_order.return_value = MagicMock(id="ord1")
    regime_result = MagicMock(score=4, condition="bullish")
    engine = MagicMock()
    engine.strategy.effective_max_atr_pct_for_entry.return_value = 10.0
    buy_req = MagicMock(quantity=3, id="buy1")
    engine.execution.build_order_for_entry.return_value = buy_req
    engine.execution.build_order.return_value = MagicMock()

    decision = MagicMock()
    decision.allowed = True
    decision.order_request = MagicMock()
    decision.position_sizing = MagicMock(notional=5000.0, shares=10)
    decision.entry_signal = MagicMock(strength=2.0, stop_pct=2.0, metadata={})

    row_tl = {
        "symbol": "NVDA",
        "sym_u": "NVDA",
        "decision": decision,
        "df": MagicMock(empty=False),
        "quote": MagicMock(
            spread_pct=0.01,
            bid=99.0,
            ask=101.0,
            reference_mid=lambda _p: 100.0,
            skip_spread_check=False,
        ),
        "notional": 5000.0,
        "trend_long_ok": True,
        "entry_regime_score": 4,
    }

    def _call_execute(_sig: Any, execute_stock: Any) -> None:
        execute_stock()

    with patch(
        "src.trend_long_ranked_dispatch.bucket_allocation_allows",
        return_value=(True, None),
    ):
        with patch(
            "src.trend_long_ranked_dispatch.evaluate_strength_based_portfolio_swap",
            return_value=(["MSFT"], None),
        ):
            with patch(
                "src.trend_long_ranked_dispatch.route_to_options_executor"
            ) as ro:
                with patch(
                    "src.trend_long_ranked_dispatch.route_to_stock_executor",
                    side_effect=_call_execute,
                ):
                    dispatch_trend_long_after_buying_power(
                        row_tl,
                        dt=MagicMock(strftime=lambda *a, **k: ""),
                        broker=broker,
                        config={"options": {"enabled": False}},
                        engine=engine,
                        verbose=False,
                        account_equity=100_000.0,
                        positions=positions_list,
                        regime_result=regime_result,
                        bearish_regime=False,
                        pct_above_50d_universe=None,
                        allowed_symbols_for_stock_orders=None,
                        max_port_positions=2,
                        port_replace=True,
                        port_allow_add=False,
                        eligible_active=["MSFT", "QQQ"],
                        strength_jitter_max=0.0,
                        rep_sub={
                            "min_hold_minutes": 0,
                            "min_market_value_to_replace_usd": 750,
                            "rotate_sell_tranche_fraction": 0.3,
                            "rotate_partial_replacement": True,
                        },
                        replace_if_weakest_older_than=None,
                        max_position_age_bars=None,
                        current_positions={},
                        user_id="test",
                        data_dir="/tmp",
                        option_chain_for_underlying=lambda *a, **k: [],
                        log_entry_skip=lambda *a, **k: None,
                        replacement_threshold=0.25,
                        allow_equal_replacement=False,
                    )
    ro.assert_not_called()
    so_calls = broker.submit_order.call_args_list
    assert len(so_calls) == 2
    # First submit = sell MSFT; second = buy NVDA (partial buy request)
    assert not removed
    assert ("MSFT", 7) in updated
    assert adds == [("NVDA", 3)]
    bfe = engine.execution.build_order_for_entry
    bfe.assert_called_once()
    _a, bfe_kw = bfe.call_args
    assert bfe_kw.get("notional") == pytest.approx(300.0)
    sell_built = engine.execution.build_order
    s_args, s_kw = sell_built.call_args
    assert s_args[0] == "MSFT" and s_args[1] == "sell" and s_args[2] == 3
