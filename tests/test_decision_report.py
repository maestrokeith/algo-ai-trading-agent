from __future__ import annotations

from types import SimpleNamespace

from src.decision_report import build_buy_decision_report


def test_build_buy_decision_report_includes_core_reasons() -> None:
    entry = SimpleNamespace(
        take_profit_pct=4.2,
        stop_pct=2.0,
        metadata={"ma_fast": 20, "ma_slow": 200},
    )
    report = build_buy_decision_report(
        symbol="NVDA",
        route="trend",
        trend_ok=True,
        pullback_ok=True,
        momentum_ok=True,
        volatility_ok=True,
        regime_score=4,
        regime_condition="bullish",
        spread_pct=0.12,
        gross_exposure_pct=57.0,
        max_gross_exposure_frac=0.80,
        entry_signal=entry,
    )
    assert report.startswith("BUY NVDA because:")
    assert "price > 200D MA" in report
    assert "20D pullback confirmed" in report
    assert "momentum positive" in report
    assert "regime bullish score 4" in report
    assert "portfolio exposure under cap (57.0% < 80.0%)" in report
    assert "expected risk/reward: 2.1x" in report
