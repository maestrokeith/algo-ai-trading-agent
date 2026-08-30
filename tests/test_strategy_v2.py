"""Tests for strategy_v2 (hedge-fund-style overlay)."""

from __future__ import annotations

import logging

import pandas as pd

from src.config_loader import load_app_config
from src.execution import ExecutionManager
from src.strategy_v2 import (
    allow_long_for_regime,
    compute_hedge_size,
    compute_targets_v2,
    entry_cycle,
    evaluate_hedge,
    evaluate_hedge_place,
    evaluate_longs,
    evaluate_options,
    hedge_allocation_pct,
    place_hedge_order,
    options_signal_independent,
    run_entry_cycle,
    position_size_v2,
    rebalance_plan,
    regime_long_mult_for_score,
    rsi_wilder_last,
    should_enter_long,
)


def _cfg_min() -> dict:
    return {
        "strategy_v2": {
            "portfolio": {"base_position_pct": 0.06},
            "regime": {
                "bullish": {"score_min": 4, "long_mult": 1.0, "hedge_mult": 0.0},
                "neutral": {"score_min": 2, "long_mult": 0.5, "hedge_mult": 0.15},
                "bearish": {"score_min": 0, "long_mult": 0.2, "hedge_mult": 0.5},
            },
            "signals": {
                "trend": {"require_price_above_ma": True},
                "momentum": {"rsi_min": 45, "rsi_max": 70},
            },
            "hedging": {
                "pct_equity_score_ge_2": 0.10,
                "pct_equity_below_2": 0.25,
            },
            "options": {
                "enabled": True,
                "independent_signal": True,
                "iv_rank_proxy_min": 60,
                "breakout_lookback_days": 5,
            },
        }
    }


def test_regime_long_mult() -> None:
    c = _cfg_min()
    assert regime_long_mult_for_score(5, c) == 1.0
    assert regime_long_mult_for_score(3, c) == 0.5
    assert regime_long_mult_for_score(1, c) == 0.2


def test_allow_long_for_regime() -> None:
    assert allow_long_for_regime(2) is True
    assert allow_long_for_regime(5) is True
    assert allow_long_for_regime(1) is False
    assert allow_long_for_regime(0) is False


def test_should_enter_long_tier_4_trend_only() -> None:
    c = _cfg_min()
    assert should_enter_long(regime_score=5, price=101.0, ma50=100.0, rsi=30.0, cfg=c) is True
    assert should_enter_long(regime_score=5, price=99.0, ma50=100.0, rsi=55.0, cfg=c) is False


def test_should_enter_long_tier_2_needs_rsi() -> None:
    c = _cfg_min()
    assert should_enter_long(regime_score=3, price=101.0, ma50=100.0, rsi=55.0, cfg=c) is True
    assert should_enter_long(regime_score=3, price=101.0, ma50=100.0, rsi=40.0, cfg=c) is False
    assert should_enter_long(regime_score=3, price=101.0, ma50=100.0, rsi=None, cfg=c) is False


def test_should_enter_long_below_2_false() -> None:
    c = _cfg_min()
    assert should_enter_long(regime_score=1, price=101.0, ma50=100.0, rsi=55.0, cfg=c) is False


def test_hedge_size_two_tier() -> None:
    c = _cfg_min()
    assert compute_hedge_size(5, 100_000, c) == 10_000.0
    assert compute_hedge_size(2, 100_000, c) == 10_000.0
    assert compute_hedge_size(1, 100_000, c) == 25_000.0
    assert compute_hedge_size(0, 100_000, c) == 25_000.0


def test_hedge_allocation_pct() -> None:
    c = _cfg_min()
    assert hedge_allocation_pct(5, c) == 0.10
    assert hedge_allocation_pct(1, c) == 0.25


def test_position_size_v2() -> None:
    c = _cfg_min()
    assert position_size_v2(100_000, 1.0, c) == 6_000.0
    assert position_size_v2(100_000, 0.5, c) == 3_000.0


def test_rsi_wilder_sane() -> None:
    close = pd.Series([float(i) for i in range(40, 80)])
    r = rsi_wilder_last(close, period=14)
    assert r is not None
    assert 0 < r < 100


def _bars_uptrend(n: int = 120) -> pd.DataFrame:
    close = pd.Series([100.0 + i * 0.2 for i in range(n)])
    return pd.DataFrame({"close": close})


def test_evaluate_longs_pass_bullish_trend() -> None:
    c = _cfg_min()
    df = _bars_uptrend()
    r = evaluate_longs(cfg=c, regime_score=5, symbols=["QQQ"], get_bars=lambda s: df)
    assert r.candidates == [("QQQ", True)]


def test_evaluate_longs_rejects_below_ma(capsys) -> None:
    c = _cfg_min()
    close = pd.Series([100.0 + i * 0.2 for i in range(115)] + [80.0] * 5)
    df = pd.DataFrame({"close": close})
    r = evaluate_longs(cfg=c, regime_score=5, symbols=["X"], get_bars=lambda s: df)
    assert r.candidates[0] == ("X", False)
    out = capsys.readouterr().out
    assert "LONG skip — reason: X: reject v2 long gate" in out


def test_evaluate_longs_no_bars() -> None:
    c = _cfg_min()
    r = evaluate_longs(
        cfg=c, regime_score=5, symbols=["Z"], get_bars=lambda s: pd.DataFrame()
    )
    assert r.candidates == [("Z", False)]
    assert "no bars" in r.reasons[0][1]


def test_evaluate_longs_short_history() -> None:
    c = _cfg_min()
    df = pd.DataFrame({"close": [100.0, 101.0, 102.0]})
    r = evaluate_longs(cfg=c, regime_score=5, symbols=["Y"], get_bars=lambda s: df)
    assert r.candidates[0] == ("Y", False)
    assert "short history" in r.reasons[0][1]


def test_evaluate_hedge_symbol_from_cfg() -> None:
    c = _cfg_min()
    c["strategy_v2"]["hedging"]["symbol"] = "SH"
    r = evaluate_hedge(cfg=c, regime_score=3, equity=100_000)
    assert r.symbol == "SH"
    assert r.hedge_pct == 0.10
    assert r.target_notional_usd == 10_000.0


def test_place_hedge_order_submits() -> None:
    c = _cfg_min()
    ex = ExecutionManager({"execution": {"max_spread_pct_to_trade": 5.0}})
    submitted: list = []

    class _Bk:
        def submit_order(self, req):
            submitted.append(req)
            return {"id": "1"}

    order, err = place_hedge_order(
        regime_score=5,
        equity=100_000,
        cfg=c,
        broker=_Bk(),
        execution_manager=ex,
        mid_price=10.0,
        spread_pct=0.1,
    )
    assert err is None
    assert order == {"id": "1"}
    assert submitted and submitted[0].symbol == "SQQQ"
    assert submitted[0].quantity == 1000  # $10k / $10


def test_evaluate_hedge_place_returns_eval_and_order() -> None:
    c = _cfg_min()
    ex = ExecutionManager({"execution": {"max_spread_pct_to_trade": 5.0}})

    class _Bk:
        def submit_order(self, req):
            return {"id": "x"}

    ev, order, err = evaluate_hedge_place(
        5,
        cfg=c,
        equity=100_000,
        broker=_Bk(),
        execution_manager=ex,
        mid_price=20.0,
        spread_pct=0.05,
    )
    assert err is None
    assert ev.hedge_pct == 0.10
    assert order == {"id": "x"}


def test_evaluate_options_delegates() -> None:
    c = _cfg_min()
    df = _bars_uptrend()
    r = evaluate_options(cfg=c, symbols=["QQQ"], get_bars=lambda s: df)
    assert len(r.signals) == 1
    sym, ok, msg = r.signals[0]
    assert sym == "QQQ"
    assert isinstance(ok, bool)
    assert msg


def test_run_entry_cycle_logs_and_matches_entry_cycle(caplog, capsys) -> None:
    caplog.set_level(logging.INFO)
    c = _cfg_min()
    df = _bars_uptrend()
    get_bars = lambda s: df
    sym = ["QQQ"]

    def _regime() -> int:
        return 5

    rep = run_entry_cycle(
        cfg=c,
        equity=100_000,
        symbols=sym,
        get_bars=get_bars,
        compute_regime=_regime,
    )
    quiet = entry_cycle(
        cfg=c,
        regime_score=5,
        equity=100_000,
        symbols=sym,
        get_bars=get_bars,
    )
    assert rep.longs.candidates == quiet.longs.candidates
    assert rep.hedge.target_notional_usd == quiet.hedge.target_notional_usd
    assert rep.options.signals == quiet.options.signals
    out = capsys.readouterr().out
    assert "running longs..." in out
    assert "running hedge..." in out
    assert "running options..." in out
    assert any("run_entry_cycle longs" in r.message for r in caplog.records)
    assert any("hedge_pct=" in r.message for r in caplog.records)


def test_entry_cycle_order() -> None:
    c = _cfg_min()
    df = _bars_uptrend()
    rep = entry_cycle(
        cfg=c,
        regime_score=5,
        equity=100_000,
        symbols=["QQQ"],
        get_bars=lambda s: df,
    )
    assert rep.longs.candidates[0] == ("QQQ", True)
    assert rep.hedge.target_notional_usd == 10_000.0
    assert rep.hedge.symbol == "SQQQ"
    assert len(rep.options.signals) == 1 and rep.options.signals[0][0] == "QQQ"


def test_options_signal_shape() -> None:
    # Steep uptrend → high vol rank + breakout on last bar
    n = 120
    close = pd.Series([100.0 + i * 0.5 + (0.3 * (i % 7)) for i in range(n)])
    df = pd.DataFrame({"close": close})
    c = _cfg_min()
    ok, msg = options_signal_independent("QQQ", df=df, cfg=c)
    assert isinstance(ok, bool)
    assert msg


def test_rebalance_plan_trim_add() -> None:
    plan = rebalance_plan({"A": 0.10, "B": 0.05}, {"A": 0.06, "B": 0.10}, tol=0.001)
    actions = {p[0]: p[1] for p in plan}
    assert "A" in actions or "B" in actions


def test_compute_targets_empty() -> None:
    assert compute_targets_v2(_cfg_min(), regime_score=3, equity=100_000) == []


def test_load_app_config_merges_v2(tmp_path) -> None:
    base_f = tmp_path / "default.yaml"
    base_f.write_text("broker:\n  paper: true\n")
    v2_f = tmp_path / "strategy_v2.yaml"
    v2_f.write_text("portfolio:\n  max_positions: 6\n")
    cfg = load_app_config(base_f)
    assert cfg["broker"]["paper"] is True
    assert cfg.get("strategy_v2", {}).get("portfolio", {}).get("max_positions") == 6
