"""Regression tests for live trading cycle control flow."""

from __future__ import annotations

import ast
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_dispatch_dynamic_rvol_diagnostics_logs_full_metadata(caplog: pytest.LogCaptureFixture) -> None:
    from src.capital_allocator_loop import _log_dispatch_dynamic_rvol_diagnostics

    candidate = {
        "symbol": "INTC",
        "route": "dynamic_momentum_override",
        "source": "dynamic_universe",
        "dynamic_candidate": True,
        "relative_volume": 0.882,
        "news_score": 8,
        "catalyst_score": 0.9,
        "event_score": 7,
        "catalyst_type": "news",
        "catalyst_age_minutes": 12,
        "scanner_effective_min_rel_volume": 0.35,
        "decision_allowed": True,
    }

    with caplog.at_level(logging.INFO, logger="src.capital_allocator_loop"):
        meta = _log_dispatch_dynamic_rvol_diagnostics(
            symbol="INTC",
            candidate=candidate,
            route="dynamic_momentum_override",
            source="dynamic_universe",
            rel_volume=0.882,
            base_min_rel_volume=1.0,
            override_active=True,
            dispatch_result="allowed",
            dispatch_reason="ok",
        )

    assert meta["news_score"] == 8.0
    assert meta["catalyst_score"] == 0.9
    assert meta["effective_min_rel_volume"] == 0.35
    assert meta["missing_fields"] == []
    assert "DISPATCH_DYNAMIC_RVOL_CHECK symbol=INTC" in caplog.text
    assert "scanner_effective_min_rel_volume=0.350" in caplog.text
    assert "override_active=true" in caplog.text
    assert "DISPATCH_DYNAMIC_METADATA_MISSING symbol=INTC" not in caplog.text


def test_dispatch_dynamic_rvol_diagnostics_logs_missing_metadata(caplog: pytest.LogCaptureFixture) -> None:
    from src.capital_allocator_loop import _log_dispatch_dynamic_rvol_diagnostics

    candidate = {
        "symbol": "AMD",
        "route": "dynamic_momentum_override",
        "source": "dynamic_universe",
        "dynamic_candidate": True,
        "relative_volume": 0.4,
    }

    with caplog.at_level(logging.INFO, logger="src.capital_allocator_loop"):
        meta = _log_dispatch_dynamic_rvol_diagnostics(
            symbol="AMD",
            candidate=candidate,
            route="dynamic_momentum_override",
            source="dynamic_universe",
            rel_volume=0.4,
            base_min_rel_volume=1.0,
            override_active=False,
            dispatch_result="skipped",
            dispatch_reason="dynamic_relative_volume",
        )

    assert set(meta["missing_fields"]) == {
        "news_score",
        "catalyst_score",
        "event_score",
        "effective_min_rel_volume",
    }
    assert "DISPATCH_DYNAMIC_RVOL_CHECK symbol=AMD" in caplog.text
    assert "dispatch_result=skipped dispatch_reason=dynamic_relative_volume" in caplog.text
    assert "DISPATCH_DYNAMIC_METADATA_MISSING symbol=AMD" in caplog.text
    assert "missing_fields=news_score,catalyst_score,event_score,effective_min_rel_volume" in caplog.text


def test_log_core_skip_reason_only_logs_core_symbols(caplog: pytest.LogCaptureFixture) -> None:
    from src.app.live_cycle import _log_core_skip_reason

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        assert _log_core_skip_reason("AAPL", "trend filter", ["AAPL", "MSFT"]) is True
        assert _log_core_skip_reason("CMND", "trend filter", ["AAPL", "MSFT"]) is False

    assert "CORE_SKIP_REASON symbol=AAPL reason=trend filter" in caplog.text
    assert "CORE_SKIP_REASON symbol=CMND" not in caplog.text


def test_dynamic_early_watch_promotes_when_alignment_appears(caplog: pytest.LogCaptureFixture) -> None:
    from src.app import live_cycle

    live_cycle._DYNAMIC_TIMING_STATE.clear()
    cfg = {
        "dynamic_universe": {
            "early_watch_enabled_live": True,
            "early_watch_gain_min_pct": 8.0,
            "early_watch_gain_max_pct": 12.0,
        }
    }
    now = datetime(2026, 6, 26, 9, 42, tzinfo=ZoneInfo("America/New_York"))

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        live_cycle._dynamic_timing_observe_scan_candidate(
            symbol="AIIO",
            gain_pct=9.5,
            price=8.2,
            rel_volume=1.4,
            vwap_above=True,
            config=cfg,
            is_live=True,
            now=now,
            eligible=False,
            eligible_reason="need 5m breakout",
        )
        live_cycle._dynamic_timing_observe_scan_candidate(
            symbol="AIIO",
            gain_pct=10.8,
            price=8.5,
            rel_volume=1.8,
            vwap_above=True,
            config=cfg,
            is_live=True,
            now=now,
            eligible=True,
            eligible_reason="scanner_selected",
        )

    assert "DYNAMIC_FIRST_SEEN symbol=AIIO gain_pct=9.50" in caplog.text
    assert "DYNAMIC_EARLY_WATCH symbol=AIIO gain_pct=9.50 reason=watch_for_alignment" in caplog.text
    assert "DYNAMIC_EARLY_WATCH_PROMOTED symbol=AIIO gain_pct=10.80 reason=alignment_confirmed" in caplog.text
    assert "DYNAMIC_FIRST_ELIGIBLE symbol=AIIO gain_pct=10.80 reason=scanner_selected" in caplog.text


def test_dynamic_latency_logs_scan_enqueue_eval_allocator_timings(caplog: pytest.LogCaptureFixture) -> None:
    from src.app import live_cycle

    live_cycle._DYNAMIC_TIMING_STATE.clear()
    live_cycle._DYNAMIC_TIMING_STATE["FCEL"] = {"scan_ms": 1000}

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        live_cycle._DYNAMIC_TIMING_STATE["FCEL"]["enqueue_ms"] = 1500
        live_cycle._DYNAMIC_TIMING_STATE["FCEL"]["eval_ms"] = 1800
        live_cycle._DYNAMIC_TIMING_STATE["FCEL"]["allocator_ms"] = 2200
        live_cycle._DYNAMIC_TIMING_STATE["FCEL"]["dispatch_ms"] = 2600
        live_cycle._log_dynamic_latency("FCEL")

    assert "DYNAMIC_LATENCY symbol=FCEL" in caplog.text
    assert "scan_to_enqueue_ms=500" in caplog.text
    assert "enqueue_to_eval_ms=300" in caplog.text
    assert "eval_to_allocator_ms=400" in caplog.text
    assert "allocator_to_dispatch_ms=400" in caplog.text
    assert "total_ms=1600" in caplog.text


def test_paper_option_route_observability_helpers(caplog: pytest.LogCaptureFixture) -> None:
    from src.app.live_cycle import (
        _log_option_route_check,
        _log_option_route_skipped,
        _option_route_skip_reason_from_text,
        _options_route_observability_active,
        _paper_option_route_observable,
        _paper_option_underlying_allowed,
    )

    cfg = {"options": {"allowed_underlyings": ["QQQ"]}}
    row = {
        "symbol": "QQQ",
        "route": "news_catalyst",
        "entry_eval_final": False,
        "news_score": 3.0,
        "event_score": 0.0,
        "catalyst_score": 0.0,
        "relative_volume": 0.1,
    }

    assert _paper_option_route_observable(row) is True
    assert _paper_option_route_observable({"symbol": "XLF", "entry_eval_final": True}) is True
    assert _paper_option_underlying_allowed(cfg, "QQQ") is True
    assert _paper_option_underlying_allowed(cfg, "XLF") is False
    assert _option_route_skip_reason_from_text(
        "option entry cooldown 4/10 min",
        (),
    ) == "cooldown"
    assert _option_route_skip_reason_from_text(
        "underlying not allowed for options",
        ("route_failed",),
    ) == "underlying_not_allowed"
    assert _option_route_skip_reason_from_text("no contract found", ()) == "no_contract_found"
    assert _option_route_skip_reason_from_text("selector rejected all", ()) == "selector_rejected_all"
    assert _options_route_observability_active(cfg) is False
    assert _options_route_observability_active({"options": {"enabled": True, "mode": "paper_only"}}) is True

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        _log_option_route_check("qqq", lane="entry_eval", row_tl=row, entry_eval_final=False)
        _log_option_route_skipped(
            "qqq",
            lane="entry_eval",
            reason="entry_eval_false",
            detail="spread",
            row_tl=row,
        )

    assert "OPTION_ROUTE_CHECK symbol=QQQ lane=entry_eval route=news_catalyst" in caplog.text
    assert "news_score=3.00" in caplog.text
    assert (
        "OPTION_ROUTE_SKIPPED symbol=QQQ route=news_catalyst underlying=QQQ "
        "lane=entry_eval reason=entry_eval_false"
    ) in caplog.text


def test_option_route_observability_allowed_underlying_logs_check(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.app.live_cycle import _log_option_route_check, _paper_option_underlying_allowed

    cfg = {"options": {"enabled": True, "mode": "paper_only", "allowed_underlyings": ["QQQ"]}}
    row = {"symbol": "QQQ", "sym_u": "QQQ", "route": "trend_long", "entry_eval_final": True}

    assert _paper_option_underlying_allowed(cfg, "QQQ") is True
    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        _log_option_route_check("QQQ", lane="ranked_or_direct", row_tl=row)

    assert "OPTION_ROUTE_CHECK symbol=QQQ lane=ranked_or_direct route=trend_long" in caplog.text


def test_option_route_observability_disallowed_underlying_logs_skip(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.app.live_cycle import _log_option_route_skipped, _paper_option_underlying_allowed

    cfg = {"options": {"enabled": True, "mode": "paper_only", "allowed_underlyings": ["QQQ"]}}
    row = {"symbol": "XLF", "sym_u": "XLF", "route": "trend_long", "entry_eval_final": True}

    assert _paper_option_underlying_allowed(cfg, "XLF") is False
    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        _log_option_route_skipped(
            "XLF",
            lane="ranked_or_direct",
            reason="underlying_not_allowed",
            detail="allowed_underlyings",
            row_tl=row,
        )

    assert (
        "OPTION_ROUTE_SKIPPED symbol=XLF route=trend_long underlying=XLF "
        "lane=ranked_or_direct reason=underlying_not_allowed detail=allowed_underlyings"
    ) in caplog.text


def test_option_route_observability_entry_eval_false_logs_skip(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.app.live_cycle import _log_option_route_check, _log_option_route_skipped

    row = {
        "symbol": "QQQ",
        "sym_u": "QQQ",
        "route": "news_catalyst",
        "entry_eval_final": False,
        "news_score": 4.0,
    }

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        _log_option_route_check("QQQ", lane="entry_eval", row_tl=row, entry_eval_final=False)
        _log_option_route_skipped(
            "QQQ",
            lane="entry_eval",
            reason="entry_eval_false",
            detail="spread",
            row_tl=row,
        )

    assert "OPTION_ROUTE_CHECK symbol=QQQ lane=entry_eval route=news_catalyst" in caplog.text
    assert (
        "OPTION_ROUTE_SKIPPED symbol=QQQ route=news_catalyst underlying=QQQ "
        "lane=entry_eval reason=entry_eval_false detail=spread"
    ) in caplog.text


def test_options_route_observability_does_not_enable_paper_order_routing() -> None:
    from src.app.live_cycle import _options_route_observability_active
    from src.live.options_paper import paper_only_options_active

    cfg = {
        "broker": {"paper": True},
        "options": {"enabled": True, "mode": "long_premium_only", "new_entries_enabled": True},
    }
    broker = SimpleNamespace(paper=True)

    assert _options_route_observability_active(cfg, broker) is True
    assert paper_only_options_active(cfg) is False


def test_allocator_entry_eval_followup_logs_required_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.app.live_cycle import (
        _allocator_entry_eval_followup,
        _entry_eval_allocator_score,
    )

    candidates: list[dict] = []
    row = {"symbol": "XLF"}
    trend_score = _entry_eval_allocator_score(
        route="trend_long",
        final=True,
        trend=True,
        pullback=True,
        momentum=True,
        volatility=True,
    )

    assert trend_score > 1.0

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        assert _allocator_entry_eval_followup(
            symbol="xlf",
            route="trend_long",
            final=True,
            reason="ok",
            allocator_on=True,
            stage="entry_eval",
            action="enqueue",
            candidates=candidates,
            row=row,
            score=trend_score,
        )
        assert _allocator_entry_eval_followup(
            symbol="iwm",
            route="trend_long",
            final=True,
            reason="missing_ohlcv",
            allocator_on=True,
            stage="entry_eval",
            action="skip",
        )
        assert not _allocator_entry_eval_followup(
            symbol="spy",
            route="trend_long",
            final=False,
            reason="entry_eval_false",
            allocator_on=True,
            stage="entry_eval",
            action="skip",
        )

    assert candidates == [row]
    assert (
        f"ALLOCATOR_ENQUEUE symbol=XLF route=trend_long reason=ok score={trend_score:.4f} "
        "allocator_on=true final=true stage=entry_eval"
    ) in caplog.text
    assert (
        "ENTRY_TO_ALLOCATOR_FOLLOWUP_END symbol=XLF route=trend_long result=enqueue stage=entry_eval"
    ) in caplog.text
    assert (
        "ENTRY_TO_ALLOCATOR_FOLLOWUP_SKIPPED symbol=IWM reason=missing_ohlcv "
        "route=trend_long stage=entry_eval"
    ) in caplog.text
    assert (
        "ALLOCATOR_ENQUEUE_SKIP symbol=IWM route=trend_long reason=missing_ohlcv "
        "allocator_on=true final=true stage=entry_eval"
    ) in caplog.text
    assert (
        "ALLOCATOR_APPEND_SKIPPED symbol=IWM route=trend_long reason=missing_ohlcv "
        "allocator_on=true final=true stage=entry_eval"
    ) in caplog.text
    assert (
        "ENTRY_TO_ALLOCATOR_FOLLOWUP_END symbol=IWM route=trend_long "
        "result=skipped reason=missing_ohlcv stage=entry_eval"
    ) in caplog.text
    assert "ALLOCATOR_ENQUEUE_SKIP symbol=SPY" not in caplog.text


class _CoreRebuildBroker:
    def __init__(self, *, spread: float = 0.2, avg_volume: float = 1_000_000.0) -> None:
        self.spread = spread
        self.avg_volume = avg_volume

    def get_latest_quote(self, symbol: str):
        return SimpleNamespace(spread_pct=self.spread)

    def get_avg_volume(self, symbol: str) -> float:
        return self.avg_volume


class _EntryTerminalRecorder:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def record_entry_terminal_outcome(self, **kwargs) -> None:
        self.rows.append(dict(kwargs))


def test_live_cycle_records_entry_terminal_outcome_helper(caplog: pytest.LogCaptureFixture) -> None:
    from src.app.live_cycle import _record_entry_terminal_outcome_live

    recorder = _EntryTerminalRecorder()

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        _record_entry_terminal_outcome_live(
            store=recorder,
            user_id="live_bot",
            symbol="payo",
            route="dynamic_universe",
            stage="skipped_with_reason",
            reason="low regime top-3 filter",
            payload={"dynamic_candidate": True, "entry_eval_final": True},
            ts=datetime(2026, 6, 9, 14, 30, tzinfo=timezone.utc),
        )

    assert recorder.rows == [
        {
            "user_id": "live_bot",
            "symbol": "PAYO",
            "route": "dynamic_universe",
            "stage": "skipped_with_reason",
            "reason": "low regime top-3 filter",
            "payload": {"dynamic_candidate": True, "entry_eval_final": True},
            "ts": "2026-06-09T14:30:00+00:00",
        }
    ]
    assert (
        "ENTRY_TERMINAL_OUTCOME symbol=PAYO stage=skipped_with_reason "
        "reason=low regime top-3 filter route=dynamic_universe"
    ) in caplog.text


def test_low_regime_dynamic_drop_can_record_terminal_reason() -> None:
    from src.app.live_cycle import (
        _record_entry_terminal_outcome_live,
        _restrict_low_regime_new_stock_entries,
    )

    recorder = _EntryTerminalRecorder()
    rows = [
        {
            "symbol": sym,
            "sym_u": sym,
            "source": "dynamic_universe",
            "route": "dynamic_universe",
            "dynamic_candidate": True,
            "entry_eval_final": True,
            "entry_regime_score": 2,
            "strength_eff": strength,
            "score": strength,
        }
        for sym, strength in (
            ("KEEP1", 0.90),
            ("KEEP2", 0.80),
            ("KEEP3", 0.70),
            ("PAYO", 0.10),
        )
    ]
    by_symbol = {row["symbol"]: row for row in rows}

    def log_drop(sym: str, why: str) -> None:
        row = by_symbol[sym]
        _record_entry_terminal_outcome_live(
            store=recorder,
            user_id="live_bot",
            symbol=sym,
            route=row["route"],
            stage="skipped_with_reason",
            reason=why,
            payload={
                "dynamic_candidate": row["dynamic_candidate"],
                "entry_eval_final": row["entry_eval_final"],
                "profile_rule": "low_regime_new_stock_entry_top_n",
                "strength_eff": row["strength_eff"],
            },
            ts=datetime(2026, 6, 9, 14, 30, tzinfo=timezone.utc),
        )

    kept = _restrict_low_regime_new_stock_entries(
        rows,
        config={
            "execution": {
                "low_regime_top_n_stock_entries": 3,
                "low_regime_top_n_regime_score_max": 3,
            }
        },
        sector_etfs=frozenset(),
        ranking_mode="strength",
        log_drop=log_drop,
    )

    assert {row["symbol"] for row in kept} == {"KEEP1", "KEEP2", "KEEP3"}
    assert recorder.rows[0]["symbol"] == "PAYO"
    assert recorder.rows[0]["stage"] == "skipped_with_reason"
    assert recorder.rows[0]["payload"]["entry_eval_final"] is True


def test_entry_allocator_stage_and_reconcile_accounting(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.app.live_cycle import (
        _log_entry_allocator_reconcile,
        _record_entry_allocator_stage_for_rows,
    )

    recorder = _EntryTerminalRecorder()
    appended: set[str] = set()
    allocator_input: set[str] = set()
    rows = [
        {
            "symbol": "CPNG",
            "sym_u": "CPNG",
            "route": "dynamic_momentum_override",
            "source": "dynamic_universe",
            "dynamic_candidate": True,
            "is_dynamic": True,
            "entry_eval_final": True,
            "notional": 1312.50,
            "score": 1.45,
            "relative_volume": 2.67,
            "rel_volume": 2.67,
            "gain_pct": 31.4,
            "day_gain_pct": 31.4,
            "dynamic_score": 39.1,
            "scanner_score": 39.1,
            "signal_score": 39.1,
            "catalyst_score": 0.0,
            "news_score": 0.0,
            "event_score": 0.0,
        },
        {
            "symbol": "XLE",
            "sym_u": "XLE",
            "route": "trend_long",
            "source": "trend_long",
            "entry_eval_final": True,
            "notional": 1312.50,
            "score": 1.45,
        },
    ]

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        _record_entry_allocator_stage_for_rows(
            rows,
            stage="allocator_appended",
            reason="queued_for_allocator",
            store=recorder,
            user_id="paper_bot",
            ts=datetime(2026, 6, 11, 16, 0, tzinfo=timezone.utc),
            symbols_out=appended,
        )
        _record_entry_allocator_stage_for_rows(
            rows,
            stage="allocator_input",
            reason="allocator_pass_start",
            store=recorder,
            user_id="paper_bot",
            ts=datetime(2026, 6, 11, 16, 0, tzinfo=timezone.utc),
            symbols_out=allocator_input,
        )
        missing = _log_entry_allocator_reconcile(
            final_true={
                "CPNG": {"route": "dynamic_momentum_override", "reason": "ok"},
                "XLE": {"route": "trend_long", "reason": "ok"},
            },
            appended=appended,
            allocator_input=allocator_input,
        )

    assert missing == set()
    assert appended == {"CPNG", "XLE"}
    assert allocator_input == {"CPNG", "XLE"}
    assert [row["stage"] for row in recorder.rows] == [
        "allocator_appended",
        "allocator_appended",
        "allocator_input",
        "allocator_input",
    ]
    cpng_payload = recorder.rows[0]["payload"]
    assert cpng_payload["route"] == "dynamic_momentum_override"
    assert cpng_payload["source"] == "dynamic_universe"
    assert cpng_payload["is_dynamic"] is True
    assert cpng_payload["relative_volume"] == pytest.approx(2.67)
    assert cpng_payload["rel_volume"] == pytest.approx(2.67)
    assert cpng_payload["gain_pct"] == pytest.approx(31.4)
    assert cpng_payload["day_gain_pct"] == pytest.approx(31.4)
    assert cpng_payload["dynamic_score"] == pytest.approx(39.1)
    assert cpng_payload["scanner_score"] == pytest.approx(39.1)
    assert cpng_payload["signal_score"] == pytest.approx(39.1)
    assert (
        "ENTRY_ALLOCATOR_RECONCILE final_true=2 appended=2 input=2 submitted=0 missing=0"
        in caplog.text
    )


def test_entry_allocator_reconcile_logs_missing_symbol(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.app.live_cycle import _log_entry_allocator_reconcile

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        missing = _log_entry_allocator_reconcile(
            final_true={
                "AAL": {"route": "dynamic_momentum_override", "reason": "ok"},
                "JPM": {"route": "trend_long", "reason": "ok"},
            },
            appended={"JPM"},
            allocator_input=set(),
            skipped=set(),
        )

    assert missing == {"AAL"}
    assert (
        "ENTRY_ALLOCATOR_RECONCILE final_true=2 appended=1 input=0 submitted=0 missing=1"
        in caplog.text
    )
    assert (
        "ENTRY_ALLOCATOR_MISSING symbol=AAL route=dynamic_momentum_override reason=ok"
        in caplog.text
    )


def _core_rebuild_config(**overrides) -> dict:
    cfg = {
        "portfolio": {
            "target_core_stock_pct": 65,
            "target_dynamic_pct": 25,
            "target_cash_pct": 10,
        },
        "allocation": {
            "core_rebuild": {
                "enabled": True,
                "underweight_threshold_pct": 10,
                "max_rebuild_notional_pct": 2,
                "max_symbols_per_cycle": 2,
                "require_non_bearish_regime": True,
                "require_spread_ok": True,
                "allow_when_below_mas": True,
                "min_cash_reserve_pct": 10,
            }
        },
    }
    cfg["allocation"]["core_rebuild"].update(overrides)
    return cfg


def test_allow_core_rebuild_buys_defaults_false() -> None:
    from src.app.live_cycle import _allow_core_rebuild_buys

    assert _allow_core_rebuild_buys({"allocation": {}}) is False
    assert (
        _allow_core_rebuild_buys({"allocation": {"allow_core_rebuild_buys": True}})
        is True
    )


def test_core_rebuild_underweight_core_adds_candidates(caplog: pytest.LogCaptureFixture) -> None:
    from src.app.live_cycle import build_core_rebuild_candidates

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        rows = build_core_rebuild_candidates(
            config=_core_rebuild_config(allow_same_day_rebuild_after_sell=True),
            core_symbols=["AAPL", "MSFT", "CMND"],
            dynamic_symbols=[],
            existing_candidates=[],
            positions=[],
            equity=10_000.0,
            cash=5_000.0,
            broker=_CoreRebuildBroker(),
            open_order_symbols=[],
            max_positions=10,
            regime_score=3,
            regime_condition="neutral",
            spread_cap_fn=lambda _sym: 1.0,
        )

    assert [row["symbol"] for row in rows] == ["AAPL", "MSFT"]
    assert all(row["route"] == "core_rebuild" for row in rows)
    assert all(row["reason"] == "allocation_underweight" for row in rows)
    assert all(row["candidate_notional_cap"] == pytest.approx(200.0) for row in rows)
    assert "CORE_REBUILD_CANDIDATE symbol=AAPL reason=allocation_underweight" in caplog.text
    assert "CORE_REBUILD_SELECTED symbol=AAPL" in caplog.text
    assert "CORE_REBUILD_SUMMARY target_core=65.00 actual_core=0.00 added=2" in caplog.text


@pytest.mark.parametrize("symbol", ["PLTR", "AVGO"])
def test_core_rebuild_rejects_below_ma_entry_eval_not_final_by_default(
    symbol: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.app.live_cycle import build_core_rebuild_candidates

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        rows = build_core_rebuild_candidates(
            config=_core_rebuild_config(allow_same_day_rebuild_after_sell=True),
            core_symbols=[symbol],
            dynamic_symbols=[],
            existing_candidates=[],
            positions=[],
            equity=100_000.0,
            cash=50_000.0,
            broker=_CoreRebuildBroker(),
            open_order_symbols=[],
            max_positions=10,
            regime_score=3,
            regime_condition="neutral",
            spread_cap_fn=lambda _sym: 1.0,
            entry_eval_final_symbols=[],
        )

    assert rows == []
    assert f"CORE_REBUILD_REJECT symbol={symbol} reason=entry_eval_not_final" in caplog.text
    assert f"CORE_REBUILD_SELECTED symbol={symbol}" not in caplog.text


def test_core_rebuild_allows_entry_eval_final_symbol(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.app.live_cycle import _allow_core_rebuild_buys, build_core_rebuild_candidates

    cfg = _core_rebuild_config(allow_same_day_rebuild_after_sell=True)
    cfg["allocation"]["allow_core_rebuild_buys"] = True
    assert _allow_core_rebuild_buys(cfg) is True

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        rows = build_core_rebuild_candidates(
            config=cfg,
            core_symbols=["PLTR"],
            dynamic_symbols=[],
            existing_candidates=[],
            positions=[],
            equity=100_000.0,
            cash=50_000.0,
            broker=_CoreRebuildBroker(),
            open_order_symbols=[],
            max_positions=10,
            regime_score=3,
            regime_condition="neutral",
            spread_cap_fn=lambda _sym: 1.0,
            entry_eval_final_symbols=["PLTR"],
        )

    assert [row["symbol"] for row in rows] == ["PLTR"]
    assert rows[0]["route"] == "core_rebuild"
    assert "CORE_REBUILD_CANDIDATE symbol=PLTR reason=allocation_underweight" in caplog.text
    assert "CORE_REBUILD_SELECTED symbol=PLTR" in caplog.text
    assert "CORE_REBUILD_REJECT symbol=PLTR reason=entry_eval_not_final" not in caplog.text


def test_core_rebuild_rejects_entry_eval_exception_even_with_bypass(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.app.live_cycle import build_core_rebuild_candidates

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        rows = build_core_rebuild_candidates(
            config=_core_rebuild_config(
                allow_same_day_rebuild_after_sell=True,
                allow_core_rebuild_bypass=True,
            ),
            core_symbols=["CRWD"],
            dynamic_symbols=[],
            existing_candidates=[],
            positions=[],
            equity=100_000.0,
            cash=50_000.0,
            broker=_CoreRebuildBroker(),
            open_order_symbols=[],
            max_positions=10,
            regime_score=3,
            regime_condition="neutral",
            spread_cap_fn=lambda _sym: 1.0,
            entry_eval_final_symbols=[],
            entry_eval_exception_symbols=["CRWD"],
        )

    assert rows == []
    assert "CORE_REBUILD_REJECT symbol=CRWD reason=entry_eval_exception" in caplog.text
    assert "CORE_REBUILD_SELECTED symbol=CRWD" not in caplog.text


def test_core_rebuild_bypass_flag_allows_entry_eval_not_final(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.app.live_cycle import build_core_rebuild_candidates

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        rows = build_core_rebuild_candidates(
            config=_core_rebuild_config(
                allow_same_day_rebuild_after_sell=True,
                allow_core_rebuild_bypass=True,
            ),
            core_symbols=["PLTR"],
            dynamic_symbols=[],
            existing_candidates=[],
            positions=[],
            equity=100_000.0,
            cash=50_000.0,
            broker=_CoreRebuildBroker(),
            open_order_symbols=[],
            max_positions=10,
            regime_score=3,
            regime_condition="neutral",
            spread_cap_fn=lambda _sym: 1.0,
            entry_eval_final_symbols=[],
        )

    assert [row["symbol"] for row in rows] == ["PLTR"]
    assert rows[0]["route"] == "core_rebuild"
    assert rows[0]["entry_eval_final"] is False
    assert "CORE_REBUILD_CANDIDATE symbol=PLTR reason=allocation_underweight" in caplog.text
    assert "CORE_REBUILD_SELECTED symbol=PLTR" in caplog.text
    assert "CORE_REBUILD_REJECT symbol=PLTR reason=entry_eval_not_final" not in caplog.text


def test_core_rebuild_strong_rebalance_flag_does_not_bypass_entry_eval(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.app.live_cycle import build_core_rebuild_candidates

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        rows = build_core_rebuild_candidates(
            config=_core_rebuild_config(
                allow_same_day_rebuild_after_sell=True,
                strong_existing_core_rebalance_enabled=True,
            ),
            core_symbols=["AVGO"],
            dynamic_symbols=[],
            existing_candidates=[],
            positions=[],
            equity=100_000.0,
            cash=50_000.0,
            broker=_CoreRebuildBroker(),
            open_order_symbols=[],
            max_positions=10,
            regime_score=3,
            regime_condition="neutral",
            spread_cap_fn=lambda _sym: 1.0,
            entry_eval_final_symbols=[],
        )

    assert rows == []
    assert "CORE_REBUILD_REJECT symbol=AVGO reason=entry_eval_not_final" in caplog.text
    assert "CORE_REBUILD_SELECTED symbol=AVGO" not in caplog.text


def test_core_rebuild_no_candidates_when_core_near_target(caplog: pytest.LogCaptureFixture) -> None:
    from src.app.live_cycle import build_core_rebuild_candidates

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        rows = build_core_rebuild_candidates(
            config=_core_rebuild_config(allow_same_day_rebuild_after_sell=True),
            core_symbols=["AAPL", "MSFT"],
            dynamic_symbols=[],
            existing_candidates=[],
            positions=[{"symbol": "AAPL", "market_value": 6_000.0}],
            equity=10_000.0,
            cash=4_000.0,
            broker=_CoreRebuildBroker(),
            open_order_symbols=[],
            max_positions=10,
            regime_score=3,
            regime_condition="neutral",
            spread_cap_fn=lambda _sym: 1.0,
        )

    assert rows == []
    assert "CORE_REBUILD_SKIP symbol=AAPL reason=core_near_target" in caplog.text


def test_core_rebuild_severe_bearish_regime_blocks_rebuild(caplog: pytest.LogCaptureFixture) -> None:
    from src.app.live_cycle import build_core_rebuild_candidates

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        rows = build_core_rebuild_candidates(
            config=_core_rebuild_config(),
            core_symbols=["AAPL"],
            dynamic_symbols=[],
            existing_candidates=[],
            positions=[],
            equity=10_000.0,
            cash=5_000.0,
            broker=_CoreRebuildBroker(),
            open_order_symbols=[],
            max_positions=10,
            regime_score=1,
            regime_condition="bearish",
            spread_cap_fn=lambda _sym: 1.0,
        )

    assert rows == []
    assert "CORE_REBUILD_SKIP symbol=AAPL reason=bearish_regime" in caplog.text


def test_core_rebuild_spread_cash_and_open_order_block(caplog: pytest.LogCaptureFixture) -> None:
    from src.app.live_cycle import build_core_rebuild_candidates

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        cash_rows = build_core_rebuild_candidates(
            config=_core_rebuild_config(),
            core_symbols=["AAPL"],
            dynamic_symbols=[],
            existing_candidates=[],
            positions=[],
            equity=10_000.0,
            cash=1_000.0,
            broker=_CoreRebuildBroker(),
            open_order_symbols=[],
            max_positions=10,
            regime_score=3,
            regime_condition="neutral",
            spread_cap_fn=lambda _sym: 1.0,
        )
        spread_rows = build_core_rebuild_candidates(
            config=_core_rebuild_config(),
            core_symbols=["MSFT"],
            dynamic_symbols=[],
            existing_candidates=[],
            positions=[],
            equity=10_000.0,
            cash=5_000.0,
            broker=_CoreRebuildBroker(spread=2.0),
            open_order_symbols=[],
            max_positions=10,
            regime_score=3,
            regime_condition="neutral",
            spread_cap_fn=lambda _sym: 1.0,
        )
        open_order_rows = build_core_rebuild_candidates(
            config=_core_rebuild_config(),
            core_symbols=["NVDA"],
            dynamic_symbols=[],
            existing_candidates=[],
            positions=[],
            equity=10_000.0,
            cash=5_000.0,
            broker=_CoreRebuildBroker(),
            open_order_symbols=["NVDA"],
            max_positions=10,
            regime_score=3,
            regime_condition="neutral",
            spread_cap_fn=lambda _sym: 1.0,
        )

    assert cash_rows == []
    assert spread_rows == []
    assert open_order_rows == []
    assert "CORE_REBUILD_SKIP symbol=AAPL reason=cash_reserve" in caplog.text
    assert "CORE_REBUILD_SKIP symbol=MSFT reason=spread" in caplog.text
    assert "CORE_REBUILD_SKIP symbol=NVDA reason=open_order" in caplog.text


def test_core_rebuild_never_adds_dynamic_symbols(caplog: pytest.LogCaptureFixture) -> None:
    from src.app.live_cycle import build_core_rebuild_candidates

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        rows = build_core_rebuild_candidates(
            config=_core_rebuild_config(),
            core_symbols=["AAPL"],
            dynamic_symbols=["AAPL"],
            existing_candidates=[],
            positions=[],
            equity=10_000.0,
            cash=5_000.0,
            broker=_CoreRebuildBroker(),
            open_order_symbols=[],
            max_positions=10,
            regime_score=3,
            regime_condition="neutral",
            spread_cap_fn=lambda _sym: 1.0,
        )

    assert rows == []
    assert "CORE_REBUILD_SKIP symbol=AAPL reason=not_core" in caplog.text


def test_core_rebuild_churn_guard_blocks_recent_short_hold_rebuild(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.app.live_cycle import build_core_rebuild_candidates

    now = datetime(2026, 6, 6, 15, 0, tzinfo=timezone.utc)
    attr_dir = tmp_path / "trade_attribution" / "daily"
    attr_dir.mkdir(parents=True)
    (attr_dir / "2026-06-06_live_bot.json").write_text(
        json.dumps(
            {
                "exits": [
                    {
                        "timestamp": "2026-06-06T14:30:00+00:00",
                        "symbol": "AAPL",
                        "exit_reason": "signal_flip",
                        "hold_minutes": 12,
                        "entry_route": "core_rebuild",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        rows = build_core_rebuild_candidates(
            config=_core_rebuild_config(allow_same_day_rebuild_after_sell=True),
            core_symbols=["AAPL", "MSFT"],
            dynamic_symbols=[],
            existing_candidates=[],
            positions=[],
            equity=10_000.0,
            cash=5_000.0,
            broker=_CoreRebuildBroker(),
            open_order_symbols=[],
            max_positions=10,
            regime_score=3,
            regime_condition="neutral",
            spread_cap_fn=lambda _sym: 1.0,
            user_id="live_bot",
            data_dir=tmp_path,
            now=now,
        )

    assert [row["symbol"] for row in rows] == ["MSFT"]
    assert "CORE_REBUILD_SKIP symbol=AAPL reason=recent_core_rebuild_churn" in caplog.text


def test_core_rebuild_churn_guard_disabled_allows_recent_short_hold_rebuild(tmp_path: Path) -> None:
    from src.app.live_cycle import build_core_rebuild_candidates

    now = datetime(2026, 6, 6, 15, 0, tzinfo=timezone.utc)
    attr_dir = tmp_path / "trade_attribution" / "daily"
    attr_dir.mkdir(parents=True)
    (attr_dir / "2026-06-06_live_bot.json").write_text(
        json.dumps(
            {
                "exits": [
                    {
                        "timestamp": "2026-06-06T14:30:00+00:00",
                        "symbol": "AAPL",
                        "hold_minutes": 12,
                        "entry_route": "core_rebuild",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    rows = build_core_rebuild_candidates(
        config=_core_rebuild_config(
            churn_guard={"enabled": False},
            allow_same_day_rebuild_after_sell=True,
        ),
        core_symbols=["AAPL"],
        dynamic_symbols=[],
        existing_candidates=[],
        positions=[],
        equity=10_000.0,
        cash=5_000.0,
        broker=_CoreRebuildBroker(),
        open_order_symbols=[],
        max_positions=10,
        regime_score=3,
        regime_condition="neutral",
        spread_cap_fn=lambda _sym: 1.0,
        user_id="live_bot",
        data_dir=tmp_path,
        now=now,
    )

    assert [row["symbol"] for row in rows] == ["AAPL"]


def test_core_rebuild_skips_same_day_sold_core_symbol(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.app.live_cycle import build_core_rebuild_candidates
    from src.trade_attribution import record_exit

    now = datetime(2026, 6, 6, 15, 0, tzinfo=timezone.utc)
    record_exit(
        data_dir=tmp_path,
        user_id="live_bot",
        timestamp=datetime(2026, 6, 6, 14, 0, tzinfo=timezone.utc),
        symbol="AAPL",
        exit_reason="take_profit",
        hold_minutes=90,
        entry_route="trend_long",
    )

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        rows = build_core_rebuild_candidates(
            config=_core_rebuild_config(),
            core_symbols=["AAPL", "MSFT"],
            dynamic_symbols=[],
            existing_candidates=[],
            positions=[],
            equity=10_000.0,
            cash=5_000.0,
            broker=_CoreRebuildBroker(),
            open_order_symbols=[],
            max_positions=10,
            regime_score=3,
            regime_condition="neutral",
            spread_cap_fn=lambda _sym: 1.0,
            user_id="live_bot",
            data_dir=tmp_path,
            now=now,
        )

    assert [row["symbol"] for row in rows] == ["MSFT"]
    assert "CORE_REBUILD_SKIP symbol=AAPL reason=sold_today" in caplog.text


def test_core_rebuild_allows_same_day_sold_symbol_when_configured(tmp_path: Path) -> None:
    from src.app.live_cycle import build_core_rebuild_candidates
    from src.trade_attribution import record_exit

    now = datetime(2026, 6, 6, 15, 0, tzinfo=timezone.utc)
    record_exit(
        data_dir=tmp_path,
        user_id="live_bot",
        timestamp=datetime(2026, 6, 6, 14, 0, tzinfo=timezone.utc),
        symbol="AAPL",
        hold_minutes=90,
        entry_route="trend_long",
    )

    rows = build_core_rebuild_candidates(
        config=_core_rebuild_config(allow_same_day_rebuild_after_sell=True),
        core_symbols=["AAPL"],
        dynamic_symbols=[],
        existing_candidates=[],
        positions=[],
        equity=10_000.0,
        cash=5_000.0,
        broker=_CoreRebuildBroker(),
        open_order_symbols=[],
        max_positions=10,
        regime_score=3,
        regime_condition="neutral",
        spread_cap_fn=lambda _sym: 1.0,
        user_id="live_bot",
        data_dir=tmp_path,
        now=now,
    )

    assert [row["symbol"] for row in rows] == ["AAPL"]


def test_core_rebuild_skips_position_state_recent_exit(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.app.live_cycle import build_core_rebuild_candidates
    from src.position_state_machine import record_sell_after_exit

    now = datetime(2026, 6, 6, 15, 0, tzinfo=timezone.utc)
    cfg = _core_rebuild_config()
    cfg["position_states"] = {"enabled": True, "cooldown_after_sell_minutes": 30}
    record_sell_after_exit(
        "AAPL",
        "live_bot",
        tmp_path,
        now,
        "take_profit",
        0,
        cfg,
    )

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        rows = build_core_rebuild_candidates(
            config=cfg,
            core_symbols=["AAPL", "MSFT"],
            dynamic_symbols=[],
            existing_candidates=[],
            positions=[],
            equity=10_000.0,
            cash=5_000.0,
            broker=_CoreRebuildBroker(),
            open_order_symbols=[],
            max_positions=10,
            regime_score=3,
            regime_condition="neutral",
            spread_cap_fn=lambda _sym: 1.0,
            user_id="live_bot",
            data_dir=tmp_path,
            now=now,
        )

    assert [row["symbol"] for row in rows] == ["MSFT"]
    assert "CORE_REBUILD_SKIP symbol=AAPL reason=recent_exit" in caplog.text


def test_dynamic_symbols_initialized_before_open_protection_skip_path() -> None:
    """The dynamic scan may be skipped near the open, but later code still reads it."""

    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    tree = ast.parse(source)
    run_live_cycle = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "run_live_cycle"
    )

    dynamic_symbol_store_lines = [
        node.lineno
        for node in ast.walk(run_live_cycle)
        if isinstance(node, ast.Name)
        and node.id == "dynamic_symbols"
        and isinstance(node.ctx, ast.Store)
    ]
    open_protection_line = next(
        index
        for index, line in enumerate(source.splitlines(), start=1)
        if "DYNAMIC_SCAN skipped" in line
    )

    assert dynamic_symbol_store_lines
    assert min(dynamic_symbol_store_lines) < open_protection_line


def test_dynamic_scan_open_protection_uses_wall_clock_not_restart_time() -> None:
    from src.app.live_cycle import (
        _dynamic_scan_open_protected,
        _market_session_entry_cadence_seconds,
    )

    ny = ZoneInfo("America/New_York")
    restart_936_et = datetime(2026, 6, 4, 9, 36, tzinfo=ny)
    open_930_et = datetime(2026, 6, 4, 9, 30, 30, tzinfo=ny)
    open_934_et = datetime(2026, 6, 4, 9, 34, 59, tzinfo=ny)
    open_935_et = datetime(2026, 6, 4, 9, 35, tzinfo=ny)

    assert (
        _dynamic_scan_open_protected(
            restart_936_et,
            enabled=True,
            configured_delay_minutes=5.0,
        )
        is False
    )
    assert (
        _dynamic_scan_open_protected(
            open_930_et,
            enabled=True,
            configured_delay_minutes=5.0,
        )
        is True
    )
    assert (
        _dynamic_scan_open_protected(
            open_934_et,
            enabled=True,
            configured_delay_minutes=5.0,
        )
        is True
    )
    assert (
        _dynamic_scan_open_protected(
            open_935_et,
            enabled=True,
            configured_delay_minutes=5.0,
        )
        is False
    )
    assert _market_session_entry_cadence_seconds(
        restart_936_et,
        default_dynamic_seconds=600.0,
        default_core_seconds=600.0,
    ) == (60.0, 180.0)


def test_live_cycle_open_window_uses_fast_cadence_and_sleep() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()

    assert "_open_accelerated_window = _market_open_accelerated_window_active(dt)" in source
    assert "_eff_dyn_ent_sec_u, _entry_interval_sec_eff = _market_session_entry_cadence_seconds(" in source
    assert "_session_dyn_sleep_sec, _session_core_sleep_sec = _market_session_entry_cadence_seconds(" in source


def test_dynamic_loosened_config_startup_log(capsys: pytest.CaptureFixture[str]) -> None:
    from src.app.live_cycle import _log_dynamic_universe_startup_config

    _log_dynamic_universe_startup_config(
        {
            "dynamic_universe": {
                "enabled": True,
                "max_symbols": 30,
                "min_history_bars": 180,
                "min_day_gain_pct": 3.0,
                "min_relative_volume": 0.3,
            },
            "dynamic_momentum_entry": {
                "min_day_gain_pct": 3.0,
                "min_relative_volume": 0.3,
                "news_dynamic_entry": {"early_min_relative_volume": 0.5},
            },
            "market": {"open_protection": {"dynamic_scan_delay_minutes": 5}},
            "portfolio": {"max_positions": 10, "target_dynamic_pct": 45},
        }
    )

    out = capsys.readouterr().out
    assert "DYNAMIC_HISTORY_CONFIG min_history_bars=180" in out
    assert "DYNAMIC_LOOSENED_CONFIG" in out
    assert "open_protection_minutes=5" in out
    assert "min_relative_volume=0.3" in out
    assert "early_min_relative_volume=0.5" in out
    assert "min_day_gain_pct=3.0" in out
    assert "max_symbols=30" in out
    assert "max_positions=10" in out
    assert "dynamic_allocation=45" in out


def test_market_session_entry_cadence_windows() -> None:
    from src.app.live_cycle import _market_session_entry_cadence_seconds

    ny = ZoneInfo("America/New_York")
    assert _market_session_entry_cadence_seconds(
        datetime(2026, 6, 4, 9, 31, tzinfo=ny),
        default_dynamic_seconds=999.0,
        default_core_seconds=999.0,
    ) == (60.0, 180.0)
    assert _market_session_entry_cadence_seconds(
        datetime(2026, 6, 4, 12, 0, tzinfo=ny),
        default_dynamic_seconds=999.0,
        default_core_seconds=999.0,
    ) == (180.0, 600.0)
    assert _market_session_entry_cadence_seconds(
        datetime(2026, 6, 4, 15, 45, tzinfo=ny),
        default_dynamic_seconds=999.0,
        default_core_seconds=999.0,
    ) == (120.0, 300.0)
    assert _market_session_entry_cadence_seconds(
        datetime(2026, 6, 4, 16, 1, tzinfo=ny),
        default_dynamic_seconds=420.0,
        default_core_seconds=600.0,
    ) == (420.0, 600.0)


def test_live_loop_fast_poll_windows() -> None:
    from src.app.live_cycle import _live_loop_fast_poll_sleep_seconds

    ny = ZoneInfo("America/New_York")
    config = {
        "live_loop": {
            "fast_poll_windows": [
                {"start": "09:20", "end": "09:45", "sleep_seconds": 15},
                {"start": "15:45", "end": "16:00", "sleep_seconds": 15},
            ]
        }
    }

    assert _live_loop_fast_poll_sleep_seconds(
        config,
        datetime(2026, 6, 9, 9, 29, tzinfo=ny),
        default_sleep_seconds=120.0,
    ) == (15.0, "fast_poll_window", "09:20-09:45")
    assert _live_loop_fast_poll_sleep_seconds(
        config,
        datetime(2026, 6, 9, 9, 35, tzinfo=ny),
        default_sleep_seconds=120.0,
    ) == (15.0, "fast_poll_window", "09:20-09:45")
    assert _live_loop_fast_poll_sleep_seconds(
        config,
        datetime(2026, 6, 9, 10, 30, tzinfo=ny),
        default_sleep_seconds=120.0,
    ) == (120.0, "normal_poll", None)
    assert _live_loop_fast_poll_sleep_seconds(
        config,
        datetime(2026, 6, 9, 15, 58, tzinfo=ny),
        default_sleep_seconds=120.0,
    ) == (15.0, "fast_poll_window", "15:45-16:00")


def test_live_loop_poll_sleep_policy_defaults() -> None:
    from src.app.live_cycle import _live_loop_poll_sleep_seconds

    config = {
        "live_loop": {
            "loop_sleep_seconds_live": 120,
            "loop_sleep_seconds_paper": 15,
            "loop_sleep_seconds_paper_options": 5,
        }
    }

    assert _live_loop_poll_sleep_seconds(
        config,
        mode="live",
        options_mode="disabled",
        default_sleep_seconds=180.0,
    ) == (120.0, "normal")
    assert _live_loop_poll_sleep_seconds(
        config,
        mode="paper",
        options_mode="disabled",
        default_sleep_seconds=120.0,
    ) == (15.0, "paper_fast")
    assert _live_loop_poll_sleep_seconds(
        config,
        mode="paper",
        options_mode="paper_only",
        default_sleep_seconds=120.0,
    ) == (5.0, "paper_options_fast")


def test_live_loop_poll_sleep_config_override() -> None:
    from src.app.live_cycle import _live_loop_poll_sleep_seconds

    config = {
        "live_loop": {
            "loop_sleep_seconds_live": 90,
            "loop_sleep_seconds_paper": 11,
            "loop_sleep_seconds_paper_options": 3,
        }
    }

    assert _live_loop_poll_sleep_seconds(
        config,
        mode="live",
        options_mode="disabled",
        default_sleep_seconds=120.0,
    ) == (90.0, "normal")
    assert _live_loop_poll_sleep_seconds(
        config,
        mode="paper",
        options_mode="disabled",
        default_sleep_seconds=120.0,
    ) == (11.0, "paper_fast")
    assert _live_loop_poll_sleep_seconds(
        config,
        mode="paper",
        options_mode="paper_only",
        default_sleep_seconds=120.0,
    ) == (3.0, "paper_options_fast")


def test_live_loop_poll_context_detects_paper_options() -> None:
    from src.app.live_cycle import _live_loop_poll_context

    paper_stock = SimpleNamespace(paper=True, config={"options": {"enabled": False}})
    paper_options = SimpleNamespace(
        paper=True,
        config={"options": {"enabled": True, "mode": "paper_only"}},
    )
    live_user = SimpleNamespace(paper=False, config={"options": {"enabled": False}})

    assert _live_loop_poll_context([live_user]) == ("live", "disabled")
    assert _live_loop_poll_context([paper_stock]) == ("paper", "disabled")
    assert _live_loop_poll_context([paper_stock, paper_options]) == ("paper", "paper_only")


def test_live_loop_sleep_log_schema(caplog: pytest.LogCaptureFixture) -> None:
    from src.app.live_cycle import _log_live_loop_sleep

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        _log_live_loop_sleep(
            5,
            mode="paper",
            options_mode="paper_only",
            reason="paper_options_fast",
        )

    assert (
        "LIVE_LOOP_SLEEP seconds=5 mode=paper options_mode=paper_only "
        "reason=paper_options_fast"
    ) in caplog.text


def test_early_session_entry_scan_orders_dynamic_before_etfs() -> None:
    from src.app.live_cycle import _entry_scan_order_for_session

    ordered = _entry_scan_order_for_session(
        ["SPY", "AAPL", "XOS", "QQQ", "NVTS"],
        dynamic_symbols=["XOS", "NVTS"],
        early_session=True,
    )

    assert ordered == ["XOS", "NVTS", "AAPL", "SPY", "QQQ"]


def test_dynamic_selected_entry_trace_logs_skip_or_evaluate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.app.live_cycle import (
        _log_dynamic_selected_entry_drop,
        _log_dynamic_selected_entry_eval_start,
        _log_dynamic_selected_entry_trace,
    )

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        _log_dynamic_selected_entry_trace(
            "LYG",
            in_universe=True,
            will_evaluate=True,
            reason="ok",
            in_dynamic_set=True,
            in_effective_universe=True,
            in_scoring_top_n=True,
            scoring_allowed=True,
            dynamic_bypass_applied=True,
            route_candidate="dynamic_momentum",
            selected_count=2,
            rank=1,
        )
        _log_dynamic_selected_entry_trace(
            "ADUR",
            in_universe=True,
            will_evaluate=False,
            reason="dynamic_entry_disabled",
        )
        _log_dynamic_selected_entry_drop(
            "RKLZ",
            stage="scoring_top_n",
            reason="not_in_scoring_top_n_candidates",
            detail="rank=4 top_n=3",
        )
        _log_dynamic_selected_entry_eval_start(
            "LYG",
            route_candidate="dynamic_momentum",
            detail="log_entry_eval",
        )

    assert (
        "DYNAMIC_SELECTED_ENTRY_TRACE symbol=LYG in_universe=true "
        "will_evaluate=true reason=ok"
    ) in caplog.text
    assert "DYNAMIC_SELECTED_ENTRY_SKIPPED symbol=LYG" not in caplog.text
    assert (
        "DYNAMIC_SELECTED_ENTRY_TRACE symbol=ADUR in_universe=true "
        "will_evaluate=false reason=dynamic_entry_disabled"
    ) in caplog.text
    assert (
        "DYNAMIC_SELECTED_ENTRY_SKIPPED symbol=ADUR reason=dynamic_entry_disabled"
    ) in caplog.text
    assert "in_dynamic_set=true" in caplog.text
    assert "in_effective_universe=true" in caplog.text
    assert "in_scoring_top_n=true" in caplog.text
    assert "scoring_allowed=true" in caplog.text
    assert "dynamic_bypass_applied=true" in caplog.text
    assert "route_candidate=dynamic_momentum" in caplog.text
    assert "selected_count=2 rank=1" in caplog.text
    assert (
        "DYNAMIC_SELECTED_ENTRY_DROP symbol=RKLZ stage=scoring_top_n "
        "reason=not_in_scoring_top_n_candidates detail=rank=4 top_n=3"
    ) in caplog.text
    assert (
        "DYNAMIC_SELECTED_ENTRY_EVAL_START symbol=LYG "
        "route_candidate=dynamic_momentum detail=log_entry_eval"
    ) in caplog.text


def test_startup_loads_cached_premarket_artifacts_before_catchup() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()
    load_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if "_startup_artifacts = _load_premarket_artifacts_into_runtime(" in line
    )
    catchup_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if index > load_line and "run_premarket_scheduler_startup_catchup(" in line
    )

    assert load_line < catchup_line
    assert "and not _startup_artifacts" in source


def test_exit_and_risk_flow_still_runs_before_entry_evaluation() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()
    manage_positions_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if "# ---------- manage_positions() ----------" in line
    )
    open_row_exit_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if "Open-row exit pass (options + equities); run before entry logic." in line
    )
    sync_options_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if "_options_position_snapshot = _sync_options_position_state(" in line
    )
    entry_eval_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if "# ---------- evaluate_entries() ----------" in line
    )

    assert manage_positions_line < entry_eval_line
    assert open_row_exit_line < entry_eval_line
    assert sync_options_line < entry_eval_line


def test_eff_for_cap_initialized_before_all_usage_paths() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()
    init_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if "_eff_for_cap: float | None = None" in line
    )
    use_lines = [
        index
        for index, line in enumerate(lines, start=1)
        if "_eff_for_cap" in line and index != init_line
    ]

    assert use_lines
    assert init_line < min(use_lines)


def test_dynamic_ema_bypass_retries_only_dynamic_momentum_route(caplog) -> None:
    from src.app.live_cycle import _run_entry_gates_dynamic_ema_bypass

    class Engine:
        def __init__(self):
            self.config = {
                "filters": {"require_price_above_20ema": True},
            }
            self.calls = []

        def run_entry_gates(self, **kwargs):
            self.calls.append(dict(self.config.get("filters") or {}))
            if len(self.calls) == 1:
                return SimpleNamespace(
                    allowed=False,
                    reason="trend filter: close 1.0000 not above 20 EMA 1.1000 (APPS)",
                )
            return SimpleNamespace(allowed=True, reason="ok")

    engine = Engine()
    caplog.set_level(logging.INFO)
    decision = _run_entry_gates_dynamic_ema_bypass(
        engine,
        config={
            "dynamic_momentum_override": {
                "enabled": True,
                "allow_without_ema_pullback": True,
            },
        },
        is_dynamic_candidate=True,
        entry_route="momentum_breakout",
        run_kwargs={"symbol": "APPS"},
    )

    assert decision.allowed
    assert engine.calls == [
        {"require_price_above_20ema": True},
        {"require_price_above_20ema": False},
    ]
    assert engine.config["filters"]["require_price_above_20ema"] is True
    assert "DYNAMIC_EMA_BYPASS symbol=APPS reason=trend filter:" in caplog.text


def test_dynamic_ema_bypass_does_not_retry_core_trend_long() -> None:
    from src.app.live_cycle import _run_entry_gates_dynamic_ema_bypass

    class Engine:
        config = {"filters": {"require_price_above_20ema": True}}

        def __init__(self):
            self.calls = 0

        def run_entry_gates(self, **kwargs):
            self.calls += 1
            return SimpleNamespace(
                allowed=False,
                reason="trend filter: close 1.0000 not above 20 EMA 1.1000 (AAPL)",
            )

    engine = Engine()
    decision = _run_entry_gates_dynamic_ema_bypass(
        engine,
        config={
            "dynamic_momentum_override": {
                "enabled": True,
                "allow_without_ema_pullback": True,
            },
        },
        is_dynamic_candidate=False,
        entry_route="trend_long",
        run_kwargs={"symbol": "AAPL"},
    )

    assert not decision.allowed
    assert engine.calls == 1


def test_dynamic_momentum_entry_config_initialized_before_symbol_loop() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    tree = ast.parse(source)
    run_live_cycle = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "run_live_cycle"
    )
    lines = source.splitlines()
    loop_line = next(
        node.lineno
        for node in ast.walk(run_live_cycle)
        if isinstance(node, ast.For)
        and isinstance(node.target, ast.Name)
        and node.target.id == "symbol"
        and isinstance(node.iter, ast.Name)
        and node.iter.id == "_syms_scan"
    )
    init_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if "DYNAMIC_MOMENTUM_ENTRY_CONFIG enabled=%s source=%s" in line
    )
    cfg_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if line.strip() == "_dme_cfg = _dynamic_momentum_entry_effective_cfg(config)"
    )

    assert cfg_line < loop_line
    assert init_line < loop_line
    assert "dynamic_momentum_entry" in source
    assert "dynamic_momentum_override" in source
    assert "_dme_on and isinstance(_dme_cfg, dict)" in source


def test_dynamic_momentum_entry_config_uses_scanner_threshold_overlay() -> None:
    from src.app.live_cycle import _dynamic_momentum_entry_effective_cfg

    cfg = _dynamic_momentum_entry_effective_cfg(
        {
            "dynamic_momentum_entry": {
                "enabled": True,
                "min_day_gain_pct": 15.0,
                "min_relative_volume": 2.0,
                "require_above_vwap": True,
            },
            "dynamic_momentum_override": {
                "enabled": True,
                "min_day_gain_pct": 20.0,
                "min_relative_volume": 1.8,
                "require_above_vwap": False,
                "allow_without_ema_pullback": True,
            },
        }
    )

    assert cfg["min_day_gain_pct"] == pytest.approx(15.0)
    assert cfg["min_relative_volume"] == pytest.approx(1.8)
    assert cfg["require_above_vwap"] is False
    assert cfg["allow_without_ema_pullback"] is True


def test_dynamic_allocator_logs_accept_missing_relative_volume() -> None:
    from src.app.live_cycle import (
        _finite_float_or_none,
        _log_allocator_dynamic_candidate,
        _log_allocator_dynamic_selected,
        _log_allocator_dynamic_skipped,
    )

    assert _finite_float_or_none(None) is None
    _log_allocator_dynamic_candidate(
        "HURC",
        reason="entry_eval_final",
        strength_eff=0.78,
        source="dynamic_universe",
        news_score=0.0,
        relative_volume=_finite_float_or_none(None),
    )
    _log_allocator_dynamic_skipped(
        "HURC",
        reason="dynamic momentum entry: relative_volume unavailable",
        strength_eff=0.78,
        source="dynamic_universe",
        news_score=0.0,
        relative_volume=_finite_float_or_none(None),
    )
    _log_allocator_dynamic_selected(
        "ARCB",
        reason="queued_for_allocator",
        strength_eff=0.81,
        source="dynamic_universe",
        news_score=0.0,
        relative_volume=_finite_float_or_none(""),
    )


def test_live_cycle_has_no_unsafe_relative_volume_float_logging() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()

    assert "float(_rel_vol_e)" not in source


def test_entry_effective_min_rel_volume_initialized_before_any_live_cycle_read() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    module = ast.parse(source)
    run_live_cycle = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "run_live_cycle"
    )

    assignments = [
        node.lineno
        for node in ast.walk(run_live_cycle)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
        and target.id == "_entry_effective_min_rel_volume"
    ]
    reads = [
        node.lineno
        for node in ast.walk(run_live_cycle)
        if isinstance(node, ast.Name)
        and node.id == "_entry_effective_min_rel_volume"
        and isinstance(node.ctx, ast.Load)
    ]

    assert assignments
    assert reads
    assert min(assignments) < min(reads)


def test_trend_long_entry_diagnostics_payload_has_safe_rvol_default() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()

    candidate_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if line.strip().startswith("_is_dynamic_candidate = (")
    )
    init_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if line.strip() == "_entry_effective_min_rel_volume = None"
        and i > candidate_idx
    )
    source_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if '"source": "dynamic_universe"' in line
        and i > init_idx
    )
    trend_fallback_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if 'else "trend_long",' in line
        and i > source_idx
    )
    payload_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if '"effective_min_rel_volume": _entry_effective_min_rel_volume,' in line
        and i > trend_fallback_idx
    )

    assert init_idx < trend_fallback_idx < payload_idx


def test_dynamic_entry_diagnostics_payload_populates_effective_rvol_before_use() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()

    dynamic_eval_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_ok_m, _rsn_m = dynamic_momentum_entry_passes(" in line
    )
    dynamic_assignment_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if line.strip() == "_entry_effective_min_rel_volume = ("
        and i > dynamic_eval_idx
    )
    guard_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if '_final_route_for_rvol_guard == "dynamic_momentum_override"' in line
        and i > dynamic_assignment_idx
    )
    payload_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if '"entry_eval_effective_min_rel_volume": _entry_effective_min_rel_volume,' in line
        and i > guard_idx
    )

    assert dynamic_eval_idx < dynamic_assignment_idx < guard_idx < payload_idx
    assert '"catalyst_fastlane_active": bool(_entry_catalyst_fastlane_active),' in source
    assert '"catalyst_min_relative_volume": _entry_catalyst_min_relative_volume,' in source


def test_dynamic_override_low_rvol_guard_runs_before_entry_eval_pass_log() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()

    eval_allowed_idx = next(
        index
        for index, line in enumerate(lines, start=1)
        if "_eval_allowed = bool(decision.allowed)" in line
    )
    final_guard_idx = next(
        index
        for index, line in enumerate(lines, start=1)
        if "_final_route_for_rvol_guard == \"dynamic_momentum_override\"" in line
    )
    guard_log_idx = next(
        index
        for index, line in enumerate(lines, start=1)
        if "ENTRY_EVAL_DYNAMIC_RVOL_GUARD symbol=%s route=%s " in line
        and index > final_guard_idx
    )
    force_false_idx = next(
        index
        for index, line in enumerate(lines, start=1)
        if "_eval_allowed = False" in line and index > final_guard_idx
    )
    entry_eval_log_idx = next(
        index
        for index, line in enumerate(lines, start=1)
        if "log_entry_eval(" in line
    )

    assert eval_allowed_idx < final_guard_idx < guard_log_idx < entry_eval_log_idx
    assert final_guard_idx < force_false_idx < entry_eval_log_idx
    assert "and _entry_rel_for_log" in source
    assert "< _entry_threshold_for_log - 1e-9" in source
    assert "guard_result=%s" in source
    assert "_guard_result = \"fail\"" in source
    assert "_eval_reason = (\n                                                    \"relative_volume %.2f < %.2f\"" in source


def test_dynamic_momentum_rank_pairs_initialized_before_conditional_read() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()
    init_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if line.strip().startswith("_rank_pairs: list[tuple[str, float]] = []")
    )
    read_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if line.strip() == "if _rank_pairs:"
    )
    momentum_gate_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if line.strip() == "_momentum_rank_on"
    )

    assert init_line < momentum_gate_line
    assert init_line < read_line


def test_dynamic_vwap_guard_logging_is_present() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()
    helper_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_ok_m, _rsn_m = dynamic_momentum_entry_passes(" in line
    )
    skip_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "dynamic momentum entry: {_rsn_m}" in line
    )
    guard_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "DYNAMIC_VWAP_GUARD symbol=%s price=%.4f vwap=%s distance_pct=%s news_score=%d allowed=%s reason=%s"
        in line
    )
    assert helper_idx < guard_idx < skip_idx


def test_dynamic_vwap_live_branch_blocks_high_conviction_news() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    branch_line = next(
        i
        for i, line in enumerate(source.splitlines(), start=1)
        if 'if _rsn_m == "price not above session VWAP":' in line
    )
    branch_text = "\n".join(source.splitlines()[branch_line - 1 : branch_line + 90])
    assert "DYNAMIC_VWAP_GUARD symbol=%s price=%.4f vwap=%s distance_pct=%s news_score=%d allowed=%s reason=%s" in branch_text
    assert "or _high_conviction_vwap" not in branch_text
    assert 'reason=vwap_safety' in branch_text
    assert "if _vwap_guard_allowed:" in branch_text
    assert "_log_allocator_dynamic_skipped(" in branch_text


def test_premarket_catalyst_rvol_bypass_thresholds() -> None:
    from src.app.live_cycle import (
        _dynamic_rvol_required_from_reason,
        _premarket_catalyst_fastlane_signal,
        _premarket_catalyst_rvol_bypass_allowed,
    )

    assert _premarket_catalyst_rvol_bypass_allowed(
        route="premarket_catalyst_replay",
        catalyst_score=0.3,
        event_score=0.0,
        news_score=0.0,
    )
    assert _premarket_catalyst_rvol_bypass_allowed(
        route="premarket_catalyst_replay",
        catalyst_score=0.0,
        event_score=3.0,
        news_score=0.0,
    )
    assert _premarket_catalyst_rvol_bypass_allowed(
        route="premarket_catalyst_replay",
        catalyst_score=0.0,
        event_score=0.0,
        news_score=3.0,
    )
    assert not _premarket_catalyst_rvol_bypass_allowed(
        route="dynamic_universe",
        catalyst_score=0.9,
        event_score=9.0,
        news_score=9.0,
    )
    assert not _premarket_catalyst_rvol_bypass_allowed(
        route="premarket_catalyst_replay",
        catalyst_score=0.29,
        event_score=2.9,
        news_score=2.9,
    )
    assert _dynamic_rvol_required_from_reason("relative_volume 0.13 <= 1.80", 0.0) == pytest.approx(1.8)
    assert _premarket_catalyst_fastlane_signal(
        premarket_injected=True,
        news_score=7,
        event_score=0,
        catalyst_score=0,
        catalyst_age_minutes=180,
    )
    assert _premarket_catalyst_fastlane_signal(
        premarket_injected=True,
        news_score=0,
        event_score=7,
        catalyst_score=0,
        catalyst_age_minutes=45,
    )
    assert _premarket_catalyst_fastlane_signal(
        premarket_injected=True,
        news_score=0,
        event_score=0,
        catalyst_score=0.7,
        catalyst_age_minutes=45,
    )
    assert not _premarket_catalyst_fastlane_signal(
        premarket_injected=False,
        news_score=9,
        event_score=9,
        catalyst_score=0.9,
        catalyst_age_minutes=45,
    )
    assert _premarket_catalyst_fastlane_signal(
        premarket_injected=True,
        news_score=9,
        event_score=9,
        catalyst_score=0.9,
        catalyst_age_minutes=300,
    )
    assert not _premarket_catalyst_fastlane_signal(
        premarket_injected=True,
        news_score=9,
        event_score=9,
        catalyst_score=0.9,
        catalyst_age_minutes=301,
    )


def test_premarket_catalyst_low_rvol_reaches_allocator_input_branch() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()
    check_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "PRE_ALLOCATOR_DYNAMIC_RVOL_CHECK symbol=%s route=%s" in line
    )
    bypass_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if '_rsn_m = "ok news_catalyst"' in line
    )
    skip_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "dynamic momentum entry: {_rsn_m}" in line
    )
    selected_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if 'reason="queued_for_allocator"' in line
    )
    enqueue_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_append_capital_allocator_candidate(" in line and i > selected_idx
    )

    assert check_idx < bypass_idx < skip_idx < selected_idx < enqueue_idx


def test_entry_eval_final_candidates_enqueue_for_allocator_input() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    entry_log_source = (PROJECT_ROOT / "src" / "entry_eval_log.py").read_text()
    lines = source.splitlines()
    final_gate_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_final_true_stock_candidate_can_enter_allocator(decision, df)" in line
        and i > next(
            j
            for j, src_line in enumerate(lines, start=1)
            if "capital_allocator: only queue where entry_eval **final** matches dispatch" in src_line
        )
    )
    enqueue_call_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_append_capital_allocator_candidate(" in line and i > final_gate_idx
    )
    allocator_run_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "available_cash = _run_live_capital_allocator_pass(" in line
    )
    old_dispatch_text = "\n".join(lines[final_gate_idx - 1 : enqueue_call_idx + 12])

    assert final_gate_idx < enqueue_call_idx < allocator_run_idx
    assert "candidates.append(row)" in source
    assert "routed_to_options_or_stock_path" not in old_dispatch_text
    assert "_trend_long_dispatch_impl(_row_alloc)" not in old_dispatch_text
    assert "and decision.order_request" not in old_dispatch_text
    assert "ALLOCATOR_ENQUEUE_SKIP symbol=%s route=%s reason=%s allocator_on=%s final=%s stage=%s" in source
    assert "ALLOCATOR_QUEUE_SUMMARY queued=%d symbols=%s" in source
    assert "ALLOCATOR_QUEUE_CONTENTS symbols=%s" in source
    assert "ALLOCATOR_QUEUE_STATE stage=%s allocator_on=%s pending_count=%d symbols=%s" in source
    assert "ALLOCATOR_QUEUE_STATE phase=%s pending_count=%d allocator_on=%s symbols=%s" in source
    assert "ALLOCATOR_DRAIN_ENTRY pending_count=%d allocator_on=%s symbols=%s" in source
    assert "ALLOCATOR_DRAIN_START count=%d symbols=%s" in source
    assert "ALLOCATOR_DRAIN_DONE actions=%d pending_count=%d symbols=%s" in source
    assert "ALLOCATOR_DRAIN_EXIT pending_count=%d reason=%s symbols=%s" in source
    assert "ALLOCATOR_DRAIN_SKIPPED reason=%s pending_count=%d symbols=%s" in source
    assert '"before_allocator_drain"' in source
    assert '"after_drain"' in source
    assert '"before_sleep"' in source
    assert "ALLOCATOR_PASS_START queued=%d" in source
    assert "ALLOCATOR_PASS_SKIP reason=%s queued=%d" in source
    assert "ALLOCATOR_SKIP reason=%s pending_count=%d symbols=%s stage=%s" in source
    assert "ALLOCATOR_SKIP symbol=%s reason=%s route=%s stage=%s" in source
    assert "ALLOCATOR_PASS_AFTER_DEDUPE queued=%d symbols=%s" in (PROJECT_ROOT / "src" / "portfolio" / "allocator.py").read_text()
    assert '"all_candidates_filtered"' in source
    assert "available_cash = _run_live_capital_allocator_pass(" in source


def test_xlf_final_true_enqueues_and_reaches_allocator_input(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    from src.app.live_cycle import (
        _append_capital_allocator_candidate,
        _final_true_stock_candidate_can_enter_allocator,
        _log_allocator_drain_entry,
        _log_allocator_drain_exit,
        _log_allocator_queue_summary,
        _log_allocator_queue_state,
        _run_live_capital_allocator_pass,
    )
    from src.portfolio.allocator_planner import parse_capital_allocator_cfg

    class Broker:
        def get_buying_power(self) -> float:
            return 10_000.0

        def get_latest_quote(self, symbol: str) -> SimpleNamespace:
            return SimpleNamespace(bid_price=39.95, ask_price=40.05)

    raw_ca_cfg = {
        "enabled": True,
        "max_positions": 5,
        "symbol_cap": 0.5,
        "min_trade_size": 500.0,
        "min_realloc_leg": 1_000_000.0,
        "fallback_on_empty_alloc": False,
        "require_net_sell_gte_buy": False,
    }
    config = {
        "portfolio": {"capital_allocator": raw_ca_cfg},
        "allocation": {},
        "risk": {},
        "options": {"enabled": False},
    }
    ca_cfg = parse_capital_allocator_cfg(config["portfolio"])
    candidates: list[dict] = []
    decision = SimpleNamespace(allowed=True, reason="ok", order_request=None)
    df = SimpleNamespace(empty=False)
    row_xlf = {
        "symbol": "XLF",
        "sym_u": "XLF",
        "entry_eval_final": True,
        "source": "trend_long",
        "route": "trend_long",
        "notional": 1200.0,
        "score": 1.45,
        "strength_eff": 1.45,
    }
    row_xle = {
        "symbol": "XLE",
        "sym_u": "XLE",
        "entry_eval_final": True,
        "source": "trend_long",
        "route": "trend_long",
        "notional": 1200.0,
        "score": 1.45,
        "strength_eff": 1.45,
    }
    row_intc = {
        "symbol": "INTC",
        "sym_u": "INTC",
        "entry_eval_final": True,
        "source": "dynamic_universe",
        "route": "dynamic_momentum_override",
        "notional": 1200.0,
        "score": 1.72,
        "strength_eff": 1.72,
        "dynamic_candidate": True,
        "dynamic_symbol": True,
        "news_score": 10.0,
        "catalyst_score": 0.91,
        "relative_volume": 0.64,
    }

    assert _final_true_stock_candidate_can_enter_allocator(decision, df) is True

    with caplog.at_level(logging.INFO):
        _append_capital_allocator_candidate(
            candidates,
            row_xlf,
            symbol="XLF",
            route="trend_long",
            reason="ok",
            score=1.45,
        )
        _append_capital_allocator_candidate(
            candidates,
            row_xle,
            symbol="XLE",
            route="trend_long",
            reason="ok",
            score=1.45,
        )
        _append_capital_allocator_candidate(
            candidates,
            row_intc,
            symbol="INTC",
            route="dynamic_momentum_override",
            reason="ok",
            score=1.72,
        )
        _log_allocator_queue_summary(candidates)
        _log_allocator_queue_state(
            "before_allocator_drain",
            candidates,
            allocator_on=True,
        )
        _log_allocator_drain_entry(candidates, allocator_on=True)
        _run_live_capital_allocator_pass(
            candidates,
            broker=Broker(),
            engine=SimpleNamespace(),
            config=config,
            dt=datetime(2026, 6, 10, 10, 0, tzinfo=ZoneInfo("America/New_York")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=10_000.0,
            available_cash=10_000.0,
            ca_cfg=ca_cfg,
            user_id="live_bot",
            data_dir=tmp_path,
            stale_quote_max_age=999.0,
            strength_jitter_max=0.0,
            et_date_iso="2026-06-10",
            cycle_risk_state=None,
            verbose=False,
            exit_context=None,
            reg_score_bp=None,
            reg_cond_bp=None,
            entry_full_invest_flag=False,
            gross_exposure_pct=0.0,
        )
        _log_allocator_drain_exit([], reason="allocator_pass")
        _log_allocator_queue_state("after_drain", [], allocator_on=True)

    assert candidates == [row_xlf, row_xle, row_intc]
    assert "ALLOCATOR_ENQUEUE symbol=XLF route=trend_long reason=ok score=1.4500" in caplog.text
    assert "ALLOCATOR_ENQUEUE symbol=XLE route=trend_long reason=ok score=1.4500" in caplog.text
    assert "ALLOCATOR_ENQUEUE symbol=INTC route=dynamic_momentum_override reason=ok score=1.7200" in caplog.text
    assert "ENTRY_TO_ALLOCATOR_FOLLOWUP_END symbol=XLF route=trend_long result=enqueue stage=allocator_queue" in caplog.text
    assert "ENTRY_TO_ALLOCATOR_FOLLOWUP_END symbol=XLE route=trend_long result=enqueue stage=allocator_queue" in caplog.text
    assert "ALLOCATOR_QUEUE_SUMMARY queued=3 symbols=XLF,XLE,INTC" in caplog.text
    assert "ALLOCATOR_QUEUE_CONTENTS symbols=XLF,XLE,INTC" in caplog.text
    assert "ALLOCATOR_QUEUE_STATE stage=before_allocator_drain allocator_on=true pending_count=3 symbols=XLF,XLE,INTC" in caplog.text
    assert "ALLOCATOR_QUEUE_STATE phase=before_allocator_drain pending_count=3 allocator_on=true symbols=XLF,XLE,INTC" in caplog.text
    assert "ALLOCATOR_DRAIN_ENTRY pending_count=3 allocator_on=true symbols=XLF,XLE,INTC" in caplog.text
    assert "ALLOCATOR_DRAIN_START count=3 symbols=XLF,XLE,INTC" in caplog.text
    assert "ALLOCATOR_PASS_START queued=3" in caplog.text
    assert "ALLOCATOR_PASS_AFTER_DEDUPE queued=3 symbols=INTC,XLF,XLE" in caplog.text
    assert "ALLOCATOR_INPUT count=3" in caplog.text
    assert "ALLOCATOR_INPUT_SYMBOLS count=3 symbols=INTC,XLF,XLE" in caplog.text
    assert "ALLOCATOR_INPUT_DETAIL count=3 symbols=INTC,XLF,XLE" in caplog.text
    assert "routes=dynamic_momentum_override,trend_long,trend_long" in caplog.text
    assert (
        "ALLOCATOR_ORDER_INTENT symbol=XLF" in caplog.text
        or "ALLOCATOR_SKIP symbol=XLF reason=" in caplog.text
    )
    assert (
        "ALLOCATOR_ORDER_INTENT symbol=XLE" in caplog.text
        or "ALLOCATOR_SKIP symbol=XLE reason=" in caplog.text
    )
    assert (
        "ALLOCATOR_ORDER_INTENT symbol=INTC" in caplog.text
        or "ALLOCATOR_SKIP symbol=INTC reason=" in caplog.text
    )
    assert "ALLOCATOR_DECISION symbol=XLF" in caplog.text
    assert "ALLOCATOR_DECISION symbol=XLE" in caplog.text
    assert "ALLOCATOR_DECISION symbol=INTC" in caplog.text
    assert "ALLOCATOR_DRAIN_DONE actions=3 pending_count=3 symbols=XLF,XLE,INTC" in caplog.text
    assert "ALLOCATOR_DRAIN_EXIT pending_count=0 reason=allocator_pass symbols=" in caplog.text
    assert "ALLOCATOR_QUEUE_STATE phase=after_drain pending_count=0 allocator_on=true symbols=" in caplog.text
    assert (
        "ALLOCATOR_INPUT count=3" in caplog.text
        or "ALLOCATOR_SKIP symbol=XLF reason=" in caplog.text
        or "ALLOCATOR_DRAIN_SKIPPED reason=" in caplog.text
    )


def test_deferred_final_true_append_resolves_followup(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.app.live_cycle import _append_capital_allocator_candidate

    candidates: list[dict] = []
    row = {"symbol": "XLF", "sym_u": "XLF", "route": "trend_long"}

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        _append_capital_allocator_candidate(
            candidates,
            row,
            symbol="XLF",
            route="trend_long",
            reason="ok",
            score=1.45,
            allocator_on=True,
            final=True,
            stage="allocator_queue",
            emit_log=False,
        )

    assert candidates == [row]
    assert (
        "ALLOCATOR_APPEND_TRACE symbol=XLF route=trend_long reason=ok "
        "score=1.4500 allocator_on=true final=true stage=allocator_queue"
    ) in caplog.text
    assert (
        "ENTRY_TO_ALLOCATOR_FOLLOWUP_END symbol=XLF route=trend_long "
        "result=append stage=allocator_queue"
    ) in caplog.text


def test_dynamic_momentum_override_append_now_enqueues_allocator_candidate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.app.live_cycle import _append_entry_eval_allocator_candidate_now

    candidates: list[dict] = []
    row = {
        "symbol": "CPNG",
        "sym_u": "CPNG",
        "route": "dynamic_momentum_override",
        "entry_eval_final": True,
        "decision": object(),
        "df": object(),
    }

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        emitted = _append_entry_eval_allocator_candidate_now(
            candidates,
            row,
            symbol="CPNG",
            route="dynamic_momentum_override",
            reason="ok",
            score=1.0,
            allocator_on=True,
            final=True,
            stage="entry_eval",
        )

    assert emitted is True
    assert candidates == [row]
    assert (
        "ENTRY_TO_ALLOCATOR_FOLLOWUP_START symbol=CPNG "
        "route=dynamic_momentum_override action=append_now stage=entry_eval"
    ) in caplog.text
    assert (
        "ALLOCATOR_APPEND_TRACE symbol=CPNG route=dynamic_momentum_override "
        "reason=ok score=1.0000 allocator_on=true final=true stage=entry_eval"
    ) in caplog.text
    assert (
        "ALLOCATOR_ENQUEUE symbol=CPNG route=dynamic_momentum_override "
        "reason=ok score=1.0000 allocator_on=true final=true stage=entry_eval"
    ) in caplog.text


def test_deferred_trend_long_followup_runs_immediately_after_entry_eval() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()
    log_eval_idx = next(
        i
        for i, line in enumerate(lines)
        if "allocator_followup=_allocator_entry_eval_followup_payload" in line
    )
    immediate_condition_idx = next(
        i
        for i, line in enumerate(lines)
        if "not _allocator_entry_eval_followup_emitted" in line and i > log_eval_idx
    )
    decision_allowed_idx = next(
        i
        for i, line in enumerate(lines)
        if 'getattr(decision, "allowed", False)' in line and i > immediate_condition_idx
    )
    order_request_idx = next(
        i
        for i, line in enumerate(lines)
        if 'getattr(decision, "order_request", None)' in line and i > decision_allowed_idx
    )
    ohlcv_present_idx = next(
        i
        for i, line in enumerate(lines)
        if 'not getattr(df, "empty", True)' in line and i > order_request_idx
    )
    append_idx = next(
        i
        for i, line in enumerate(lines)
        if "_append_entry_eval_allocator_candidate_now(" in line and i > ohlcv_present_idx
    )
    emitted_idx = next(
        i
        for i, line in enumerate(lines)
        if "_allocator_entry_eval_followup_emitted = True" in line and i > append_idx
    )
    option_route_idx = next(
        i
        for i, line in enumerate(lines)
        if "_options_route_observability_active(config, broker)" in line and i > log_eval_idx
    )

    assert log_eval_idx < immediate_condition_idx < decision_allowed_idx < order_request_idx < ohlcv_present_idx < append_idx < emitted_idx
    assert emitted_idx < option_route_idx
    assert '"followup_emitted"' in source
    assert '"append_now"' in source
    assert '_allocator_entry_eval_action = "defer"' not in source
    assert 'stage="entry_eval"' in source


def test_allocator_off_pending_candidates_log_deterministic_skip(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.app.live_cycle import (
        _log_allocator_drain_entry,
        _log_allocator_drain_exit,
        _log_allocator_drain_skipped,
        _log_allocator_queue_state,
        _log_allocator_skip_for_rows,
    )

    candidates = [
        {
            "symbol": "XLF",
            "sym_u": "XLF",
            "route": "trend_long",
            "entry_eval_final": True,
        }
    ]

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        _log_allocator_queue_state("before_drain", candidates, allocator_on=False)
        _log_allocator_drain_entry(candidates, allocator_on=False)
        _log_allocator_drain_skipped("allocator_off", candidates)
        _log_allocator_skip_for_rows("allocator_off", candidates)
        candidates.clear()
        _log_allocator_drain_exit(candidates, reason="allocator_off")
        _log_allocator_queue_state("after_drain", candidates, allocator_on=False)

    assert "ALLOCATOR_QUEUE_STATE phase=before_drain pending_count=1 allocator_on=false symbols=XLF" in caplog.text
    assert "ALLOCATOR_DRAIN_ENTRY pending_count=1 allocator_on=false symbols=XLF" in caplog.text
    assert "ALLOCATOR_DRAIN_SKIPPED reason=allocator_off pending_count=1 symbols=XLF" in caplog.text
    assert "ALLOCATOR_SKIP reason=allocator_off pending_count=1 symbols=XLF stage=allocator_drain" in caplog.text
    assert "ALLOCATOR_SKIP symbol=XLF reason=allocator_off route=trend_long stage=allocator_drain" in caplog.text
    assert "ALLOCATOR_DRAIN_EXIT pending_count=0 reason=allocator_off symbols=" in caplog.text
    assert "ALLOCATOR_QUEUE_STATE phase=after_drain pending_count=0 allocator_on=false symbols=" in caplog.text


def test_live_signal_scan_end_and_allocator_drain_fatal_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.app.live_cycle import (
        _log_allocator_drain_fatal,
        _log_live_signal_scan_end,
    )

    candidates = [
        {
            "symbol": "XLF",
            "sym_u": "XLF",
            "route": "trend_long",
            "entry_eval_final": True,
        },
        {
            "symbol": "INTC",
            "sym_u": "INTC",
            "route": "dynamic_momentum_override",
            "entry_eval_final": True,
        },
    ]

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        _log_live_signal_scan_end(
            user_id="paper_bot",
            pass_index=1,
            checked_count=42,
            rows=candidates,
            allocator_on=True,
        )
        assert _log_allocator_drain_fatal(
            "pending_before_sleep",
            candidates,
            allocator_on=True,
            stage="before_sleep",
        )
        assert not _log_allocator_drain_fatal(
            "allocator_off",
            candidates,
            allocator_on=False,
            stage="before_sleep",
        )
        assert not _log_allocator_drain_fatal(
            "empty_queue",
            [],
            allocator_on=True,
            stage="before_sleep",
        )

    assert (
        "LIVE_SIGNAL_SCAN_END user=paper_bot pass=1 checked=42 queued=2 "
        "allocator_on=true symbols=XLF,INTC"
    ) in caplog.text
    assert (
        "ALLOCATOR_DRAIN_FATAL reason=pending_before_sleep pending_count=2 "
        "allocator_on=true stage=before_sleep symbols=XLF,INTC"
    ) in caplog.text


def test_allocator_queue_state_before_sleep_is_after_drain_guard() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()
    scan_end_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_log_live_signal_scan_end(" in line
    )
    drain_exit_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if line.strip() == "_log_allocator_drain_exit("
        and "user_trading_pass_exception" not in "\n".join(lines[max(0, i - 5) : i + 5])
    )
    after_drain_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if '"after_drain"' in line and i > drain_exit_idx
    )
    before_sleep_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if '"before_sleep"' in line and i > after_drain_idx
    )
    allocator_off_reason_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if '"allocator_off"' in line and i < after_drain_idx
    )
    fatal_after_drain_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if '"pending_after_allocator_drain"' in line and i > after_drain_idx
    )
    fatal_before_sleep_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if '"pending_before_sleep"' in line and i > before_sleep_idx
    )

    assert scan_end_idx < drain_exit_idx
    assert drain_exit_idx < after_drain_idx < before_sleep_idx
    assert after_drain_idx < fatal_after_drain_idx < before_sleep_idx < fatal_before_sleep_idx
    assert allocator_off_reason_idx < after_drain_idx
    assert "ALLOCATOR_SKIP reason=%s pending_count=%d symbols=%s stage=%s" in source
    assert "ALLOCATOR_DRAIN_FATAL reason=%s pending_count=%d allocator_on=%s stage=%s symbols=%s" in source
    assert "LIVE_SIGNAL_SCAN_END user=%s pass=%d checked=%d queued=%d allocator_on=%s symbols=%s" in source


def test_allocator_on_final_true_has_enqueue_or_skip_guard() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    entry_log_source = (PROJECT_ROOT / "src" / "entry_eval_log.py").read_text()
    lines = source.splitlines()
    final_eval_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "final_signal=_eval_allowed" in line
    )
    pending_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_allocator_final_true_pending = bool(_eval_allowed)" in line
    )
    payload_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_allocator_entry_eval_followup_payload = None" in line
    )
    payload_pass_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "allocator_followup=_allocator_entry_eval_followup_payload" in line
    )
    attribution_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "record_trade_attribution_candidate(" in line
    )
    enqueue_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_append_capital_allocator_candidate(" in line and i > pending_idx
    )
    skip_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_log_allocator_enqueue_skip(" in line and i > pending_idx
    )
    guard_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if '"unhandled_allocator_enqueue_path"' in line
    )
    continue_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if line.strip() == "continue" and i > guard_idx
    )

    assert payload_idx < final_eval_idx < payload_pass_idx < attribution_idx < pending_idx
    assert pending_idx < skip_idx < enqueue_idx < guard_idx < continue_idx
    assert "_mark_allocator_final_true_handled()" in source
    assert '"action": _allocator_entry_eval_action' in source
    assert '_allocator_entry_eval_action = "append_now"' in source
    assert '_allocator_entry_eval_action = "defer"' not in source
    assert "ENTRY_EVAL_PASS symbol=%s route=%s reason=%s allocator_on=%s" in entry_log_source
    assert "ENTRY_TO_ALLOCATOR_TRACE symbol=%s route=%s decision_present=%s " in entry_log_source
    assert "ALLOCATOR_APPEND_TRACE symbol=%s route=%s reason=%s score=%.4f allocator_on=%s final=%s stage=%s" in source
    assert "ALLOCATOR_APPEND_SKIPPED symbol=%s route=%s reason=%s allocator_on=%s final=%s stage=%s" in entry_log_source
    assert "ALLOCATOR_ENQUEUE_SKIP symbol=%s route=%s reason=%s allocator_on=%s final=%s stage=%s" in source
    assert '"stage": "entry_eval"' in source
    assert "stage=\"allocator_queue\"" in source
    assert "def _allocator_entry_eval_followup(" in source
    assert "allocator_followup=_allocator_entry_eval_followup_payload" in source
    assert "_allocator_final_true_handled = bool(\n                                            _allocator_entry_eval_followup_emitted" in source


def test_dynamic_selected_symbols_reach_entry_eval_check_list() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()
    loop_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if line.strip() == "for symbol in _syms_scan:"
    )
    trace_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_log_dynamic_selected_entry_trace(" in line and i < loop_idx
    )
    skipped_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "DYNAMIC_SELECTED_ENTRY_SKIPPED symbol=%s reason=%s" in line
    )
    check_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "ENTRY_CHECK_SYMBOL symbol=%s source=%s is_dynamic=%s" in line
    )
    rank_skip_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "dynamic momentum rank: not in top %d" in line
    )
    catalyst_fastlane_rank_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "CATALYST_FASTLANE_BYPASS_RANK symbol=%s" in line
    )
    nonblocking_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "DYNAMIC_MOMENTUM_RANK_NONBLOCKING symbol=%s" in line
    )
    entry_eval_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "log_entry_eval(" in line
    )
    rank_block = "\n".join(lines[rank_skip_idx - 1 : nonblocking_idx + 4])

    assert trace_idx < loop_idx < check_idx < catalyst_fastlane_rank_idx < rank_skip_idx < nonblocking_idx < entry_eval_idx
    assert "continue" not in rank_block
    assert "DYNAMIC_SELECTED_DROPPED symbol=%s reason=%s" in source
    assert skipped_idx < loop_idx
    assert "DYNAMIC_SELECTED_ENTRY_TRACE symbol=%s in_universe=%s will_evaluate=%s reason=%s" in source


def test_dynamic_selected_symbols_bypass_empty_breakout_prefilter_before_entry_eval() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()

    breakout_none_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "— breakout prefilter: top sectors %s | candidates %s" in line
    )
    dynamic_set_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if line.strip() == "if _rfc_entry_pass == 1 and dynamic_set:"
        and i > breakout_none_idx
    )
    append_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if line.strip() == "_syms_scan = list(_syms_scan or []) + [_dyn_selected_u]"
        and i > dynamic_set_idx
    )
    log_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "DYNAMIC_ENTRY_PREFILTER_BYPASS symbol=%s reason=dynamic_selected" in line
        and i > append_idx
    )
    loop_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if line.strip() == "for symbol in _syms_scan:"
        and i > log_idx
    )
    route_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if '"dynamic_momentum_override"' in line
        and i > loop_idx
    )
    entry_eval_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "log_entry_eval(" in line
        and i > route_idx
    )

    assert breakout_none_idx < dynamic_set_idx < append_idx < log_idx < loop_idx
    assert loop_idx < route_idx < entry_eval_idx
    assert "_universe_symbol_set = {" in source
    assert "} | set(dynamic_set)" in source


def test_scanner_selected_dynamic_symbols_are_enqueued_for_entry_lane() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()

    scanner_set_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if line.strip() == "_scanner_selected_dynamic_set = {"
    )
    union_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if line.strip() == "dynamic_set |= _scanner_selected_dynamic_set"
        and i > scanner_set_idx
    )
    runtime_symbols_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_dynamic_entry_runtime_symbols = list(" in line
        and i > union_idx
    )
    append_scanner_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "+ sorted(_scanner_selected_dynamic_set)" in line
        and i > runtime_symbols_idx
    )
    enqueue_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_log_dynamic_entry_candidate_enqueued(" in line
        and i > append_scanner_idx
    )
    loop_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if line.strip() == "for symbol in _syms_scan:"
        and i > enqueue_idx
    )
    scanner_lane_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if line.strip() == "_is_scanner_selected_lane = _sym_lane_u in _scanner_selected_dynamic_set"
        and i > loop_idx
    )
    scanner_candidate_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if line.strip() == "or sym_u in _scanner_selected_dynamic_set"
        and i > scanner_lane_idx
    )
    entry_eval_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "log_entry_eval(" in line
        and i > scanner_candidate_idx
    )

    assert scanner_set_idx < union_idx < runtime_symbols_idx < append_scanner_idx
    assert append_scanner_idx < enqueue_idx < loop_idx < scanner_lane_idx
    assert scanner_lane_idx < scanner_candidate_idx < entry_eval_idx
    assert "DYNAMIC_ENTRY_CANDIDATE_ENQUEUED symbol=%s source=%s" in source
    assert "DYNAMIC_ENTRY_CANDIDATE_SKIPPED symbol=%s reason=%s" in source


def test_dynamic_entry_candidate_audit_logs_scanner_selected_symbols(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.app.live_cycle import (
        _finalize_dynamic_entry_eval_audit,
        _log_dynamic_entry_candidate_enqueued,
        _log_dynamic_entry_scanset_debug,
        _log_dynamic_entry_candidate_skipped,
        _log_dynamic_entry_eval_dropped,
        _log_dynamic_entry_eval_start,
    )

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        _log_dynamic_entry_scanset_debug(
            selected=["GALT", "BLZE", "SOXS"],
            universe_added=["GALT", "BLZE", "SOXS", "SPCH"],
            entry_scan_symbols=["IWM", "XLK", "GALT", "BLZE", "SOXS", "SPCH"],
        )
        for symbol in ("GALT", "BLZE", "SOXS"):
            _log_dynamic_entry_candidate_enqueued(symbol, source="scanner_selected")
            _log_dynamic_entry_eval_start(
                symbol,
                source="scanner_selected",
                route="dynamic_momentum_override",
            )
        _log_dynamic_entry_candidate_skipped("SOXS", reason="dynamic_entry_disabled")
        _log_dynamic_entry_eval_dropped("SOXS", reason="dynamic_entry_disabled")
        dropped: set[str] = set()
        _finalize_dynamic_entry_eval_audit(
            enqueued_symbols={"BLZE", "GALT", "SOXS"},
            started_symbols={"BLZE"},
            dropped_symbols=dropped,
            reason="not_processed_after_entry_loop",
        )

    assert (
        "DYNAMIC_ENTRY_SCANSET_DEBUG selected=['GALT', 'BLZE', 'SOXS'] "
        "universe_added=['GALT', 'BLZE', 'SOXS', 'SPCH']"
    ) in caplog.text
    assert "DYNAMIC_ENTRY_CANDIDATE_ENQUEUED symbol=GALT source=scanner_selected" in caplog.text
    assert "DYNAMIC_ENTRY_CANDIDATE_ENQUEUED symbol=BLZE source=scanner_selected" in caplog.text
    assert "DYNAMIC_ENTRY_CANDIDATE_ENQUEUED symbol=SOXS source=scanner_selected" in caplog.text
    assert "DYNAMIC_ENTRY_CANDIDATE_SKIPPED symbol=SOXS reason=dynamic_entry_disabled" in caplog.text
    assert (
        "DYNAMIC_ENTRY_EVAL_START symbol=BLZE source=scanner_selected "
        "route=dynamic_momentum_override"
    ) in caplog.text
    assert "DYNAMIC_ENTRY_EVAL_DROPPED symbol=SOXS reason=dynamic_entry_disabled" in caplog.text
    assert "DYNAMIC_ENTRY_EVAL_DROPPED symbol=GALT reason=not_processed_after_entry_loop" in caplog.text
    assert "SOXS" in dropped


def test_dynamic_scan_selected_path_emits_entry_scanset_debug_before_scheduler() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()

    scan_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "dynamic_scan_result = scan_candidates_batch(" in line
    )
    selected_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_dynamic_scan_selected_symbols = [" in line
        and i > scan_idx
    )
    universe_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if 'f"DYNAMIC_UNIVERSE: base={len(core_symbols)} added={dynamic_symbols} total={len(symbols)}"' in line
        and i > selected_idx
    )
    debug_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_log_dynamic_entry_scanset_debug(" in line
        and i > universe_idx
    )
    scheduler_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "# ---------- evaluate_entries() ----------" in line
        and i > debug_idx
    )

    assert scan_idx < selected_idx < universe_idx < debug_idx < scheduler_idx
    assert "selected=_dynamic_scan_selected_symbols" in source
    assert "universe_added=dynamic_symbols" in source
    assert "entry_scan_symbols=_dynamic_entry_projected_scan_symbols" in source
    assert "reason=\"not_added_to_dynamic_universe\"" in source
    assert "reason=\"not_in_entry_scan\"" in source


def test_scanner_selected_symbols_get_skip_log_when_entry_scheduler_blocks() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()

    decision_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "ENTRY_LANE_DECISION user=%s now_et=%s entries_on=%s entry_scan_allowed=%s" in line
    )
    skip_guard_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if line.strip() == "if _dynamic_scan_selected_symbols and not do_dynamic_entry:"
        and i > decision_idx
    )
    skip_call_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_log_dynamic_entry_candidate_skipped(" in line
        and i > skip_guard_idx
    )
    do_any_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "do_any_entry = do_core_entry or do_dynamic_entry" in line
    )

    assert decision_idx < skip_guard_idx < skip_call_idx < do_any_idx
    assert '_dynamic_entry_block_reason = "entries_disabled"' in source
    assert '_dynamic_entry_block_reason = "entry_scan_not_allowed"' in source
    assert '_dynamic_entry_block_reason = "startup_warmup"' in source


def test_scanner_selected_dynamic_symbols_force_dynamic_route_and_eval_audit() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()

    scanner_candidate_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if line.strip() == "or sym_u in _scanner_selected_dynamic_set"
    )
    prefilter_bypass_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "DYNAMIC_ENTRY_PREFILTER_BYPASS symbol=%s reason=scanner_selected" in line
        and i > scanner_candidate_idx
    )
    route_force_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if line.strip() == '_entry_gates_route = "dynamic_momentum_override"'
        and i > prefilter_bypass_idx
    )
    eval_start_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_log_dynamic_entry_eval_start(" in line
        and i > route_force_idx
    )
    log_entry_eval_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "log_entry_eval(" in line
        and i > eval_start_idx
    )

    assert scanner_candidate_idx < prefilter_bypass_idx < route_force_idx
    assert route_force_idx < eval_start_idx < log_entry_eval_idx
    assert "DYNAMIC_ENTRY_EVAL_START symbol=%s source=%s route=%s" in source
    assert "DYNAMIC_ENTRY_EVAL_DROPPED symbol=%s reason=%s" in source


def test_enqueued_scanner_selected_symbols_reconciled_after_entry_loop() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()

    enqueue_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_dynamic_entry_enqueued_symbols.add(_sym_selected_u)" in line
    )
    loop_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if line.strip() == "for symbol in _syms_scan:"
        and i > enqueue_idx
    )
    finalizer_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_finalize_dynamic_entry_eval_audit(" in line
        and i > loop_idx
    )
    enqueued_arg_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "enqueued_symbols=_dynamic_entry_enqueued_symbols" in line
        and i > finalizer_idx
    )
    dropped_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if 'reason="not_processed_after_entry_loop"' in line
        and i > enqueued_arg_idx
    )
    scan_end_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_log_live_signal_scan_end(" in line
        and i > dropped_idx
    )

    assert enqueue_idx < loop_idx < finalizer_idx < enqueued_arg_idx < dropped_idx < scan_end_idx


def test_scanner_selected_symbols_prioritized_before_core_scan() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()

    scanner_set_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if line.strip() == "_scanner_selected_dynamic_set = {"
    )
    scanner_order_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_scanner_ordered_symbols = [" in line
        and i > scanner_set_idx
    )
    non_scanner_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_non_scanner_ordered_symbols = [" in line
        and i > scanner_order_idx
    )
    assign_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if line.strip() == "_syms_scan = list("
        and i > non_scanner_idx
    )
    loop_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if line.strip() == "for symbol in _syms_scan:"
        and i > assign_idx
    )

    assert scanner_set_idx < scanner_order_idx < non_scanner_idx < assign_idx < loop_idx


def test_scanner_selected_dynamic_lane_runs_when_trend_longs_blocked() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()

    enqueue_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_dynamic_entry_enqueued_symbols.add(_sym_selected_u)" in line
    )
    lane_active_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if line.strip() == "_scanner_dynamic_entry_lane_active = bool(_dynamic_entry_enqueued_symbols)"
        and i > enqueue_idx
    )
    dedicated_log_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "DYNAMIC_ENTRY_DEDICATED_LANE_ACTIVE symbols=%s reason=scanner_selected" in line
        and i > lane_active_idx
    )
    entry_machine_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if line.strip() == "if run_trend_long_entries or _scanner_dynamic_entry_lane_active:"
        and i > dedicated_log_idx
    )
    core_skip_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if line.strip() == "if not _is_dyn_lane and not run_trend_long_entries:"
        and i > entry_machine_idx
    )
    eval_start_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_log_dynamic_entry_eval_start(" in line
        and i > core_skip_idx
    )

    assert enqueue_idx < lane_active_idx < dedicated_log_idx < entry_machine_idx
    assert entry_machine_idx < core_skip_idx < eval_start_idx
    assert "do_dynamic_entry and _dynamic_entry_enqueued_symbols" not in source


def test_scanner_selected_dynamic_bypasses_cadence_gate_not_safety_gates() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()

    trace_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "will_evaluate=bool(" in line
    )
    scanner_or_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "or _dyn_selected_sym in _scanner_selected_dynamic_set" in line
        and i > trace_idx
    )
    route_disabled_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "and not _is_scanner_selected_lane" in line
        and i > scanner_or_idx
    )
    safety_skip_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if line.strip() == "if _portfolio_mode_reduce_only:"
        and i > route_disabled_idx
    )
    eval_start_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_log_dynamic_entry_eval_start(" in line
        and i > safety_skip_idx
    )

    assert trace_idx < scanner_or_idx < route_disabled_idx < safety_skip_idx < eval_start_idx


def test_sqqq_regime_skip_does_not_block_scanner_selected_dynamic_lane() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()

    sqqq_policy_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "run_bear_inverse_flow(" in line
    )
    lane_active_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if line.strip() == "_scanner_dynamic_entry_lane_active = bool(_dynamic_entry_enqueued_symbols)"
        and i > sqqq_policy_idx
    )
    entry_machine_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if line.strip() == "if run_trend_long_entries or _scanner_dynamic_entry_lane_active:"
        and i > lane_active_idx
    )
    scanner_order_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_scanner_ordered_symbols = [" in line
        and i > entry_machine_idx
    )
    eval_start_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_log_dynamic_entry_eval_start(" in line
        and i > scanner_order_idx
    )

    assert sqqq_policy_idx < lane_active_idx < entry_machine_idx
    assert entry_machine_idx < scanner_order_idx < eval_start_idx


def test_dynamic_lane_not_processed_has_immediate_drop_log() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()

    ordered_set_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_ordered_scan_symbol_set = {" in line
    )
    missing_guard_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_dynamic_entry_enqueued_symbols" in line
        and "- _ordered_scan_symbol_set" in lines[i]
        and i > ordered_set_idx
    )
    drop_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if 'reason="dynamic_lane_not_processed"' in line
        and i > missing_guard_idx
    )
    loop_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if line.strip() == "for symbol in _syms_scan:"
        and i > drop_idx
    )

    assert ordered_set_idx < missing_guard_idx < drop_idx < loop_idx


def test_dynamic_entry_eval_audit_finalizer_runs_in_user_epilogue() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()

    inner_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_log_live_signal_scan_end(" in line
    )
    epilogue_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_finalize_dynamic_entry_eval_audit(" in line
        and i > inner_idx
    )
    summary_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "ENTRY_DECISION_SUMMARY options_attempted=%d" in line
        and i > epilogue_idx
    )
    exception_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if 'reason="not_processed_after_entry_loop"' in line
        and i > summary_idx
    )

    assert inner_idx < epilogue_idx < summary_idx < exception_idx


def test_dynamic_entry_eval_audit_finalizer_runs_in_per_user_finally() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()

    user_try_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if line.strip() == "try:"
        and i > next(
            j
            for j, candidate in enumerate(lines, start=1)
            if "for _uctx in user_contexts:" in candidate
        )
    )
    init_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_dynamic_entry_enqueued_symbols: set[str] = set()" in line
        and i > user_try_idx
    )
    early_continue_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if line.strip() == "continue"
        and i > init_idx
    )
    finally_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if line.strip() == "finally:"
        and i > early_continue_idx
    )
    finalizer_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_finalize_dynamic_entry_eval_audit(" in line
        and i > finally_idx
    )
    reason_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if 'reason="not_processed_after_entry_loop"' in line
        and i > finalizer_idx
    )

    assert user_try_idx < init_idx < early_continue_idx < finally_idx < finalizer_idx < reason_idx


def test_dynamic_selected_entry_drop_diagnostics_cover_pre_entry_gates() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()

    assert "DYNAMIC_SELECTED_ENTRY_DROP symbol=%s stage=%s reason=%s detail=%s" in source
    assert "DYNAMIC_SELECTED_ENTRY_EVAL_START symbol=%s route_candidate=%s detail=%s" in source
    assert "_log_dynamic_entry_candidate_skipped(" in source
    assert "stage=\"scoring_top_n\"" in source
    assert "\"not_in_scoring_top_n_candidates\"" in source
    assert "stage=\"history_guard\"" in source
    assert "\"short_history\"" in source
    assert "stage=\"trend_prefilter\"" in source
    assert "\"trend_prefilter\"" in source
    assert "stage=\"cooldown\"" in source
    assert "\"cooldown\"" in source
    assert "stage=\"route_disabled\"" in source
    assert "stage=\"bad_quote_or_spread\"" in source
    assert "stage=\"missing_ohlcv_or_entry_guard\"" in source


def test_live_catalyst_fastlane_trace_precedes_rank_skip_and_uses_relaxed_rvol() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()
    trace_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_log_catalyst_fastlane_entry_trace(sym_u, _rank_fastlane_trace)" in line
    )
    rank_skip_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "dynamic momentum rank: not in top %d" in line
    )
    rvol_call_idx = next(
        i
        for i, line in enumerate(lines, start=1)
        if "_ok_m, _rsn_m = dynamic_momentum_entry_passes(" in line
    )

    assert trace_idx < rank_skip_idx < rvol_call_idx
    assert '_dme_eff_cfg["catalyst_fastlane_active"] = True' in source
    assert '_dme_eff_cfg["catalyst_min_relative_volume"] = float(' in source


def test_orcl_like_premarket_fastlane_trace_is_eligible_at_rvol_036() -> None:
    from src.app.live_cycle import _catalyst_fastlane_entry_trace_fields

    trace = _catalyst_fastlane_entry_trace_fields(
        premarket_injected=True,
        news_score=7,
        event_score=7.2,
        catalyst_score=0.72,
        catalyst_age_minutes=184.0,
        relative_volume=0.36,
        threshold=0.35,
    )

    assert trace["eligible"] is True
    assert trace["reason"] == "ok"
    assert trace["threshold"] == pytest.approx(0.35)
    assert trace["relative_volume"] == pytest.approx(0.36)


def test_orcl_like_live_fastlane_entry_uses_035_not_normal_075() -> None:
    from src.dynamic_universe import dynamic_momentum_entry_passes

    bars_1m = pd.DataFrame(
        {
            "high": [100.2, 100.3],
            "low": [99.8, 99.9],
            "open": [100.0, 100.0],
            "close": [100.0, 100.1],
            "volume": [50_000, 50_000],
        }
    )

    ok, reason = dynamic_momentum_entry_passes(
        gain_pct=3.0,
        relative_volume=0.36,
        vwap_above=True,
        spread_pct=1.0,
        bars_1m=bars_1m,
        bars_5m=pd.DataFrame({"close": [100.0, 100.1]}),
        ref_price=100.1,
        cfg={
            "enabled": True,
            "min_day_gain_pct": 3.0,
            "min_relative_volume": 0.75,
            "catalyst_fastlane_active": True,
            "catalyst_min_relative_volume": 0.35,
        },
        symbol="ORCL",
        news_score=7,
        event_score=7.2,
        catalyst_score=0.72,
        catalyst_age_minutes=184.0,
        is_dynamic=True,
    )

    assert ok is True
    assert reason == "ok catalyst_fastlane"


def test_dynamic_high_conviction_trend_prefilter_override_allows_fresh_dynamic() -> None:
    from src.app.live_cycle import _dynamic_high_conviction_trend_prefilter_override_decision

    allowed, reason, score = _dynamic_high_conviction_trend_prefilter_override_decision(
        {
            "trading": {
                "dynamic": {
                    "high_conviction_news_override": {
                        "enabled": True,
                        "min_news_score": 7.0,
                        "min_event_score": 7.0,
                        "min_catalyst_score": 8.0,
                        "min_relative_volume": 1.5,
                        "max_catalyst_age_minutes": 180,
                        "require_positive_sentiment": True,
                    }
                }
            }
        },
        route="dynamic_momentum",
        is_dynamic_candidate=True,
        news_score=7.4,
        event_score=0.0,
        catalyst_score=0.0,
        catalyst_age_minutes=45,
        relative_volume=2.1,
        sentiment=0.2,
    )

    assert allowed is True
    assert reason == "high_conviction_fresh_catalyst"
    assert score == pytest.approx(7.4)


def test_dynamic_high_conviction_trend_prefilter_override_blocks_non_dynamic() -> None:
    from src.app.live_cycle import _dynamic_high_conviction_trend_prefilter_override_decision

    allowed, reason, _score = _dynamic_high_conviction_trend_prefilter_override_decision(
        {
            "trading": {
                "dynamic": {
                    "high_conviction_news_override": {
                        "enabled": True,
                        "require_positive_sentiment": False,
                    }
                }
            }
        },
        route="trend_long",
        is_dynamic_candidate=True,
        news_score=9,
        catalyst_age_minutes=10,
        relative_volume=3.0,
    )

    assert allowed is False
    assert reason == "route_not_dynamic"


def test_dynamic_high_conviction_trend_prefilter_override_blocks_stale_or_safety() -> None:
    from src.app.live_cycle import _dynamic_high_conviction_trend_prefilter_override_decision

    cfg = {
        "trading": {
            "dynamic": {
                "high_conviction_news_override": {
                    "enabled": True,
                    "min_news_score": 7.0,
                    "min_relative_volume": 1.5,
                    "max_catalyst_age_minutes": 180,
                    "require_positive_sentiment": False,
                }
            }
        }
    }

    stale, stale_reason, _ = _dynamic_high_conviction_trend_prefilter_override_decision(
        cfg,
        route="dynamic_momentum",
        is_dynamic_candidate=True,
        news_score=9,
        catalyst_age_minutes=181,
        relative_volume=2.0,
    )
    bearish, bearish_reason, _ = _dynamic_high_conviction_trend_prefilter_override_decision(
        cfg,
        route="dynamic_momentum",
        is_dynamic_candidate=True,
        news_score=9,
        catalyst_age_minutes=10,
        relative_volume=2.0,
        severe_bearish_lockout=True,
    )
    cooldown, cooldown_reason, _ = _dynamic_high_conviction_trend_prefilter_override_decision(
        cfg,
        route="dynamic_momentum",
        is_dynamic_candidate=True,
        news_score=9,
        catalyst_age_minutes=10,
        relative_volume=2.0,
        cooldown_active=True,
    )

    assert stale is False
    assert stale_reason == "stale_catalyst"
    assert bearish is False
    assert bearish_reason == "severe_bearish_lockout"
    assert cooldown is False
    assert cooldown_reason == "cooldown"


def test_high_score_catalyst_bypasses_trend_prefilter() -> None:
    from src.app.live_cycle import _news_trend_prefilter_override_decision

    allowed, score, threshold = _news_trend_prefilter_override_decision(
        {
            "entries": {
                "news_override_trend_prefilter_enabled": True,
                "news_override_min_score": 8,
            }
        },
        news_score=3,
        event_score=8,
        catalyst_score=0,
    )

    assert allowed is True
    assert score == pytest.approx(8.0)
    assert threshold == pytest.approx(8.0)


def test_unit_interval_catalyst_score_uses_news_score_scale_for_trend_prefilter() -> None:
    from src.app.live_cycle import _news_trend_prefilter_override_decision

    allowed, score, threshold = _news_trend_prefilter_override_decision(
        {"entries": {"news_override_min_score": 8}},
        news_score=0,
        event_score=0,
        catalyst_score=0.8,
    )

    assert allowed is True
    assert score == pytest.approx(8.0)
    assert threshold == pytest.approx(8.0)


def test_low_score_news_does_not_bypass_trend_prefilter() -> None:
    from src.app.live_cycle import _news_trend_prefilter_override_decision

    allowed, score, threshold = _news_trend_prefilter_override_decision(
        {
            "entries": {
                "news_override_trend_prefilter_enabled": True,
                "news_override_min_score": 8,
            }
        },
        news_score=7.9,
        event_score=0,
        catalyst_score=0,
    )

    assert allowed is False
    assert score == pytest.approx(7.9)
    assert threshold == pytest.approx(8.0)


def test_catalyst_trend_override_strong_catalyst_below_ema_passes() -> None:
    from src.app.live_cycle import _catalyst_trend_override_decision

    allowed, reason, rank = _catalyst_trend_override_decision(
        news_score=8.2,
        catalyst_score=0.40,
        premarket_rank=4,
        momentum_confirmed=True,
        spread_ok=True,
        atr_ok=True,
        price_above_vwap=True,
        day_gain_pct=5.4,
    )

    assert allowed is True
    assert reason == "ok"
    assert rank == 4


def test_catalyst_trend_override_weak_catalyst_below_ema_rejected() -> None:
    from src.app.live_cycle import _catalyst_trend_override_decision

    allowed, reason, _rank = _catalyst_trend_override_decision(
        news_score=7.9,
        catalyst_score=0.79,
        premarket_rank=4,
        momentum_confirmed=True,
        spread_ok=True,
        atr_ok=True,
        price_above_vwap=True,
        day_gain_pct=5.4,
    )

    assert allowed is False
    assert reason == "weak_catalyst"


@pytest.mark.parametrize(
    ("field", "expected_reason"),
    [
        ("spread_ok", "spread"),
        ("atr_ok", "atr"),
        ("price_above_vwap", "vwap"),
    ],
)
def test_catalyst_trend_override_hard_failures_still_reject(
    field: str,
    expected_reason: str,
) -> None:
    from src.app.live_cycle import _catalyst_trend_override_decision

    kwargs = {
        "news_score": 9,
        "catalyst_score": 0.91,
        "premarket_rank": 1,
        "momentum_confirmed": True,
        "spread_ok": True,
        "atr_ok": True,
        "price_above_vwap": True,
        "day_gain_pct": 7.3,
    }
    kwargs[field] = False

    allowed, reason, rank = _catalyst_trend_override_decision(**kwargs)

    assert allowed is False
    assert reason == expected_reason
    assert rank == 1


def test_catalyst_trend_override_requires_premarket_rank_momentum_and_gain() -> None:
    from src.app.live_cycle import _catalyst_trend_override_decision

    no_rank, no_rank_reason, _ = _catalyst_trend_override_decision(
        news_score=9,
        catalyst_score=0.91,
        premarket_rank=11,
        momentum_confirmed=True,
        spread_ok=True,
        atr_ok=True,
        price_above_vwap=True,
        day_gain_pct=7.3,
    )
    no_momentum, no_momentum_reason, _ = _catalyst_trend_override_decision(
        news_score=9,
        catalyst_score=0.91,
        premarket_rank=1,
        momentum_confirmed=False,
        spread_ok=True,
        atr_ok=True,
        price_above_vwap=True,
        day_gain_pct=7.3,
    )
    low_gain, low_gain_reason, _ = _catalyst_trend_override_decision(
        news_score=9,
        catalyst_score=0.91,
        premarket_rank=1,
        momentum_confirmed=True,
        spread_ok=True,
        atr_ok=True,
        price_above_vwap=True,
        day_gain_pct=5.0,
    )

    assert no_rank is False
    assert no_rank_reason == "premarket_rank"
    assert no_momentum is False
    assert no_momentum_reason == "momentum_confirmation"
    assert low_gain is False
    assert low_gain_reason == "day_gain"


def test_catalyst_trend_override_log_is_wired() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()

    assert "CATALYST_TREND_OVERRIDE symbol=%s close=%.4f ema20=%s" in source
    assert "trend_long_ok = True" in source


def test_news_trend_override_only_bypasses_prefilter_not_safety_filters() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()

    override_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if "NEWS_TREND_OVERRIDE symbol=%s score=%.2f reason=high_conviction_catalyst" in line
    )
    prefilter_skip_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if "below MAs (trend prefilter); no news override or alternate entry" in line
    )
    quote_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if index > prefilter_skip_line and "quote = broker.get_latest_quote(symbol)" in line
    )
    vwap_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if index > quote_line and "DYNAMIC_VWAP_GUARD" in line
    )

    assert prefilter_skip_line < quote_line < vwap_line < override_line
    assert "and not _news_trend_override" in source


def test_core_news_trend_override_creates_entry_override_and_runs_safety_checks() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()

    log_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if "NEWS_TREND_OVERRIDE symbol=%s score=%.2f reason=high_conviction_catalyst" in line
    )
    route_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if '_entry_gates_route = "news_trend_override"' in line
    )
    trend_route_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if "trend_scan_route_label(" in line and index > route_line
    )
    debug_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if "NEWS_OVERRIDE_DEBUG symbol=%s news_score=%.2f event_score=%.2f" in line
    )
    source_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if '"source": "high_conviction_catalyst"' in line
    )
    run_gates_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if index > route_line and "decision = engine.run_entry_gates(" in line
    )
    entry_eval_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if "log_entry_eval(" in line
    )
    route_selected_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if "ENTRY_ROUTE_SELECTED symbol=%s route=%s override=%s score=%.2f" in line
    )

    assert debug_line < log_line < route_line < trend_route_line
    assert route_line < source_line < run_gates_line < route_selected_line < entry_eval_line
    assert "_entry_eval_route_log_from_metadata(" in source
    assert "spread_pct=spread_pct" in source[lines[run_gates_line - 1].find("decision") :]


def test_scanner_selected_dynamic_route_log_initialized_before_rvol_guard() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()

    loop_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if "for symbol in _syms_scan:" in line
    )
    symbol_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if index > loop_line and "sym_u = _sym_lane_u" in line
    )
    dynamic_added_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if index > symbol_line and "_is_dynamic_added = (" in line
    )
    route_log_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if index > dynamic_added_line and "_route_log = (" in line
    )
    scanner_route_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if index > route_log_line and '"dynamic_momentum_override"' in line
    )
    dynamic_disabled_guard_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if index > scanner_route_line and "and not do_dynamic_entry" in line
    )
    first_rvol_route_use = next(
        index
        for index, line in enumerate(lines, start=1)
        if index > dynamic_added_line and 'str(_route_log or _entry_gates_route or "n/a")' in line
    )

    assert loop_line < symbol_line < dynamic_added_line < route_log_line
    assert route_log_line < scanner_route_line < dynamic_disabled_guard_line
    assert dynamic_disabled_guard_line < first_rvol_route_use


def test_core_avgo_high_conviction_logs_news_route_not_trend_long() -> None:
    from src.app.live_cycle import _entry_eval_route_log_from_metadata

    route = _entry_eval_route_log_from_metadata(
        "trend_long",
        {
            "source": "high_conviction_catalyst",
            "news_trend_override": True,
            "event_score": 8.0,
        },
    )

    assert route == "news_trend_override"
    assert route != "trend_long"


def _intraday_bars(closes: list[float], *, volume: float = 1000.0):
    import pandas as pd

    return pd.DataFrame(
        {
            "close": closes,
            "high": [c + 0.05 for c in closes],
            "low": [c - 0.05 for c in closes],
            "volume": [volume] * len(closes),
        }
    )


def test_news_trend_override_blocks_large_intraday_loss() -> None:
    from src.app.live_cycle import _news_trend_override_price_confirmation

    allowed, reason, day_change, _vwap = _news_trend_override_price_confirmation(
        price=100.5,
        day_change_pct=-14.0,
        bars_1m=_intraday_bars([99.0, 100.0, 100.5]),
        bars_5m=_intraday_bars([100.0, 100.5]),
        config={"entries": {"max_day_loss_pct_for_news_override": -5.0}},
    )

    assert allowed is False
    assert reason == "day_loss_too_large"
    assert day_change == pytest.approx(-14.0)


def test_news_trend_override_blocks_below_vwap() -> None:
    from src.app.live_cycle import _news_trend_override_price_confirmation

    allowed, reason, _day_change, vwap = _news_trend_override_price_confirmation(
        price=99.0,
        day_change_pct=1.0,
        bars_1m=_intraday_bars([100.0, 100.2, 99.0]),
        bars_5m=_intraday_bars([98.0, 99.0]),
        config={"entries": {"max_day_loss_pct_for_news_override": -5.0}},
    )

    assert allowed is False
    assert reason == "below_vwap"
    assert vwap is not None


def test_news_trend_override_allows_vwap_and_positive_momentum() -> None:
    from src.app.live_cycle import _news_trend_override_price_confirmation

    allowed, reason, _day_change, vwap = _news_trend_override_price_confirmation(
        price=101.5,
        day_change_pct=1.0,
        bars_1m=_intraday_bars([100.0, 100.8, 101.5]),
        bars_5m=_intraday_bars([100.0, 101.5]),
        config={"entries": {"max_day_loss_pct_for_news_override": -5.0}},
    )

    assert allowed is True
    assert reason == "ok"
    assert vwap is not None


def test_news_trend_override_blocks_without_5m_momentum() -> None:
    from src.app.live_cycle import _news_trend_override_price_confirmation

    allowed, reason, _day_change, vwap = _news_trend_override_price_confirmation(
        price=101.5,
        day_change_pct=1.0,
        bars_1m=_intraday_bars([100.0, 100.8, 101.5]),
        bars_5m=_intraday_bars([102.0, 101.5]),
        config={"entries": {"max_day_loss_pct_for_news_override": -5.0}},
    )

    assert allowed is False
    assert reason == "no_5m_momentum"
    assert vwap is not None


def test_dynamic_vwap_live_skip_branch_logs_guard_before_skip() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()
    branch_line = next(
        i
        for i, line in enumerate(lines, start=1)
        if 'if _rsn_m == "price not above session VWAP":' in line
    )
    window = lines[branch_line - 1 : branch_line + 80]
    guard_idx = next(
        i
        for i, line in enumerate(window, start=1)
        if "DYNAMIC_VWAP_GUARD symbol=%s price=%.4f vwap=%s distance_pct=%s news_score=%d allowed=%s reason=%s"
        in line
    )
    allowed_idx = next(
        i
        for i, line in enumerate(window, start=1)
        if "if _vwap_guard_allowed:" in line
    )
    skip_idx = next(
        i
        for i, line in enumerate(window, start=1)
        if "_log_allocator_dynamic_skipped(" in line
    )

    assert guard_idx < allowed_idx < skip_idx
    assert 'if _rsn_m == "price not above session VWAP":' in window[0]


def test_strong_news_persistence_map_keeps_three_hour_candidates() -> None:
    from src.app.live_cycle import _strong_news_dynamic_persistence_map

    artifacts = {
        "NVTS": {
            "news_score": 8,
            "age_minutes": 120,
            "headline": "NVTS strong catalyst",
            "catalyst_type": "earnings",
        },
        "OLD": {
            "news_score": 8,
            "age_minutes": 301,
            "headline": "stale",
        },
        "WEAK": {
            "news_score": 5,
            "age_minutes": 60,
        },
    }

    out = _strong_news_dynamic_persistence_map(artifacts, [])
    assert set(out) == {"NVTS"}
    assert out["NVTS"]["news_score"] == 8
    assert out["NVTS"]["age_minutes"] == 120


def test_dynamic_fastlane_active_only_first_30_minutes_after_open() -> None:
    from src.app.live_cycle import _dynamic_fastlane_window_active

    assert _dynamic_fastlane_window_active(
        datetime(2026, 6, 4, 9, 30, tzinfo=timezone.utc).astimezone()
    ) is False
    assert _dynamic_fastlane_window_active(
        datetime(2026, 6, 4, 13, 30, tzinfo=timezone.utc)
    ) is True
    assert _dynamic_fastlane_window_active(
        datetime(2026, 6, 4, 13, 59, tzinfo=timezone.utc)
    ) is True
    assert _dynamic_fastlane_window_active(
        datetime(2026, 6, 4, 14, 0, tzinfo=timezone.utc)
    ) is False


def test_dynamic_fastlane_allows_only_fresh_strong_news() -> None:
    from src.app.live_cycle import _dynamic_fastlane_allowed

    now = datetime(2026, 6, 4, 13, 35, tzinfo=timezone.utc)
    assert _dynamic_fastlane_allowed(now, news_score=7, catalyst_age_minutes=180)[0] is True
    assert _dynamic_fastlane_allowed(now, news_score=7, catalyst_age_minutes=300)[0] is True
    assert _dynamic_fastlane_allowed(now, news_score=6, catalyst_age_minutes=30) == (
        False,
        "weak_or_stale_news",
    )
    assert _dynamic_fastlane_allowed(now, news_score=8, catalyst_age_minutes=301) == (
        False,
        "weak_or_stale_news",
    )


def test_startup_warmup_bypass_symbols_for_strong_news_only() -> None:
    from src.app.live_cycle import _dynamic_fastlane_startup_bypass_symbols

    now = datetime(2026, 6, 4, 13, 35, tzinfo=timezone.utc)
    out = _dynamic_fastlane_startup_bypass_symbols(
        ["XOS", "WEAK", "OLD"],
        {
            "XOS": {"news_score": 8, "age_minutes": 45},
            "WEAK": {"news_score": 6, "age_minutes": 20},
            "OLD": {"news_score": 9, "age_minutes": 301},
        },
        now,
    )

    assert out == ["XOS"]


def test_dynamic_fastlane_bypass_log_shape(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    from src.app.live_cycle import _log_dynamic_fastlane

    caplog.set_level(logging.INFO)
    _log_dynamic_fastlane(
        "xos",
        news_score=8,
        catalyst_age_minutes=42.5,
        allowed=True,
        reason="strong_news_open_fastlane",
    )

    assert (
        "DYNAMIC_FASTLANE symbol=XOS news_score=8 catalyst_age_minutes=42.5 "
        "allowed=true reason=strong_news_open_fastlane"
    ) in caplog.text


def test_strong_dynamic_etf_penalty_reduces_index_candidates(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    from src.app.live_cycle import _apply_strong_dynamic_etf_penalty

    row = {"strength_eff": 0.92, "composite_score": 0.88, "priority_score": 0.81}
    caplog.set_level(logging.INFO)
    penalty = _apply_strong_dynamic_etf_penalty(
        row,
        "SPY",
        strong_dynamic_candidates_present=True,
    )

    assert penalty == pytest.approx(0.5)
    assert row["strength_eff"] == pytest.approx(0.46)
    assert row["composite_score"] == pytest.approx(0.44)
    assert row["priority_score"] == pytest.approx(0.405)
    assert row["dynamic_etf_penalty"] == pytest.approx(0.5)
    assert "CORE_BUY_DEPRIORITIZED symbol=SPY reason=strong_dynamic_candidates" in caplog.text


def test_dynamic_exposure_deprioritizes_etf_candidates(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    from src.app.live_cycle import _apply_strong_dynamic_etf_penalty, _dynamic_exposure_pct

    exposure = _dynamic_exposure_pct(
        [
            {"symbol": "XOS", "market_value": 15_000.0},
            {"symbol": "NVTS", "market_value": 7_000.0},
            {"symbol": "AAPL", "market_value": 20_000.0},
        ],
        dynamic_symbols=["XOS", "NVTS"],
        account_equity=100_000.0,
    )
    assert exposure == pytest.approx(22.0)

    row = {"strength_eff": 1.0}
    caplog.set_level(logging.INFO)
    penalty = _apply_strong_dynamic_etf_penalty(
        row,
        "QQQ",
        strong_dynamic_candidates_present=False,
        dynamic_exposure_pct=exposure,
    )

    assert penalty == pytest.approx(0.5)
    assert row["strength_eff"] == pytest.approx(0.5)
    assert "CORE_BUY_DEPRIORITIZED symbol=QQQ reason=dynamic_exposure" in caplog.text


def test_dynamic_reentry_block_log_string_present() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    assert "DYNAMIC_REENTRY_BLOCK symbol=%s minutes_remaining=%d reason=post_exit_cooldown" in source
    assert "DYNAMIC_REENTRY_BLOCK symbol=%s remaining_minutes=%d reason=post_exit_cooldown" in source


def test_startup_no_new_entries_guard(monkeypatch) -> None:
    import src.app.live_cycle as lc

    monkeypatch.setattr(lc, "_PROCESS_START_TS", 1_000.0)
    assert lc._startup_no_new_entries_active(1_100.0) is True
    assert lc._startup_no_new_entries_active(1_301.0) is False


def test_intraday_restart_warmup_decision_allows_entries_when_state_loaded() -> None:
    from src.app.live_cycle import _entry_startup_warmup_decision
    from src.universe import SessionType

    active, reason = _entry_startup_warmup_decision(
        process_warmup_active=True,
        session=SessionType.REGULAR,
        account_loaded=True,
        positions_loaded=True,
        premarket_required=True,
        premarket_loaded=True,
        local_state_loaded=True,
    )

    assert active is False
    assert reason == "intraday_restart"


def test_restart_before_market_open_keeps_startup_warmup() -> None:
    from src.app.live_cycle import _entry_startup_warmup_decision
    from src.universe import SessionType

    active, reason = _entry_startup_warmup_decision(
        process_warmup_active=True,
        session=SessionType.PRE_MARKET,
        account_loaded=True,
        positions_loaded=True,
        premarket_required=True,
        premarket_loaded=True,
        local_state_loaded=True,
    )

    assert active is True
    assert reason == "process_start"


def test_intraday_restart_at_1006_allows_entries_without_premarket_artifacts() -> None:
    from src.app.live_cycle import _entry_startup_warmup_decision
    from src.universe import SessionType

    active, reason = _entry_startup_warmup_decision(
        process_warmup_active=True,
        session=SessionType.REGULAR,
        account_loaded=True,
        positions_loaded=True,
        premarket_required=True,
        premarket_loaded=False,
        local_state_loaded=True,
    )

    assert active is False
    assert reason == "intraday_restart"


def test_missing_restart_state_keeps_startup_warmup() -> None:
    from src.app.live_cycle import _entry_startup_warmup_decision
    from src.universe import SessionType

    active, reason = _entry_startup_warmup_decision(
        process_warmup_active=True,
        session=SessionType.REGULAR,
        account_loaded=True,
        positions_loaded=True,
        premarket_required=True,
        premarket_loaded=True,
        local_state_loaded=False,
    )

    assert active is True
    assert reason == "missing_state_or_premarket"


def test_premarket_missing_artifacts_keeps_startup_warmup() -> None:
    from src.app.live_cycle import _entry_startup_warmup_decision
    from src.universe import SessionType

    active, reason = _entry_startup_warmup_decision(
        process_warmup_active=True,
        session=SessionType.PRE_MARKET,
        account_loaded=True,
        positions_loaded=True,
        premarket_required=True,
        premarket_loaded=False,
        local_state_loaded=True,
    )

    assert active is True
    assert reason == "missing_state_or_premarket"


def test_startup_warmup_decision_logs_are_wired() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()

    assert "STARTUP_WARMUP_SKIPPED reason=intraday_restart" in source
    assert "STARTUP_WARMUP_ACTIVE reason=missing_state_or_premarket" in source


def test_entry_lane_scheduler_diagnostics_logged_before_do_any_entry() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()

    decision_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if "ENTRY_LANE_DECISION user=%s now_et=%s entries_on=%s entry_scan_allowed=%s" in line
    )
    do_any_entry_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if "do_any_entry = do_core_entry or do_dynamic_entry" in line
    )

    assert decision_line < do_any_entry_line
    assert "ENTRY_LANE_BLOCKED reason=entry_scan_not_allowed" in source
    for field in (
        "startup_warmup_active=%s",
        "startup_warmup_reason=%s",
        "process_warmup_active=%s",
        "open_accelerated_window=%s",
        "do_core_entry=%s",
        "do_dynamic_entry=%s",
        "last_core_entry_age_sec=%s",
        "last_dynamic_entry_age_sec=%s",
        "core_interval_sec=%.1f",
        "dynamic_interval_sec=%.1f",
        "dynamic_symbols_count=%d",
        "fastlane_symbols_count=%d",
        "premarket_loaded=%s",
        "account_loaded=%s",
        "positions_loaded=%s",
        "local_state_loaded=%s",
    ):
        assert field in source


def test_newsapi_startup_status_key_present(monkeypatch) -> None:
    import src.app.live_cycle as lc

    monkeypatch.setenv("NEWSAPI_KEY", "secret")

    env_name, loaded = lc._newsapi_startup_status(
        {"news_sentiment": {"enabled": True}}
    )

    assert env_name == "NEWSAPI_KEY"
    assert loaded is True


def test_newsapi_startup_status_key_absent(monkeypatch) -> None:
    import src.app.live_cycle as lc

    monkeypatch.delenv("NEWSAPI_KEY", raising=False)

    env_name, loaded = lc._newsapi_startup_status(
        {"news_sentiment": {"enabled": True}}
    )

    assert env_name == "NEWSAPI_KEY"
    assert loaded is False


def test_newsapi_startup_status_custom_env_var(monkeypatch) -> None:
    import src.app.live_cycle as lc

    monkeypatch.delenv("NEWSAPI_KEY", raising=False)
    monkeypatch.setenv("CUSTOM_NEWS_KEY", "secret")

    env_name, loaded = lc._newsapi_startup_status(
        {
            "news_sentiment": {
                "enabled": True,
                "newsapi_key_env": "CUSTOM_NEWS_KEY",
            }
        }
    )

    assert env_name == "CUSTOM_NEWS_KEY"
    assert loaded is True


def test_news_fast_lane_interval_seconds() -> None:
    import src.app.live_cycle as lc

    assert lc._news_fast_lane_interval_seconds(None) is None
    assert lc._news_fast_lane_interval_seconds({"news_fast_lane": {"enabled": False}}) is None
    assert lc._news_fast_lane_interval_seconds({"news_fast_lane": {"enabled": True, "scan_interval_seconds": 60}}) == 60.0


def test_maybe_scan_options_for_dynamic_candidates_scan_only(monkeypatch) -> None:
    import src.app.live_cycle as lc

    calls = []

    def fake_scan(*args, **kwargs):
        calls.append((args, kwargs))
        return ["ok"]

    monkeypatch.setattr(lc, "_scan_dynamic_candidates_option_chains", fake_scan)
    out = lc._maybe_scan_options_for_dynamic_candidates(
        object(),
        {"options": {"mode": "scan_only"}},
        [SimpleNamespace(symbol="HPE", price=20.0, score=9.0)],
        now=datetime(2026, 6, 5, 9, 45, tzinfo=timezone.utc),
    )
    assert out == ["ok"]
    assert len(calls) == 1


def test_maybe_scan_options_for_dynamic_candidates_non_scan_only(monkeypatch) -> None:
    import src.app.live_cycle as lc

    monkeypatch.setattr(
        lc,
        "_scan_dynamic_candidates_option_chains",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not scan")),
    )
    out = lc._maybe_scan_options_for_dynamic_candidates(
        object(),
        {"options": {"mode": "long_premium_only"}},
        [SimpleNamespace(symbol="HPE", price=20.0, score=9.0)],
        now=datetime(2026, 6, 5, 9, 45, tzinfo=timezone.utc),
    )
    assert out == []


def test_live_cycle_loads_premarket_artifacts_into_runtime(tmp_path, caplog) -> None:
    import logging

    import src.app.live_cycle as lc
    import src.premarket_intelligence as pm

    caplog.set_level(logging.INFO)
    now = datetime(2026, 6, 1, 5, 0, tzinfo=timezone.utc)
    project_root = tmp_path
    import src.news_catalyst as nc
    nc._NEWS_CACHE.clear()
    pm.write_premarket_artifacts(
        project_root,
        now=now,
        source="news_5am",
        events=[
            pm.NewsEvent(
                symbol="GOOGL",
                headline="Google wins cloud contract",
                source="alpaca",
                score=6.5,
            )
        ],
        catalysts={
            "GOOGL": pm.NewsCatalyst(
                "GOOGL",
                7,
                "Google wins cloud contract",
                source="alpaca",
                catalyst_type="deal",
                article_count=2,
                sentiment=0.65,
            )
        },
        rankings=[
            pm.PremarketRankEntry("GOOGL", 6.5, "deal", "alpaca", 0.92, "deal headline")
        ],
    )
    engine = SimpleNamespace()

    loaded = lc._load_premarket_artifacts_into_runtime(
        engine=engine,
        project_root=project_root,
        now=now,
    )

    assert "GOOGL" in loaded
    assert engine.dynamic_news_scores["GOOGL"] >= 7
    assert engine.dynamic_event_scores["GOOGL"] == 7.0
    assert engine.dynamic_catalyst_scores["GOOGL"] >= 0.7
    assert "PREMARKET_ARTIFACT_LOADED path=" in caplog.text
    assert "PREMARKET_CATALYST_APPLIED symbol=GOOGL score=6.50 source=alpaca" in caplog.text
    assert "PREMARKET_ARTIFACT_RUNTIME_SCORE symbol=GOOGL news_score=7 event_score=7.00 catalyst_score=0.70" in caplog.text


def test_dynamic_scan_runtime_score_maps_include_event_and_catalyst_scores() -> None:
    import src.app.live_cycle as lc
    from src.dynamic_universe import DynamicScanCandidate

    row = DynamicScanCandidate(
        symbol="irez",
        score=25.0,
        accepted=True,
        rejection_reason=None,
        price=12.0,
        day_gain_pct=15.0,
        volume=1_000_000,
        avg_volume=900_000,
        relative_volume=1.11,
        spread_pct=0.4,
        quality=None,
        news_score=7,
        event_score=6.5,
        catalyst_score=0.7,
        news_headline="IREZ wins AI contract",
        catalyst_type="ai",
    )

    news, headlines, catalyst_types, events, catalysts = lc._dynamic_scan_runtime_score_maps([row])

    assert news["IREZ"] == 7
    assert headlines["IREZ"] == "IREZ wins AI contract"
    assert catalyst_types["IREZ"] == "ai"
    assert events["IREZ"] == pytest.approx(6.5)
    assert catalysts["IREZ"] == pytest.approx(0.7)


def _write_premarket_candidate_artifacts(
    root: Path,
    *,
    generated_at: datetime,
    ttl_minutes: int = 390,
) -> None:
    premarket = root / "data" / "premarket"
    premarket.mkdir(parents=True, exist_ok=True)
    common = {
        "generated_at": generated_at.isoformat(),
        "ttl_minutes": ttl_minutes,
        "source": "test",
    }
    rankings = {
        **common,
        "rankings": [
            {
                "rank": 1,
                "symbol": "AMZN",
                "score": 8.4,
                "event_score": 7.5,
                "news_score": 8,
                "catalyst_score": 0.84,
                "catalyst_type": "earnings",
                "source": "alpaca",
                "headline": "Amazon raises guidance",
            },
            {
                "rank": 2,
                "symbol": "GOOGL",
                "score": 7.8,
                "event_score": 6.5,
                "news_score": 7,
                "catalyst_score": 0.78,
                "catalyst_type": "deal",
                "source": "sec",
                "headline": "Google wins cloud contract",
            },
        ],
        "symbols": ["AMZN", "GOOGL"],
    }
    catalysts = {
        **common,
        "catalysts": [
            {
                "symbol": "AMZN",
                "score": 8,
                "headline": "Amazon raises guidance",
                "source": "alpaca",
                "catalyst_type": "earnings",
            },
            {
                "symbol": "GOOGL",
                "score": 7,
                "headline": "Google wins cloud contract",
                "source": "sec",
                "catalyst_type": "deal",
            },
        ],
        "symbols": ["AMZN", "GOOGL"],
    }
    events = {
        **common,
        "events": [
            {
                "symbol": "AMZN",
                "headline": "Amazon raises guidance",
                "source": "alpaca",
                "score": 7.5,
            },
            {
                "symbol": "GOOGL",
                "headline": "Google wins cloud contract",
                "source": "sec",
                "score": 6.5,
            },
        ],
        "symbols": ["AMZN", "GOOGL"],
    }
    (premarket / "latest_rankings.json").write_text(json.dumps(rankings), encoding="utf-8")
    (premarket / "latest_catalysts.json").write_text(json.dumps(catalysts), encoding="utf-8")
    (premarket / "latest_event_feed.json").write_text(json.dumps(events), encoding="utf-8")


def test_fresh_premarket_rankings_inject_candidates_and_preserve_scores(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import src.app.live_cycle as lc
    from src.news_catalyst import load_premarket_artifacts

    now = datetime(2026, 6, 9, 13, 40, tzinfo=timezone.utc)
    _write_premarket_candidate_artifacts(tmp_path, generated_at=now)
    artifacts = load_premarket_artifacts(tmp_path, now=now, emit_log=False)

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        rows = lc._inject_premarket_ranked_candidates(
            config={"dynamic_universe": {"premarket_candidate_injection_top_n": 5}},
            project_root=tmp_path,
            now=now,
            artifact_summary=artifacts,
            existing_symbols=["TNGX", "AMD", "ORCL", "AMZN"],
            dynamic_symbols=["TNGX"],
            paused_symbols=set(),
        )

    assert [row["symbol"] for row in rows] == ["AMZN", "GOOGL"]
    assert rows[0]["dynamic_candidate"] is True
    assert rows[0]["already_in_universe"] is True
    assert rows[0]["news_score"] == pytest.approx(8.0)
    assert rows[0]["event_score"] >= 7.5
    assert rows[0]["catalyst_score"] == pytest.approx(0.84)
    assert rows[0]["article_count"] == 0
    assert rows[0]["headline"] == "Amazon raises guidance"
    assert rows[0]["catalyst_headline"] == "Amazon raises guidance"
    assert rows[0]["catalyst_type"] == "earnings"
    assert "PREMARKET_RANKING_SCORE_TRACE symbol=AMZN rank=1" in caplog.text
    assert "PREMARKET_CANDIDATE_SCORE_TRACE symbol=AMZN action=inject rank=1" in caplog.text
    assert "PREMARKET_CANDIDATE_INJECTED symbol=AMZN rank=1" in caplog.text
    assert "PREMARKET_CANDIDATE_INJECTION_SUMMARY injected=2 skipped_existing=0 skipped_stale=0" in caplog.text


def test_premarket_injected_symbol_missing_metadata_logs_guard(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import src.app.live_cycle as lc

    now = datetime(2026, 6, 9, 13, 40, tzinfo=timezone.utc)
    premarket = tmp_path / "data" / "premarket"
    premarket.mkdir(parents=True, exist_ok=True)
    common = {"generated_at": now.isoformat(), "ttl_minutes": 390, "source": "test"}
    (premarket / "latest_rankings.json").write_text(
        json.dumps({**common, "rankings": [{"rank": 1, "symbol": "AMZN", "score": 0.0}], "symbols": ["AMZN"]}),
        encoding="utf-8",
    )
    (premarket / "latest_catalysts.json").write_text(
        json.dumps({**common, "catalysts": [{"symbol": "AMZN"}], "symbols": ["AMZN"]}),
        encoding="utf-8",
    )
    (premarket / "latest_event_feed.json").write_text(
        json.dumps({**common, "events": [], "symbols": ["AMZN"]}),
        encoding="utf-8",
    )

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        rows = lc._inject_premarket_ranked_candidates(
            config={"dynamic_universe": {"premarket_candidate_injection_top_n": 5}},
            project_root=tmp_path,
            now=now,
            artifact_summary={"AMZN": {"symbol": "AMZN"}},
            existing_symbols=[],
            dynamic_symbols=[],
            paused_symbols=set(),
        )

    assert rows == []
    assert "CATALYST_METADATA_MISSING symbol=AMZN reason=missing_or_zero_metadata" in caplog.text
    assert "PREMARKET_CANDIDATE_SCORE_TRACE symbol=AMZN action=skip reason=zero_scores" in caplog.text


def test_strong_news_fastlane_map_requires_confirmed_metadata() -> None:
    import src.app.live_cycle as lc

    rows = lc._strong_news_dynamic_persistence_map(
        {
            "AMZN": {"symbol": "AMZN", "news_score": 8, "age_minutes": 12.0},
            "META": {"symbol": "META"},
        },
        [],
    )

    assert "AMZN" in rows
    assert "META" not in rows


def test_fresh_premarket_artifacts_load_and_log_live_runtime(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import src.app.live_cycle as lc

    now = datetime(2026, 6, 9, 13, 40, tzinfo=timezone.utc)
    _write_premarket_candidate_artifacts(tmp_path, generated_at=now)
    engine = SimpleNamespace()

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        artifacts = lc._load_premarket_artifacts_into_runtime(
            engine=engine,
            project_root=tmp_path,
            now=now,
        )

    assert set(artifacts) >= {"AMZN", "GOOGL"}
    assert engine.dynamic_news_scores["AMZN"] == 8
    assert engine.dynamic_catalyst_scores["AMZN"] == pytest.approx(0.84)
    assert "PREMARKET_RUNTIME_LOAD status=loaded fresh=true rankings=2 catalysts=2 events=2" in caplog.text
    assert (
        "CATALYST_RUNTIME_SYMBOL symbol=AMZN premarket_injected=true news_score=8.00 "
        "event_score=8.00 catalyst_score=0.84 article_count=0 rank=1 headline=Amazon raises guidance"
    ) in caplog.text


def test_stale_premarket_artifacts_trigger_live_runtime_guard(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import src.app.live_cycle as lc

    now = datetime(2026, 6, 9, 20, 0, tzinfo=timezone.utc)
    _write_premarket_candidate_artifacts(
        tmp_path,
        generated_at=datetime(2026, 6, 9, 10, 0, tzinfo=timezone.utc),
        ttl_minutes=60,
    )

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        allowed = lc._live_premarket_runtime_guard_allows_dynamic(
            project_root=tmp_path,
            now=now,
            is_live=True,
        )

    assert allowed is False
    assert "PREMARKET_RUNTIME_GUARD status=blocked reason=missing_or_stale_premarket_artifacts" in caplog.text


def test_absent_premarket_artifacts_do_not_silently_allow_live_dynamic_news(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import src.app.live_cycle as lc

    now = datetime(2026, 6, 9, 13, 40, tzinfo=timezone.utc)

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        artifacts = lc._load_premarket_artifacts_into_runtime(
            engine=SimpleNamespace(),
            project_root=tmp_path,
            now=now,
        )
        allowed = lc._live_premarket_runtime_guard_allows_dynamic(
            project_root=tmp_path,
            now=now,
            is_live=True,
        )

    assert artifacts == {}
    assert allowed is False
    assert "PREMARKET_RUNTIME_LOAD status=missing fresh=false rankings=0 catalysts=0 events=0" in caplog.text
    assert "PREMARKET_RUNTIME_GUARD status=blocked reason=missing_or_stale_premarket_artifacts" in caplog.text


def test_paper_runtime_dynamic_guard_remains_unchanged_for_missing_artifacts(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import src.app.live_cycle as lc

    now = datetime(2026, 6, 9, 13, 40, tzinfo=timezone.utc)

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        allowed = lc._live_premarket_runtime_guard_allows_dynamic(
            project_root=tmp_path,
            now=now,
            is_live=False,
        )

    assert allowed is True
    assert "PREMARKET_RUNTIME_GUARD" not in caplog.text


def test_catalyst_fastlane_logs_explicit_allowed_and_reject(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import src.app.live_cycle as lc

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        lc._log_dynamic_fastlane(
            "amzn",
            news_score=8,
            catalyst_age_minutes=12,
            allowed=True,
            reason="strong_news_open_fastlane",
        )
        lc._log_dynamic_fastlane(
            "googl",
            news_score=1,
            catalyst_age_minutes=400,
            allowed=False,
            reason="weak_or_stale_news",
        )

    assert "CATALYST_FASTLANE_CHECK symbol=AMZN news_score=8 catalyst_age_minutes=12 allowed=true reason=strong_news_open_fastlane" in caplog.text
    assert "CATALYST_FASTLANE_ALLOWED symbol=AMZN reason=strong_news_open_fastlane" in caplog.text
    assert "CATALYST_FASTLANE_REJECT symbol=GOOGL reason=weak_or_stale_news" in caplog.text


def test_premarket_rank_merge_preserves_artifact_scores_when_ranking_score_is_lower(
    tmp_path: Path,
) -> None:
    import src.app.live_cycle as lc

    now = datetime(2026, 6, 11, 13, 40, tzinfo=timezone.utc)
    premarket = tmp_path / "data" / "premarket"
    premarket.mkdir(parents=True, exist_ok=True)
    common = {
        "generated_at": now.isoformat(),
        "ttl_minutes": 390,
        "source": "test",
        "symbols": ["AMZN"],
    }
    (premarket / "latest_rankings.json").write_text(
        json.dumps(
            {
                **common,
                "rankings": [
                    {
                        "rank": 1,
                        "symbol": "AMZN",
                        "score": 2.5,
                        "catalyst_type": "sec_filing",
                        "source": "sec_filing",
                    }
                ],
                "catalysts": [
                    {
                        "symbol": "AMZN",
                        "score": 7.2,
                        "event_score": 7.2,
                        "headline": "SEC filing 8-K",
                        "source": "sec",
                        "catalyst_type": "sec_filing",
                    }
                ],
                "events": [],
            }
        ),
        encoding="utf-8",
    )
    (premarket / "latest_catalysts.json").write_text(
        json.dumps(
            {
                **common,
                "rankings": [],
                "catalysts": [
                    {
                        "symbol": "AMZN",
                        "score": 7.2,
                        "event_score": 7.2,
                        "headline": "SEC filing 8-K",
                        "source": "sec",
                        "catalyst_type": "sec_filing",
                    }
                ],
                "events": [],
            }
        ),
        encoding="utf-8",
    )
    (premarket / "latest_event_feed.json").write_text(
        json.dumps({**common, "rankings": [], "catalysts": [], "events": []}),
        encoding="utf-8",
    )

    artifacts = lc.load_premarket_artifacts(tmp_path, now=now, emit_log=False)
    rows = lc._premarket_artifact_rank_rows(project_root=tmp_path, artifact_summary=artifacts)

    assert rows[0]["symbol"] == "AMZN"
    assert rows[0]["ranking_score"] == pytest.approx(2.5)
    assert rows[0]["event_score"] == pytest.approx(7.2)
    assert rows[0]["news_score"] == pytest.approx(7.0)
    assert rows[0]["catalyst_score"] == pytest.approx(0.72)
    assert rows[0]["score"] == pytest.approx(7.2)


def test_stale_premarket_rankings_do_not_inject_candidates(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import src.app.live_cycle as lc
    from src.news_catalyst import load_premarket_artifacts

    now = datetime(2026, 6, 9, 20, 0, tzinfo=timezone.utc)
    _write_premarket_candidate_artifacts(
        tmp_path,
        generated_at=datetime(2026, 6, 9, 10, 0, tzinfo=timezone.utc),
        ttl_minutes=60,
    )
    artifacts = load_premarket_artifacts(tmp_path, now=now, emit_log=False)

    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        rows = lc._inject_premarket_ranked_candidates(
            config={},
            project_root=tmp_path,
            now=now,
            artifact_summary=artifacts,
            existing_symbols=[],
            dynamic_symbols=[],
            paused_symbols=set(),
        )

    assert rows == []
    assert "PREMARKET_CANDIDATE_INJECTION_SUMMARY injected=0 skipped_existing=0 skipped_stale=2" in caplog.text


def test_existing_dynamic_premarket_symbol_is_not_duplicated(tmp_path: Path) -> None:
    import src.app.live_cycle as lc
    from src.news_catalyst import load_premarket_artifacts

    now = datetime(2026, 6, 9, 13, 40, tzinfo=timezone.utc)
    _write_premarket_candidate_artifacts(tmp_path, generated_at=now)
    artifacts = load_premarket_artifacts(tmp_path, now=now, emit_log=False)

    rows = lc._inject_premarket_ranked_candidates(
        config={},
        project_root=tmp_path,
        now=now,
        artifact_summary=artifacts,
        existing_symbols=["AMZN"],
        dynamic_symbols=["AMZN"],
        paused_symbols=set(),
    )

    assert [row["symbol"] for row in rows] == ["GOOGL"]


def test_momentum_only_dynamic_candidate_still_rejects_without_catalyst() -> None:
    from src.allocation_profile import filter_allocator_candidates_for_profile

    rows = filter_allocator_candidates_for_profile(
        [
            {
                "symbol": "TNGX",
                "dynamic_candidate": True,
                "source": "dynamic_universe",
                "news_score": 0,
                "event_score": 0,
                "catalyst_score": 0,
                "relative_volume": 20.0,
            },
            {
                "symbol": "AMZN",
                "dynamic_candidate": True,
                "source": "premarket",
                "news_score": 8,
                "event_score": 7.5,
                "catalyst_score": 0.84,
            },
        ],
        config={"portfolio": {"target_core_stock_pct": 65, "target_dynamic_pct": 25, "target_cash_pct": 10}},
        equity=100_000,
    )

    assert [row["symbol"] for row in rows] == ["AMZN"]


def test_premarket_artifact_metadata_defaults_when_symbol_missing() -> None:
    import src.app.live_cycle as lc

    catalyst_type, catalyst_age = lc._premarket_artifact_metadata_fields({}, "SNOW")

    assert catalyst_type == ""
    assert catalyst_age is None


def test_premarket_artifact_catalyst_type_passes_to_high_conviction(monkeypatch) -> None:
    import src.app.live_cycle as lc

    captured = {}

    def fake_evaluate(_config, **kwargs):
        captured.update(kwargs)
        return True, "ok", 8.0, {}

    monkeypatch.setattr(lc, "evaluate_high_conviction_news_override", fake_evaluate)
    catalyst_type, catalyst_age = lc._premarket_artifact_metadata_fields(
        {"ARM": {"catalyst_type": "ai", "age_minutes": 42.0}},
        "ARM",
    )

    allowed, reason, score = lc._dynamic_high_conviction_trend_prefilter_override_decision(
        {
            "trading": {
                "dynamic": {
                    "high_conviction_news_override": {
                        "enabled": True,
                        "min_news_score": 7,
                        "min_event_score": 6,
                        "min_catalyst_score": 0.6,
                        "min_relative_volume": 1.5,
                        "max_catalyst_age_minutes": 180,
                    }
                }
            }
        },
        route="dynamic_momentum",
        is_dynamic_candidate=True,
        is_core_symbol=False,
        is_etf=False,
        news_score=8,
        event_score=7,
        catalyst_score=0.8,
        catalyst_type=catalyst_type,
        catalyst_age_minutes=catalyst_age,
        relative_volume=2.0,
        sentiment=0.5,
    )

    assert allowed is True
    assert reason == "high_conviction_fresh_catalyst"
    assert score == pytest.approx(8.0)
    assert captured["catalyst_type"] == "ai"
    assert captured["catalyst_age_minutes"] == pytest.approx(42.0)


def test_live_entry_scan_initializes_artifact_catalyst_type_before_use() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()

    assignment_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if '_artifact_catalyst_type = ""' in line
    )
    helper_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if "_premarket_artifact_metadata_fields(" in line and index > assignment_line
    )
    use_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if "catalyst_type=_artifact_catalyst_type" in line
    )

    assert assignment_line < helper_line < use_line


def test_dynamic_short_history_fallback_requires_dynamic_catalyst_and_min_bars() -> None:
    import src.app.live_cycle as lc

    cfg = {
        "dynamic_universe": {
            "short_history_fallback_enabled": True,
            "short_history_min_daily_bars": 90,
            "short_history_min_catalyst_score": 3.0,
        }
    }

    allowed, reason = lc._dynamic_short_history_fallback_decision(
        cfg,
        is_dynamic_candidate=True,
        available_bars=94,
        required_bars=200,
        news_score=0,
        event_score=3.2,
        catalyst_score=0.0,
    )
    assert allowed is True
    assert reason == "catalyst_short_history"

    allowed, reason = lc._dynamic_short_history_fallback_decision(
        cfg,
        is_dynamic_candidate=True,
        available_bars=89,
        required_bars=200,
        news_score=7,
        event_score=0,
        catalyst_score=0,
    )
    assert allowed is False
    assert "fallback_min" in reason

    allowed, reason = lc._dynamic_short_history_fallback_decision(
        cfg,
        is_dynamic_candidate=False,
        available_bars=94,
        required_bars=200,
        news_score=7,
        event_score=0,
        catalyst_score=0,
    )
    assert allowed is False
    assert reason == "not_dynamic"


def _scanner_approved_dynamic_meta(symbol: str = "BLZE") -> dict[str, object]:
    return {
        "symbol": symbol,
        "scanner_selected": True,
        "dynamic_scanner_selected": True,
        "selected_by_dynamic_scanner": True,
        "dynamic_selected": True,
        "entry_alignment_ok": True,
        "entry_alignment_passed": True,
        "score": 37.0,
        "price": 6.75,
        "day_gain_pct": 18.0,
        "avg_volume": 6000,
        "relative_volume": 1.35,
        "spread_pct": 0.24,
        "scanner_atr_expansion_ratio": 1.1,
    }


def test_dynamic_short_history_scanner_selected_momentum_bypass_allows_sndq_like_candidate() -> None:
    import src.app.live_cycle as lc

    cfg = {
        "dynamic_universe": {
            "min_avg_volume": 5000,
            "min_price": 2.0,
            "max_spread_pct": 1.0,
            "short_history_fallback_enabled": True,
            "short_history_min_daily_bars": 90,
            "short_history_scanner_selected_min_daily_bars": 40,
        }
    }
    meta = _scanner_approved_dynamic_meta("SNDQ")
    meta.update({"day_gain_pct": 16.2, "relative_volume": 1.55, "avg_volume": 8500})

    allowed, reason = lc._dynamic_short_history_fallback_decision(
        cfg,
        is_dynamic_candidate=True,
        available_bars=42,
        required_bars=180,
        news_score=0,
        event_score=0,
        catalyst_score=0,
        scanner_selected=True,
        scanner_meta=meta,
    )

    assert allowed is True
    assert reason == "scanner_selected_dynamic_momentum"


def test_dynamic_short_history_scanner_selected_momentum_keeps_bar_floor() -> None:
    import src.app.live_cycle as lc

    cfg = {
        "dynamic_universe": {
            "short_history_fallback_enabled": True,
            "short_history_min_daily_bars": 90,
            "short_history_scanner_selected_min_daily_bars": 40,
        }
    }

    allowed, reason = lc._dynamic_short_history_fallback_decision(
        cfg,
        is_dynamic_candidate=True,
        available_bars=39,
        required_bars=180,
        scanner_selected=True,
        scanner_meta=_scanner_approved_dynamic_meta("SNDQ"),
    )

    assert allowed is False
    assert reason == "bars 39 < scanner_selected_min 40"


def test_dynamic_entry_scanner_approval_override_allows_alignment_only_reject() -> None:
    import src.app.live_cycle as lc

    cfg = {
        "dynamic_universe": {
            "min_avg_volume": 5000,
            "min_price": 2.0,
            "max_spread_pct": 1.0,
        }
    }

    allowed, reason = lc._dynamic_entry_scanner_approval_override_decision(
        cfg,
        _scanner_approved_dynamic_meta("BLZE"),
        "need 5m breakout OR new intraday high OR strong green 1m OR opening-range breakout",
    )

    assert allowed is True
    assert reason == "scanner_selected_dynamic_momentum"


def test_dynamic_entry_scanner_approval_override_does_not_cover_spread_reject() -> None:
    import src.app.live_cycle as lc

    allowed, reason = lc._dynamic_entry_scanner_approval_override_decision(
        {"dynamic_universe": {"min_avg_volume": 5000, "max_spread_pct": 1.0}},
        _scanner_approved_dynamic_meta("BLZE"),
        "spread_pct 2.500% >= 1.00%",
    )

    assert allowed is False
    assert reason == "not_alignment_reject"


def test_live_cycle_passes_premarket_artifacts_into_dynamic_scan() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()

    assert "_premarket_artifacts = _load_premarket_artifacts_into_runtime(" in source
    assert "premarket_artifacts=_premarket_artifacts," in source


def test_live_cycle_contains_paper_only_options_branch() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    shadow_source = (PROJECT_ROOT / "src" / "live" / "options_shadow.py").read_text()
    readiness_source = (PROJECT_ROOT / "src" / "options_readiness.py").read_text()

    assert "OPTIONS_CONFIG enabled=%s mode=%s paper_only_active=%s live_pilot_active=%s" in source
    assert "broker_options_supported=%s final_status=%s" in readiness_source
    assert "OPTIONS_LIVE_PILOT enabled=%s" in source
    assert "OPTIONS_LIVE_PILOT exposure_limit=%s" in source
    assert "OPTIONS_LIVE_PILOT max_positions=%s" in source
    assert "OPTIONS_LIVE_PILOT max_contracts=%s" in source
    assert "_log_options_startup_config(first_config, broker=broker)" in source
    assert "_shadow_live_options_active(config)" in source
    assert "_attempt_shadow_option_entry(" in source
    assert "OPTIONS_SHADOW_ORDER_INTENDED" in shadow_source
    assert "_paper_only_options_active(config)" in source
    assert "_live_pilot_options_active(config, broker)" in source
    assert "_attempt_paper_option_entry(" in source
    assert "_log_options_disabled_non_paper_once(" in source
    assert "_reset_options_non_paper_log_flags()" in source
    assert "if not _options_runtime_enabled(broker, config):" in source
    assert "OPTIONS_ENTRY_LANE symbol=%s lane=%s action=attempt reason=%s" in source
    assert "live_pilot_active" in source
    assert "OPTIONS_LIVE_BLOCKED reason=live_pilot_disabled" in source
    assert "OPTIONS_ENTRY_LANE symbol=%s lane=%s action=skip reason=" in source
    assert "OPTION_ROUTE_CHECK symbol=%s lane=%s route=%s entry_eval_final=%s" in source
    assert "OPTION_ROUTE_SKIPPED symbol=%s route=%s underlying=%s lane=%s reason=%s detail=%s" in source
    for reason in (
        "entry_eval_false",
        "underlying_not_allowed",
        "require_top_signal_failed",
        "environment_blocked",
        "daily_cap",
        "cooldown",
        "gross_exposure",
        "no_contract_found",
        "selector_rejected_all",
        "fallback_to_stock",
        "stock_route_selected",
    ):
        assert reason in source
    assert "PAPER_ONLY_OPTIONS_FILLED" in source
    assert "ALLOCATOR_DYNAMIC_CANDIDATES" in source
    assert "ALLOCATOR_DYNAMIC_SKIPPED" in source
    assert "ALLOCATOR_DYNAMIC_SELECTED" in source
    assert "spread_cap=" in source
    assert "vwap_above=" in source
    assert "DYNAMIC_ENTRY_GUARD symbol=%s vwap=%s price=%.4f distance_from_vwap=%.3f news_score=%d" in source
    assert "DYNAMIC_HIGH_CONVICTION_TREND_PREFILTER_OVERRIDE symbol=%s reason=%s" in source
    assert "DYNAMIC_HIGH_CONVICTION_BLOCKED symbol=%s reason=vwap_safety" in source
    assert "ENTRY_DECISION_SUMMARY options_attempted=%d options_selected=%d options_ordered=%d stock_fallback=%d blocked_cooldown=%d blocked_vwap=%d blocked_option_liquidity=%d" in source


def test_options_startup_config_logs_default_disabled_paper_only(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from src.app.live_cycle import _log_options_startup_config

    cfg = {"options": {"enabled": False, "mode": "paper_only"}}
    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        _log_options_startup_config(cfg)

    out = capsys.readouterr().out
    assert "OPTIONS_CONFIG enabled=false mode=paper_only paper_only_active=false live_pilot_active=false" in caplog.text
    assert "OPTIONS_CONFIG enabled=false mode=paper_only paper_only_active=false live_pilot_active=false" in out


def test_options_startup_config_logs_live_long_premium_pilot(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from src.app.live_cycle import _log_options_startup_config

    cfg = {
        "options": {
            "enabled": True,
            "mode": "live_long_premium",
            "live_pilot": {"enabled": True},
            "total_exposure_limit": 0.01,
            "max_positions": 1,
            "max_contracts_per_trade": 1,
        }
    }
    with caplog.at_level(logging.INFO, logger="src.app.live_cycle"):
        _log_options_startup_config(cfg)

    out = capsys.readouterr().out
    assert "OPTIONS_CONFIG enabled=true mode=live_long_premium paper_only_active=false live_pilot_active=true" in caplog.text
    assert "OPTIONS_CONFIG enabled=true mode=live_long_premium live_pilot_enabled=true long_premium_only=true" in caplog.text
    assert "OPTIONS_LIVE_PILOT enabled=true" in caplog.text
    assert "OPTIONS_LIVE_PILOT exposure_limit=1%" in out
    assert "OPTIONS_LIVE_PILOT max_positions=1" in out
    assert "OPTIONS_LIVE_PILOT max_contracts=1" in out


def test_live_cycle_attempts_paper_options_before_stock_allocator_fallback() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()
    attempt_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if "_attempt_paper_option_entry_for_row(" in line
    )
    allocator_append_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if "_append_capital_allocator_candidate(" in line and index > attempt_line
    )
    dispatch_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if "return _trend_long_dispatch_impl(row_tl)" in line
    )

    assert attempt_line < allocator_append_line
    assert attempt_line < dispatch_line


def test_live_cycle_logs_batch_news_mode() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()

    assert "NEWS_MODE batch" in source
    assert "NEWS_MODE per_symbol disabled" in source


def test_dynamic_history_live_default_uses_configured_180() -> None:
    from src.app.live_cycle import _dynamic_daily_history_requirement

    cfg = {
        "dynamic_universe": {
            "min_history_bars": 180,
        }
    }

    need, default_need, active = _dynamic_daily_history_requirement(
        cfg,
        symbol="ASTN",
        ma_slow_period=50,
        min_history_bars=50,
        is_dynamic_candidate=True,
        broker_is_paper=False,
    )

    assert (need, default_need, active) == (180, 200, False)
    assert 180 >= need
    assert 179 < need


def test_dynamic_history_live_dynamic_only_uses_live_min_history_bars() -> None:
    from src.app.live_cycle import _dynamic_daily_history_requirement

    cfg = {
        "dynamic_universe": {
            "min_history_bars": 180,
            "live_min_history_bars": 50,
        }
    }

    need, default_need, active = _dynamic_daily_history_requirement(
        cfg,
        symbol="ASTN",
        ma_slow_period=50,
        min_history_bars=50,
        is_dynamic_candidate=True,
        broker_is_paper=False,
        candidate_type="DYNAMIC_ONLY",
    )

    assert (need, default_need, active) == (50, 200, False)
    assert 50 >= need
    assert 49 < need


def test_dynamic_history_live_core_scoring_keeps_default_slow_history() -> None:
    from src.app.live_cycle import _dynamic_daily_history_requirement

    cfg = {
        "dynamic_universe": {
            "min_history_bars": 180,
            "live_min_history_bars": 50,
        }
    }

    need, default_need, active = _dynamic_daily_history_requirement(
        cfg,
        symbol="SPY",
        ma_slow_period=200,
        min_history_bars=50,
        is_dynamic_candidate=False,
        broker_is_paper=False,
        candidate_type="CORE_WITH_DYNAMIC_SIGNAL",
    )

    assert (need, default_need, active) == (200, 200, False)


def test_dynamic_history_live_core_with_dynamic_signal_keeps_configured_dynamic_floor() -> None:
    from src.app.live_cycle import _dynamic_daily_history_requirement

    cfg = {
        "dynamic_universe": {
            "min_history_bars": 180,
            "live_min_history_bars": 50,
        }
    }

    need, default_need, active = _dynamic_daily_history_requirement(
        cfg,
        symbol="MSFT",
        ma_slow_period=50,
        min_history_bars=50,
        is_dynamic_candidate=True,
        broker_is_paper=False,
        candidate_type="CORE_WITH_DYNAMIC_SIGNAL",
    )

    assert (need, default_need, active) == (180, 200, False)


def test_dynamic_history_paper_default_uses_configured_180() -> None:
    from src.app.live_cycle import _dynamic_daily_history_requirement

    need, default_need, active = _dynamic_daily_history_requirement(
        {"dynamic_universe": {"min_history_bars": 180, "live_min_history_bars": 50}},
        symbol="ASTN",
        ma_slow_period=50,
        min_history_bars=50,
        is_dynamic_candidate=True,
        broker_is_paper=True,
        candidate_type="DYNAMIC_ONLY",
    )

    assert (need, default_need, active) == (180, 200, False)


def test_dynamic_history_experiment_paper_uses_configured_min_bars() -> None:
    from src.app.live_cycle import _dynamic_daily_history_requirement

    cfg = {
        "dynamic_universe": {
            "min_history_bars": 180,
            "paper_min_history_bars_experiment": {
                "enabled": True,
                "min_bars": 50,
            }
        }
    }

    need, default_need, active = _dynamic_daily_history_requirement(
        cfg,
        symbol="ASTN",
        ma_slow_period=50,
        min_history_bars=50,
        is_dynamic_candidate=True,
        broker_is_paper=True,
    )

    assert (need, default_need, active) == (50, 200, True)


def test_astn_like_87_bars_passes_only_in_paper_history_experiment() -> None:
    from src.app.live_cycle import _dynamic_daily_history_requirement

    cfg = {
        "dynamic_universe": {
            "min_history_bars": 180,
            "paper_min_history_bars_experiment": {
                "enabled": True,
                "min_bars": 50,
            }
        }
    }

    paper_need, _, paper_active = _dynamic_daily_history_requirement(
        cfg,
        symbol="ASTN",
        ma_slow_period=50,
        min_history_bars=50,
        is_dynamic_candidate=True,
        broker_is_paper=True,
    )
    live_need, _, live_active = _dynamic_daily_history_requirement(
        cfg,
        symbol="ASTN",
        ma_slow_period=50,
        min_history_bars=50,
        is_dynamic_candidate=True,
        broker_is_paper=False,
    )
    paper_default_need, _, paper_default_active = _dynamic_daily_history_requirement(
        {"dynamic_universe": {"min_history_bars": 180}},
        symbol="ASTN",
        ma_slow_period=50,
        min_history_bars=50,
        is_dynamic_candidate=True,
        broker_is_paper=True,
    )

    assert paper_active is True
    assert live_active is False
    assert paper_default_active is False
    assert 87 >= paper_need
    assert 87 < live_need
    assert 87 < paper_default_need


def test_paper_week_options_enabled_without_live_pilot() -> None:
    text = (PROJECT_ROOT / "config" / "default.yaml").read_text(encoding="utf-8")
    options_start = text.index("\noptions:\n")
    options_block = text[options_start: options_start + 2400]

    assert "  enabled: true" in options_block
    assert "  mode: paper_only" in options_block
    assert "  live_pilot_enabled: false" in options_block
    assert "  total_exposure_limit: 0.02" in options_block
    assert "  max_option_position_pct: 0.02" in options_block


def test_live_cycle_wires_health_alerts_into_heartbeat() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    lines = source.splitlines()
    eval_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if "evaluate_runtime_health(" in line
    )
    alert_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if index > eval_line and "notify_alpaca_loop_health_alert(" in line
    )
    heartbeat_line = next(
        index
        for index, line in enumerate(lines, start=1)
        if index > alert_line and "notify_alpaca_loop_heartbeat(" in line
    )

    assert eval_line < alert_line < heartbeat_line


def test_startup_warmup_blocks_entry_lanes_without_disabling_exits() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()
    tree = ast.parse(source)
    run_live_cycle = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "run_live_cycle"
    )

    warmup_line = next(
        index
        for index, line in enumerate(source.splitlines(), start=1)
        if "startup warmup active: blocking new entries" in line
    )
    manage_positions_line = next(
        index
        for index, line in enumerate(source.splitlines(), start=1)
        if "manage_positions()" in line
    )
    dispatch_lines = [
        node.lineno
        for node in ast.walk(run_live_cycle)
        if isinstance(node, ast.Name)
        and node.id in {"dispatch_trend_long_after_buying_power", "run_post_scan_capital_allocator"}
    ]

    assert manage_positions_line < warmup_line
    assert dispatch_lines
    assert warmup_line < min(dispatch_lines)


def test_market_vwap_feature_result_returns_complete_unavailable_on_broker_failure() -> None:
    from src.app.live_cycle import _market_vwap_feature_result

    class Broker:
        def get_bars(self, *args, **kwargs):
            raise RuntimeError("market data unavailable")

    result = _market_vwap_feature_result(Broker(), symbol="SPY")

    assert result == {
        "data_available": False,
        "market_price": None,
        "market_vwap": None,
        "distance_pct": None,
        "slope": None,
        "state": "unavailable",
        "confirmed": False,
        "score_fraction": 0.0,
    }


def test_market_vwap_feature_result_scores_available_market_data() -> None:
    from src.app.live_cycle import _market_vwap_feature_result

    class Broker:
        def get_bars(self, *args, **kwargs):
            return pd.DataFrame(
                {
                    "high": [99.5, 100.0, 100.5, 101.0, 101.5, 102.0],
                    "low": [98.5, 99.0, 99.5, 100.0, 100.5, 101.0],
                    "close": [99.0, 99.5, 100.0, 100.5, 101.0, 102.0],
                    "volume": [1000, 1000, 1000, 1000, 1000, 1000],
                }
            )

    result = _market_vwap_feature_result(Broker(), symbol="SPY")

    assert result["data_available"] is True
    assert result["confirmed"] is True
    assert result["state"] == "confirmed"
    assert result["market_price"] == pytest.approx(102.0)
    assert result["market_vwap"] is not None
    assert result["distance_pct"] is not None
    assert result["slope"] == pytest.approx(3.0)


def test_live_cycle_uses_safe_market_vwap_feature_for_entry_quality_call() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()

    assert "_market_feature = _market_vwap_feature_result(" in source
    assert "market_vwap_data_available=bool(_quality_market_data_available)" in source
    assert "bool(_quality_market_vwap is not None and _quality_market_px is not None)" not in source


def test_entry_evaluation_runtime_error_reason_is_stable() -> None:
    from src.app.live_cycle import _entry_evaluation_runtime_error_reason

    assert _entry_evaluation_runtime_error_reason(UnboundLocalError("x")) == (
        "entry_evaluation_runtime_error:UnboundLocalError"
    )


def test_session_feature_result_available_and_unavailable() -> None:
    from src.app.live_cycle import _session_feature_result

    available = _session_feature_result(datetime(2026, 7, 17, 14, 0, tzinfo=timezone.utc))
    unavailable = _session_feature_result(None)

    assert available["session_available"] is True
    assert available["session_open"] is not None
    assert available["session_close"] is not None
    assert available["session_duration"] > 0
    assert available["session_elapsed"] >= 0
    assert available["session_elapsed_minutes"] >= 0
    assert available["session_remaining_minutes"] >= 0
    assert unavailable == {
        "session_open": None,
        "session_close": None,
        "session_duration": None,
        "session_elapsed": None,
        "session_elapsed_minutes": None,
        "session_remaining_minutes": None,
        "session_available": False,
    }


def test_live_cycle_quality_metadata_uses_session_helper_not_conditional_local() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()

    assert "_sess_open_dt" not in source
    assert "_quality_session_features = _session_feature_result(dt)" in source
    assert "start=_quality_session_start_utc" in source
    assert "end=_quality_session_end_utc" in source


def test_entry_evaluation_context_prefers_valid_quote_reference_price() -> None:
    from src.app.live_cycle import _entry_evaluation_context

    quote = SimpleNamespace(bid=39.90, ask=40.10, mid=40.0)
    bars = pd.DataFrame({"close": [38.0, 39.0]})

    context = _entry_evaluation_context(
        symbol="XLF",
        route="trend_long",
        quote=quote,
        bars=bars,
        current_price=39.0,
        now=datetime(2026, 7, 17, 14, 0, tzinfo=timezone.utc),
        stale_quote_max_age=60,
    )

    assert context["reference_price_available"] is True
    assert context["reference_price"] == pytest.approx(40.0)
    assert context["reference_price_source"] == "quote"
    assert context["reference_price_attempted_sources"][0]["source"] == "quote"
    assert context["reference_price_attempted_sources"][0]["value"] == pytest.approx(40.0)
    assert context["session_available"] is True


def test_entry_evaluation_context_uses_latest_close_when_quote_unavailable() -> None:
    from src.app.live_cycle import _entry_evaluation_context

    bars = pd.DataFrame({"close": [38.0, 39.25]})

    context = _entry_evaluation_context(
        symbol="XLF",
        route="trend_long",
        quote=None,
        bars=bars,
        current_price=39.0,
        now=datetime(2026, 7, 17, 14, 0, tzinfo=timezone.utc),
    )

    assert context["reference_price_available"] is True
    assert context["reference_price"] == pytest.approx(39.25)
    assert context["reference_price_source"] == "latest_intraday_close"
    assert [row["source"] for row in context["reference_price_attempted_sources"][:2]] == [
        "quote",
        "latest_intraday_close",
    ]


@pytest.mark.parametrize("bad_value", [0.0, -1.0, float("nan"), float("inf")])
def test_entry_evaluation_context_rejects_invalid_reference_values(bad_value: float) -> None:
    from src.app.live_cycle import _entry_evaluation_context

    context = _entry_evaluation_context(
        symbol="XLF",
        route="trend_long",
        quote=None,
        bars=pd.DataFrame({"close": [bad_value]}),
        signal_price=bad_value,
        scanner_price=bad_value,
        current_price=bad_value,
        now=None,
    )

    assert context["reference_price_available"] is False
    assert context["reference_price"] is None
    assert context["reference_price_source"] == "unavailable"
    assert context["reference_price_unavailable_reason"] == "no_valid_reference_price"
    assert all(row["available"] is False for row in context["reference_price_attempted_sources"])
    assert context["session_available"] is False


def test_entry_evaluation_context_rejects_stale_and_crossed_quotes() -> None:
    from src.app.live_cycle import _entry_evaluation_context

    stale_quote = SimpleNamespace(
        bid=39.90,
        ask=40.10,
        mid=40.0,
        is_stale=lambda max_age: True,
    )
    stale_context = _entry_evaluation_context(
        symbol="XLF",
        quote=stale_quote,
        bars=None,
        now=datetime(2026, 7, 17, 14, 0, tzinfo=timezone.utc),
        stale_quote_max_age=60,
    )
    crossed_context = _entry_evaluation_context(
        symbol="XLF",
        quote=SimpleNamespace(bid=40.10, ask=39.90, mid=40.0),
        bars=None,
        now=datetime(2026, 7, 17, 14, 0, tzinfo=timezone.utc),
        stale_quote_max_age=60,
    )

    assert stale_context["reference_price_available"] is False
    assert stale_context["reference_price_unavailable_reason"] == "quote_stale"
    assert stale_context["reference_price_attempted_sources"] == [
        {"source": "quote", "available": False, "reason": "quote_stale"}
    ]
    assert crossed_context["reference_price_available"] is False
    assert crossed_context["reference_price_unavailable_reason"] == "quote_crossed"


def test_entry_evaluation_context_records_quote_age_and_bar_timestamp() -> None:
    from src.app.live_cycle import _entry_evaluation_context

    bars = pd.DataFrame(
        {"close": [39.25]},
        index=pd.DatetimeIndex([datetime(2026, 7, 24, 18, 54, tzinfo=timezone.utc)]),
    )
    context = _entry_evaluation_context(
        symbol="XLE",
        route="trend_long",
        quote=SimpleNamespace(bid=39.9, ask=40.1, mid=40.0, age_seconds=12.5),
        bars=bars,
        now=datetime(2026, 7, 24, 18, 54, tzinfo=timezone.utc),
    )

    assert context["reference_price_source"] == "quote"
    assert context["reference_price_diagnostics"]["quote_age_seconds"] == pytest.approx(12.5)
    assert "2026-07-24" in context["reference_price_diagnostics"]["bar_timestamp"]


def test_live_cycle_entry_quality_uses_context_reference_price_guard() -> None:
    source = (PROJECT_ROOT / "src" / "app" / "live_cycle.py").read_text()

    assert "_entry_eval_context = _entry_evaluation_context(" in source
    assert "_ref_px = None\n                                    quote = broker.get_latest_quote(symbol)" in source
    assert "_ref_px = _entry_eval_context.get(\"reference_price\")" in source
    assert "\n                                        _ref_px = float(close)" not in source
    assert "if _eval_allowed and _entry_quality_route_eligible and _ref_px is None:" in source
    assert "reference_price_unavailable" in source
    assert "attempted_sources=%s" in source
