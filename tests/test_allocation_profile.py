"""Tests for medium-aggressive allocation profile policy."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.allocation_profile import (
    CORE_STOCK_SYMBOLS,
    allocation_target_fractions,
    clip_actions_for_allocation_profile,
    deployable_cash_after_reserve,
    dynamic_lockout_reason,
    dynamic_quality_decision,
    dynamic_quality_reject_reason,
    filter_allocator_candidates_for_profile,
    normalize_strategy_route,
)


def _config() -> dict:
    return {
        "portfolio": {
            "target_core_stock_pct": 65,
            "target_dynamic_pct": 25,
            "target_cash_pct": 10,
            "capital_allocator": {"etf_fallback_enabled": False},
        }
    }


def test_allocation_targets_65_25_10() -> None:
    targets = allocation_target_fractions(_config())
    assert targets == {"core": pytest.approx(0.65), "dynamic": pytest.approx(0.25), "cash": pytest.approx(0.10)}
    assert {"AAPL", "MSFT", "NVDA", "LLY"}.issubset(CORE_STOCK_SYMBOLS)


def test_normalize_strategy_route_preserves_reporting_buckets() -> None:
    assert normalize_strategy_route("dynamic_momentum_override") == "dynamic_momentum"
    assert normalize_strategy_route(None, "dynamic_universe") == "dynamic_momentum"
    assert normalize_strategy_route("core_rebuild") == "core_rebuild"
    assert normalize_strategy_route("", None) == "unknown"


def test_core_candidates_prioritized_when_core_bucket_underweight() -> None:
    out = filter_allocator_candidates_for_profile(
        [
            {"symbol": "AAPL", "score": 1.0},
            {"symbol": "ZZZ", "score": 1.1},
        ],
        config=_config(),
        portfolio=[],
        tracked={},
        equity=100_000.0,
    )
    aapl = next(row for row in out if row["symbol"] == "AAPL")
    assert aapl["score"] == pytest.approx(1.2)
    assert aapl["core_bucket_priority"] is True


def test_dynamic_no_catalyst_rejected(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO", logger="src.allocation_profile"):
        out = filter_allocator_candidates_for_profile(
            [
                {
                    "symbol": "MOBX",
                    "score": 9.0,
                    "dynamic_candidate": True,
                    "route": "dynamic_replay",
                    "catalyst_type": "none",
                    "catalyst_age_minutes": 12,
                }
            ],
            config=_config(),
            portfolio=[],
            tracked={},
            equity=100_000.0,
    )
    assert out == []
    assert dynamic_quality_reject_reason({"dynamic_candidate": True}) == "no_catalyst"
    assert "DYNAMIC_REJECT symbol=MOBX reason=no_catalyst" in caplog.text
    assert "required_catalyst_score=0.30 required_event_score=3.00 required_news_score=3.00" in caplog.text
    assert (
        "ALLOCATION_PROFILE_NO_CATALYST symbol=MOBX route=dynamic_replay dynamic_score=9.00 "
        "news_score=0.00 event_score=0.00 catalyst_score=0.00 catalyst_type=none "
        "catalyst_age_minutes=12 require_catalyst=true config_key=portfolio.dynamic_quality.enabled"
    ) in caplog.text
    assert (
        "threshold_keys=portfolio.dynamic_quality.min_catalyst_score,"
        "portfolio.dynamic_quality.min_event_score,portfolio.dynamic_quality.min_news_score"
    ) in caplog.text


def test_dynamic_event_news_fallback_threshold_is_configurable() -> None:
    cfg = _config()
    cfg["portfolio"]["dynamic_quality"] = {
        "allow_event_news_fallback": True,
        "min_catalyst_score": 0.3,
        "min_event_score": 1.0,
        "min_news_score": 1.0,
    }
    row = {
        "symbol": "TNGX",
        "dynamic_candidate": True,
        "catalyst_score": 0.1,
        "event_score": 1.0,
        "news_score": 1.0,
    }

    assert dynamic_quality_reject_reason(row, config=cfg) is None
    decision = dynamic_quality_decision(row, config=cfg)
    assert decision["passes"] is True
    assert decision["path"] == "event_score"


def test_dynamic_no_catalyst_high_quality_pure_momentum_passes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    row = {
        "symbol": "BTQ",
        "dynamic_candidate": True,
        "route": "dynamic_momentum",
        "score": 40.0,
        "gain_pct": 4.0,
        "relative_volume": 1.0,
        "news_score": 0.0,
        "event_score": 0.0,
        "catalyst_score": 0.0,
    }

    with caplog.at_level("INFO", logger="src.allocation_profile"):
        out = filter_allocator_candidates_for_profile(
            [row],
            config=_config(),
            portfolio=[],
            tracked={},
            equity=100_000.0,
        )

    assert [item["symbol"] for item in out] == ["BTQ"]
    decision = dynamic_quality_decision(row, config=_config())
    assert decision["passes"] is True
    assert decision["path"] == "pure_momentum"
    assert dynamic_quality_reject_reason(row, config=_config()) is None
    assert "DYNAMIC_ALLOCATOR_PURE_MOMENTUM_PASS symbol=BTQ score=40.00 rel=1.000 gain=4.000" in caplog.text


def test_dynamic_entry_eval_payload_scanner_fields_drive_pure_momentum_pass(
    caplog: pytest.LogCaptureFixture,
) -> None:
    row = {
        "symbol": "AAL",
        "dynamic_candidate": True,
        "dynamic_symbol": True,
        "is_dynamic": True,
        "route": "dynamic_momentum_override",
        "source": "dynamic_universe",
        "score": 1.2,
        "strength_eff": 1.2,
        "dynamic_score": 41.0,
        "scanner_score": 41.0,
        "signal_score": 41.0,
        "gain_pct": 4.2,
        "day_gain_pct": 4.2,
        "relative_volume": 1.1,
        "rel_volume": 1.1,
        "spread_pct": 0.14,
        "news_score": 0.0,
        "event_score": 0.0,
        "catalyst_score": 0.0,
        "entry_eval_final": True,
    }

    with caplog.at_level("INFO", logger="src.allocation_profile"):
        out = filter_allocator_candidates_for_profile(
            [row],
            config=_config(),
            portfolio=[],
            tracked={},
            equity=100_000.0,
        )

    assert [item["symbol"] for item in out] == ["AAL"]
    decision = dynamic_quality_decision(row, config=_config())
    assert decision["passes"] is True
    assert decision["path"] == "pure_momentum"
    assert (
        "DYNAMIC_ALLOCATOR_INPUT symbol=AAL route=dynamic_momentum_override "
        "source=dynamic_universe score=41.00 gain=4.200 rel=1.100 "
        "catalyst_score=0.00 news_score=0.00 event_score=0.00"
    ) in caplog.text
    assert "DYNAMIC_ALLOCATOR_PURE_MOMENTUM_PASS symbol=AAL score=41.00 rel=1.100 gain=4.200" in caplog.text


def test_scanner_selected_dynamic_override_bypasses_no_catalyst_after_entry_safety(
    caplog: pytest.LogCaptureFixture,
) -> None:
    row = {
        "symbol": "HIVE",
        "dynamic_candidate": True,
        "route": "dynamic_momentum_override",
        "source": "dynamic_universe",
        "scanner_selected": True,
        "entry_eval_final": True,
        "score": 2.0,
        "dynamic_score": 12.0,
        "scanner_score": 12.0,
        "signal_score": 12.0,
        "day_gain_pct": 4.2,
        "relative_volume": 0.8,
        "entry_eval_effective_min_rel_volume": 0.3,
        "price_above_vwap": True,
        "news_score": 0.0,
        "event_score": 0.0,
        "catalyst_score": 0.0,
    }

    with caplog.at_level("INFO", logger="src.allocation_profile"):
        out = filter_allocator_candidates_for_profile(
            [row],
            config=_config(),
            portfolio=[],
            tracked={},
            equity=100_000.0,
        )

    assert [item["symbol"] for item in out] == ["HIVE"]
    decision = dynamic_quality_decision(row, config=_config())
    assert decision["passes"] is True
    assert decision["path"] == "scanner_selected"
    assert "DYNAMIC_ALLOCATOR_CATALYST_BYPASS symbol=HIVE reason=scanner_selected" in caplog.text
    assert "DYNAMIC_ALLOCATOR_LOW_SCORE_ALLOWED symbol=HIVE score=12.00 reason=scanner_selected" in caplog.text


def test_dynamic_override_without_scanner_selected_still_requires_catalyst(
    caplog: pytest.LogCaptureFixture,
) -> None:
    row = {
        "symbol": "HIVE",
        "dynamic_candidate": True,
        "route": "dynamic_momentum_override",
        "source": "dynamic_universe",
        "day_gain_pct": 4.2,
        "relative_volume": 0.8,
        "entry_eval_effective_min_rel_volume": 0.3,
        "price_above_vwap": True,
        "news_score": 0.0,
        "event_score": 0.0,
        "catalyst_score": 0.0,
    }

    with caplog.at_level("INFO", logger="src.allocation_profile"):
        out = filter_allocator_candidates_for_profile(
            [row],
            config=_config(),
            portfolio=[],
            tracked={},
            equity=100_000.0,
        )

    assert out == []
    decision = dynamic_quality_decision(row, config=_config())
    assert decision["passes"] is False
    assert decision["reason"] == "no_catalyst"
    assert "DYNAMIC_ALLOCATOR_CATALYST_REQUIRED symbol=HIVE reason=no_catalyst" in caplog.text


def test_trend_long_no_catalyst_behavior_unchanged() -> None:
    row = {
        "symbol": "QQQ",
        "route": "trend_long",
        "score": 1.0,
        "day_gain_pct": 0.4,
        "relative_volume": 0.5,
        "news_score": 0.0,
        "event_score": 0.0,
        "catalyst_score": 0.0,
    }

    out = filter_allocator_candidates_for_profile(
        [row],
        config=_config(),
        portfolio=[],
        tracked={},
        equity=100_000.0,
    )

    assert [item["symbol"] for item in out] == ["QQQ"]


def test_dynamic_no_catalyst_low_score_fails_pure_momentum() -> None:
    row = {
        "symbol": "LOWQ",
        "dynamic_candidate": True,
        "route": "dynamic_momentum",
        "score": 20.0,
        "gain_pct": 4.0,
        "relative_volume": 1.0,
    }

    decision = dynamic_quality_decision(row, config=_config())

    assert decision["passes"] is False
    assert decision["reason"] == "no_catalyst"
    assert decision["pure_momentum_score_ok"] is False


def test_dynamic_no_catalyst_low_rvol_fails_pure_momentum(
    caplog: pytest.LogCaptureFixture,
) -> None:
    row = {
        "symbol": "LOWR",
        "dynamic_candidate": True,
        "route": "dynamic_momentum_override",
        "score": 40.0,
        "gain_pct": 4.0,
        "relative_volume": 0.5,
    }

    with caplog.at_level("INFO", logger="src.allocation_profile"):
        out = filter_allocator_candidates_for_profile(
            [row],
            config=_config(),
            portfolio=[],
            tracked={},
            equity=100_000.0,
        )

    assert out == []
    decision = dynamic_quality_decision(row, config=_config())
    assert decision["pure_momentum_rel_volume_ok"] is False
    assert dynamic_quality_reject_reason(row, config=_config()) == "no_catalyst"
    assert "DYNAMIC_ALLOCATOR_NO_CATALYST_REJECT symbol=LOWR score=40.00 rel=0.500 gain=4.000 required_score=35.00" in caplog.text


def test_dynamic_no_signal_still_rejects_with_event_news_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = _config()
    cfg["portfolio"]["dynamic_quality"] = {
        "allow_event_news_fallback": True,
        "min_event_score": 1.0,
        "min_news_score": 1.0,
    }

    with caplog.at_level("INFO", logger="src.allocation_profile"):
        assert dynamic_quality_reject_reason(
            {
                "symbol": "TNGX",
                "dynamic_candidate": True,
                "catalyst_score": 0,
                "event_score": 0,
                "news_score": 0,
            },
            config=cfg,
        ) == "no_catalyst"
    assert (
        "DYNAMIC_ALLOCATOR_INPUT symbol=TNGX route=n/a source=n/a score=0.00 gain=n/a rel=n/a "
        "catalyst_score=0.00 news_score=0.00 event_score=0.00"
    ) in caplog.text
    assert "DYNAMIC_ALLOCATOR_NO_CATALYST_REJECT symbol=TNGX missing_fields=score,rel,gain" in caplog.text


def test_existing_catalyst_score_threshold_behavior_remains() -> None:
    assert dynamic_quality_reject_reason(
        {"dynamic_candidate": True, "catalyst_score": 0.29, "event_score": 0, "news_score": 0},
        config=_config(),
    ) == "no_catalyst"
    assert dynamic_quality_reject_reason(
        {"dynamic_candidate": True, "catalyst_score": 0.3, "event_score": 0, "news_score": 0},
        config=_config(),
    ) is None
    decision = dynamic_quality_decision(
        {"dynamic_candidate": True, "catalyst_score": 0.3, "event_score": 0, "news_score": 0},
        config=_config(),
    )
    assert decision["path"] == "catalyst_score"


def test_non_dynamic_core_quality_behavior_unchanged() -> None:
    out = filter_allocator_candidates_for_profile(
        [{"symbol": "AAPL", "score": 40.0, "route": "dynamic_momentum", "relative_volume": 1.0, "gain_pct": 4.0}],
        config=_config(),
        portfolio=[{"symbol": "AAPL", "value": 70_000.0}],
        tracked={},
        equity=100_000.0,
    )

    assert [row["symbol"] for row in out] == ["AAPL"]
    assert "core_bucket_priority" not in out[0]


def test_valid_dynamic_candidate_survives_profile_filter_for_paper_thresholds() -> None:
    cfg = _config()
    cfg["portfolio"]["dynamic_quality"] = {
        "allow_event_news_fallback": True,
        "min_event_score": 1.0,
        "min_news_score": 1.0,
    }

    out = filter_allocator_candidates_for_profile(
        [
            {
                "symbol": "TNGX",
                "score": 9.0,
                "dynamic_candidate": True,
                "news_score": 1.0,
                "event_score": 1.0,
                "catalyst_score": 0.1,
            }
        ],
        config=cfg,
        portfolio=[],
        tracked={},
        equity=100_000.0,
    )

    assert [row["symbol"] for row in out] == ["TNGX"]


def test_dynamic_exposure_capped_at_25_pct() -> None:
    out = clip_actions_for_allocation_profile(
        [{"action": "buy", "symbol": "ABSI", "notional": 10_000.0}],
        candidates=[{"symbol": "ABSI", "dynamic_candidate": True, "news_score": 3}],
        portfolio=[{"symbol": "MOBX", "value": 23_000.0, "dynamic_candidate": True}],
        tracked={},
        equity=100_000.0,
        config=_config(),
        min_realloc_leg=100.0,
    )
    assert out == [{"action": "buy", "symbol": "ABSI", "notional": pytest.approx(2_000.0)}]


def test_single_dynamic_position_capped_at_4_pct() -> None:
    out = clip_actions_for_allocation_profile(
        [{"action": "buy", "symbol": "ABSI", "notional": 10_000.0}],
        candidates=[{"symbol": "ABSI", "dynamic_candidate": True, "news_score": 3}],
        portfolio=[],
        tracked={},
        equity=100_000.0,
        config=_config(),
        min_realloc_leg=100.0,
    )
    assert out == [{"action": "buy", "symbol": "ABSI", "notional": pytest.approx(4_000.0)}]


def test_catalyst_dynamic_buy_survives_when_profile_caps_allow() -> None:
    out = clip_actions_for_allocation_profile(
        [{"action": "buy", "symbol": "GOOGL", "notional": 1_312.50, "route": "premarket_catalyst_replay"}],
        candidates=[
            {
                "symbol": "GOOGL",
                "dynamic_candidate": True,
                "news_score": 4.0,
                "event_score": 4.0,
                "catalyst_score": 0.4,
                "route": "premarket_catalyst_replay",
            }
        ],
        portfolio=[],
        tracked={},
        equity=100_000.0,
        config=_config(),
        min_realloc_leg=1_200.0,
    )

    assert out == [
        {
            "action": "buy",
            "symbol": "GOOGL",
            "notional": pytest.approx(1_312.50),
            "route": "premarket_catalyst_replay",
        }
    ]


def test_dynamic_buy_above_min_survives_single_dynamic_cap_below_min(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = _config()
    config["portfolio"]["capital_allocator"].update(
        {"max_single_order_notional_pct": 0.10, "max_single_order_notional": 5_000.0}
    )
    with caplog.at_level("INFO", logger="src.allocation_profile"):
        out = clip_actions_for_allocation_profile(
            [{"action": "buy", "symbol": "QQQ", "notional": 1_312.50, "route": "dynamic_replay"}],
            candidates=[
                {
                    "symbol": "QQQ",
                    "dynamic_candidate": True,
                    "news_score": 4.0,
                    "event_score": 4.0,
                    "catalyst_score": 0.4,
                    "route": "dynamic_replay",
                }
            ],
            portfolio=[],
            tracked={},
            equity=28_000.0,
            config=config,
            min_realloc_leg=1_200.0,
        )

    assert out == [{"action": "buy", "symbol": "QQQ", "notional": 1_200.0, "route": "dynamic_replay"}]
    assert "ALLOCATION_PROFILE_CLIP_DEBUG symbol=QQQ action=buy route=dynamic_replay" in caplog.text
    assert "requested_notional=1312.50" in caplog.text
    assert "raw_clipped_notional=1120.00" in caplog.text
    assert "clipped_notional=1200.00" in caplog.text
    assert "min_realloc_leg=1200.00" in caplog.text
    assert "dynamic_cap=1120.00" in caplog.text
    assert "single_dynamic_cap=1120.00" in caplog.text
    assert "single_order_cap=2800.00" in caplog.text
    assert "gross_headroom=7000.00" in caplog.text
    assert "final_post_planner_notional=1200.00" in caplog.text
    assert "cap_floor_applied=true" in caplog.text
    assert "ALLOCATION_PROFILE_CLIP_REASON symbol=QQQ" not in caplog.text


def test_no_catalyst_dynamic_buy_still_removed_by_profile_clip(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("INFO", logger="src.allocation_profile"):
        out = clip_actions_for_allocation_profile(
            [{"action": "buy", "symbol": "TNGX", "notional": 500.0, "route": "dynamic_replay"}],
            candidates=[{"symbol": "TNGX", "dynamic_candidate": True, "route": "dynamic_replay"}],
            portfolio=[],
            tracked={},
            equity=10_000.0,
            config=_config(),
            min_realloc_leg=500.0,
        )

    assert out == []
    assert "ALLOCATION_PROFILE_CLIP_REASON symbol=TNGX action=buy route=dynamic_replay" in caplog.text
    assert "is_dynamic=true catalyst_score=0.00 event_score=0.00 news_score=0.00" in caplog.text
    assert "profile_rule=min_realloc_leg_after_single_dynamic_cap" in caplog.text


def test_core_rebuild_buy_unchanged_by_dynamic_profile_clip() -> None:
    action = {"action": "buy", "symbol": "ORCL", "notional": 1_200.0, "route": "core_rebuild"}

    out = clip_actions_for_allocation_profile(
        [action],
        candidates=[{"symbol": "ORCL", "route": "core_rebuild", "core_rebuild": True}],
        portfolio=[],
        tracked={},
        equity=28_000.0,
        config=_config(),
        min_realloc_leg=1_200.0,
    )

    assert out == [action]


def test_dynamic_concurrent_positions_capped_at_six() -> None:
    portfolio = [
        {"symbol": f"DYN{i}", "value": 1_000.0, "dynamic_candidate": True}
        for i in range(6)
    ]
    out = clip_actions_for_allocation_profile(
        [{"action": "buy", "symbol": "NEW", "notional": 1_000.0}],
        candidates=[{"symbol": "NEW", "dynamic_candidate": True, "news_score": 3}],
        portfolio=portfolio,
        tracked={},
        equity=100_000.0,
        config=_config(),
        min_realloc_leg=100.0,
    )
    assert out == []


def test_cash_reserve_preserved(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO", logger="src.allocation_profile"):
        assert deployable_cash_after_reserve(cash=10_000.0, equity=100_000.0, config=_config()) == 0.0
    assert deployable_cash_after_reserve(cash=16_000.0, equity=100_000.0, config=_config()) == pytest.approx(6_000.0)
    assert "CASH_RESERVE_BLOCKED reason=target_cash_pct" in caplog.text


def test_etf_fallback_disabled_excludes_leveraged_etfs(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO", logger="src.allocation_profile"):
        out = filter_allocator_candidates_for_profile(
            [{"symbol": "SOXL", "score": 99.0, "dynamic_candidate": True, "news_score": 9}],
            config=_config(),
            portfolio=[],
            tracked={},
            equity=100_000.0,
        )
    assert out == []
    assert "ALLOCATOR_SKIP_ETF symbol=SOXL reason=etf_excluded" in caplog.text


def test_dynamic_lockout_after_two_stop_losses(caplog: pytest.LogCaptureFixture) -> None:
    engine = SimpleNamespace(dynamic_stop_loss_count_today=2)
    assert dynamic_lockout_reason(engine, 100_000.0) == "stop_loss_count"
    with caplog.at_level("INFO", logger="src.allocation_profile"):
        out = filter_allocator_candidates_for_profile(
            [{"symbol": "ABSI", "score": 9.0, "dynamic_candidate": True, "news_score": 3}],
            config=_config(),
            portfolio=[],
            tracked={},
            equity=100_000.0,
            engine=engine,
        )
    assert out == []
    assert "DYNAMIC_LOCKOUT reason=stop_loss_count" in caplog.text


def test_dynamic_lockout_after_realized_dynamic_loss() -> None:
    engine = SimpleNamespace(dynamic_realized_loss_today=1_500.0)
    assert dynamic_lockout_reason(engine, 100_000.0) == "realized_loss_limit"
    engine_pnl = SimpleNamespace(dynamic_realized_pnl_today=-1_500.0)
    assert dynamic_lockout_reason(engine_pnl, 100_000.0) == "realized_loss_limit"


def test_existing_sell_actions_and_risk_controls_unchanged() -> None:
    out = clip_actions_for_allocation_profile(
        [
            {"action": "sell", "symbol": "MOBX", "notional": 2_000.0},
            {"action": "buy", "symbol": "ABSI", "notional": 2_000.0},
        ],
        candidates=[{"symbol": "ABSI", "dynamic_candidate": True, "news_score": 3}],
        portfolio=[
            {"symbol": f"DYN{i}", "value": 1_000.0, "dynamic_candidate": True}
            for i in range(6)
        ],
        tracked={},
        equity=100_000.0,
        config=_config(),
        min_realloc_leg=100.0,
    )
    assert out == [{"action": "sell", "symbol": "MOBX", "notional": 2_000.0}]
