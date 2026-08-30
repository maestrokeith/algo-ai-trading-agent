from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytz

import src.dynamic_universe as du
from src.strategies.exits.context import LiveExitContext
from src.strategies.exits import stock_exit
from src.strategy import ExitReason, ExitSignal


def _bars() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [95.0, 100.0],
            "high": [101.0, 106.0],
            "low": [94.0, 99.0],
            "close": [100.0, 105.0],
            "volume": [1_000_000, 4_000_000],
        }
    )


def _ctx(tmp_path: Path, *, symbol: str, dynamic_symbols: set[str]) -> tuple[LiveExitContext, MagicMock]:
    submit_order = MagicMock()

    class Quote:
        bid = 104.95
        ask = 105.05
        spread_pct = 0.1
        skip_spread_check = False

        @staticmethod
        def reference_mid(_fallback: float) -> float:
            return 105.0

    broker = SimpleNamespace(
        get_latest_quote=MagicMock(return_value=Quote()),
        get_bars=MagicMock(return_value=_bars()),
        available_position_qty=MagicMock(return_value=(10.0, 0.0, 10.0)),
        get_snapshot=MagicMock(
            return_value={
                "day_gain_pct": 8.0,
                "volume": 4_000_000,
                "bid": 104.95,
                "ask": 105.05,
            }
        ),
        get_avg_volume=MagicMock(return_value=1_000_000),
        submit_order=submit_order,
    )

    strategy = SimpleNamespace(
        ma_fast=10,
        ma_slow=50,
        min_hold_minutes=60.0,
        no_trim_before_min_hold=True,
        trim_deferred_for_min_hold=MagicMock(return_value=False),
        trim_on_overweight=False,
        time_based_trim_enabled=False,
        smart_trailing_enabled=False,
        entry_eval_components_for_log=MagicMock(return_value=(True, True, True, True)),
    )
    execution = SimpleNamespace(
        build_order=MagicMock(side_effect=lambda _sym, _side, qty, *_args, **_kw: SimpleNamespace(quantity=qty)),
        partial_exit_sell_quantity=MagicMock(return_value=0),
    )
    engine = SimpleNamespace(
        strategy=strategy,
        execution=execution,
        dynamic_symbols=dynamic_symbols,
        check_exit=MagicMock(),
        record_stop_loss=MagicMock(),
        record_profit_exit=MagicMock(),
        record_kill_switch_exit=MagicMock(),
    )
    now = pytz.timezone("America/New_York").localize(datetime(2026, 5, 28, 10, 30))
    ctx = LiveExitContext(
        user_id="live_bot",
        data_dir=tmp_path,
        now=now,
        verbose=False,
        broker=broker,
        engine=engine,  # type: ignore[arg-type]
        config={},
        account_equity=25_000.0,
        symbols=[symbol],
        news_enabled=False,
        news_pipeline=None,
        news_rules=None,
    )
    ctx.note_daily_risk_order = MagicMock()
    ctx.log_sell_event = MagicMock()
    ctx.record_engine_after_sell = MagicMock()
    return ctx, submit_order


def _tracked(symbol: str) -> dict[str, dict[str, object]]:
    now = pytz.timezone("America/New_York").localize(datetime(2026, 5, 28, 10, 30))
    return {
        symbol: {
            "qty": 10,
            "side": "long",
            "entry_price": 100.0,
            "entry_time": (now - timedelta(minutes=90)).isoformat(),
            "partial_taken": False,
        }
    }


def _tracked_aggressive(
    symbol: str,
    *,
    entry_price: float,
    minutes_ago: float,
    qty: float = 10.5,
) -> dict[str, dict[str, object]]:
    now = pytz.timezone("America/New_York").localize(datetime(2026, 5, 28, 10, 30))
    return {
        symbol: {
            "qty": qty,
            "side": "long",
            "entry_price": entry_price,
            "entry_time": (now - timedelta(minutes=minutes_ago)).isoformat(),
            "partial_taken": False,
            "route": "dynamic_aggressive_scalp",
            "source": "dynamic_aggressive",
        }
    }


def _patch_common(monkeypatch, symbol: str) -> None:
    monkeypatch.setattr(stock_exit, "load_tracked", lambda *_a, **_kw: _tracked(symbol))
    monkeypatch.setattr(stock_exit, "update_tracked", lambda *_a, **_kw: None)
    monkeypatch.setattr(stock_exit, "remove_tracked", lambda *_a, **_kw: None)
    monkeypatch.setattr(stock_exit, "blocks_discretionary_stock_exit", lambda *_a, **_kw: (False, None))
    monkeypatch.setattr(stock_exit, "do_not_sell_winners_early_blocks", lambda *_a, **_kw: False)
    monkeypatch.setattr(stock_exit, "exit_trim_suppressed_trend_still_strong", lambda *_a, **_kw: False)
    monkeypatch.setattr(stock_exit, "load_dynamic_state", lambda: {})
    monkeypatch.setattr(stock_exit, "mark_dynamic_cooldown", lambda *_a, **_kw: None)


def test_dynamic_strong_momentum_suppresses_kill_switch_partial(tmp_path, monkeypatch, caplog) -> None:
    symbol = "APPS"
    _patch_common(monkeypatch, symbol)
    caplog.set_level("INFO", logger="src.strategies.exits.stock_exit")
    ctx, submit_order = _ctx(tmp_path, symbol=symbol, dynamic_symbols={symbol})
    ctx.symbols = ["SPY"]
    ctx.engine.check_exit.return_value = ExitSignal(
        symbol=symbol,
        reason=ExitReason.KILL_SWITCH_PARTIAL,
        metadata={"qty_to_sell": 2},
    )

    stock_exit.manage_stock_position(ctx, {"symbol": symbol, "qty": 10, "market_value": 1050})

    submit_order.assert_not_called()
    assert (
        "KILL_SWITCH_PARTIAL_SUPPRESSED_DYNAMIC_MOMENTUM "
        "symbol=APPS reason=strong_momentum"
    ) in caplog.text


def test_dynamic_symbol_still_exits_on_stop_loss(tmp_path, monkeypatch) -> None:
    symbol = "APPS"
    _patch_common(monkeypatch, symbol)
    ctx, submit_order = _ctx(tmp_path, symbol=symbol, dynamic_symbols={symbol})
    ctx.engine.check_exit.return_value = ExitSignal(
        symbol=symbol,
        reason=ExitReason.STOP_LOSS,
        metadata={},
    )

    stock_exit.manage_stock_position(ctx, {"symbol": symbol, "qty": 10, "market_value": 1050})

    submit_order.assert_called_once()


def test_dynamic_strong_news_hold_timer_allows_stop_loss_exit(
    tmp_path, monkeypatch, caplog
) -> None:
    symbol = "APPS"
    _patch_common(monkeypatch, symbol)
    state_path = tmp_path / "dynamic_state.json"
    monkeypatch.setattr(du, "STATE_FILE", state_path)
    start = 2_000_000_000
    monkeypatch.setattr(du, "_now", lambda: start)
    du.remember_entry(symbol, 100.0, du.load_state())
    monkeypatch.setattr(du, "_now", lambda: start + 20 * 60)
    monkeypatch.setattr(du, "get_news_score", lambda *_a, **_kw: (8, "strong_news"))
    monkeypatch.setattr(stock_exit, "session_vwap_and_ema9", lambda *_a, **_kw: (99.0, None))
    caplog.set_level("INFO")

    ctx, submit_order = _ctx(tmp_path, symbol=symbol, dynamic_symbols={symbol})
    ctx.symbols = ["SPY"]
    ctx.config = {
        "dynamic_exits": {"enabled": True, "take_profit_1_pct": 2.0, "strong_news_hold_minutes": 30},
        "dynamic_universe": {},
    }
    ctx.engine.check_exit.return_value = ExitSignal(
        symbol=symbol,
        reason=ExitReason.STOP_LOSS,
        metadata={},
    )

    stock_exit.manage_stock_position(ctx, {"symbol": symbol, "qty": 10, "market_value": 1030})

    submit_order.assert_called_once()
    assert "DYNAMIC_HOLD_TIMER symbol=APPS" in caplog.text


def test_dynamic_aggressive_stop_loss_full_closes_fractional_qty(
    tmp_path, monkeypatch, caplog
) -> None:
    symbol = "FCEL"
    _patch_common(monkeypatch, symbol)
    monkeypatch.setattr(
        stock_exit,
        "load_tracked",
        lambda *_a, **_kw: _tracked_aggressive(symbol, entry_price=109.0, minutes_ago=5),
    )
    ctx, submit_order = _ctx(tmp_path, symbol=symbol, dynamic_symbols={symbol})
    ctx.broker.close_position = MagicMock(return_value=SimpleNamespace(id="close-aggressive"))
    ctx.config = {"dynamic_aggressive": {"stop_loss_pct": 3.0, "take_profit_pct": 4.0, "max_hold_minutes": 20}}
    ctx.broker.available_position_qty.return_value = (10.5, 0.0, 10.5)
    caplog.set_level("INFO", logger="src.strategies.exits.stock_exit")

    stock_exit.manage_stock_position(ctx, {"symbol": symbol, "qty": 10.5, "market_value": 1102.5})

    submit_order.assert_not_called()
    ctx.broker.close_position.assert_called_once_with(symbol)
    assert "DYNAMIC_AGGRESSIVE_EXIT symbol=FCEL reason=stop_loss qty=10.5" in caplog.text


def test_dynamic_aggressive_take_profit_full_closes_fractional_qty(
    tmp_path, monkeypatch, caplog
) -> None:
    symbol = "FCEL"
    _patch_common(monkeypatch, symbol)
    monkeypatch.setattr(
        stock_exit,
        "load_tracked",
        lambda *_a, **_kw: _tracked_aggressive(symbol, entry_price=100.0, minutes_ago=5),
    )
    ctx, submit_order = _ctx(tmp_path, symbol=symbol, dynamic_symbols={symbol})
    ctx.broker.close_position = MagicMock(return_value=SimpleNamespace(id="close-aggressive"))
    ctx.config = {"dynamic_aggressive": {"stop_loss_pct": 3.0, "take_profit_pct": 4.0, "max_hold_minutes": 20}}
    ctx.broker.available_position_qty.return_value = (10.5, 0.0, 10.5)
    caplog.set_level("INFO", logger="src.strategies.exits.stock_exit")

    stock_exit.manage_stock_position(ctx, {"symbol": symbol, "qty": 10.5, "market_value": 1102.5})

    submit_order.assert_not_called()
    ctx.broker.close_position.assert_called_once_with(symbol)
    assert "DYNAMIC_AGGRESSIVE_EXIT symbol=FCEL reason=take_profit qty=10.5" in caplog.text


def test_dynamic_aggressive_max_hold_full_closes_fractional_qty(
    tmp_path, monkeypatch, caplog
) -> None:
    symbol = "FCEL"
    _patch_common(monkeypatch, symbol)
    monkeypatch.setattr(
        stock_exit,
        "load_tracked",
        lambda *_a, **_kw: _tracked_aggressive(symbol, entry_price=104.0, minutes_ago=25),
    )
    ctx, submit_order = _ctx(tmp_path, symbol=symbol, dynamic_symbols={symbol})
    ctx.broker.close_position = MagicMock(return_value=SimpleNamespace(id="close-aggressive"))
    ctx.config = {"dynamic_aggressive": {"stop_loss_pct": 3.0, "take_profit_pct": 4.0, "max_hold_minutes": 20}}
    ctx.broker.available_position_qty.return_value = (10.5, 0.0, 10.5)
    caplog.set_level("INFO", logger="src.strategies.exits.stock_exit")

    stock_exit.manage_stock_position(ctx, {"symbol": symbol, "qty": 10.5, "market_value": 1102.5})

    submit_order.assert_not_called()
    ctx.broker.close_position.assert_called_once_with(symbol)
    assert "DYNAMIC_AGGRESSIVE_EXIT symbol=FCEL reason=max_hold qty=10.5" in caplog.text


def test_stock_exit_skips_when_position_fully_held_by_open_sell_order(
    tmp_path, monkeypatch
) -> None:
    symbol = "AAPL"
    _patch_common(monkeypatch, symbol)
    ctx, submit_order = _ctx(tmp_path, symbol=symbol, dynamic_symbols=set())
    ctx.broker.available_position_qty.return_value = (10.0, 10.0, 0.0)
    ctx.engine.check_exit.return_value = ExitSignal(
        symbol=symbol,
        reason=ExitReason.STOP_LOSS,
        metadata={},
    )

    stock_exit.manage_stock_position(ctx, {"symbol": symbol, "qty": 10, "market_value": 1050})

    submit_order.assert_not_called()
    ctx.engine.execution.build_order.assert_not_called()


def test_stock_exit_full_exit_uses_available_qty_when_order_holds_shares(
    tmp_path, monkeypatch
) -> None:
    symbol = "MSFT"
    _patch_common(monkeypatch, symbol)
    ctx, submit_order = _ctx(tmp_path, symbol=symbol, dynamic_symbols=set())
    ctx.broker.available_position_qty.return_value = (10.0, 1.0, 9.0)
    ctx.engine.check_exit.return_value = ExitSignal(
        symbol=symbol,
        reason=ExitReason.STOP_LOSS,
        metadata={},
    )

    stock_exit.manage_stock_position(
        ctx,
        {"symbol": symbol, "qty": 10, "market_value": 1050, "qty_held_for_orders": 1},
    )

    submit_order.assert_called_once()
    assert submit_order.call_args.args[0].quantity == 9
    ctx.engine.execution.build_order.assert_not_called()


def test_stock_exit_full_exit_clamps_fractional_available_qty_with_open_sell_order(
    tmp_path, monkeypatch, caplog
) -> None:
    symbol = "MSFT"
    _patch_common(monkeypatch, symbol)
    now = pytz.timezone("America/New_York").localize(datetime(2026, 5, 28, 10, 30))
    existing_qty = 2.876432235
    held_for_orders = 1.0
    available_qty = 1.876432235
    monkeypatch.setattr(
        stock_exit,
        "load_tracked",
        lambda *_a, **_kw: {
            symbol: {
                "qty": existing_qty,
                "side": "long",
                "entry_price": 100.0,
                "entry_time": (now - timedelta(minutes=90)).isoformat(),
                "partial_taken": False,
            }
        },
    )
    ctx, submit_order = _ctx(tmp_path, symbol=symbol, dynamic_symbols=set())
    ctx.broker.available_position_qty.return_value = (
        existing_qty,
        held_for_orders,
        available_qty,
    )
    ctx.engine.check_exit.return_value = ExitSignal(
        symbol=symbol,
        reason=ExitReason.STOP_LOSS,
        metadata={},
    )

    caplog.set_level("INFO", logger="src.safe_sell")

    stock_exit.manage_stock_position(
        ctx,
        {
            "symbol": symbol,
            "qty": existing_qty,
            "market_value": existing_qty * 105.0,
            "qty_held_for_orders": held_for_orders,
        },
    )

    submit_order.assert_called_once()
    assert submit_order.call_args.args[0].quantity == available_qty
    ctx.engine.execution.build_order.assert_not_called()
    assert (
        "FRACTIONAL_FULL_CLOSE symbol=MSFT qty=1.87643224 reason=stop_loss"
    ) in caplog.text


def test_core_symbol_keeps_kill_switch_partial_behavior(tmp_path, monkeypatch) -> None:
    symbol = "AAPL"
    _patch_common(monkeypatch, symbol)
    ctx, submit_order = _ctx(tmp_path, symbol=symbol, dynamic_symbols=set())
    ctx.engine.check_exit.return_value = ExitSignal(
        symbol=symbol,
        reason=ExitReason.KILL_SWITCH_PARTIAL,
        metadata={"qty_to_sell": 2},
    )

    stock_exit.manage_stock_position(ctx, {"symbol": symbol, "qty": 10, "market_value": 1050})

    submit_order.assert_called_once()


def test_core_with_dynamic_signal_does_not_use_dynamic_only_exits(tmp_path, monkeypatch) -> None:
    symbol = "MSFT"
    _patch_common(monkeypatch, symbol)
    ctx, submit_order = _ctx(tmp_path, symbol=symbol, dynamic_symbols={symbol})
    ctx.engine.symbol_classifications = {symbol: "CORE_WITH_DYNAMIC_SIGNAL"}
    dynamic_exit_called = {"value": False}

    def fail_dynamic_exit(*_args, **_kwargs):
        dynamic_exit_called["value"] = True
        raise AssertionError("dynamic exit should not be used for core_with_dynamic_signal")

    monkeypatch.setattr(stock_exit, "manage_dynamic_exit", fail_dynamic_exit)
    ctx.engine.check_exit.return_value = None

    stock_exit.manage_stock_position(ctx, {"symbol": symbol, "qty": 10, "market_value": 1050})

    assert not dynamic_exit_called["value"]
    submit_order.assert_not_called()


def test_dynamic_small_profit_full_exit_is_blocked(tmp_path, monkeypatch, caplog) -> None:
    symbol = "APPS"
    _patch_common(monkeypatch, symbol)
    monkeypatch.setattr(stock_exit, "session_vwap_and_ema9", lambda *_a, **_kw: (100.0, None))
    monkeypatch.setattr(stock_exit, "get_cached_news_score", lambda *_a, **_kw: (0, "cache"))
    caplog.set_level("INFO", logger="src.strategies.exits.stock_exit")
    ctx, submit_order = _ctx(tmp_path, symbol=symbol, dynamic_symbols={symbol})
    ctx.symbols = ["SPY"]
    class CloseQuote:
        bid = 101.45
        ask = 101.55
        spread_pct = 0.1
        skip_spread_check = False

        @staticmethod
        def reference_mid(_fallback: float) -> float:
            return 101.5

    ctx.broker.get_latest_quote.return_value = CloseQuote()
    ctx.config = {
        "dynamic_exits": {"enabled": False},
        "dynamic_universe": {"min_profit_before_full_exit_pct": 2.0},
        "news_ai": {"enabled": False},
    }
    ctx.engine.check_exit.return_value = ExitSignal(
        symbol=symbol,
        reason=ExitReason.TAKE_PROFIT,
        metadata={"ret_pct": 1.5},
    )

    stock_exit.manage_stock_position(ctx, {"symbol": symbol, "qty": 10, "market_value": 1015})

    submit_order.assert_not_called()
    assert "DYNAMIC_HOLD_SMALL_PROFIT symbol=APPS profit_pct=1.50 min=2.00" in caplog.text


def test_dynamic_eod_flatten_closes_unless_strong_news_allows_hold(
    tmp_path, monkeypatch, caplog
) -> None:
    symbol = "APPS"
    _patch_common(monkeypatch, symbol)
    monkeypatch.setattr(stock_exit, "session_vwap_and_ema9", lambda *_a, **_kw: (100.0, None))
    monkeypatch.setattr(stock_exit, "get_cached_news_score", lambda *_a, **_kw: (0, "cache"))
    caplog.set_level("INFO", logger="src.strategies.exits.stock_exit")
    ctx, submit_order = _ctx(tmp_path, symbol=symbol, dynamic_symbols={symbol})
    ctx.symbols = ["SPY"]
    ctx.now = pytz.timezone("America/New_York").localize(datetime(2026, 5, 28, 15, 55))
    class CloseQuote:
        bid = 100.95
        ask = 101.05
        spread_pct = 0.1
        skip_spread_check = False

        @staticmethod
        def reference_mid(_fallback: float) -> float:
            return 101.0

    ctx.broker.get_latest_quote.return_value = CloseQuote()
    ctx.config = {
        "dynamic_exits": {"enabled": True},
        "dynamic_universe": {
            "close_intraday_positions_before_close": True,
            "minutes_before_close_to_flatten": 10,
            "allow_overnight_dynamic_hold": False,
        },
        "news_ai": {"enabled": False, "allow_overnight_if_score_gte": 8},
    }
    ctx.engine.check_exit.return_value = None

    stock_exit.manage_stock_position(ctx, {"symbol": symbol, "qty": 10, "market_value": 1015})

    submit_order.assert_called_once()
    assert "DYNAMIC_EOD_FLATTEN symbol=APPS reason=intraday close window" in caplog.text

    submit_order.reset_mock()
    caplog.clear()
    monkeypatch.setattr(stock_exit, "get_cached_news_score", lambda *_a, **_kw: (10, "cache"))
    stock_exit.manage_stock_position(ctx, {"symbol": symbol, "qty": 10, "market_value": 1015})

    submit_order.assert_not_called()
