from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from src.dynamic_entry_adaptive import (
    build_dynamic_entry_adaptive_report,
    classify_setup,
    dynamic_feature_readiness,
    dynamic_size_multiplier,
    one_minor_rule_exception_allowed,
    rank_dynamic_candidates,
    render_dynamic_entry_adaptive_report,
    resolve_adaptive_sensitivity,
    write_dynamic_entry_adaptive_report,
)
from src.dynamic_entry_rejection_report import (
    build_dynamic_entry_rejection_report,
    classify_dynamic_entry_rejection_class,
)
from src.dynamic_universe import dynamic_momentum_entry_passes


def _cfg() -> dict[str, object]:
    return {
        "adaptive_sensitivity": {
            "enabled": True,
            "default_mode": "normal",
            "lookback_trading_days": 10,
            "minimum_observations": 5,
            "target_entries_per_day": {"minimum": 2, "preferred": 4, "maximum": 6},
            "quality_score": {
                "normal_min_quality_score": 80,
                "relaxed_reduction": 5,
                "absolute_floor": 70,
            },
            "rvol": {
                "normal_min": 1.8,
                "relaxed_large_cap_min": 1.25,
                "relaxed_other_min": 1.5,
            },
            "no_chase": {
                "normal_max_vwap_distance_atr": 1.0,
                "relaxed_max_vwap_distance_atr": 1.25,
                "absolute_max_vwap_distance_atr": 1.5,
            },
            "one_minor_rule_exception": {"enabled": True, "max_failed_minor_rules": 1},
            "sizing": {"relaxed_multiplier": 0.5, "exception_multiplier": 0.35},
            "safety": {
                "max_rolling_drawdown_pct": 2.0,
                "max_relaxed_loss_rate": 0.6,
                "disable_in_risk_off_regime": True,
            },
        }
    }


def _metrics(**overrides: object) -> dict[str, object]:
    out: dict[str, object] = {
        "observations": 10,
        "trades": 4,
        "trades_per_day": 0.4,
        "win_rate": 0.5,
        "loss_rate": 0.25,
        "max_drawdown_pct": 0.5,
        "relaxed_underperformance": False,
    }
    out.update(overrides)
    return out


def test_default_mode_is_normal_without_enough_observations() -> None:
    state = resolve_adaptive_sensitivity(_cfg(), metrics=_metrics(observations=1), base_min_rvol=1.8)

    assert state.mode == "normal"
    assert state.reason == "insufficient_observations"


def test_relaxed_mode_activates_only_when_safe_and_under_target() -> None:
    state = resolve_adaptive_sensitivity(_cfg(), metrics=_metrics(), context={"market_regime": "normal"}, base_min_rvol=1.8)

    assert state.mode == "relaxed"
    assert state.reason == "low_trade_frequency_safe_drawdown"


def test_relaxed_mode_does_not_activate_in_risk_off_or_daily_loss() -> None:
    risk_off = resolve_adaptive_sensitivity(_cfg(), metrics=_metrics(), context={"market_regime": "risk_off"}, base_min_rvol=1.8)
    loss_lock = resolve_adaptive_sensitivity(_cfg(), metrics=_metrics(), context={"daily_loss_lockout": True}, base_min_rvol=1.8)

    assert risk_off.mode == "tight"
    assert risk_off.reason == "risk_off_regime"
    assert loss_lock.mode == "tight"
    assert loss_lock.reason == "daily_loss_lockout"


def test_quality_reduction_respects_absolute_floor() -> None:
    cfg = _cfg()
    adaptive = cfg["adaptive_sensitivity"]
    assert isinstance(adaptive, dict)
    adaptive["quality_score"] = {"normal_min_quality_score": 72, "relaxed_reduction": 10, "absolute_floor": 70}

    state = resolve_adaptive_sensitivity(cfg, metrics=_metrics(), context={"market_regime": "normal"}, base_min_rvol=1.8)

    assert state.mode == "relaxed"
    assert state.effective_quality_score == 70


def test_rvol_relaxation_uses_configured_bounds() -> None:
    other = resolve_adaptive_sensitivity(_cfg(), metrics=_metrics(), context={"market_regime": "normal"}, base_min_rvol=1.8)
    large = resolve_adaptive_sensitivity(_cfg(), metrics=_metrics(), context={"market_regime": "normal", "large_cap": True}, base_min_rvol=1.8)

    assert other.effective_rvol == pytest.approx(1.5)
    assert large.effective_rvol == pytest.approx(1.25)


def test_no_chase_relaxation_respects_absolute_atr_ceiling() -> None:
    cfg = _cfg()
    adaptive = cfg["adaptive_sensitivity"]
    assert isinstance(adaptive, dict)
    adaptive["no_chase"] = {
        "normal_max_vwap_distance_atr": 1.0,
        "relaxed_max_vwap_distance_atr": 2.0,
        "absolute_max_vwap_distance_atr": 1.5,
    }

    state = resolve_adaptive_sensitivity(cfg, metrics=_metrics(), context={"market_regime": "normal"}, base_min_rvol=1.8)

    assert state.max_vwap_distance_atr == pytest.approx(1.5)


def test_one_minor_rule_exception_allows_exactly_one_minor_rule() -> None:
    state = resolve_adaptive_sensitivity(_cfg(), metrics=_metrics(), context={"market_regime": "normal"}, base_min_rvol=1.8)

    allowed, rule = one_minor_rule_exception_allowed(state, failed_rules=["slightly_below_rvol"], quality_score=90)

    assert allowed is True
    assert rule == "slightly_below_rvol"


def test_two_minor_rules_or_hard_rule_are_rejected() -> None:
    state = resolve_adaptive_sensitivity(_cfg(), metrics=_metrics(), context={"market_regime": "normal"}, base_min_rvol=1.8)

    two, _ = one_minor_rule_exception_allowed(
        state,
        failed_rules=["slightly_below_rvol", "entry_alignment"],
        quality_score=90,
    )
    hard, _ = one_minor_rule_exception_allowed(
        state,
        failed_rules=["slightly_below_rvol"],
        hard_rules=["portfolio_exposure_cap"],
        quality_score=90,
    )

    assert two is False
    assert hard is False


def test_relaxed_and_exception_entries_receive_reduced_sizing() -> None:
    relaxed = resolve_adaptive_sensitivity(_cfg(), metrics=_metrics(), context={"market_regime": "normal"}, base_min_rvol=1.8)
    normal = resolve_adaptive_sensitivity(_cfg(), metrics=_metrics(observations=1), base_min_rvol=1.8)

    assert dynamic_size_multiplier(relaxed) == pytest.approx(0.5)
    assert dynamic_size_multiplier(relaxed, exception=True) == pytest.approx(0.35)
    assert dynamic_size_multiplier(normal) == pytest.approx(1.0)


def test_hard_contexts_keep_portfolio_spread_and_data_gates_tight() -> None:
    portfolio = resolve_adaptive_sensitivity(
        _cfg(),
        metrics=_metrics(),
        context={"gross_exposure_pct": 99.0, "gross_exposure_cap_pct": 99.0},
        base_min_rvol=1.8,
    )
    spread = resolve_adaptive_sensitivity(_cfg(), metrics=_metrics(), context={"spread_liquidity_bad": True}, base_min_rvol=1.8)
    data = resolve_adaptive_sensitivity(_cfg(), metrics=_metrics(), context={"data_quality_bad": True}, base_min_rvol=1.8)

    assert portfolio.mode == "tight"
    assert spread.mode == "tight"
    assert data.mode == "tight"


def test_pullback_entry_requires_confirmation() -> None:
    assert classify_setup(price_above_vwap=True, five_min_trend=True, vwap_distance_atr=0.2) == "vwap_reclaim"
    assert classify_setup(price_above_vwap=True, five_min_trend=False, vwap_distance_atr=0.2) == "none"


def test_candidate_ranking_selects_strongest_without_forcing_low_quality() -> None:
    ranked = rank_dynamic_candidates(
        [
            {"symbol": "LOW", "quality_score": 60, "relative_volume": 1.0, "spread_ok": True},
            {
                "symbol": "HIGH",
                "quality_score": 82,
                "relative_volume": 2.4,
                "sector_confirmed": True,
                "spy_qqq_aligned": True,
                "spread_ok": True,
            },
        ],
        top_n=1,
    )

    assert [row["symbol"] for row in ranked] == ["HIGH"]
    assert rank_dynamic_candidates([], top_n=2) == []


def test_relaxed_mode_disables_after_poor_rolling_performance_and_hysteresis() -> None:
    under = resolve_adaptive_sensitivity(
        _cfg(),
        metrics=_metrics(relaxed_underperformance=True),
        context={"market_regime": "normal"},
        base_min_rvol=1.8,
    )
    insufficient = resolve_adaptive_sensitivity(_cfg(), metrics=_metrics(observations=2), base_min_rvol=1.8)

    assert under.mode == "normal"
    assert under.reason == "relaxed_underperformance"
    assert insufficient.mode == "normal"


def test_rejection_report_distinguishes_hard_and_minor_failures(tmp_path: Path) -> None:
    report = build_dynamic_entry_rejection_report(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        day="2026-07-14",
        user_id="live_bot",
        log_text="\n".join(
            [
                "2026-07-14 ENTRY_EVAL final=F reason=entry_alignment: need 5m breakout",
                "2026-07-14 SKIP ABC: reason=soft_cap: no buy headroom under cap",
                "2026-07-14 ENTRY_EVAL final=F reason=bad_quote",
            ]
        ),
    )

    assert classify_dynamic_entry_rejection_class("reason=entry_alignment: need 5m breakout") == "minor_rule_failure"
    assert report["counts_by_class"]["minor_rule_failure"] == 1
    assert report["counts_by_class"]["risk_block"] == 1
    assert report["counts_by_class"]["data_quality_block"] == 1


def test_adaptive_report_marks_unavailable_metrics_without_row_level_data(tmp_path: Path) -> None:
    daily = tmp_path / "data" / "profitability_attribution" / "daily"
    daily.mkdir(parents=True)
    (daily / "2026-07-14_live_bot.json").write_text(
        json.dumps({"route_stats": {"dynamic_momentum": {"trades": 1, "wins": 1, "pnl": 5.0}}}),
        encoding="utf-8",
    )

    report = build_dynamic_entry_adaptive_report(
        data_dir=tmp_path / "data",
        user_id="live_bot",
        report_date="2026-07-14",
        config={"dynamic_momentum_entry": {"min_relative_volume": 1.8}, "dynamic_entry": _cfg()},
    )

    assert report["current_mode"] in {"normal", "relaxed", "tight"}
    assert report["mfe_mae_by_mode"] == "unavailable"
    assert "data_limitations" in report


def test_dynamic_gate_behavior_unchanged_when_adaptive_disabled() -> None:
    bars_1m = pd.DataFrame(
        {
            "open": [10.0, 10.0, 10.0],
            "high": [10.4, 10.3, 10.2],
            "low": [9.9, 9.9, 9.9],
            "close": [10.0, 10.0, 10.0],
            "volume": [1000, 1000, 1000],
        }
    )
    bars_5m = pd.DataFrame(
        {
            "open": [9.8, 9.9, 10.0],
            "high": [10.2, 10.2, 10.2],
            "low": [9.7, 9.8, 9.9],
            "close": [9.9, 10.0, 10.05],
            "volume": [1000, 1000, 1000],
        }
    )

    ok, reason = dynamic_momentum_entry_passes(
        gain_pct=12.0,
        relative_volume=1.8,
        vwap_above=True,
        spread_pct=0.2,
        bars_1m=bars_1m,
        bars_5m=bars_5m,
        ref_price=10.05,
        cfg={"min_day_gain_pct": 10, "min_relative_volume": 1.5, "adaptive_sensitivity": {"enabled": False}},
        is_dynamic=True,
    )

    assert ok is False
    assert "need 5m breakout" in reason


def test_dynamic_feature_readiness_classifies_missing_inputs() -> None:
    cfg = {
        "flexible_entries": {
            "data_quality": {
                "require_vwap": True,
                "require_short_ema": True,
                "require_atr": True,
                "require_5m_trend": True,
            }
        }
    }

    not_ready = dynamic_feature_readiness(
        bars_1m_count=12,
        bars_5m_count=3,
        vwap=None,
        ema20=101.2,
        ema50=None,
        atr="bad",
        momentum_score=0.7,
        trend_5m=None,
        config=cfg,
    )
    ready = dynamic_feature_readiness(
        bars_1m_count=20,
        bars_5m_count=5,
        vwap=100.5,
        ema20=101.2,
        ema50=100.1,
        atr=1.4,
        momentum_score=0.7,
        trend_5m=True,
        config=cfg,
    )

    assert not_ready["final_status"] == "not_ready"
    assert {"vwap", "ema50", "atr", "trend_5m"}.issubset(set(not_ready["missing_features"]))
    assert ready["final_status"] == "ready"


def test_write_dynamic_entry_adaptive_report_includes_adaptive_safety_line(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    day_dir = data_dir / "research_metrics" / "2026-07-22"
    day_dir.mkdir(parents=True)
    (day_dir / "dynamic_funnel_live.json").write_text(
        json.dumps(
            {
                "scanner": {"accepted": 4},
                "entry": {"passed": 1, "failed": 3, "reasons": {"entry_alignment": 3}},
            }
        ),
        encoding="utf-8",
    )
    (day_dir / "dynamic_entry_alignment.json").write_text(
        json.dumps({"events": [{"momentum_score": 82, "raw_reason": "entry_alignment: below vwap"}]}),
        encoding="utf-8",
    )
    daily_dir = data_dir / "profitability_attribution" / "daily"
    daily_dir.mkdir(parents=True)
    for idx in range(5):
        (daily_dir / f"2026-07-1{idx}_live_bot.json").write_text(
            json.dumps({"route_stats": {"dynamic_momentum": {"trades": 1, "wins": 1, "pnl": 2.0}}}),
            encoding="utf-8",
        )
    cfg = _cfg()
    cfg = {
        "dynamic_momentum_entry": {
            "adaptive_sensitivity": cfg["adaptive_sensitivity"],
            "trading_control": {"adaptive_relaxation": {"production_auto_apply": False}},
        }
    }

    json_path, md_path, report = write_dynamic_entry_adaptive_report(
        data_dir=data_dir,
        user_id="live_bot",
        report_date="2026-07-22",
        config=cfg,
        context={"environment": "live", "production": True},
    )
    rendered = render_dynamic_entry_adaptive_report(report)

    assert json_path.exists()
    assert md_path.exists()
    assert report["current_mode"] == "normal"
    assert report["reason_for_mode"] == "low_trade_frequency_informational_only"
    assert report["safety_trigger_status"] == "low_trade_frequency_informational_only"
    assert "DYNAMIC_ENTRY_ADAPTIVE_CONFIG" in rendered
