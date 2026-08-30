"""Tests for scoring_prefilter helpers."""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import pytest

from src.scoring_prefilter import (
    build_scoring_allowlist_from_ranked,
    compute_scoring_allowed_symbols,
    merge_scoring_config_for_cash,
    scoring_inputs_from_daily_bars,
    scoring_max_bucket_n,
    should_apply_scoring_gate,
)


def _daily_df(n: int = 220, *, last_vol_mult: float = 1.0) -> pd.DataFrame:
    close = np.linspace(50.0, 150.0, n)
    base_vol = np.full(n, 1e6)
    base_vol[-1] *= last_vol_mult
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    return pd.DataFrame({"close": close, "volume": base_vol}, index=idx)


def test_scoring_inputs_short_history() -> None:
    assert scoring_inputs_from_daily_bars(_daily_df(199)) is None


def test_scoring_inputs_dynamic_short_history_requires_explicit_override() -> None:
    assert scoring_inputs_from_daily_bars(_daily_df(96), min_bars=80) is None
    data = scoring_inputs_from_daily_bars(
        _daily_df(96),
        min_bars=80,
        allow_short_ma200=True,
    )
    assert data is not None
    assert data["ma200"] == pytest.approx(_daily_df(96)["close"].mean())


def test_merge_scoring_config_for_cash_overlays_when_triggered() -> None:
    cfg = {
        "portfolio": {"high_cash_deploy_pct": 30},
        "scoring": {
            "enabled": True,
            "min_score": 8,
            "top_n_candidates": 5,
            "when_high_cash": {"min_score": 5, "top_n_candidates": 99},
        },
    }
    base = merge_scoring_config_for_cash(
        cfg, account_cash=20_000.0, account_equity=100_000.0
    )
    assert base["min_score"] == 8
    assert scoring_max_bucket_n(base) == 5
    hi = merge_scoring_config_for_cash(cfg, account_cash=35_000.0, account_equity=100_000.0)
    assert hi["min_score"] == 5
    assert hi["top_n_candidates"] == 99
    assert scoring_max_bucket_n(hi) == 99
    assert "when_high_cash" not in hi


def test_scoring_max_bucket_n_prefers_top_n_over_max_candidates() -> None:
    assert scoring_max_bucket_n({"top_n_candidates": 12, "max_candidates": 5}) == 12
    assert scoring_max_bucket_n({"max_candidates": 7}) == 7
    assert scoring_max_bucket_n({}) == 5


def test_compute_warns_when_allowlist_only_true(caplog: pytest.LogCaptureFixture) -> None:
    bars = {"AAA": _daily_df(220, last_vol_mult=2.0)}

    def _get(sym: str) -> pd.DataFrame:
        return bars[sym]

    cfg = {
        "scoring": {
            "enabled": True,
            "allowlist_only": True,
            "min_score": 0,
            "top_n_candidates": 1,
            "selection_mode": "ranked_top_n",
            "weights": {},
        }
    }
    with caplog.at_level(logging.WARNING):
        out = compute_scoring_allowed_symbols(cfg, ["AAA"], _get, 4)
    assert out is not None
    assert "allowlist_only" in caplog.text
    assert "AAA" in out


def test_scoring_inputs_ok() -> None:
    d = scoring_inputs_from_daily_bars(_daily_df(220))
    assert d is not None
    assert d["price"] == 150.0
    assert d["volume"] == 1e6
    assert d["avg_volume"] == 1e6


def test_compute_disabled_returns_none() -> None:
    assert (
        compute_scoring_allowed_symbols(
            {"scoring": {"enabled": False}},
            ["AAA"],
            lambda s: _daily_df(),
            4,
        )
        is None
    )


def test_compute_returns_allowlist() -> None:
    bars = {"AAA": _daily_df(220, last_vol_mult=2.0), "BBB": _daily_df(220, last_vol_mult=1.0)}

    def _get(sym: str) -> pd.DataFrame:
        return bars[sym]

    allowed = compute_scoring_allowed_symbols(
        {"scoring": {"enabled": True, "min_score": 0, "max_candidates": 5, "weights": {}}},
        ["AAA", "BBB"],
        _get,
        4,
    )
    assert allowed is not None
    assert "AAA" in allowed
    assert "BBB" in allowed


def test_compute_dynamic_short_history_default_still_requires_ma200(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bars = {"IREZ": _daily_df(96, last_vol_mult=2.0)}

    def _get(sym: str) -> pd.DataFrame:
        return bars[sym]

    cfg = {
        "scoring": {
            "enabled": True,
            "min_score": 0,
            "top_n_candidates": 5,
            "selection_mode": "ranked_top_n",
            "weights": {},
            "min_history_bars": {
                "core": 200,
                "dynamic": 80,
                "enable_dynamic_override": False,
            },
        }
    }
    with caplog.at_level(logging.INFO, logger="src.scoring_prefilter"):
        allowed = compute_scoring_allowed_symbols(
            cfg,
            ["IREZ"],
            _get,
            4,
            dynamic_symbols=["IREZ"],
        )

    assert allowed == frozenset()
    assert "SCORING_PREFILTER_SHORT_HISTORY symbol=IREZ candidate_type=dynamic bars=96 required_bars=200" in caplog.text
    assert "missing_indicators=ma200,ma50_gt_ma200,configured_min_history" in caplog.text
    assert "dynamic_override_enabled=False" in caplog.text


def test_compute_dynamic_short_history_override_is_config_gated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bars = {
        "IREZ": _daily_df(96, last_vol_mult=2.0),
        "CORE": _daily_df(96, last_vol_mult=2.0),
    }

    def _get(sym: str) -> pd.DataFrame:
        return bars[sym]

    cfg = {
        "scoring": {
            "enabled": True,
            "min_score": 0,
            "top_n_candidates": 5,
            "selection_mode": "ranked_top_n",
            "weights": {},
            "min_history_bars": {
                "core": 200,
                "dynamic": 80,
                "enable_dynamic_override": True,
            },
        }
    }
    with caplog.at_level(logging.INFO, logger="src.scoring_prefilter"):
        allowed = compute_scoring_allowed_symbols(
            cfg,
            ["IREZ", "CORE"],
            _get,
            4,
            dynamic_symbols=["IREZ"],
        )

    assert allowed is not None
    assert "IREZ" in allowed
    assert "CORE" not in allowed
    assert "SCORING_PREFILTER_DYNAMIC_SHORT_HISTORY symbol=IREZ bars=96 required_bars=80" in caplog.text
    assert "SCORING_PREFILTER_SHORT_HISTORY symbol=CORE candidate_type=core bars=96 required_bars=200" in caplog.text


def test_build_allowlist_top_n_vs_threshold() -> None:
    """top_n only inspects the head slice; threshold fills from all passing names."""
    # Caller must pass scores descending (same contract as compute_scoring_allowed_symbols).
    ranked = [("A", 10.0, None), ("C", 9.0, None), ("D", 8.0, None), ("E", 8.0, None), ("B", 4.0, None)]
    top = build_scoring_allowlist_from_ranked(
        ranked, min_score=8.0, max_candidates=2, selection_mode="top_n"
    )
    assert top == frozenset({"A", "C"})
    thr = build_scoring_allowlist_from_ranked(
        ranked, min_score=8.0, max_candidates=4, selection_mode="threshold"
    )
    assert thr == frozenset({"A", "C", "D", "E"})


def test_build_allowlist_ranked_top_n_ignores_min_score() -> None:
    ranked = [("A", 10.0, None), ("C", 9.0, None), ("B", 2.0, None)]
    out = build_scoring_allowlist_from_ranked(
        ranked, min_score=100.0, max_candidates=2, selection_mode="ranked_top_n"
    )
    assert out == frozenset({"A", "C"})


def test_build_allowlist_unknown_mode_falls_back_to_top_n() -> None:
    ranked = [("A", 10.0, None), ("B", 2.0, None)]
    out = build_scoring_allowlist_from_ranked(
        ranked, min_score=0.0, max_candidates=1, selection_mode="nope"
    )
    assert out == frozenset({"A"})


def test_build_allowlist_max_candidates_zero() -> None:
    assert (
        build_scoring_allowlist_from_ranked(
            [("A", 10.0, None)], min_score=0.0, max_candidates=0, selection_mode="threshold"
        )
        == frozenset()
    )


def test_compute_respects_max_candidates_and_min_score() -> None:
    """No row meets min_score → empty allowlist."""

    def _get(sym: str) -> pd.DataFrame:
        return _daily_df(220, last_vol_mult=1.01)

    allowed = compute_scoring_allowed_symbols(
        {
            "scoring": {
                "enabled": True,
                "min_score": 100,
                "max_candidates": 5,
                "weights": {"trend": 1.0, "pullback": 1.0, "volume": 1.0, "regime": 1.0},
            }
        },
        ["AAA", "BBB"],
        _get,
        0,
    )
    assert allowed is not None
    assert len(allowed) == 0


def test_should_apply_scoring_gate() -> None:
    assert not should_apply_scoring_gate(
        scoring_allowed=None,
        sym_upper="X",
        current_positions=set(),
        tracked_keys_upper=set(),
    )
    assert not should_apply_scoring_gate(
        scoring_allowed=frozenset({"X"}),
        sym_upper="X",
        current_positions={"X"},
        tracked_keys_upper=set(),
    )
    assert not should_apply_scoring_gate(
        scoring_allowed=frozenset({"X"}),
        sym_upper="X",
        current_positions=set(),
        tracked_keys_upper={"X"},
    )
    assert should_apply_scoring_gate(
        scoring_allowed=frozenset({"X"}),
        sym_upper="Y",
        current_positions=set(),
        tracked_keys_upper=set(),
    )
