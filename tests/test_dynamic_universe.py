"""Tests for :mod:`src.dynamic_universe`."""

from __future__ import annotations

import logging
import os
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src import dynamic_universe as du
from src import news_catalyst as nc
from src.news_catalyst import NewsCatalyst


def test_alpaca_news_catalyst_injects_dynamic_mover(caplog: pytest.LogCaptureFixture) -> None:
    movers: list[dict[str, str]] = []
    mover_symbols: list[str] = []
    cat = NewsCatalyst(
        symbol="ABCD",
        score=4,
        headline="ABCD wins contract with major customer",
        published_at=datetime.now(timezone.utc) - timedelta(seconds=12),
        source="alpaca",
        catalyst_type="deal",
    )

    caplog.set_level(logging.INFO, logger="src.dynamic_universe")
    skipped_core = du._append_news_dynamic_movers(movers, mover_symbols, {"ABCD": cat}, core_symbols=[])

    assert skipped_core == 0
    assert movers == [{"symbol": "ABCD"}]
    assert mover_symbols == ["ABCD"]
    assert "ALPACA_NEWS_CANDIDATE_INJECTED symbol=ABCD" in caplog.text


class _OneMoverMarket:
    def __init__(
        self,
        symbol: str,
        *,
        price: float,
        avg_volume: float,
        relative_volume: float = 2.0,
        day_gain_pct: float = 10.0,
        bid: float | None = None,
        ask: float | None = None,
        quote_timestamp: str | None = None,
        quote_age_seconds: float | None = None,
        quote_source: str | None = None,
    ) -> None:
        self.symbol = symbol
        bid = price * 0.999 if bid is None else bid
        ask = price * 1.001 if ask is None else ask
        self.snapshot = {
            "symbol": symbol,
            "price": price,
            "day_gain_pct": day_gain_pct,
            "volume": avg_volume * relative_volume,
            "bid": bid,
            "ask": ask,
        }
        if quote_timestamp is not None:
            self.snapshot["quote_timestamp"] = quote_timestamp
        if quote_age_seconds is not None:
            self.snapshot["quote_age_seconds"] = quote_age_seconds
        if quote_source is not None:
            self.snapshot["quote_source"] = quote_source
        self.avg_volume = avg_volume

    def get_top_movers(self):
        return [{"symbol": self.symbol}]

    def get_snapshots_batch(self, symbols):
        return {self.symbol: self.snapshot}

    def get_avg_volumes(self, symbols):
        return {self.symbol: self.avg_volume}

    def get_bars_batch(self, symbols, timeframe: str = "1Min", limit: int = 60):
        return {self.symbol: pd.DataFrame() for _s in symbols}


class _CorporateActionMarket(_OneMoverMarket):
    def __init__(self, *args, actions=None, fail_actions: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.actions = list(actions or [])
        self.fail_actions = fail_actions

    def get_corporate_actions(self, symbols, **_kwargs):
        if self.fail_actions:
            raise RuntimeError("corporate actions unavailable")
        return [row for row in self.actions if str(row.get("symbol", "")).upper() in {str(s).upper() for s in symbols}]


class _ThemeMoverMarket(_OneMoverMarket):
    def get_snapshots_batch(self, symbols):
        out = {self.symbol: self.snapshot}
        for sym in symbols:
            su = str(sym).upper()
            if su in {"SMH", "SOXX"}:
                out[su] = {"symbol": su, "day_gain_pct": 4.0}
        return out


class _RetryQuoteMarket(_OneMoverMarket):
    def __init__(self, *args, retry_snapshots: list[dict[str, float]], **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.retry_snapshots = [dict(row) for row in retry_snapshots]
        self.get_snapshot_calls: list[str] = []

    def get_snapshot(self, symbol):
        self.get_snapshot_calls.append(str(symbol).upper())
        if self.retry_snapshots:
            row = self.retry_snapshots.pop(0)
            row.setdefault("symbol", self.symbol)
            return row
        return dict(self.snapshot)


class _MultiMoverMarket:
    def __init__(self, rows: dict[str, dict[str, float]]) -> None:
        self.rows = {str(sym).upper(): dict(row) for sym, row in rows.items()}
        self.snapshot_symbols: list[str] = []
        self.avg_volume_symbols: list[str] = []
        self.bars_symbols: list[str] = []

    def get_top_movers(self):
        return [{"symbol": sym} for sym in self.rows]

    def get_snapshots_batch(self, symbols):
        self.snapshot_symbols = [str(s).upper() for s in symbols]
        out = {}
        for sym in self.snapshot_symbols:
            row = self.rows.get(sym, {})
            price = float(row.get("price", 10.0))
            rel = float(row.get("relative_volume", 2.0))
            avg = float(row.get("avg_volume", 50_000.0))
            bid = float(row.get("bid", price * 0.999))
            ask = float(row.get("ask", price * 1.001))
            out[sym] = {
                "symbol": sym,
                "price": price,
                "day_gain_pct": float(row.get("day_gain_pct", 10.0)),
                "volume": avg * rel,
                "bid": bid,
                "ask": ask,
            }
        return out

    def get_avg_volumes(self, symbols):
        self.avg_volume_symbols = [str(s).upper() for s in symbols]
        return {
            sym: float(self.rows.get(sym, {}).get("avg_volume", 50_000.0))
            for sym in self.avg_volume_symbols
        }

    def get_bars_batch(self, symbols, timeframe: str = "1Min", limit: int = 60):
        self.bars_symbols.extend(str(s).upper() for s in symbols)
        return {str(s).upper(): pd.DataFrame() for s in symbols}


def _scanner_cfg(**overrides) -> dict:
    cfg = {
        "enabled": True,
        "max_symbols": 3,
        "min_price": 2,
        "max_price": 150,
        "min_day_gain_pct": 8.0,
        "max_day_gain_pct": 80.0,
        "min_avg_volume": 10_000,
        "min_relative_volume": 0.75,
        "min_rel_volume": 0.75,
        "min_intraday_range_pct": 0.0,
        "min_atr_expansion_ratio": 0.0,
        "max_spread_pct": 5.0,
        "catalyst_boost": {
            "enabled": True,
            "min_news_score": 0.60,
            "score_boost": 2.0,
            "allow_rel_volume_relax": True,
            "min_relative_volume_with_catalyst": 0.75,
            "allow_vwap_relax": True,
            "max_gain_pct_catalyst": 250,
            "max_gain_pct_with_catalyst": 250,
        },
        "strong_catalyst_override": {
            "enabled": True,
            "min_news_score": 3,
            "min_event_score": 2.5,
            "max_day_gain_pct": 250,
            "max_spread_pct": 4.0,
            "keep_min_price_filter": True,
            "keep_bad_quote_filter": True,
            "keep_unstable_quote_filter": True,
        },
        "artifact_history": {"enabled": False},
    }
    cfg.update(overrides)
    return cfg


def _scan_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    symbol: str,
    *,
    price: float,
    avg_volume: float,
    relative_volume: float = 2.0,
    day_gain_pct: float = 10.0,
    bid: float | None = None,
    ask: float | None = None,
    quote_timestamp: str | None = None,
    quote_age_seconds: float | None = None,
    quote_source: str | None = None,
    cfg: dict | None = None,
    premarket_artifacts: dict[str, dict[str, object]] | None = None,
    emit_logs: bool = False,
    now: datetime | None = None,
) -> du.DynamicScanBatchResult:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})
    return du.scan_candidates_batch(
        _OneMoverMarket(
            symbol,
            price=price,
            avg_volume=avg_volume,
            relative_volume=relative_volume,
            day_gain_pct=day_gain_pct,
            bid=bid,
            ask=ask,
            quote_timestamp=quote_timestamp,
            quote_age_seconds=quote_age_seconds,
            quote_source=quote_source,
        ),
        [],
        cfg or _scanner_cfg(),
        emit_logs=emit_logs,
        premarket_artifacts=premarket_artifacts,
        now=now,
    )


def _loosened_scanner_cfg(**overrides) -> dict:
    cfg = _scanner_cfg(
        min_day_gain_pct=2.0,
        min_relative_volume=0.5,
        min_rel_volume=0.5,
        max_spread_pct=2.5,
    )
    cfg.update(overrides)
    return cfg


def _live_quote_retry_cfg(**overrides) -> dict:
    cfg = _loosened_scanner_cfg(
        broker_is_paper=False,
        market_data={
            "live_dynamic_quote_retry": {
                "enabled": True,
                "attempts": 2,
                "delay_seconds": 0.0,
            }
        },
    )
    cfg.update(overrides)
    return cfg


def _paper_quote_retry_cfg(**overrides) -> dict:
    cfg = _loosened_scanner_cfg(
        broker_is_paper=True,
        market_data={
            "dynamic_quote_retry": {
                "enabled": True,
                "paper_enabled": True,
                "live_enabled": True,
                "attempts": 2,
                "delay_seconds": 0.0,
            }
        },
    )
    cfg.update(overrides)
    return cfg


def _paper_quote_retry_disabled_cfg(**overrides) -> dict:
    cfg = _loosened_scanner_cfg(
        broker_is_paper=True,
        market_data={
            "dynamic_quote_retry": {
                "enabled": True,
                "paper_enabled": False,
                "live_enabled": True,
                "attempts": 2,
                "delay_seconds": 0.0,
            }
        },
    )
    cfg.update(overrides)
    return cfg


def _range_bars(price: float, range_pct: float) -> pd.DataFrame:
    span = price * range_pct / 100.0
    high = price + span / 2.0
    low = price - span / 2.0
    return pd.DataFrame(
        {
            "high": [high] * 30,
            "low": [low] * 30,
            "close": [price] * 30,
            "volume": [100_000] * 30,
        }
    )


def test_dynamic_candidate_range_105_passes_after_loosen(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})
    mc = _OneMoverMarket("HIVE", price=10.0, avg_volume=100_000, relative_volume=2.0)
    mc.get_bars_batch = MagicMock(
        side_effect=lambda symbols, timeframe="1Min", limit=60: {
            "HIVE": _range_bars(10.0, 1.05) if timeframe == "1Min" else pd.DataFrame()
        }
    )

    out = du.scan_candidates_batch(
        mc,
        [],
        cfg=_loosened_scanner_cfg(min_intraday_range_pct=1.0),
    )

    assert out.selected == ["HIVE"]
    assert [row.symbol for row in out.accepted] == ["HIVE"]


def test_dynamic_candidate_range_08_still_rejects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})
    mc = _OneMoverMarket("HIVE", price=10.0, avg_volume=100_000, relative_volume=2.0)
    mc.get_bars_batch = MagicMock(
        side_effect=lambda symbols, timeframe="1Min", limit=60: {
            "HIVE": _range_bars(10.0, 0.8) if timeframe == "1Min" else pd.DataFrame()
        }
    )

    out = du.scan_candidates_batch(
        mc,
        [],
        cfg=_loosened_scanner_cfg(min_intraday_range_pct=1.0),
    )

    assert out.selected == []
    assert out.rejected[0].symbol == "HIVE"
    assert out.rejected[0].rejection_reason == "intraday range"


def _candidate(
    symbol: str,
    *,
    accepted: bool,
    reason: str | None = None,
    news_score: int = 0,
    catalyst_score: float = 0.0,
    theme_bonus: float = 0.0,
) -> du.DynamicScanCandidate:
    return du.DynamicScanCandidate(
        symbol=symbol,
        score=1.0 if accepted else 0.0,
        accepted=accepted,
        rejection_reason=reason,
        price=10.0,
        day_gain_pct=10.0,
        volume=100_000,
        avg_volume=50_000,
        relative_volume=2.0,
        spread_pct=0.2,
        quality=None,
        news_score=news_score,
        catalyst_score=catalyst_score,
        theme_bonus=theme_bonus,
    )


def test_dynamic_loosened_gain_and_rvol_prefilter_accepts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    result = _scan_one(
        monkeypatch,
        tmp_path,
        "LOOSE",
        price=10.0,
        avg_volume=100_000,
        relative_volume=0.65,
        day_gain_pct=2.4,
        cfg=_loosened_scanner_cfg(),
        emit_logs=True,
    )

    assert result.selected == ["LOOSE"]
    assert [row.symbol for row in result.accepted] == ["LOOSE"]
    out = capsys.readouterr().out
    assert "DYNAMIC_LOOSENED_PASS symbol=LOOSE old_reason=below_min_day_gain" in out
    assert "DYNAMIC_LOOSENED_PASS symbol=LOOSE old_reason=below_min_relative_volume" in out


def test_dynamic_stock_rvol_loosen_accepts_exact_050_floor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = _scan_one(
        monkeypatch,
        tmp_path,
        "FLOOR",
        price=10.0,
        avg_volume=100_000,
        relative_volume=0.50,
        day_gain_pct=2.4,
        cfg=_loosened_scanner_cfg(),
    )

    assert result.selected == ["FLOOR"]
    assert result.accepted[0].relative_volume == pytest.approx(0.50)


def test_dynamic_loosened_gates_keep_unstable_quote_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = _scan_one(
        monkeypatch,
        tmp_path,
        "WIDE",
        price=10.0,
        avg_volume=100_000,
        relative_volume=0.65,
        day_gain_pct=2.4,
        bid=8.0,
        ask=12.0,
        cfg=_loosened_scanner_cfg(),
    )

    assert result.accepted == []
    assert result.rejected[0].symbol == "WIDE"
    assert result.rejected[0].rejection_reason == "unstable quote"


def test_live_dynamic_quote_retry_accepts_after_transient_unstable_quote(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})
    market = _RetryQuoteMarket(
        "RETRY",
        price=10.0,
        avg_volume=100_000,
        relative_volume=1.0,
        day_gain_pct=10.0,
        bid=8.0,
        ask=12.0,
        retry_snapshots=[
            {
                "symbol": "RETRY",
                "price": 10.0,
                "day_gain_pct": 10.0,
                "volume": 100_000.0,
                "bid": 9.99,
                "ask": 10.01,
            }
        ],
    )

    caplog.set_level(logging.INFO, logger="src.dynamic_universe")
    out = du.scan_candidates_batch(
        market,
        [],
        _live_quote_retry_cfg(),
        emit_logs=True,
        now=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )

    assert out.selected == ["RETRY"]
    assert out.rejected == []
    assert market.get_snapshot_calls == ["RETRY"]
    assert "QUOTE_RETRY_START symbol=RETRY reason=unstable_quote attempt=1" in caplog.text
    assert "QUOTE_RETRY_SUCCESS symbol=RETRY attempt=1" in caplog.text


def test_live_dynamic_quote_retry_rejects_after_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})
    market = _RetryQuoteMarket(
        "FAILQ",
        price=10.0,
        avg_volume=100_000,
        relative_volume=1.0,
        day_gain_pct=10.0,
        bid=8.0,
        ask=12.0,
        retry_snapshots=[
            {"symbol": "FAILQ", "price": 10.0, "day_gain_pct": 10.0, "volume": 100_000.0, "bid": 8.0, "ask": 12.0},
            {"symbol": "FAILQ", "price": 10.0, "day_gain_pct": 10.0, "volume": 100_000.0, "bid": 8.1, "ask": 11.9},
        ],
    )

    caplog.set_level(logging.INFO, logger="src.dynamic_universe")
    out = du.scan_candidates_batch(
        market,
        [],
        _live_quote_retry_cfg(),
        emit_logs=True,
        now=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )

    assert out.accepted == []
    assert out.rejected[0].symbol == "FAILQ"
    assert out.rejected[0].rejection_reason == "unstable quote"
    assert market.get_snapshot_calls == ["FAILQ", "FAILQ"]
    assert "QUOTE_RETRY_FAILED symbol=FAILQ attempts=2" in caplog.text
    assert "QUOTE_RETRY_FINAL_REJECT symbol=FAILQ reason=unstable_quote" in caplog.text


def test_dynamic_quote_retry_paper_behavior_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})
    market = _RetryQuoteMarket(
        "PAPERQ",
        price=10.0,
        avg_volume=100_000,
        relative_volume=1.0,
        day_gain_pct=10.0,
        bid=8.0,
        ask=12.0,
        retry_snapshots=[
            {"symbol": "PAPERQ", "price": 10.0, "day_gain_pct": 10.0, "volume": 100_000.0, "bid": 9.99, "ask": 10.01}
        ],
    )

    caplog.set_level(logging.INFO, logger="src.dynamic_universe")
    out = du.scan_candidates_batch(
        market,
        [],
        _live_quote_retry_cfg(broker_is_paper=True),
        emit_logs=True,
        now=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )

    assert out.accepted == []
    assert out.rejected[0].rejection_reason == "unstable quote"
    assert market.get_snapshot_calls == []
    assert "QUOTE_RETRY_START" not in caplog.text


def test_paper_dynamic_quote_retry_accepts_after_transient_unstable_quote(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})
    market = _RetryQuoteMarket(
        "PAPERQ",
        price=10.0,
        avg_volume=100_000,
        relative_volume=1.0,
        day_gain_pct=10.0,
        bid=8.0,
        ask=12.0,
        retry_snapshots=[
            {"symbol": "PAPERQ", "price": 10.0, "day_gain_pct": 10.0, "volume": 100_000.0, "bid": 9.99, "ask": 10.01}
        ],
    )

    caplog.set_level(logging.INFO, logger="src.dynamic_universe")
    out = du.scan_candidates_batch(
        market,
        [],
        _paper_quote_retry_cfg(),
        emit_logs=True,
        now=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )

    assert out.selected == ["PAPERQ"]
    assert out.rejected == []
    assert market.get_snapshot_calls == ["PAPERQ"]
    assert "QUOTE_RETRY_START symbol=PAPERQ reason=unstable_quote attempt=1" in caplog.text
    assert "QUOTE_RETRY_SUCCESS symbol=PAPERQ attempt=1" in caplog.text


def test_paper_dynamic_quote_retry_disabled_preserves_unstable_reject(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})
    market = _RetryQuoteMarket(
        "PAPERD",
        price=10.0,
        avg_volume=100_000,
        relative_volume=1.0,
        day_gain_pct=10.0,
        bid=8.0,
        ask=12.0,
        retry_snapshots=[
            {"symbol": "PAPERD", "price": 10.0, "day_gain_pct": 10.0, "volume": 100_000.0, "bid": 9.99, "ask": 10.01}
        ],
    )

    caplog.set_level(logging.INFO, logger="src.dynamic_universe")
    out = du.scan_candidates_batch(
        market,
        [],
        _paper_quote_retry_disabled_cfg(),
        emit_logs=True,
        now=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )

    assert out.accepted == []
    assert out.rejected[0].rejection_reason == "unstable quote"
    assert market.get_snapshot_calls == []
    assert "QUOTE_RETRY_START" not in caplog.text


def test_dynamic_quote_retry_core_behavior_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})
    market = _RetryQuoteMarket(
        "COREQ",
        price=10.0,
        avg_volume=100_000,
        relative_volume=1.0,
        day_gain_pct=10.0,
        bid=8.0,
        ask=12.0,
        retry_snapshots=[
            {"symbol": "COREQ", "price": 10.0, "day_gain_pct": 10.0, "volume": 100_000.0, "bid": 9.99, "ask": 10.01}
        ],
    )

    caplog.set_level(logging.INFO, logger="src.dynamic_universe")
    out = du.scan_candidates_batch(
        market,
        ["COREQ"],
        _live_quote_retry_cfg(),
        emit_logs=True,
        now=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )

    assert out.selected == []
    assert out.accepted == []
    assert out.rejected == []
    assert market.get_snapshot_calls == []
    assert "QUOTE_RETRY_START" not in caplog.text


def test_live_dynamic_quote_retry_keeps_bad_quote_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    result = _scan_one(
        monkeypatch,
        tmp_path,
        "BADQ",
        price=0.0,
        avg_volume=100_000,
        relative_volume=1.0,
        day_gain_pct=10.0,
        bid=0.0,
        ask=0.0,
        cfg=_live_quote_retry_cfg(),
        emit_logs=True,
    )

    assert result.accepted == []
    assert result.rejected[0].rejection_reason == "bad quote"
    assert "QUOTE_RETRY_START" not in caplog.text


def test_live_dynamic_quote_retry_still_applies_spread_cap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})
    market = _RetryQuoteMarket(
        "SPREADQ",
        price=10.0,
        avg_volume=100_000,
        relative_volume=1.0,
        day_gain_pct=10.0,
        bid=8.0,
        ask=12.0,
        retry_snapshots=[
            {
                "symbol": "SPREADQ",
                "price": 10.0,
                "day_gain_pct": 10.0,
                "volume": 100_000.0,
                "bid": 9.75,
                "ask": 10.25,
            }
        ],
    )

    out = du.scan_candidates_batch(
        market,
        [],
        _live_quote_retry_cfg(max_spread_pct=2.5),
        emit_logs=True,
        now=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )

    assert out.accepted == []
    assert out.rejected[0].rejection_reason == "spread too wide"


def test_dynamic_loosened_gates_keep_below_min_price_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = _scan_one(
        monkeypatch,
        tmp_path,
        "PENNY",
        price=1.99,
        avg_volume=100_000,
        relative_volume=0.65,
        day_gain_pct=2.4,
        cfg=_loosened_scanner_cfg(),
    )

    assert result.accepted == []
    assert result.rejected[0].symbol == "PENNY"
    assert result.rejected[0].rejection_reason == "below_min_price"


def test_dynamic_history_180_config_keeps_unstable_quote_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = _scan_one(
        monkeypatch,
        tmp_path,
        "HISTWIDE",
        price=10.0,
        avg_volume=100_000,
        relative_volume=0.8,
        day_gain_pct=3.0,
        bid=8.0,
        ask=12.0,
        cfg=_loosened_scanner_cfg(min_history_bars=180),
    )

    assert result.accepted == []
    assert result.rejected[0].symbol == "HISTWIDE"
    assert result.rejected[0].rejection_reason == "unstable quote"


def test_dynamic_history_180_config_keeps_below_min_price_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = _scan_one(
        monkeypatch,
        tmp_path,
        "HISTPENNY",
        price=1.99,
        avg_volume=100_000,
        relative_volume=0.8,
        day_gain_pct=3.0,
        cfg=_loosened_scanner_cfg(min_history_bars=180),
    )

    assert result.accepted == []
    assert result.rejected[0].symbol == "HISTPENNY"
    assert result.rejected[0].rejection_reason == "below_min_price"


def test_dynamic_reject_funnel_logs_stage_and_reason(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("INFO"):
        result = _scan_one(
            monkeypatch,
            tmp_path,
            "FUNNEL",
            price=1.99,
            avg_volume=100_000,
            relative_volume=0.8,
            day_gain_pct=3.0,
            cfg=_loosened_scanner_cfg(min_history_bars=180),
            emit_logs=True,
        )

    assert result.accepted == []
    assert result.rejected[0].rejection_reason == "below_min_price"
    assert "DYNAMIC_REJECT_FUNNEL reason=below_min_price symbol=FUNNEL stage=scanner" in caplog.text


def test_july1_dynamic_history_change_keeps_unstable_quote_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = _scan_one(
        monkeypatch,
        tmp_path,
        "NOISY",
        price=10.0,
        avg_volume=100_000,
        relative_volume=0.8,
        day_gain_pct=3.0,
        bid=7.0,
        ask=13.0,
        cfg=_loosened_scanner_cfg(min_history_bars=180, live_min_history_bars=50),
    )

    assert result.accepted == []
    assert result.rejected[0].symbol == "NOISY"
    assert result.rejected[0].rejection_reason == "unstable quote"


def test_dynamic_scan_analytics_counts_reasons_and_accepts(capsys: pytest.CaptureFixture) -> None:
    result = du.DynamicScanBatchResult(
        selected=["GOOD"],
        accepted=[
            _candidate("GOOD", accepted=True, news_score=8),
            _candidate("THEME", accepted=True, theme_bonus=1.5),
            _candidate("PLAIN", accepted=True),
        ],
        rejected=[
            _candidate("A", accepted=False, reason="below_min_relative_volume"),
            _candidate("B", accepted=False, reason="below_min_relative_volume"),
            _candidate("C", accepted=False, reason="entry_alignment: spread_pct 7.0 > 5.0"),
            _candidate("D", accepted=False, reason="unstable quote"),
        ],
        elapsed_ms=10,
    )

    analytics = du.log_dynamic_scan_rejection_summary(result, emit_logs=True, top_n=3)

    assert analytics["rejections"]["below_min_relative_volume"] == 2
    assert analytics["rejections"]["entry_alignment"] == 1
    assert analytics["rejections"]["unstable_quote"] == 1
    assert analytics["accepts"]["catalyst"] == 1
    assert analytics["accepts"]["theme_momentum"] == 1
    assert analytics["accepts"]["quality_filters"] == 1
    out = capsys.readouterr().out
    assert "DYNAMIC_REJECTION_SUMMARY" in out
    assert "below_min_relative_volume=2" in out
    assert "entry_alignment=1" in out


def test_dynamic_scan_artifact_history_persists_candidates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    history_dir = tmp_path / "history"
    cfg = _scanner_cfg(
        artifact_history={
            "enabled": True,
            "directory": str(history_dir),
            "retention_days": 30,
        },
    )
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})

    result = du.scan_candidates_batch(
        _OneMoverMarket(
            "LOWP",
            price=1.0,
            avg_volume=20_000,
            bid=0.999,
            ask=1.001,
            quote_timestamp="2026-06-15T13:38:07+00:00",
            quote_source="alpaca",
        ),
        [],
        cfg,
        emit_logs=False,
        history_user_id="u1",
        history_project_root=tmp_path,
        now=datetime(2026, 6, 15, 13, 38, 10, tzinfo=timezone.utc),
    )

    artifacts = list(history_dir.glob("*.json"))
    assert len(artifacts) == 1
    payload = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert payload["user_id"] == "u1"
    assert payload["selected"] == result.selected
    assert payload["counts"]["accepted"] == 0
    assert payload["counts"]["rejected"] == 1
    assert payload["rejected"][0]["symbol"] == "LOWP"
    assert payload["rejected"][0]["accepted"] is False
    assert payload["rejected"][0]["rejection_reason"] == "below_min_price"
    assert payload["rejected"][0]["timestamp"]
    assert payload["rejected"][0]["price"] == pytest.approx(1.0)
    assert payload["rejected"][0]["gain_pct"] == pytest.approx(10.0)
    assert payload["rejected"][0]["rel_volume"] == pytest.approx(2.0)
    assert payload["rejected"][0]["spread_pct"] == pytest.approx(0.2, abs=0.01)
    assert payload["rejected"][0]["bid"] == pytest.approx(0.999)
    assert payload["rejected"][0]["ask"] == pytest.approx(1.001)
    assert payload["rejected"][0]["quote_timestamp"] == "2026-06-15T13:38:07+00:00"
    assert payload["rejected"][0]["quote_age_seconds"] == pytest.approx(3.0)
    assert payload["rejected"][0]["quote_source"] == "alpaca"
    assert payload["rejected"][0]["scan_timestamp"] == "2026-06-15T13:38:10+00:00"
    assert payload["rejected"][0]["news_score"] == 0
    assert payload["rejected"][0]["catalyst_score"] == pytest.approx(0.0)
    assert payload["analytics"]["rejections"] == {"below_min_price": 1}
    daily_reports = list((history_dir / "daily").glob("*.json"))
    assert len(daily_reports) == 1
    daily = json.loads(daily_reports[0].read_text(encoding="utf-8"))
    assert daily["cycles"] == 1
    assert daily["rejection_counts"] == {"below_min_price": 1}
    assert daily["top_rejection_causes"] == {"below_min_price": 1}
    assert daily["last_cycle"]["analytics"]["rejections"] == {"below_min_price": 1}


def test_scan_candidates_batch_persists_selected_candidate_bar_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})
    index = pd.date_range("2026-06-12T14:00:00Z", periods=2, freq="1min")
    bars = pd.DataFrame(
        {
            "open": [10.0, 10.2],
            "high": [10.3, 10.5],
            "low": [9.9, 10.1],
            "close": [10.2, 10.4],
            "volume": [100_000, 125_000],
        },
        index=index,
    )

    class _BarsMarket(_OneMoverMarket):
        def get_bars_batch(self, symbols, timeframe: str = "1Min", limit: int = 60):
            if timeframe == "1Min":
                return {self.symbol: bars}
            return {self.symbol: pd.DataFrame() for _s in symbols}

    result = du.scan_candidates_batch(
        _BarsMarket("ASTN", price=10.4, avg_volume=50_000, relative_volume=2.0),
        [],
        _scanner_cfg(artifact_history={"enabled": False}),
        emit_logs=True,
        history_user_id="live_bot",
        history_project_root=tmp_path,
        now=datetime(2026, 6, 12, 10, 5, tzinfo=ZoneInfo("America/New_York")),
    )

    assert result.selected == ["ASTN"]
    path = tmp_path / "data" / "research" / "dynamic_candidate_bars" / "2026-06-12" / "live_bot" / "ASTN.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["symbol"] == "ASTN"
    assert payload["user"] == "live_bot"
    assert payload["source"] == "dynamic_selected"
    assert payload["timeframe"] == "1Min"
    assert payload["bars"][0]["timestamp"] == "2026-06-12T14:00:00+00:00"
    assert payload["bars"][1]["close"] == pytest.approx(10.4)
    assert payload["bars"][1]["volume"] == 125_000


def test_dynamic_scan_artifact_history_records_later_same_day_rejection_move(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    history_dir = tmp_path / "history"
    cfg = _scanner_cfg(
        artifact_history={
            "enabled": True,
            "directory": str(history_dir),
            "retention_days": 30,
        },
    )
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})
    future_index = pd.date_range(
        datetime.now(timezone.utc) + timedelta(minutes=1),
        periods=3,
        freq="1min",
        tz=timezone.utc,
    )
    bars = pd.DataFrame(
        {
            "high": [10.2, 11.2, 10.8],
            "low": [9.8, 10.1, 10.4],
            "close": [10.0, 10.9, 10.7],
            "volume": [100_000, 120_000, 90_000],
        },
        index=future_index,
    )

    class _FutureBarsMarket(_OneMoverMarket):
        def get_bars_batch(self, symbols, timeframe: str = "1Min", limit: int = 60):
            return {self.symbol: bars for _s in symbols}

    du.scan_candidates_batch(
        _FutureBarsMarket("MISS", price=10.0, avg_volume=5_000, relative_volume=2.0),
        [],
        cfg,
        emit_logs=False,
        history_user_id="u1",
        history_project_root=tmp_path,
    )

    artifacts = list(history_dir.glob("*.json"))
    payload = json.loads(artifacts[0].read_text(encoding="utf-8"))
    row = payload["rejected"][0]
    assert row["symbol"] == "MISS"
    assert row["rejection_reason"] == "below_min_avg_volume"
    assert row["later_same_day_high"] == pytest.approx(11.2)
    assert row["later_same_day_return_pct"] == pytest.approx(12.0)


def test_dynamic_scan_filters_core_symbols_before_market_data_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = _scanner_cfg(max_symbols=1)
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})
    market = _MultiMoverMarket(
        {
            "AAPL": {"price": 190.0},
            "DYN1": {"price": 10.0},
            "MSFT": {"price": 420.0},
            "DYN2": {"price": 12.0},
        }
    )

    with caplog.at_level(logging.INFO, logger="src.dynamic_universe"):
        out = du.scan_candidates_batch(
            market,
            ["AAPL", "MSFT"],
            cfg,
            emit_logs=True,
            history_project_root=tmp_path,
        )

    assert "AAPL" not in market.snapshot_symbols
    assert "MSFT" not in market.snapshot_symbols
    assert set(market.snapshot_symbols) == {"DYN1", "DYN2"}
    assert out.selected == ["DYN1"]
    assert "DYNAMIC_SCAN_CORE_SKIPPED_SUMMARY count=2" in caplog.text


def test_dynamic_scan_quality_atr_expansion_zero_when_recent_range_flat() -> None:
    bars_1m = pd.DataFrame(
        {
            "high": [10.1] * 25 + [10.0] * 5,
            "low": [9.9] * 25 + [10.0] * 5,
            "close": [10.0] * 30,
            "volume": [100_000.0] * 30,
        }
    )

    quality = du._intraday_quality_from_bars(
        bars_1m=bars_1m,
        bars_5m=pd.DataFrame(),
        price=10.0,
    )

    assert quality.current_atr == pytest.approx(0.0)
    assert quality.baseline_atr == pytest.approx(2.0)
    assert quality.atr_expansion_ratio == pytest.approx(0.0)


def test_dynamic_scan_logs_atr_debug_detail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})
    bars_1m = pd.DataFrame(
        {
            "high": [10.1] * 25 + [10.0] * 5,
            "low": [9.9] * 25 + [10.0] * 5,
            "close": [10.0] * 30,
            "volume": [100_000.0] * 30,
        }
    )

    class FakeMarket(_OneMoverMarket):
        def get_bars_batch(self, symbols, timeframe: str = "1Min", limit: int = 60):
            if timeframe == "5Min":
                return {s: pd.DataFrame({"close": [10.0, 10.1, 10.2]}) for s in symbols}
            return {s: bars_1m for s in symbols}

    with caplog.at_level(logging.INFO, logger="src.dynamic_universe"):
        out = du.scan_candidates_batch(
            FakeMarket("IREZ", price=10.0, avg_volume=500_000.0, relative_volume=2.0),
            [],
            _scanner_cfg(),
            emit_logs=True,
            history_project_root=tmp_path,
        )

    assert out.selected == ["IREZ"]
    assert out.accepted[0].quality is not None
    assert out.accepted[0].quality.atr_expansion_ratio == pytest.approx(0.0)
    assert (
        "ATR_DEBUG_DETAIL symbol=IREZ current_atr=0.0000 "
        "baseline_atr=2.0000 atr_expansion_ratio=0.0000"
    ) in caplog.text


def test_dynamic_scan_theme_momentum_boosts_matching_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg = _scanner_cfg(
        theme_intelligence={
            "enabled": True,
            "bonus_weight": 0.5,
            "max_bonus": 3.0,
        },
    )
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})

    result = du.scan_candidates_batch(
        _ThemeMoverMarket("AMD", price=10.0, avg_volume=50_000, day_gain_pct=8.0),
        [],
        cfg,
        emit_logs=False,
    )

    assert result.selected == ["AMD"]
    assert result.accepted[0].theme == "semiconductors"
    assert result.accepted[0].theme_bonus == pytest.approx(2.0)
    assert result.accepted[0].score >= 8.0 + 2.0


def test_dynamic_scan_artifact_history_prunes_old_files(tmp_path: Path) -> None:
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    old = history_dir / "old.json"
    old.write_text("{}", encoding="utf-8")
    old_ts = datetime(2026, 4, 1, tzinfo=timezone.utc).timestamp()
    os.utime(old, (old_ts, old_ts))
    result = du.DynamicScanBatchResult(
        selected=["GOOD"],
        accepted=[
            du.DynamicScanCandidate(
                symbol="GOOD",
                score=12.0,
                accepted=True,
                rejection_reason=None,
                price=10.0,
                day_gain_pct=12.0,
                volume=100_000,
                avg_volume=50_000,
                relative_volume=2.0,
                spread_pct=0.2,
                quality=None,
                news_score=8,
            )
        ],
        rejected=[],
        elapsed_ms=7,
    )

    path = du.persist_dynamic_scan_history(
        result,
        {
            "artifact_history": {
                "enabled": True,
                "directory": str(history_dir),
                "retention_days": 30,
            }
        },
        user_id="u1",
        now=datetime(2026, 6, 5, tzinfo=timezone.utc),
    )

    assert path is not None and path.exists()
    assert not old.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["accepted"][0]["symbol"] == "GOOD"
    assert payload["accepted"][0]["news_score"] == 8


def test_dynamic_scan_low_price_names_pass_with_min_price_2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    namm = _scan_one(monkeypatch, tmp_path, "NAMM", price=2.065, avg_volume=11_254)
    assert namm.selected == ["NAMM"]

    jz = _scan_one(monkeypatch, tmp_path, "JZ", price=2.67, avg_volume=20_000)
    assert jz.selected == ["JZ"]


def test_dynamic_scan_live_rejects_below_execution_min_price(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    out = _scan_one(
        monkeypatch,
        tmp_path,
        "RPAY",
        price=4.33,
        avg_volume=100_000,
        cfg=_scanner_cfg(min_price=2.0, broker_is_paper=False),
    )

    assert out.selected == []
    assert out.rejected[0].rejection_reason == "below_min_price"


def test_dynamic_scan_live_accepts_above_execution_min_price(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    out = _scan_one(
        monkeypatch,
        tmp_path,
        "RPAY",
        price=5.01,
        avg_volume=100_000,
        cfg=_scanner_cfg(min_price=2.0, broker_is_paper=False),
    )

    assert out.selected == ["RPAY"]


def test_dynamic_scan_above_max_price_reject_reason(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    out = _scan_one(monkeypatch, tmp_path, "NEBX", price=181.0, avg_volume=100_000)
    assert out.selected == []
    assert out.rejected[0].rejection_reason == "above_max_price"


def test_dynamic_scan_current_volume_thresholds_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    flnc = _scan_one(
        monkeypatch,
        tmp_path,
        "FLNC",
        price=25.0,
        avg_volume=10_000,
        relative_volume=1.53,
    )
    assert flnc.selected == ["FLNC"]

    namm = _scan_one(
        monkeypatch,
        tmp_path,
        "NAMM",
        price=2.065,
        avg_volume=11_254,
        relative_volume=1.1,
    )
    assert namm.selected == ["NAMM"]


def test_dynamic_scan_split_rejection_reasons(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    below_price = _scan_one(monkeypatch, tmp_path, "LOWP", price=1.99, avg_volume=100_000)
    assert below_price.rejected[0].rejection_reason == "below_min_price"

    low_avg = _scan_one(monkeypatch, tmp_path, "LOWAVG", price=10.0, avg_volume=9_999)
    assert low_avg.rejected[0].rejection_reason == "below_min_avg_volume"

    low_rel = _scan_one(
        monkeypatch,
        tmp_path,
        "LOWREL",
        price=10.0,
        avg_volume=100_000,
        relative_volume=0.74,
    )
    assert low_rel.rejected[0].rejection_reason == "below_min_relative_volume"


def test_dynamic_scan_blocks_reverse_split_corporate_action(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})
    market = _CorporateActionMarket(
        "RSPL",
        price=10,
        avg_volume=100_000,
        actions=[{"symbol": "RSPL", "ca_type": "split", "description": "1-for-20 reverse split"}],
    )
    cfg = _scanner_cfg(corporate_actions={"enabled": True, "persist_dir": str(tmp_path)})
    caplog.set_level(logging.INFO, logger="src.dynamic_universe")

    out = du.scan_candidates_batch(market, [], cfg, emit_logs=True, now=datetime(2026, 6, 18, tzinfo=timezone.utc))

    assert out.accepted == []
    assert out.rejected[0].rejection_reason == "corporate_action_reverse_split"
    assert out.rejected[0].corporate_action_type == "reverse_split"
    assert out.rejected[0].corporate_action_severity == "block"
    assert "ALPACA_CORP_ACTION_MATCH symbol=RSPL" in caplog.text
    assert "DYNAMIC_CORP_ACTION_FILTER symbol=RSPL" in caplog.text
    assert (tmp_path / "2026-06-18.jsonl").exists()


def test_dynamic_scan_allows_normal_split_with_annotation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})
    market = _CorporateActionMarket(
        "SPLT",
        price=10,
        avg_volume=100_000,
        actions=[{"symbol": "SPLT", "ca_type": "split", "description": "2-for-1 stock split"}],
    )
    cfg = _scanner_cfg(corporate_actions={"enabled": True, "persist_dir": str(tmp_path)})
    caplog.set_level(logging.INFO, logger="src.dynamic_universe")

    out = du.scan_candidates_batch(market, [], cfg, emit_logs=True, now=datetime(2026, 6, 18, tzinfo=timezone.utc))

    assert out.rejected == []
    assert out.accepted[0].symbol == "SPLT"
    assert out.accepted[0].corporate_action_type == "split"
    assert out.accepted[0].corporate_action_severity == "warn"
    assert "DYNAMIC_CORP_ACTION_ALLOW symbol=SPLT" in caplog.text


def test_dynamic_scan_no_corporate_action_leaves_candidate_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})
    market = _CorporateActionMarket("NORM", price=10, avg_volume=100_000, actions=[])
    cfg = _scanner_cfg(corporate_actions={"enabled": True, "persist_dir": str(tmp_path)})

    out = du.scan_candidates_batch(market, [], cfg, emit_logs=False, now=datetime(2026, 6, 18, tzinfo=timezone.utc))

    assert out.rejected == []
    assert out.accepted[0].symbol == "NORM"
    assert out.accepted[0].corporate_action_type is None
    assert out.accepted[0].corporate_action_severity is None


def test_dynamic_scan_corporate_action_api_failure_continues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})
    market = _CorporateActionMarket("SAFE", price=10, avg_volume=100_000, fail_actions=True)
    cfg = _scanner_cfg(corporate_actions={"enabled": True, "persist_dir": str(tmp_path)})
    caplog.set_level(logging.INFO, logger="src.dynamic_universe")

    out = du.scan_candidates_batch(market, [], cfg, emit_logs=True, now=datetime(2026, 6, 18, tzinfo=timezone.utc))

    assert out.rejected == []
    assert out.accepted[0].symbol == "SAFE"
    assert "ALPACA_CORP_ACTION_FALLBACK reason=fetch_failed" in caplog.text


def test_load_save_state_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    assert du.load_state() == {"cooldowns": {}, "active": {}}
    s = {"cooldowns": {"ZZZ": 1}, "active": {}}
    du.save_state(s)
    assert du.load_state() == s


def test_load_state_corrupt_returns_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    monkeypatch.setattr(du, "STATE_FILE", p)
    assert du.load_state() == {"cooldowns": {}, "active": {}}


def test_cooldown_and_entry_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    st = du.load_state()
    assert not du.in_cooldown("AAPL", st)
    du.mark_cooldown("AAPL", 60, st)
    st2 = du.load_state()
    assert du.in_cooldown("AAPL", st2)
    du.remember_entry("MSFT", 100.0, st2)
    st3 = du.load_state()
    assert "MSFT" in st3["active"]
    assert st3["active"]["MSFT"]["entry_price"] == pytest.approx(100.0)
    du.update_high("MSFT", 105.0, st3)
    st4 = du.load_state()
    assert st4["active"]["MSFT"]["high_price"] == pytest.approx(105.0)
    du.remove_dynamic_symbol("MSFT", st4)
    st5 = du.load_state()
    assert "MSFT" not in st5.get("active", {})


def test_expand_mover_feed_includes_leader_pools() -> None:
    mc = MagicMock()
    mc.get_top_movers.return_value = [{"symbol": "ZZZ"}]
    cfg = {
        "leader_pools": {"ai_semis": ["NVDA", "AMD"], "energy_spikes": ["XLE"]},
        "extra_mover_symbols": ["EARN1"],
        "ai_dynamic_symbols": ["PLTR", "NVDA"],
    }
    rows = du._expand_mover_feed(mc, cfg)
    syms = [r["symbol"] for r in rows]
    assert syms == ["ZZZ", "NVDA", "AMD", "XLE", "EARN1", "PLTR"]


def test_expand_mover_feed_excludes_configured_suffixes() -> None:
    mc = MagicMock()
    mc.get_top_movers.return_value = [
        {"symbol": "PIIIW"},
        {"symbol": "DSYWW"},
        {"symbol": "KVACW"},
        {"symbol": "REAL"},
    ]
    cfg = {
        "exclude_suffixes": ["W", "WS", "WT", "U", "R"],
        "extra_mover_symbols": ["SPACU", "RIGHTR", "KEEP"],
    }

    rows = du._expand_mover_feed(mc, cfg)

    assert [r["symbol"] for r in rows] == ["REAL", "KEEP"]


def test_expand_mover_feed_excludes_occ_option_symbols() -> None:
    mc = MagicMock()
    mc.get_top_movers.return_value = [
        {"symbol": "AAPL260619C00200000"},
        {"symbol": "REAL"},
    ]
    cfg = {
        "extra_mover_symbols": ["MSFT260619P00400000", "KEEP"],
    }

    rows = du._expand_mover_feed(mc, cfg)

    assert [r["symbol"] for r in rows] == ["REAL", "KEEP"]


def test_scan_dynamic_candidates_disabled() -> None:
    assert du.scan_dynamic_candidates(MagicMock(), [], {"enabled": False}) == []


def test_scan_dynamic_candidates_filters_and_sorts(monkeypatch: pytest.MonkeyPatch) -> None:
    mc = MagicMock()
    mc.get_top_movers.return_value = [
        {"symbol": "CORE"},
        {"symbol": "AAA"},
        {"symbol": "BBB"},
        {"symbol": "BAD"},
    ]
    mc.get_snapshot.side_effect = lambda sym: {
        "AAA": {
            "price": 50.0,
            "day_gain_pct": 10.0,
            "volume": 3_000_000,
            "bid": 49.9,
            "ask": 50.1,
        },
        "BBB": {
            "price": 40.0,
            "day_gain_pct": 12.0,
            "volume": 3_000_000,
            "bid": 39.9,
            "ask": 40.1,
        },
        "BAD": {
            "price": 5.0,
            "day_gain_pct": 50.0,
            "volume": 1.0,
            "bid": 4.9,
            "ask": 5.1,
        },
    }[sym]
    mc.get_avg_volume.return_value = 2_000_000.0

    out = du.scan_dynamic_candidates(
        mc,
        ["CORE"],
        cfg={
            "enabled": True,
            "max_symbols": 2,
            "min_price": 10,
            "max_price": 1000,
            "min_day_gain_pct": 3.0,
            "max_day_gain_pct": 15.0,
            "min_avg_volume": 2_000_000,
            "min_relative_volume": 1.0,
            "max_spread_pct": 1.0,
        },
    )
    # Higher day_gain_pct first
    assert out == ["BBB", "AAA"]


def test_dynamic_scan_loosened_gain_and_rvol_boundaries_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})

    out = du.scan_candidates_batch(
        _OneMoverMarket("FLOOR", price=10.0, avg_volume=1_000_000, relative_volume=1.0, day_gain_pct=3.0),
        [],
        {
            "enabled": True,
            "max_symbols": 20,
            "min_price": 2,
            "max_price": 150,
            "min_day_gain_pct": 3.0,
            "max_day_gain_pct": 80.0,
            "min_avg_volume": 10_000,
            "min_relative_volume": 1.0,
            "max_spread_pct": 2.5,
        },
        emit_logs=False,
    )

    assert out.selected == ["FLOOR"]
    assert out.accepted[0].day_gain_pct == pytest.approx(3.0)
    assert out.accepted[0].relative_volume == pytest.approx(1.0)


def test_dynamic_scan_relative_volume_equal_threshold_passes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    out = _scan_one(
        monkeypatch,
        tmp_path,
        "SPCH",
        price=10.0,
        avg_volume=1_000_000,
        relative_volume=0.60,
        day_gain_pct=10.0,
        cfg=_scanner_cfg(min_relative_volume=0.60, min_rel_volume=0.60),
        emit_logs=True,
    )

    assert out.selected == ["SPCH"]
    assert out.accepted[0].relative_volume == pytest.approx(0.60)
    assert out.rejected == []


def test_dynamic_scan_relative_volume_slightly_below_threshold_rejects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    out = _scan_one(
        monkeypatch,
        tmp_path,
        "SPCL",
        price=10.0,
        avg_volume=1_000_000,
        relative_volume=0.60 - 1e-6,
        day_gain_pct=10.0,
        cfg=_scanner_cfg(min_relative_volume=0.60, min_rel_volume=0.60),
        emit_logs=True,
    )

    assert out.selected == []
    assert out.rejected[0].symbol == "SPCL"
    assert out.rejected[0].rejection_reason == "below_min_relative_volume"


def test_dynamic_scan_relative_volume_rounding_does_not_false_reject(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    out = _scan_one(
        monkeypatch,
        tmp_path,
        "RNDV",
        price=10.0,
        avg_volume=1_000_000,
        relative_volume=0.60 - 5e-10,
        day_gain_pct=10.0,
        cfg=_scanner_cfg(min_relative_volume=0.60, min_rel_volume=0.60),
        emit_logs=True,
    )

    captured = capsys.readouterr()
    assert out.selected == ["RNDV"]
    assert out.rejected == []
    assert "below_min_relative_volume rel=0.60 min=0.60" not in captured.out


def test_scan_candidates_batch_matches_per_symbol_scan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")

    class FakeMarket:
        def __init__(self) -> None:
            self.snapshots = {
                "AAA": {
                    "symbol": "AAA",
                    "price": 50.0,
                    "day_gain_pct": 10.0,
                    "volume": 3_000_000,
                    "bid": 49.9,
                    "ask": 50.1,
                },
                "BBB": {
                    "symbol": "BBB",
                    "price": 40.0,
                    "day_gain_pct": 12.0,
                    "volume": 3_000_000,
                    "bid": 39.9,
                    "ask": 40.1,
                },
                "BAD": {
                    "symbol": "BAD",
                    "price": 5.0,
                    "day_gain_pct": 50.0,
                    "volume": 1.0,
                    "bid": 4.9,
                    "ask": 5.1,
                },
            }
            self.avg_volumes = {"AAA": 2_000_000.0, "BBB": 2_000_000.0, "BAD": 2_000_000.0}
            self.batch_snapshot_calls = 0
            self.batch_bars_calls = 0

        def get_top_movers(self):
            return [{"symbol": "CORE"}, {"symbol": "AAA"}, {"symbol": "BBB"}, {"symbol": "BAD"}]

        def get_snapshot(self, symbol: str):
            return self.snapshots[symbol]

        def get_snapshots_batch(self, symbols):
            self.batch_snapshot_calls += 1
            return {s: self.snapshots[s] for s in symbols if s in self.snapshots}

        def get_avg_volume(self, symbol: str) -> float:
            return self.avg_volumes[symbol]

        def get_avg_volumes(self, symbols):
            return {s: self.avg_volumes[s] for s in symbols if s in self.avg_volumes}

        def get_bars(self, symbol: str, timeframe: str = "1Min", limit: int = 60):
            return pd.DataFrame()

        def get_bars_batch(self, symbols, timeframe: str = "1Min", limit: int = 60):
            self.batch_bars_calls += 1
            return {s: pd.DataFrame() for s in symbols}

    cfg = {
        "enabled": True,
        "max_symbols": 2,
        "min_price": 10,
        "max_price": 1000,
        "min_day_gain_pct": 3.0,
        "max_day_gain_pct": 15.0,
        "min_avg_volume": 2_000_000,
        "min_relative_volume": 1.0,
        "max_spread_pct": 1.0,
    }

    per_symbol = du._scan_candidates_per_symbol(FakeMarket(), ["CORE"], cfg, emit_logs=False)
    batch_market = FakeMarket()
    batch = du.scan_candidates_batch(batch_market, ["CORE"], cfg, emit_logs=False)

    assert batch.selected == per_symbol.selected == ["BBB", "AAA"]
    assert [c.symbol for c in batch.accepted] == [c.symbol for c in per_symbol.accepted]
    assert batch_market.batch_snapshot_calls == 1
    assert batch_market.batch_bars_calls == 2


def test_scan_candidates_batch_passes_news_ttl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    captured: dict[str, float | None] = {}

    def fake_fetch(*_args, **kwargs):
        captured["max_age_seconds"] = kwargs.get("max_age_seconds")
        return {}

    monkeypatch.setattr(du, "fetch_recent_news_catalysts", fake_fetch)

    class FakeMarket:
        def get_top_movers(self):
            return [{"symbol": "AAA"}]

        def get_snapshots_batch(self, symbols):
            return {s: {"symbol": s, "price": 10.0, "day_gain_pct": 4.0, "volume": 3_000_000, "bid": 9.9, "ask": 10.1} for s in symbols}

        def get_avg_volumes(self, symbols):
            return {s: 2_000_000.0 for s in symbols}

        def get_bars_batch(self, symbols, timeframe: str = "1Min", limit: int = 60):
            return {s: pd.DataFrame({"high": [10.0], "low": [9.9], "open": [9.95], "close": [10.0], "volume": [1000.0]}) for s in symbols}

    cfg = {
        "enabled": True,
        "max_symbols": 1,
        "min_price": 1,
        "max_price": 1000,
        "min_day_gain_pct": 3.0,
        "max_day_gain_pct": 15.0,
        "min_avg_volume": 2_000_000,
        "min_relative_volume": 1.0,
        "max_spread_pct": 1.0,
    }

    du.scan_candidates_batch(FakeMarket(), ["CORE"], cfg, emit_logs=False, news_max_age_seconds=300.0)
    assert captured["max_age_seconds"] == 300.0


def test_dynamic_scan_news_score_boosts_ranking(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")

    class FakeMarket:
        def get_top_movers(self):
            return [{"symbol": "AAA"}, {"symbol": "NEWS"}]

        def get_snapshots_batch(self, symbols):
            return {
                "AAA": {
                    "price": 50.0,
                    "day_gain_pct": 8.0,
                    "volume": 3_000_000,
                    "bid": 49.9,
                    "ask": 50.1,
                },
                "NEWS": {
                    "price": 40.0,
                    "day_gain_pct": 5.0,
                    "volume": 4_000_000,
                    "bid": 39.95,
                    "ask": 40.05,
                },
            }

        def get_avg_volumes(self, symbols):
            return {s: 2_000_000.0 for s in symbols}

        def get_bars_batch(self, symbols, timeframe: str = "1Min", limit: int = 60):
            return {s: pd.DataFrame() for s in symbols}

    monkeypatch.setattr(
        du,
        "fetch_recent_news_catalysts",
        lambda *_a, **_kw: {
            "NEWS": NewsCatalyst("NEWS", 4, "NEWS wins government award")
        },
    )
    caplog.set_level("INFO", logger="src.dynamic_universe")
    out = du.scan_candidates_batch(
        FakeMarket(),
        [],
        {
            "enabled": True,
            "max_symbols": 2,
            "min_price": 10,
            "max_price": 1000,
            "min_day_gain_pct": 3.0,
            "max_day_gain_pct": 15.0,
            "min_avg_volume": 2_000_000,
            "min_relative_volume": 1.0,
            "max_spread_pct": 1.0,
        },
        emit_logs=True,
    )

    assert out.selected[0] == "NEWS"
    assert out.accepted[0].news_score == 4
    assert "NEWS_CATALYST symbol=NEWS score=4 catalyst_type=unknown headline=NEWS wins government award" in caplog.text


def test_dynamic_scan_logs_news_summary_for_candidate(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FakeMarket:
        def get_top_movers(self):
            return [{"symbol": "XYZ"}]

        def get_snapshots_batch(self, symbols):
            return {
                "XYZ": {
                    "price": 40.0,
                    "day_gain_pct": 5.0,
                    "volume": 4_000_000,
                    "bid": 39.95,
                    "ask": 40.05,
                }
            }

        def get_avg_volumes(self, symbols):
            return {s: 2_000_000.0 for s in symbols}

        def get_bars_batch(self, symbols, timeframe: str = "1Min", limit: int = 60):
            return {s: pd.DataFrame() for s in symbols}

    monkeypatch.setattr(
        du,
        "fetch_recent_news_catalysts",
        lambda *_a, **_kw: {
            "XYZ": NewsCatalyst(
                "XYZ",
                7,
                "XYZ wins new contract",
                source="premarket_rank",
                catalyst_type="deal",
                article_count=4,
                sentiment=0.63,
            )
        },
    )
    caplog.set_level("INFO", logger="src.dynamic_universe")

    du.scan_candidates_batch(
        FakeMarket(),
        [],
        {
            "enabled": True,
            "max_symbols": 1,
            "min_price": 10,
            "max_price": 1000,
            "min_day_gain_pct": 3.0,
            "max_day_gain_pct": 15.0,
            "min_avg_volume": 2_000_000,
            "min_relative_volume": 1.0,
            "max_spread_pct": 1.0,
        },
        emit_logs=True,
    )

    assert "DYNAMIC_NEWS symbol=XYZ news_score=7 articles=4" in caplog.text
    assert (
        "DYNAMIC_ACCEPTED symbol=XYZ news_score=7 article_count=4 "
        "sentiment_score=0.63 catalyst_type=deal"
    ) in caplog.text


def test_dynamic_scan_uses_cached_news_metadata_when_fetch_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FakeMarket:
        def get_top_movers(self):
            return [{"symbol": "AMC"}]

        def get_snapshots_batch(self, symbols):
            return {
                "AMC": {
                    "price": 18.0,
                    "day_gain_pct": 6.0,
                    "volume": 3_000_000,
                    "bid": 17.95,
                    "ask": 18.05,
                }
            }

        def get_avg_volumes(self, symbols):
            return {s: 1_000_000.0 for s in symbols}

        def get_bars_batch(self, symbols, timeframe: str = "1Min", limit: int = 60):
            return {s: pd.DataFrame() for s in symbols}

    now = datetime.now(timezone.utc)
    nc._NEWS_CACHE.clear()
    nc._NEWS_CACHE["AMC"] = nc._NewsCacheEntry(
        score=5,
        headline="AMC sees upgraded outlook",
        fetched_at=now,
        catalyst=NewsCatalyst(
            "AMC",
            5,
            "AMC sees upgraded outlook",
            source="premarket_rank",
            catalyst_type="upgrade",
            article_count=2,
            sentiment=0.50,
        ),
        article_count=2,
        sentiment=0.50,
    )
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})
    caplog.set_level("INFO", logger="src.dynamic_universe")
    caplog.set_level("INFO", logger="src.news_catalyst")

    out = du.scan_candidates_batch(
        FakeMarket(),
        [],
        {
            "enabled": True,
            "max_symbols": 1,
            "min_price": 10,
            "max_price": 1000,
            "min_day_gain_pct": 3.0,
            "max_day_gain_pct": 15.0,
            "min_avg_volume": 500_000,
            "min_relative_volume": 1.0,
            "max_spread_pct": 1.0,
        },
        emit_logs=True,
    )

    assert out.accepted[0].news_score == 5
    assert "NEWS_LOOKUP symbol=AMC matched_articles=2 cache_hit=true sentiment_score=0.50" in caplog.text
    assert "DYNAMIC_ACCEPTED symbol=AMC news_score=5 article_count=2 sentiment_score=0.50 catalyst_type=upgrade" in caplog.text
    nc._NEWS_CACHE.clear()


def test_dynamic_scan_uses_premarket_artifact_catalyst_score(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    artifact = {
        "OKTA": {
            "symbol": "OKTA",
            "news_score": 7,
            "event_score": 6.5,
            "catalyst_score": 0.7,
            "headline": "Okta wins cloud security deal",
            "source": "alpaca",
            "catalyst_type": "deal",
            "article_count": 3,
            "sentiment": 0.74,
            "age_minutes": 12.0,
        }
    }
    out = _scan_one(
        monkeypatch,
        tmp_path,
        "OKTA",
        price=85.0,
        avg_volume=1_000_000,
        relative_volume=1.2,
        premarket_artifacts=artifact,
    )

    assert out.selected == ["OKTA"]
    assert out.accepted[0].news_score == 7
    assert out.accepted[0].catalyst_score >= 0.7
    assert out.accepted[0].catalyst_type == "deal"


def test_dynamic_scan_attaches_fresh_artifact_metadata_and_lookup_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    artifact = {
        "AMZN": {
            "symbol": "AMZN",
            "news_score": 8,
            "event_score": 7.5,
            "catalyst_score": 0.84,
            "headline": "Amazon raises guidance",
            "source": "catalysts",
            "catalyst_type": "earnings",
            "article_count": 4,
            "sentiment": 0.74,
            "age_minutes": 9.0,
        }
    }
    with caplog.at_level(logging.INFO, logger="src.dynamic_universe"):
        out = _scan_one(
            monkeypatch,
            tmp_path,
            "AMZN",
            price=85.0,
            avg_volume=10_000_000,
            relative_volume=1.4,
            premarket_artifacts=artifact,
            emit_logs=True,
        )

    row = out.accepted[0]
    assert row.symbol == "AMZN"
    assert row.news_score == 8
    assert row.event_score == pytest.approx(7.5)
    assert row.catalyst_score == pytest.approx(0.84)
    assert row.article_count == 4
    assert row.premarket_injected is True
    assert row.catalyst_headline == "Amazon raises guidance"
    assert "CATALYST_LOOKUP symbol=AMZN found=true lookup_key=AMZN source=catalysts" in caplog.text
    assert "article_count=4 headline=Amazon raises guidance" in caplog.text


def test_dynamic_scan_fastlane_does_not_activate_without_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    artifact = {"AMZN": {"symbol": "AMZN"}}
    with caplog.at_level(logging.INFO, logger="src.dynamic_universe"):
        out = _scan_one(
            monkeypatch,
            tmp_path,
            "AMZN",
            price=85.0,
            avg_volume=10_000_000,
            relative_volume=0.8,
            day_gain_pct=12.0,
            cfg=_scanner_cfg(min_relative_volume=1.25, min_rel_volume=1.25),
            premarket_artifacts=artifact,
            emit_logs=True,
        )

    assert out.selected == []
    assert out.rejected[0].rejection_reason == "below_min_relative_volume"
    assert out.rejected[0].catalyst_fastlane_active is False
    assert out.rejected[0].premarket_injected is False
    assert "CATALYST_LOOKUP symbol=AMZN found=false reason=below_threshold lookup_key=AMZN" in caplog.text
    assert "CATALYST_RVOL_RELAXED symbol=AMZN" not in caplog.text


def test_dynamic_scan_logs_artifact_coverage_for_non_core_mover(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.INFO, logger="src.dynamic_universe")
    artifact = {
        "DXST": {
            "symbol": "DXST",
            "news_score": 7,
            "event_score": 6.5,
            "catalyst_score": 0.7,
            "headline": "DXST wins cloud deal",
            "source": "alpaca",
            "catalyst_type": "deal",
            "article_count": 2,
            "sentiment": 0.72,
            "age_minutes": 7.0,
        }
    }

    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})
    out = du.scan_candidates_batch(
        _OneMoverMarket(
            "DXST",
            price=18.0,
            avg_volume=1_000_000,
            relative_volume=1.5,
        ),
        [],
        _scanner_cfg(),
        emit_logs=True,
        premarket_artifacts=artifact,
    )

    assert out.accepted[0].news_score == 7
    assert "DYNAMIC_CATALYST_COVERAGE symbol=DXST has_artifact=true" in caplog.text
    assert "CATALYST_MATCH_DEBUG symbol=DXST source=alpaca headline=DXST wins cloud deal" in caplog.text
    assert (
        "DYNAMIC_SCANNER_SCORE_TRACE symbol=DXST artifact_match=true ranking_score=0.00 "
        "artifact_news_score=7.00 artifact_event_score=6.50 artifact_catalyst_score=0.70"
    ) in caplog.text
    assert (
        "DYNAMIC_SCORE_SOURCE symbol=DXST news_score=7 catalyst_score=0.70 "
        "event_score=6.50 from_artifact=true from_live_feed=false"
    ) in caplog.text
    assert (
        "DYNAMIC_OVERRIDE_DECISION symbol=DXST reason=fresh_score_match "
        "override_active=True required_score=7.00 actual_score=7.00"
    ) in caplog.text


def test_dynamic_scan_preserves_catalyst_score_only_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    artifact = {
        "ABAT": {
            "symbol": "ABAT",
            "news_score": 4,
            "event_score": 0.0,
            "catalyst_score": 0.4,
            "headline": "ABAT files material update",
            "source": "sec",
            "catalyst_type": "sec_filing",
            "age_minutes": 5.0,
        }
    }
    with caplog.at_level(logging.INFO, logger="src.dynamic_universe"):
        out = _scan_one(
            monkeypatch,
            tmp_path,
            "ABAT",
            price=12.0,
            avg_volume=1_000_000,
            relative_volume=1.5,
            day_gain_pct=12.0,
            premarket_artifacts=artifact,
            emit_logs=True,
        )

    assert out.selected == ["ABAT"]
    assert out.accepted[0].news_score == 4
    assert out.accepted[0].catalyst_score == pytest.approx(0.4)
    assert out.accepted[0].catalyst_age_minutes == pytest.approx(5.0)
    assert (
        "DYNAMIC_SCORE_SOURCE symbol=ABAT news_score=4 catalyst_score=0.40 "
        "event_score=0.00 from_artifact=true from_live_feed=false"
    ) in caplog.text
    assert (
        "DYNAMIC_OVERRIDE_DECISION symbol=ABAT reason=below_required_score "
        "override_active=False required_score=7.00 actual_score=4.00"
    ) in caplog.text


def test_dynamic_scan_catalyst_relaxes_relative_volume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    artifact = {
        "PD": {
            "symbol": "PD",
            "news_score": 6,
            "event_score": 6.0,
            "catalyst_score": 0.7,
            "headline": "PagerDuty announces AI operations deal",
            "source": "alpaca",
            "catalyst_type": "ai",
            "article_count": 2,
            "sentiment": 0.70,
            "age_minutes": 8.0,
        }
    }
    with caplog.at_level(logging.INFO, logger="src.dynamic_universe"):
        out = _scan_one(
            monkeypatch,
            tmp_path,
            "PD",
            price=23.0,
            avg_volume=1_000_000,
            relative_volume=0.76,
            cfg=_scanner_cfg(min_relative_volume=1.25, min_rel_volume=1.25),
            premarket_artifacts=artifact,
            emit_logs=True,
        )

    assert out.selected == ["PD"]
    assert out.accepted[0].relative_volume == pytest.approx(0.76)
    assert out.accepted[0].news_score == 6
    assert "DYNAMIC_CATALYST_RELAXED_GATE symbol=PD gate=relative_volume old=1.250 new=0.750" in caplog.text


def test_dynamic_scan_non_catalyst_uses_strict_relative_volume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    out = _scan_one(
        monkeypatch,
        tmp_path,
        "NOCAT",
        price=23.0,
        avg_volume=1_000_000,
        relative_volume=0.76,
        cfg=_scanner_cfg(min_relative_volume=1.25, min_rel_volume=1.25),
    )

    assert out.selected == []
    assert out.rejected[0].rejection_reason == "below_min_relative_volume"


def test_dynamic_scan_catalyst_relaxes_gain_filter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    artifact = {
        "GAIN": {
            "symbol": "GAIN",
            "news_score": 5,
            "event_score": 4.0,
            "catalyst_score": 0.7,
            "headline": "GAIN announces major contract",
            "source": "alpaca",
            "catalyst_type": "contract",
            "article_count": 2,
            "sentiment": 0.70,
            "age_minutes": 8.0,
        }
    }
    with caplog.at_level(logging.INFO, logger="src.dynamic_universe"):
        out = _scan_one(
            monkeypatch,
            tmp_path,
            "GAIN",
            price=23.0,
            avg_volume=1_000_000,
            relative_volume=1.5,
            day_gain_pct=100.0,
            cfg=_scanner_cfg(max_day_gain_pct=80.0),
            premarket_artifacts=artifact,
            emit_logs=True,
        )

    assert out.selected == ["GAIN"]
    assert "DYNAMIC_CATALYST_RELAXED_GATE symbol=GAIN gate=gain_filter reason=catalyst_backed" in caplog.text
    assert (
        "DYNAMIC_GAIN_FILTER_LIMITS symbol=GAIN normal_max_gain_pct=80.00 "
        "catalyst_override_max_gain_pct=250.00"
    ) in caplog.text
    assert "DYNAMIC_GATE_DEBUG symbol=GAIN" in caplog.text
    assert "gain_ok=True" in caplog.text
    assert "min_gain_ok=True" in caplog.text
    assert "max_gain_ok=True" in caplog.text
    assert "breakout_ok=False" in caplog.text
    assert "catalyst_ok=True" in caplog.text


def test_dynamic_scan_sune_artifact_bypasses_extended_gain_filter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    artifact = {
        "SUNE": {
            "symbol": "SUNE",
            "news_score": 7,
            "event_score": 6.0,
            "catalyst_score": 0.8,
            "headline": "SUNE announces strategic financing",
            "source": "alpaca",
            "catalyst_type": "financing",
            "age_minutes": 10.0,
        }
    }

    out = _scan_one(
        monkeypatch,
        tmp_path,
        "SUNE",
        price=23.0,
        avg_volume=1_000_000,
        relative_volume=239.67,
        day_gain_pct=149.0,
        cfg=_scanner_cfg(max_day_gain_pct=80.0),
        premarket_artifacts=artifact,
    )

    assert out.selected == ["SUNE"]
    assert out.accepted[0].news_score == 7
    assert out.accepted[0].event_score == pytest.approx(6.0)
    assert out.accepted[0].catalyst_score == pytest.approx(0.8)
    assert out.accepted[0].catalyst_age_minutes == pytest.approx(10.0)


def test_dynamic_scan_non_catalyst_uses_strict_gain_filter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="src.dynamic_universe"):
        out = _scan_one(
            monkeypatch,
            tmp_path,
            "GAIN",
            price=23.0,
            avg_volume=1_000_000,
            relative_volume=1.5,
            day_gain_pct=100.0,
            cfg=_scanner_cfg(max_day_gain_pct=80.0),
            emit_logs=True,
        )

    assert out.selected == []
    assert out.rejected[0].rejection_reason == "gain filter"
    assert out.rejected[0].catalyst_score == pytest.approx(0.0)
    assert "DYNAMIC_GATE_DEBUG symbol=GAIN" in caplog.text
    assert "gain_ok=False" in caplog.text
    assert "min_gain_ok=True" in caplog.text
    assert "max_gain_ok=False" in caplog.text
    assert "breakout_ok=False" in caplog.text
    assert "catalyst_ok=False" in caplog.text


def test_dynamic_scan_low_gain_uses_below_min_gain_reason(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="src.dynamic_universe"):
        out = _scan_one(
            monkeypatch,
            tmp_path,
            "LOWG",
            price=23.0,
            avg_volume=1_000_000,
            relative_volume=1.5,
            day_gain_pct=4.4,
            cfg=_scanner_cfg(max_day_gain_pct=80.0),
            emit_logs=True,
        )

    assert out.selected == []
    assert out.rejected[0].rejection_reason == "below_min_day_gain"
    assert out.rejected[0].day_gain_pct == pytest.approx(4.4)
    assert "DYNAMIC_GATE_DEBUG symbol=LOWG" in caplog.text
    assert "price_ok=True" in caplog.text
    assert "spread_ok=True" in caplog.text
    assert "avg_volume_ok=True" in caplog.text
    assert "rel_volume_ok=True" in caplog.text
    assert "gain_ok=False" in caplog.text
    assert "min_gain_ok=False" in caplog.text
    assert "max_gain_ok=True" in caplog.text
    assert "breakout_ok=False" in caplog.text
    assert "catalyst_ok=False" in caplog.text
    assert "entry_alignment_ok=True" in caplog.text


def test_dynamic_scan_negative_gain_uses_below_min_gain_reason(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="src.dynamic_universe"):
        out = _scan_one(
            monkeypatch,
            tmp_path,
            "NEGG",
            price=23.0,
            avg_volume=1_000_000,
            relative_volume=1.5,
            day_gain_pct=-2.8,
            cfg=_scanner_cfg(max_day_gain_pct=80.0),
            emit_logs=True,
        )

    assert out.selected == []
    assert out.rejected[0].rejection_reason == "below_min_day_gain"
    assert out.rejected[0].day_gain_pct == pytest.approx(-2.8)
    assert "DYNAMIC_GATE_DEBUG symbol=NEGG" in caplog.text
    assert "gain_ok=False" in caplog.text
    assert "min_gain_ok=False" in caplog.text
    assert "max_gain_ok=True" in caplog.text
    assert "breakout_ok=False" in caplog.text
    assert "catalyst_ok=False" in caplog.text


def test_dynamic_scan_catalyst_gain_override_config_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    artifact = {
        "RUNR": {
            "symbol": "RUNR",
            "news_score": 5,
            "event_score": 4.0,
            "catalyst_score": 0.7,
            "headline": "RUNR announces major contract",
            "source": "alpaca",
            "catalyst_type": "contract",
            "age_minutes": 8.0,
        }
    }
    cfg = _scanner_cfg(max_day_gain_pct=80.0)
    cfg["catalyst_boost"]["max_gain_pct_catalyst"] = 300
    cfg["catalyst_boost"].pop("max_gain_pct_with_catalyst", None)

    with caplog.at_level(logging.INFO, logger="src.dynamic_universe"):
        out = _scan_one(
            monkeypatch,
            tmp_path,
            "RUNR",
            price=23.0,
            avg_volume=1_000_000,
            relative_volume=1.5,
            day_gain_pct=275.0,
            cfg=cfg,
            premarket_artifacts=artifact,
            emit_logs=True,
        )

    assert out.selected == ["RUNR"]
    assert (
        "DYNAMIC_GAIN_FILTER_LIMITS symbol=RUNR normal_max_gain_pct=80.00 "
        "catalyst_override_max_gain_pct=300.00"
    ) in caplog.text


def test_dynamic_scan_strong_catalyst_override_allows_gain_and_spread(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    catalyst = {
        "BJDX": {
            "symbol": "BJDX",
            "news_score": 7,
            "event_score": 6.5,
            "catalyst_score": 0.7,
            "headline": "BJDX wins AI contract",
            "source": "alpaca",
            "catalyst_type": "deal",
            "article_count": 3,
            "sentiment": 0.74,
            "age_minutes": 6.0,
        }
    }

    class _Market(_OneMoverMarket):
        def get_bars_batch(self, symbols, timeframe: str = "1Min", limit: int = 60):
            return {self.symbol: _vwap_reclaim_bars() for _s in symbols}

    out = du.scan_candidates_batch(
        _Market(
            "BJDX",
            price=12.0,
            avg_volume=1_000_000,
            relative_volume=1.4,
            day_gain_pct=120.0,
            bid=11.79,
            ask=12.21,
        ),
        [],
        _scanner_cfg(require_above_vwap=False),
        emit_logs=False,
        premarket_artifacts=catalyst,
    )

    assert out.selected == ["BJDX"]
    assert out.accepted[0].catalyst_type == "deal"
    assert out.accepted[0].catalyst_score > 0


def test_dynamic_scan_catalyst_does_not_bypass_unstable_quote_spread_or_min_price(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    catalyst = {
        "SNOW": {
            "symbol": "SNOW",
            "news_score": 7,
            "event_score": 6.5,
            "catalyst_score": 0.7,
            "headline": "Snowflake wins AI deal",
            "source": "alpaca",
            "catalyst_type": "deal",
            "article_count": 2,
            "sentiment": 0.72,
            "age_minutes": 4.0,
        }
    }

    low_price = _scan_one(
        monkeypatch,
        tmp_path,
        "SNOW",
        price=1.75,
        avg_volume=1_000_000,
        relative_volume=2.0,
        premarket_artifacts=catalyst,
    )
    assert low_price.selected == []
    assert low_price.rejected[0].rejection_reason == "below_min_price"

    wide_spread = _scan_one(
        monkeypatch,
        tmp_path,
        "SNOW",
        price=20.0,
        avg_volume=1_000_000,
        relative_volume=2.0,
        bid=18.0,
        ask=22.0,
        premarket_artifacts=catalyst,
    )
    assert wide_spread.selected == []
    assert wide_spread.rejected[0].rejection_reason in {"unstable quote", "spread too wide"}

    too_wide_spread = _scan_one(
        monkeypatch,
        tmp_path,
        "SNOW",
        price=20.0,
        avg_volume=1_000_000,
        relative_volume=2.0,
        bid=19.4,
        ask=20.6,
        premarket_artifacts=catalyst,
    )
    assert too_wide_spread.selected == []
    assert too_wide_spread.rejected[0].rejection_reason == "spread too wide"


def test_dynamic_scan_news_early_entry_accepts_before_gain_filter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    bars = pd.DataFrame(
        {
            "high": [10.0, 10.1, 10.2, 10.3],
            "low": [9.8, 9.9, 10.0, 10.1],
            "close": [9.95, 10.05, 10.15, 10.25],
            "volume": [100_000, 100_000, 100_000, 100_000],
        }
    )

    class FakeMarket:
        def get_top_movers(self):
            return [{"symbol": "CAT"}]

        def get_snapshots_batch(self, symbols):
            return {
                "CAT": {
                    "price": 10.5,
                    "day_gain_pct": 0.5,
                    "volume": 2_000_000,
                    "bid": 10.45,
                    "ask": 10.55,
                }
            }

        def get_avg_volumes(self, symbols):
            return {"CAT": 1_000_000.0}

        def get_bars_batch(self, symbols, timeframe: str = "1Min", limit: int = 60):
            return {s: bars for s in symbols}

    monkeypatch.setattr(
        du,
        "fetch_recent_news_catalysts",
        lambda *_a, **_kw: {"CAT": NewsCatalyst("CAT", 3, "CAT wins contract")},
    )

    out = du.scan_candidates_batch(
        FakeMarket(),
        [],
        {
            "enabled": True,
            "max_symbols": 1,
            "min_price": 1,
            "max_price": 1000,
            "min_day_gain_pct": 3.0,
            "max_day_gain_pct": 15.0,
            "min_avg_volume": 5_000_000,
            "min_relative_volume": 5.0,
            "max_spread_pct": 0.25,
            "require_5m_trend_alignment": True,
        },
        emit_logs=False,
    )

    assert out.selected == ["CAT"]
    assert out.accepted[0].day_gain_pct == pytest.approx(0.5)
    assert out.accepted[0].spread_pct <= 1.5


def test_scan_dynamic_candidates_quality_filters() -> None:
    mc = MagicMock()
    mc.get_top_movers.return_value = [{"symbol": "AAA"}, {"symbol": "LOW"}]
    mc.get_snapshot.side_effect = lambda sym: {
        "AAA": {
            "price": 105.0,
            "day_gain_pct": 8.0,
            "volume": 4_000_000,
            "bid": 104.9,
            "ask": 105.1,
        },
        "LOW": {
            "price": 98.0,
            "day_gain_pct": 9.0,
            "volume": 4_000_000,
            "bid": 97.9,
            "ask": 98.1,
        },
    }[sym]
    mc.get_avg_volume.return_value = 2_000_000.0

    bars_1m_good = pd.DataFrame(
        {
            "high": [100 + i * 0.2 for i in range(30)],
            "low": [99 + i * 0.15 for i in range(30)],
            "close": [100 + i * 0.18 for i in range(30)],
            "volume": [100_000] * 30,
        }
    )
    bars_1m_bad = pd.DataFrame(
        {
            "high": [100.1] * 30,
            "low": [99.9] * 30,
            "close": [100.0] * 30,
            "volume": [100_000] * 30,
        }
    )
    bars_5m_good = pd.DataFrame({"close": [100.0, 101.0, 102.0, 103.0]})
    bars_5m_bad = pd.DataFrame({"close": [100.0, 99.0, 98.0, 97.0]})

    def _bars(sym: str, timeframe: str = "1Min", limit: int = 60):
        if sym == "AAA":
            return bars_5m_good if timeframe == "5Min" else bars_1m_good
        return bars_5m_bad if timeframe == "5Min" else bars_1m_bad

    mc.get_bars.side_effect = _bars

    out = du.scan_dynamic_candidates(
        mc,
        [],
        cfg={
            "enabled": True,
            "max_symbols": 3,
            "min_price": 10,
            "max_price": 1000,
            "min_day_gain_pct": 3.0,
            "max_day_gain_pct": 15.0,
            "min_avg_volume": 1_000_000,
            "min_rel_volume": 1.5,
            "min_intraday_range_pct": 1.0,
            "require_above_vwap": True,
            "require_5m_trend_alignment": True,
            "max_spread_pct": 1.0,
        },
    )
    assert out == ["AAA"]


def test_scan_dynamic_candidates_rejects_crossed_quote_by_absolute_mid_spread() -> None:
    mc = MagicMock()
    mc.get_top_movers.return_value = [{"symbol": "CROSSED"}]
    mc.get_snapshot.return_value = {
        "price": 100.0,
        "day_gain_pct": 8.0,
        "volume": 4_000_000,
        "bid": 101.0,
        "ask": 99.0,
    }
    mc.get_avg_volume.return_value = 2_000_000.0
    mc.get_bars.return_value = pd.DataFrame()

    out = du.scan_dynamic_candidates(
        mc,
        [],
        cfg={
            "enabled": True,
            "max_symbols": 3,
            "min_price": 10,
            "max_price": 1000,
            "min_day_gain_pct": 3.0,
            "max_day_gain_pct": 15.0,
            "min_avg_volume": 1_000_000,
            "min_rel_volume": 1.5,
            "max_spread_pct": 1.0,
        },
    )
    assert out == []


def test_scan_dynamic_candidates_rejects_unstable_quote_even_with_loose_cap(caplog: pytest.LogCaptureFixture) -> None:
    mc = MagicMock()
    mc.get_top_movers.return_value = [{"symbol": "WIDE"}]
    mc.get_snapshot.return_value = {
        "price": 100.0,
        "day_gain_pct": 8.0,
        "volume": 4_000_000,
        "bid": 90.0,
        "ask": 110.0,
    }
    mc.get_avg_volume.return_value = 2_000_000.0
    mc.get_bars.return_value = pd.DataFrame()

    with caplog.at_level("WARNING"):
        out = du.scan_dynamic_candidates(
            mc,
            [],
            cfg={
                "enabled": True,
                "max_symbols": 3,
                "min_price": 10,
                "max_price": 1000,
                "min_day_gain_pct": 3.0,
                "max_day_gain_pct": 15.0,
                "min_avg_volume": 1_000_000,
                "min_rel_volume": 1.5,
                "max_spread_pct": 50.0,
            },
        )

    assert out == []
    assert "Unstable quote WIDE" in caplog.text


def test_is_dynamic_symbol() -> None:
    core = ["SPY", "qqq"]
    assert not du.is_dynamic_symbol("SPY", core)
    assert not du.is_dynamic_symbol("qqq", core)
    assert du.is_dynamic_symbol("XYZ", core)


def test_classify_symbol_precedence() -> None:
    core = ["AAPL", "MSFT"]
    dynamic = ["MSFT", "XYZ"]
    allocator = ["TSLA"]

    assert (
        du.classify_symbol(
            "MSFT",
            core,
            allocator_holdings=allocator,
            dynamic_symbols=dynamic,
        )
        == "CORE_WITH_DYNAMIC_SIGNAL"
    )
    assert (
        du.classify_symbol(
            "TSLA",
            core,
            allocator_holdings=allocator,
            dynamic_symbols=dynamic,
        )
        == "ALLOCATOR_HOLDING"
    )
    assert (
        du.classify_symbol(
            "XYZ",
            core,
            allocator_holdings=allocator,
            dynamic_symbols=dynamic,
        )
        == "DYNAMIC_ONLY"
    )
    assert (
        du.classify_symbol(
            "QQQ",
            core,
            allocator_holdings=allocator,
            dynamic_symbols=dynamic,
        )
        == "OTHER"
    )


def test_merge_dynamic_momentum_override_scan_cfg() -> None:
    du_cfg = {"min_day_gain_pct": 4.0, "min_rel_volume": 1.5}
    full = {
        "dynamic_momentum_override": {
            "enabled": True,
            "min_day_gain_pct": 20,
            "min_relative_volume": 1.8,
            "require_above_vwap": False,
        }
    }
    out = du.merge_dynamic_momentum_override_scan_cfg(du_cfg, full)
    assert out["min_day_gain_pct"] == pytest.approx(20.0)
    assert out["min_rel_volume"] == pytest.approx(1.8)
    assert out["min_relative_volume"] == pytest.approx(1.8)
    assert out["require_above_vwap"] is False
    assert du.merge_dynamic_momentum_override_scan_cfg(du_cfg, {}) == du_cfg
    assert du.merge_dynamic_momentum_override_scan_cfg(
        du_cfg, {"dynamic_momentum_override": {"enabled": False, "min_day_gain_pct": 99}}
    ) == du_cfg


def test_dynamic_regime_strength_threshold_multiplier() -> None:
    assert du.dynamic_regime_strength_threshold_multiplier({}) == 1.0
    assert du.dynamic_regime_strength_threshold_multiplier(
        {"dynamic_universe": {}}
    ) == 1.0
    assert du.dynamic_regime_strength_threshold_multiplier(
        {
            "dynamic_universe": {
                "regime_relax": {"enabled": True, "strength_threshold_mult": 0.85}
            }
        }
    ) == pytest.approx(0.85)
    assert du.dynamic_regime_strength_threshold_multiplier(
        {
            "dynamic_universe": {
                "regime_relax": {"enabled": False, "strength_threshold_mult": 0.5}
            }
        }
    ) == 1.0


def _synth_1m_bars(n: int, *, uptrend: bool) -> pd.DataFrame:
    """Minimal OHLCV where close rises if uptrend."""
    rows = []
    base = 100.0
    for i in range(n):
        c = base + (0.05 * i if uptrend else -0.05 * i)
        rows.append(
            {
                "high": c + 0.1,
                "low": c - 0.1,
                "close": c,
                "volume": 10_000.0,
            }
        )
    return pd.DataFrame(rows)


def test_compute_dynamic_entry_signals_uptrend_structure() -> None:
    df = _synth_1m_bars(40, uptrend=True)
    last = float(df["close"].iloc[-1])
    sig = du.compute_dynamic_entry_signals(df, last)
    assert sig.price_above_vwap
    assert sig.ema_5_above_20
    # Monotonic uptrend can push RSI > 75 even when structure looks fine — guard is checked separately.


def test_entry_target_dollars_core_vs_dynamic() -> None:
    cfg = {"dynamic_universe": {"max_symbol_exposure_pct": 3}}
    core = ["SPY"]
    assert du.entry_target_dollars_for_symbol(
        9000.0,
        symbol="SPY",
        core_symbols=core,
        account_equity=100_000.0,
        config=cfg,
    ) == pytest.approx(9000.0)
    assert du.entry_target_dollars_for_symbol(
        9000.0,
        symbol="XYZ",
        core_symbols=core,
        account_equity=100_000.0,
        config=cfg,
    ) == pytest.approx(3000.0)


def test_dynamic_entry_target_uses_fastlane_symbol_cap() -> None:
    cfg = {"dynamic_universe": {"max_symbol_exposure_pct": 12}}

    assert du.entry_target_dollars_for_symbol(
        9000.0,
        symbol="XOS",
        core_symbols=["SPY", "QQQ"],
        account_equity=28_800.0,
        config=cfg,
    ) == pytest.approx(3456.0)


def test_manage_dynamic_exit_tp1_and_skips_when_no_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn_exit.json")
    sells: list[tuple[str, int, str]] = []

    def _submit(sym: str, q: int, tag: str) -> bool:
        sells.append((sym, q, tag))
        return True

    cfg = {"dynamic_exits": {}, "dynamic_universe": {}}
    assert not du.manage_dynamic_exit("ZZZ", {"qty": 5}, 100.0, 99.0, cfg, _submit)
    assert sells == []

    du.remember_entry("XYZ", 100.0, du.load_state())
    assert du.manage_dynamic_exit(
        "XYZ",
        {"qty": 10},
        103.0,
        99.0,
        cfg,
        _submit,
    )
    assert sells[-1][0] == "XYZ" and sells[-1][1] == 5 and sells[-1][2] == "dynamic_tp1"
    assert du.load_state()["active"]["XYZ"]["tp1_done"] is True


def test_dynamic_vwap_extension_and_spread_override_helpers() -> None:
    assert du.dynamic_entry_vwap_extension_pct(108.0, 100.0) == pytest.approx(8.0)
    assert du.dynamic_entry_vwap_extension_pct(100.0, None) is None
    assert du.dynamic_entry_spread_override_cap(gain_pct=16.0, relative_volume=1.3) == pytest.approx(3.5)
    assert du.dynamic_entry_spread_override_cap(gain_pct=14.9, relative_volume=2.0) is None


def test_dynamic_reentry_cooldown_remaining_minutes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn_cd.json")
    state = du.load_state()
    du.mark_cooldown("APPS", 60, state)
    active, rem = du.dynamic_reentry_cooldown_active("APPS", state=du.load_state())
    assert active is True
    assert rem is not None and rem > 0


def test_dynamic_reentry_cooldown_blocks_scan_buy_but_not_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn_cd2.json")
    now = 2_000_000_000
    monkeypatch.setattr(du, "_now", lambda: now)
    state = {
        "cooldowns": {"APPS": now + 3600},
        "active": {
            "APPS": {
                "entry_price": 100.0,
                "entry_time": "2026-05-28T09:30:00-04:00",
                "high_price": 100.0,
                "tp1_done": False,
            }
        },
    }
    du.save_state(state)

    class FakeMarket:
        def get_top_movers(self):
            return [{"symbol": "APPS"}]

        def get_snapshot(self, symbol: str):
            return {
                "symbol": symbol,
                "price": 103.0,
                "day_gain_pct": 12.0,
                "volume": 4_000_000,
                "bid": 102.9,
                "ask": 103.1,
            }

        def get_avg_volume(self, symbol: str) -> float:
            return 1_000_000.0

        def get_bars(self, symbol: str, timeframe: str = "1Min", limit: int = 60):
            return pd.DataFrame()

    caplog.set_level(logging.INFO)
    batch = du.scan_candidates_batch(
        FakeMarket(),
        [],
        {
            "enabled": True,
            "max_symbols": 3,
            "min_price": 3,
            "max_price": 500,
            "min_day_gain_pct": 8.0,
            "max_day_gain_pct": 100.0,
            "min_avg_volume": 75_000,
            "min_relative_volume": 1.0,
            "max_spread_pct": 1.0,
        },
        emit_logs=False,
    )
    assert "APPS" not in batch.selected

    caplog.clear()
    du.scan_candidates_batch(
        FakeMarket(),
        [],
        {
            "enabled": True,
            "max_symbols": 3,
            "min_price": 3,
            "max_price": 500,
            "min_day_gain_pct": 8.0,
            "max_day_gain_pct": 100.0,
            "min_avg_volume": 75_000,
            "min_relative_volume": 1.0,
            "max_spread_pct": 1.0,
        },
        emit_logs=True,
    )
    assert "DYNAMIC_REENTRY_BLOCK symbol=APPS minutes_remaining=" in caplog.text

    sells: list[tuple[str, int, str]] = []

    def _submit(sym: str, q: int, tag: str) -> bool:
        sells.append((sym, q, tag))
        return True

    cfg = {"dynamic_exits": {}, "dynamic_universe": {}}
    assert du.manage_dynamic_exit("APPS", {"qty": 10}, 103.0, 100.0, cfg, _submit)
    assert sells and sells[-1][2] == "dynamic_tp1"


def test_manage_dynamic_exit_atr_stop_and_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn_atr.json")
    du.remember_entry("APPS", 100.0, du.load_state())
    sells: list[tuple[str, int, str]] = []

    def _submit(sym: str, q: int, tag: str) -> bool:
        sells.append((sym, q, tag))
        return True

    cfg = {
        "dynamic_exits": {"take_profit_1_pct": 2.0},
        "dynamic_universe": {
            "atr_exit": {
                "enabled": True,
                "stop_atr_mult": 1.5,
                "target_atr_mult": 3.0,
                "trail_after_atr_mult": 2.0,
                "trail_atr_mult": 1.0,
            },
            "reentry_cooldown_minutes": 60,
        },
        "news_ai": {"enabled": False},
    }

    assert du.manage_dynamic_exit("APPS", {"qty": 10}, 96.5, 100.0, cfg, _submit, atr=2.0)
    assert sells[-1][2] == "dynamic_atr_stop"

    du.remember_entry("APPS", 100.0, du.load_state())
    sells.clear()
    assert du.manage_dynamic_exit("APPS", {"qty": 10}, 106.5, 100.0, cfg, _submit, atr=2.0)
    assert sells[-1][2] == "dynamic_atr_target"


def test_manage_dynamic_exit_strong_news_hold_timer_blocks_non_emergency_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn_hold.json")
    start = 2_000_000_000
    monkeypatch.setattr(du, "_now", lambda: start)
    du.remember_entry("NVTS", 100.0, du.load_state())
    monkeypatch.setattr(du, "_now", lambda: start + 20 * 60)
    monkeypatch.setattr(du, "get_news_score", lambda *args, **kwargs: (8, "strong_news"))
    sells: list[tuple[str, int, str]] = []

    def _submit(sym: str, q: int, tag: str) -> bool:
        sells.append((sym, q, tag))
        return True

    caplog.set_level(logging.INFO)
    cfg = {
        "dynamic_exits": {
            "take_profit_1_pct": 2.0,
            "strong_news_hold_minutes": 30,
        },
        "dynamic_universe": {},
    }

    assert not du.manage_dynamic_exit("NVTS", {"qty": 10}, 103.0, 99.0, cfg, _submit)
    assert sells == []
    assert "DYNAMIC_HOLD_TIMER symbol=NVTS" in caplog.text
    assert "DYNAMIC_EXIT_REASON symbol=NVTS reason=strong_news_hold_timer" in caplog.text


def test_manage_dynamic_exit_strong_news_switches_to_4pct_trailing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn_trail.json")
    start = 2_000_000_000
    monkeypatch.setattr(du, "_now", lambda: start)
    du.remember_entry("XOS", 100.0, du.load_state())
    state = du.load_state()
    state["active"]["XOS"]["high_price"] = 115.0
    du.save_state(state)
    monkeypatch.setattr(du, "_now", lambda: start + 40 * 60)
    monkeypatch.setattr(du, "get_news_score", lambda *args, **kwargs: (8, "strong_news"))
    sells: list[tuple[str, int, str]] = []

    def _submit(sym: str, q: int, tag: str) -> bool:
        sells.append((sym, q, tag))
        return True

    caplog.set_level(logging.INFO)
    cfg = {
        "dynamic_exits": {
            "take_profit_1_pct": 2.0,
            "strong_news_trailing_trigger_pct": 8.0,
            "strong_news_trailing_stop_pct": 4.0,
        },
        "dynamic_universe": {},
    }

    assert du.manage_dynamic_exit("XOS", {"qty": 10}, 109.0, 100.0, cfg, _submit)
    assert sells[-1][2] == "dynamic_trailing_stop"
    assert "DYNAMIC_TRAILING_STOP symbol=XOS" in caplog.text
    assert "DYNAMIC_EXIT_REASON symbol=XOS reason=strong_news_trailing_stop" in caplog.text


def test_manage_dynamic_exit_strong_news_trailing_holds_before_4pct_drawdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn_trail_hold.json")
    start = 2_000_000_000
    monkeypatch.setattr(du, "_now", lambda: start)
    du.remember_entry("XOS", 100.0, du.load_state())
    state = du.load_state()
    state["active"]["XOS"]["high_price"] = 112.0
    du.save_state(state)
    monkeypatch.setattr(du, "_now", lambda: start + 40 * 60)
    monkeypatch.setattr(du, "get_news_score", lambda *args, **kwargs: (8, "strong_news"))
    sells: list[tuple[str, int, str]] = []

    def _submit(sym: str, q: int, tag: str) -> bool:
        sells.append((sym, q, tag))
        return True

    caplog.set_level(logging.INFO)
    cfg = {
        "dynamic_exits": {
            "strong_news_trailing_trigger_pct": 8.0,
            "strong_news_trailing_stop_pct": 4.0,
        },
        "dynamic_universe": {},
    }

    assert not du.manage_dynamic_exit("XOS", {"qty": 10}, 110.0, 100.0, cfg, _submit)
    assert sells == []
    assert "DYNAMIC_TRAILING_STOP symbol=XOS" in caplog.text
    assert "trail_pct=4.00" in caplog.text
    assert "DYNAMIC_EXIT_REASON symbol=XOS reason=strong_news_trailing_hold" in caplog.text


def test_dynamic_guard_failure_reason_rsi_and_distance() -> None:
    hi_rsi = du.DynamicEntrySignals(True, True, 80.0, 0.5)
    assert not du.dynamic_entry_guard_passes(hi_rsi)
    assert "RSI" in du.dynamic_entry_guard_failure_reason(hi_rsi)

    ext = du.DynamicEntrySignals(True, True, 50.0, 3.5)
    assert not du.dynamic_entry_guard_passes(ext)
    assert "2.0" in du.dynamic_entry_guard_failure_reason(ext)
    assert du.dynamic_entry_guard_passes(ext, max_distance_from_vwap_pct=5.0)

    ema_miss = du.DynamicEntrySignals(True, False, 50.0, 0.5)
    assert not du.dynamic_entry_guard_passes(ema_miss)
    assert du.dynamic_entry_guard_passes(
        ema_miss,
        require_ema_5_above_20=False,
    )


def test_five_min_breakout_and_intraday_high_helpers() -> None:
    df5 = pd.DataFrame({"high": [10.0, 11.0, 12.0, 13.0], "close": [10.5, 11.5, 12.5, 13.5]})
    assert du.five_min_breakout_from_bars(df5, ref_price=13.6)
    assert not du.five_min_breakout_from_bars(df5, ref_price=11.5)

    df1 = pd.DataFrame(
        {
            "high": [100.0, 101.0, 102.0],
            "low": [99.0, 100.0, 101.0],
            "open": [99.5, 100.5, 101.5],
            "close": [100.5, 101.5, 101.9],
            "volume": [10_000.0, 10_000.0, 10_000.0],
        }
    )
    assert du.new_intraday_high_from_1m(df1, ref_price=102.0)
    assert not du.new_intraday_high_from_1m(df1, ref_price=101.0)


def test_strong_green_candle_1m() -> None:
    df = pd.DataFrame(
        {
            "open": [99.0, 100.0],
            "high": [101.0, 102.0],
            "low": [98.5, 99.0],
            "close": [100.5, 101.8],
            "volume": [10_000.0, 10_000.0],
        }
    )
    assert du.strong_green_candle_1m(df, body_frac=0.55)
    bear = pd.DataFrame(
        {
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [99.5],
            "volume": [10_000.0],
        }
    )
    assert not du.strong_green_candle_1m(bear)


def test_opening_range_high_and_breakout() -> None:
    et = ZoneInfo("America/New_York")
    idx = pd.date_range("2024-06-10 09:30", periods=8, freq="1min", tz=et)
    df = pd.DataFrame(
        {
            "open": [100.0] * 8,
            "high": [100.2, 100.4, 100.5, 100.3, 100.6, 100.55, 100.7, 100.65],
            "low": [99.9] * 8,
            "close": [100.1] * 8,
            "volume": [5000.0] * 8,
        },
        index=idx,
    )
    sd = date(2024, 6, 10)
    orh = du.opening_range_high_first_minutes(df, minutes=15, session_date=sd)
    assert orh == pytest.approx(100.7)
    assert du.opening_range_breakout_above(df, 100.75, minutes=15, session_date=sd)
    assert not du.opening_range_breakout_above(df, 100.65, minutes=15, session_date=sd)


def test_dynamic_momentum_entry_orb_satisfies_gate() -> None:
    et = ZoneInfo("America/New_York")
    idx = pd.date_range("2024-06-10 09:30", periods=6, freq="1min", tz=et)
    bars_1m = pd.DataFrame(
        {
            "high": [100.0, 100.2, 100.1, 100.15, 100.1, 100.12],
            "low": [99.5] * 6,
            "open": [99.8] * 6,
            "close": [100.05] * 6,
            "volume": [10_000.0] * 6,
        },
        index=idx,
    )
    ok, msg = du.dynamic_momentum_entry_passes(
        gain_pct=16.0,
        relative_volume=2.5,
        vwap_above=True,
        spread_pct=2.0,
        bars_1m=bars_1m,
        bars_5m=pd.DataFrame({"high": [98.0, 99.0]}),
        ref_price=100.25,
        cfg={
            "opening_range_breakout": {"enabled": True, "minutes": 15},
        },
        session_date=date(2024, 6, 10),
    )
    assert ok and msg == "ok"


def test_dynamic_momentum_entry_passes_happy_and_or_branch() -> None:
    bars_1m = pd.DataFrame(
        {
            "high": [100.0, 101.0, 102.0],
            "low": [99.0, 100.0, 101.0],
            "open": [99.5, 100.5, 101.5],
            "close": [100.5, 101.5, 101.9],
            "volume": [10_000.0, 10_000.0, 10_000.0],
        }
    )
    ok, msg = du.dynamic_momentum_entry_passes(
        gain_pct=16.0,
        relative_volume=2.5,
        vwap_above=True,
        spread_pct=2.0,
        bars_1m=bars_1m,
        bars_5m=pd.DataFrame({"high": [12.0, 13.0]}),
        ref_price=102.0,
    )
    assert ok and msg == "ok"

    ok2, msg2 = du.dynamic_momentum_entry_passes(
        gain_pct=16.0,
        relative_volume=2.5,
        vwap_above=True,
        spread_pct=2.0,
        bars_1m=bars_1m,
        bars_5m=pd.DataFrame({"high": [98.0, 99.0, 100.0, 101.0]}),
        ref_price=100.0,
        cfg={"min_day_gain_pct": 15.0, "min_relative_volume": 2.0, "max_entry_spread_pct": 3.0},
    )
    assert not ok2
    assert "opening-range" in msg2 or "nh=" in msg2 or "orb=" in msg2


def test_dynamic_momentum_entry_adaptive_volume_allows_minor_after_11am() -> None:
    bars_1m = pd.DataFrame(
        {
            "high": [100.0, 101.0, 102.0],
            "low": [99.0, 100.0, 101.0],
            "open": [99.5, 100.5, 101.5],
            "close": [100.5, 101.5, 101.9],
            "volume": [10_000.0, 10_000.0, 10_000.0],
        }
    )
    cfg = {
        "min_day_gain_pct": 2.0,
        "min_relative_volume": 1.0,
        "max_entry_spread_pct": 3.0,
        "adaptive_volume_confirmation": {
            "enabled": True,
            "after_11am_enabled": True,
            "after_11am_min_relative_volume": 0.85,
            "minor_miss_tolerance": 0.15,
            "strong_gain_pct_min": 8.0,
        },
    }
    ok, msg = du.dynamic_momentum_entry_passes(
        gain_pct=12.0,
        relative_volume=0.87,
        vwap_above=True,
        spread_pct=0.2,
        bars_1m=bars_1m,
        bars_5m=pd.DataFrame({"high": [98.0, 99.0, 100.0, 101.0]}),
        ref_price=102.0,
        cfg=cfg,
        current_time=datetime(2026, 7, 6, 11, 30, tzinfo=ZoneInfo("America/New_York")),
        is_dynamic=True,
    )
    assert ok, msg


def test_dynamic_momentum_entry_adaptive_volume_disabled_preserves_reject() -> None:
    bars_1m = pd.DataFrame(
        {
            "high": [100.0, 101.0, 102.0],
            "low": [99.0, 100.0, 101.0],
            "open": [99.5, 100.5, 101.5],
            "close": [100.5, 101.5, 101.9],
            "volume": [10_000.0, 10_000.0, 10_000.0],
        }
    )
    ok, msg = du.dynamic_momentum_entry_passes(
        gain_pct=12.0,
        relative_volume=0.87,
        vwap_above=True,
        spread_pct=0.2,
        bars_1m=bars_1m,
        bars_5m=pd.DataFrame({"high": [98.0, 99.0, 100.0, 101.0]}),
        ref_price=102.0,
        cfg={
            "min_day_gain_pct": 2.0,
            "min_relative_volume": 1.0,
            "adaptive_volume_confirmation": {"enabled": False},
        },
        current_time=datetime(2026, 7, 6, 11, 30, tzinfo=ZoneInfo("America/New_York")),
        is_dynamic=True,
    )
    assert not ok
    assert "relative_volume" in msg


def test_dynamic_momentum_entry_vwap_score_alignment_passes_without_breakout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bars_1m = pd.DataFrame(
        {
            "high": [100.0, 101.0, 102.0],
            "low": [99.0, 100.0, 101.0],
            "open": [99.5, 100.5, 101.5],
            "close": [100.5, 101.5, 101.9],
            "volume": [10_000.0, 10_000.0, 10_000.0],
        }
    )

    with caplog.at_level(logging.INFO, logger="src.dynamic_universe"):
        ok, msg = du.dynamic_momentum_entry_passes(
            gain_pct=10.0,
            relative_volume=0.30,
            vwap_above=True,
            spread_pct=2.0,
            bars_1m=bars_1m,
            bars_5m=pd.DataFrame(
                {
                    "high": [98.0, 99.0, 100.0, 101.0],
                    "low": [97.0, 98.0, 99.0, 100.0],
                    "close": [97.5, 98.5, 99.0, 99.5],
                }
            ),
            ref_price=100.0,
            cfg={
                "min_day_gain_pct": 2.0,
                "min_relative_volume": 0.30,
                "max_entry_spread_pct": 3.0,
                "vwap_score_alignment": {"enabled": True, "min_score": 80, "min_day_gain_pct": 15},
            },
            is_dynamic=True,
            alignment_score=85.0,
            symbol="APGE",
        )

    assert ok
    assert msg == "ok vwap_score_alignment"
    assert "DYNAMIC_ALIGNMENT_PASS_VWAP_SCORE symbol=APGE" in caplog.text
    assert "effective_min_rel=0.300" in caplog.text
    assert "score=85.00" in caplog.text
    assert "day_gain_pct=10.00" in caplog.text
    assert "vwap_above=true" in caplog.text


def test_dynamic_momentum_entry_low_score_vwap_alignment_still_rejects(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bars_1m = pd.DataFrame(
        {
            "high": [100.0, 101.0, 102.0],
            "low": [99.0, 100.0, 101.0],
            "open": [99.5, 100.5, 101.5],
            "close": [100.5, 101.5, 101.9],
            "volume": [10_000.0, 10_000.0, 10_000.0],
        }
    )

    with caplog.at_level(logging.INFO, logger="src.dynamic_universe"):
        ok, msg = du.dynamic_momentum_entry_passes(
            gain_pct=10.0,
            relative_volume=0.30,
            vwap_above=True,
            spread_pct=2.0,
            bars_1m=bars_1m,
            bars_5m=pd.DataFrame(
                {
                    "high": [98.0, 99.0, 100.0, 101.0],
                    "low": [97.0, 98.0, 99.0, 100.0],
                    "close": [97.5, 98.5, 99.0, 99.5],
                }
            ),
            ref_price=100.0,
            cfg={
                "min_day_gain_pct": 2.0,
                "min_relative_volume": 0.30,
                "max_entry_spread_pct": 3.0,
                "vwap_score_alignment": {"enabled": True, "min_score": 80, "min_day_gain_pct": 15},
            },
            is_dynamic=True,
            alignment_score=40.0,
            symbol="BIRD",
        )

    assert not ok
    assert "need 5m breakout" in msg
    assert "DYNAMIC_ALIGNMENT_REJECT symbol=BIRD" in caplog.text
    assert "ENTRY_ALIGNMENT_CONTEXT symbol=BIRD outcome=fail" in caplog.text
    assert "momentum_score=40.0000" in caplog.text
    assert "ema20=" in caplog.text
    assert "ema50=" in caplog.text
    assert "vwap_distance_pct=" in caplog.text
    assert "five_min_trend_direction=up" in caplog.text
    assert "atr=1.5000" in caplog.text
    assert "relative_volume=0.3000" in caplog.text
    assert "score=40.00" in caplog.text
    assert "vwap_above=true" in caplog.text


def test_dynamic_momentum_entry_vwap_score_alignment_keeps_wide_spread_rejected() -> None:
    ok, msg = du.dynamic_momentum_entry_passes(
        gain_pct=10.0,
        relative_volume=0.30,
        vwap_above=True,
        spread_pct=3.2,
        bars_1m=pd.DataFrame({"high": [100.0], "low": [99.0], "open": [99.5], "close": [100.0]}),
        bars_5m=pd.DataFrame({"high": [99.0]}),
        ref_price=100.0,
        cfg={
            "min_day_gain_pct": 2.0,
            "min_relative_volume": 0.30,
            "max_entry_spread_pct": 3.0,
            "vwap_score_alignment": {"enabled": True, "min_score": 80},
        },
        is_dynamic=True,
        alignment_score=90.0,
        symbol="HIVE",
    )

    assert not ok
    assert "spread_pct 3.200% >= 3.00%" in msg


def test_dynamic_momentum_high_momentum_bypass_skips_minor_confirmation() -> None:
    bars_1m = pd.DataFrame(
        {
            "high": [110.0, 110.0],
            "low": [99.0, 99.0],
            "open": [100.0, 100.0],
            "close": [100.1, 100.1],
            "volume": [10_000.0, 10_000.0],
        }
    )
    bars_5m = pd.DataFrame({"high": [150.0, 151.0]})

    ok, msg = du.dynamic_momentum_entry_passes(
        gain_pct=16.0,
        relative_volume=4.2,
        vwap_above=True,
        spread_pct=0.49,
        bars_1m=bars_1m,
        bars_5m=bars_5m,
        ref_price=100.0,
        cfg={
            "min_day_gain_pct": 15.0,
            "min_relative_volume": 4.0,
            "max_entry_spread_pct": 3.0,
            "opening_range_breakout": {"enabled": False},
        },
    )

    assert ok
    assert msg == "ok high_momentum_bypass"


def test_high_momentum_bypass_requires_vwap_and_tight_spread() -> None:
    assert du.high_momentum_bypass_ok(
        gain_pct=12.1,
        relative_volume=3.1,
        vwap_above=True,
        spread_pct=0.49,
    )
    assert not du.high_momentum_bypass_ok(
        gain_pct=12.1,
        relative_volume=3.1,
        vwap_above=False,
        spread_pct=0.49,
    )
    assert not du.high_momentum_bypass_ok(
        gain_pct=12.1,
        relative_volume=3.1,
        vwap_above=True,
        spread_pct=0.5,
    )


def test_compute_intraday_momentum_score_normalized() -> None:
    cfg = {
        "momentum_score": {
            "weights": {
                "rel_volume": 0.3,
                "gain_pct": 0.3,
                "five_min_breakout": 0.2,
                "vwap_distance": 0.2,
            },
            "normalize": {
                "rel_volume_max": 5.0,
                "gain_pct_max": 50.0,
                "vwap_distance_pct_max": 15.0,
            },
        }
    }
    sc, br = du.compute_intraday_momentum_score(
        relative_volume=5.0,
        gain_pct=50.0,
        five_min_breakout=True,
        distance_from_vwap_pct=15.0,
        cfg=cfg,
    )
    assert sc == pytest.approx(1.0)
    assert br["rel_volume_norm"] == pytest.approx(1.0)
    assert br["gain_pct_norm"] == pytest.approx(1.0)
    assert br["five_min_breakout"] == pytest.approx(1.0)
    assert br["vwap_distance_norm"] == pytest.approx(1.0)


def test_pick_top_n_momentum_symbols_tiebreak() -> None:
    pairs = [("C", 0.5), ("A", 0.9), ("B", 0.9)]
    top = du.pick_top_n_momentum_symbols(pairs, top_n=2)
    assert top == frozenset({"A", "B"})


def test_dynamic_momentum_entry_base_constraints() -> None:
    bars_1m = pd.DataFrame(
        {
            "high": [102.0],
            "low": [101.0],
            "open": [101.2],
            "close": [101.9],
            "volume": [10_000.0],
        }
    )
    ok, msg = du.dynamic_momentum_entry_passes(
        gain_pct=14.0,
        relative_volume=3.0,
        vwap_above=True,
        spread_pct=1.0,
        bars_1m=bars_1m,
        bars_5m=None,
        ref_price=102.0,
    )
    assert not ok and "gain_pct" in msg

    ok2, msg2 = du.dynamic_momentum_entry_passes(
        gain_pct=20.0,
        relative_volume=1.5,
        vwap_above=True,
        spread_pct=1.0,
        bars_1m=bars_1m,
        bars_5m=None,
        ref_price=102.0,
    )
    assert not ok2 and "relative_volume" in msg2

    ok3, msg3 = du.dynamic_momentum_entry_passes(
        gain_pct=7.0,
        relative_volume=1.4,
        vwap_above=False,
        spread_pct=1.0,
        bars_1m=bars_1m,
        bars_5m=None,
        ref_price=102.0,
        cfg={"min_day_gain_pct": 3.0, "min_relative_volume": 1.0},
    )
    assert not ok3 and "VWAP" in msg3

    ok3b, msg3b = du.dynamic_momentum_entry_passes(
        gain_pct=20.0,
        relative_volume=3.0,
        vwap_above=False,
        spread_pct=1.0,
        bars_1m=bars_1m,
        bars_5m=None,
        ref_price=102.0,
    )
    assert not ok3b and "VWAP" in msg3b

    close_to_vwap_bars = pd.DataFrame(
        {
            "high": [100.0, 100.0, 100.0, 100.0],
            "low": [100.0, 100.0, 100.0, 100.0],
            "open": [100.0, 100.0, 100.0, 100.0],
            "close": [100.0, 100.0, 100.0, 100.0],
            "volume": [50_000, 50_000, 50_000, 50_000],
        }
    )
    ok3c, msg3c = du.dynamic_momentum_entry_passes(
        gain_pct=20.0,
        relative_volume=3.0,
        vwap_above=False,
        spread_pct=1.0,
        bars_1m=close_to_vwap_bars,
        bars_5m=None,
        ref_price=100.4,
        news_score=7,
        catalyst_age_minutes=45.0,
        is_dynamic=True,
    )
    assert ok3c and msg3c == "ok news_catalyst"

    reclaim_bars = pd.DataFrame(
        {
            "high": [99.2, 99.3, 100.8, 101.1],
            "low": [98.8, 98.9, 100.0, 100.4],
            "open": [99.0, 99.1, 100.1, 100.6],
            "close": [99.0, 99.1, 100.5, 100.9],
            "volume": [50_000, 50_000, 50_000, 50_000],
        }
    )
    reclaim_ok, reclaim_msg = du.dynamic_momentum_entry_passes(
        gain_pct=20.0,
        relative_volume=3.0,
        vwap_above=False,
        spread_pct=1.0,
        bars_1m=reclaim_bars,
        bars_5m=None,
        ref_price=101.0,
        news_score=7,
        catalyst_age_minutes=45.0,
        is_dynamic=True,
    )
    assert reclaim_ok and reclaim_msg == "ok news_catalyst"

    below_vwap_but_close = pd.DataFrame(
        {
            "high": [100.0, 100.0, 100.0, 100.0],
            "low": [100.0, 100.0, 100.0, 100.0],
            "open": [100.0, 100.0, 100.0, 100.0],
            "close": [100.0, 100.0, 100.0, 100.0],
            "volume": [50_000, 50_000, 50_000, 50_000],
        }
    )
    close_ok, close_msg = du.dynamic_momentum_entry_passes(
        gain_pct=20.0,
        relative_volume=3.0,
        vwap_above=False,
        spread_pct=1.0,
        bars_1m=below_vwap_but_close,
        bars_5m=None,
        ref_price=99.6,
        news_score=7,
        catalyst_age_minutes=45.0,
        is_dynamic=True,
    )
    assert close_ok and close_msg == "ok news_catalyst"

    ok4, msg4 = du.dynamic_momentum_entry_passes(
        gain_pct=20.0,
        relative_volume=3.0,
        vwap_above=True,
        spread_pct=4.0,
        bars_1m=bars_1m,
        bars_5m=None,
        ref_price=102.0,
    )
    assert not ok4 and "spread_pct" in msg4

    ok5, msg5 = du.dynamic_momentum_entry_passes(
        gain_pct=20.0,
        relative_volume=3.0,
        vwap_above=False,
        spread_pct=3.8,
        bars_1m=close_to_vwap_bars,
        bars_5m=None,
        ref_price=100.4,
        news_score=7,
        catalyst_age_minutes=45.0,
        is_dynamic=True,
        cfg={"max_entry_spread_pct": 4.0},
    )
    assert ok5 and msg5 == "ok"


def test_dynamic_momentum_entry_loosened_floor_boundaries_pass() -> None:
    bars_1m = pd.DataFrame(
        {
            "high": [101.0],
            "low": [100.0],
            "open": [100.2],
            "close": [100.9],
            "volume": [10_000.0],
        }
    )

    ok, msg = du.dynamic_momentum_entry_passes(
        gain_pct=3.0,
        relative_volume=1.0,
        vwap_above=True,
        spread_pct=1.0,
        bars_1m=bars_1m,
        bars_5m=None,
        ref_price=101.0,
        cfg={"min_day_gain_pct": 3.0, "min_relative_volume": 1.0},
        is_dynamic=True,
    )

    assert ok
    assert msg in {"ok", "ok news_catalyst"}


def test_dynamic_news_early_entry_allows_rvol_1_2_floor() -> None:
    ok, reason = nc.news_early_entry_passes(
        news_score=3,
        relative_volume=1.2,
        price_above_vwap=True,
        spread_pct=1.0,
        cfg={"news_dynamic_entry": {"early_min_relative_volume": 1.2}},
    )

    assert ok
    assert reason == "news_early_entry"


def test_catalyst_fastlane_rvol_036_passes(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    bars_1m = pd.DataFrame(
        {
            "high": [100.2, 100.3],
            "low": [99.8, 99.9],
            "open": [100.0, 100.0],
            "close": [100.0, 100.1],
            "volume": [50_000, 50_000],
        }
    )

    ok, msg = du.dynamic_momentum_entry_passes(
        gain_pct=3.0,
        relative_volume=0.36,
        vwap_above=True,
        spread_pct=1.0,
        bars_1m=bars_1m,
        bars_5m=pd.DataFrame({"close": [100.0, 99.9]}),
        ref_price=100.1,
        cfg={
            "min_day_gain_pct": 3.0,
            "min_relative_volume": 1.0,
            "catalyst_fastlane_active": True,
            "catalyst_min_relative_volume": 0.35,
        },
        symbol="ORCL",
        news_score=7,
        event_score=7.0,
        catalyst_score=0.7,
        catalyst_age_minutes=45.0,
        is_dynamic=True,
    )

    assert ok
    assert msg == "ok catalyst_fastlane"
    assert "CATALYST_FASTLANE_CHECK symbol=ORCL" in caplog.text
    assert "rel_volume=0.360" in caplog.text
    assert "eligible=true reason=ok" in caplog.text
    assert "CATALYST_FASTLANE_RVOL_THRESHOLD symbol=ORCL threshold=0.35" in caplog.text


def test_catalyst_fastlane_rvol_020_fails() -> None:
    ok, msg = du.dynamic_momentum_entry_passes(
        gain_pct=3.0,
        relative_volume=0.20,
        vwap_above=True,
        spread_pct=1.0,
        bars_1m=pd.DataFrame({"high": [100.2], "low": [99.8], "open": [100.0], "close": [100.1]}),
        bars_5m=pd.DataFrame({"close": [100.0, 100.1]}),
        ref_price=100.1,
        cfg={
            "min_day_gain_pct": 3.0,
            "min_relative_volume": 1.0,
            "catalyst_fastlane_active": True,
            "catalyst_min_relative_volume": 0.35,
        },
        news_score=7,
        event_score=7.0,
        catalyst_score=0.7,
        catalyst_age_minutes=45.0,
        is_dynamic=True,
    )

    assert not ok
    assert "relative_volume" in msg


def test_dynamic_momentum_no_fastlane_uses_effective_rvol_floor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    ok, msg = du.dynamic_momentum_entry_passes(
        gain_pct=4.0,
        relative_volume=0.35,
        vwap_above=True,
        spread_pct=1.0,
        bars_1m=pd.DataFrame({"high": [100.2], "low": [99.8], "open": [100.0], "close": [100.1]}),
        bars_5m=pd.DataFrame({"close": [100.0, 100.1]}),
        ref_price=100.1,
        cfg={
            "min_day_gain_pct": 3.0,
            "min_relative_volume": 0.60,
            "catalyst_fastlane_active": False,
            "catalyst_min_relative_volume": 0.35,
        },
        symbol="AVGO",
        news_score=7,
        event_score=7.0,
        catalyst_score=0.7,
        catalyst_age_minutes=45.0,
        is_dynamic=True,
    )

    assert not ok
    assert msg == "relative_volume 0.35 < 0.60"
    assert "DYNAMIC_RVOL_GUARD symbol=AVGO" in caplog.text
    assert "effective_min=0.600" in caplog.text


def test_catalyst_fastlane_wide_spread_fails() -> None:
    ok, msg = du.dynamic_momentum_entry_passes(
        gain_pct=3.0,
        relative_volume=0.41,
        vwap_above=True,
        spread_pct=3.5,
        bars_1m=pd.DataFrame({"high": [100.2], "low": [99.8], "open": [100.0], "close": [100.1]}),
        bars_5m=pd.DataFrame({"close": [100.0, 100.1]}),
        ref_price=100.1,
        cfg={
            "min_day_gain_pct": 3.0,
            "min_relative_volume": 1.0,
            "max_entry_spread_pct": 3.0,
            "catalyst_fastlane_active": True,
            "catalyst_min_relative_volume": 0.35,
        },
        news_score=7,
        event_score=7.0,
        catalyst_score=0.7,
        catalyst_age_minutes=45.0,
        is_dynamic=True,
    )

    assert not ok
    assert "spread_pct" in msg


def test_catalyst_fastlane_unstable_quote_fails() -> None:
    ok, msg = du.dynamic_momentum_entry_passes(
        gain_pct=3.0,
        relative_volume=0.41,
        vwap_above=True,
        spread_pct=1.0,
        bars_1m=pd.DataFrame({"high": [100.2], "low": [99.8], "open": [100.0], "close": [100.1]}),
        bars_5m=pd.DataFrame({"close": [100.0, 100.1]}),
        ref_price=100.1,
        cfg={
            "min_day_gain_pct": 3.0,
            "min_relative_volume": 1.0,
            "catalyst_fastlane_active": True,
            "catalyst_min_relative_volume": 0.35,
        },
        news_score=7,
        event_score=7.0,
        catalyst_score=0.7,
        catalyst_age_minutes=45.0,
        is_dynamic=True,
        quote_unstable=True,
    )

    assert not ok
    assert "unstable_quote" in msg


def test_catalyst_fastlane_without_momentum_confirmation_fails(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    flat_1m = pd.DataFrame(
        {
            "high": [100.0, 100.0],
            "low": [100.0, 100.0],
            "open": [100.0, 100.0],
            "close": [100.0, 100.0],
            "volume": [50_000, 50_000],
        }
    )

    ok, msg = du.dynamic_momentum_entry_passes(
        gain_pct=3.0,
        relative_volume=0.41,
        vwap_above=False,
        spread_pct=1.0,
        bars_1m=flat_1m,
        bars_5m=pd.DataFrame({"close": [100.0, 100.0]}),
        ref_price=99.9,
        cfg={
            "min_day_gain_pct": 3.0,
            "min_relative_volume": 1.0,
            "require_above_vwap": False,
            "catalyst_fastlane_active": True,
            "catalyst_min_relative_volume": 0.35,
        },
        news_score=7,
        event_score=7.0,
        catalyst_score=0.7,
        catalyst_age_minutes=45.0,
        is_dynamic=True,
    )

    assert not ok
    assert "no_momentum_confirmation" in msg
    assert "CATALYST_FASTLANE_CHECK symbol=" in caplog.text
    assert "reason=no_momentum_confirmation" in caplog.text


def _entry_rvol_relax_bars() -> tuple[pd.DataFrame, pd.DataFrame]:
    bars_1m = pd.DataFrame(
        {
            "high": [100.1, 100.4],
            "low": [99.8, 99.9],
            "open": [100.0, 100.0],
            "close": [100.0, 100.3],
            "volume": [50_000, 60_000],
        }
    )
    bars_5m = pd.DataFrame({"high": [100.0, 100.5], "close": [100.0, 100.4]})
    return bars_1m, bars_5m


def test_catalyst_entry_rvol_relax_intc_like_passes(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    bars_1m, bars_5m = _entry_rvol_relax_bars()

    ok, msg = du.dynamic_momentum_entry_passes(
        gain_pct=7.3,
        relative_volume=0.64,
        vwap_above=True,
        spread_pct=0.38,
        bars_1m=bars_1m,
        bars_5m=bars_5m,
        ref_price=100.3,
        cfg={"min_day_gain_pct": 3.0, "min_relative_volume": 1.71, "max_entry_spread_pct": 3.0},
        symbol="INTC",
        news_score=10,
        catalyst_score=0.91,
        premarket_rank=4,
        current_time=datetime(2026, 6, 11, 10, 38, tzinfo=ZoneInfo("America/New_York")),
        is_dynamic=True,
    )

    assert ok
    assert msg in {"ok", "ok news_catalyst"}
    assert "CATALYST_ENTRY_RVOL_RELAXED symbol=INTC rel=0.640 old_min=1.71 new_min=0.50" in caplog.text


def test_catalyst_entry_rvol_relax_orcl_rank_passes(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    bars_1m, bars_5m = _entry_rvol_relax_bars()

    ok, msg = du.dynamic_momentum_entry_passes(
        gain_pct=7.3,
        relative_volume=0.64,
        vwap_above=True,
        spread_pct=0.38,
        bars_1m=bars_1m,
        bars_5m=bars_5m,
        ref_price=100.3,
        cfg={"min_day_gain_pct": 3.0, "min_relative_volume": 1.71, "max_entry_spread_pct": 3.0},
        symbol="ORCL",
        news_score=0,
        catalyst_score=0.0,
        premarket_rank=1,
        current_time=datetime(2026, 6, 11, 10, 38, tzinfo=ZoneInfo("America/New_York")),
        is_dynamic=True,
    )

    assert ok
    assert msg == "ok"
    assert "CATALYST_ENTRY_RVOL_RELAXED symbol=ORCL rel=0.640 old_min=1.71 new_min=0.50" in caplog.text


def test_catalyst_entry_rvol_relax_amzn_below_relaxed_floor_fails(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    bars_1m, bars_5m = _entry_rvol_relax_bars()

    ok, msg = du.dynamic_momentum_entry_passes(
        gain_pct=7.3,
        relative_volume=0.28,
        vwap_above=True,
        spread_pct=0.38,
        bars_1m=bars_1m,
        bars_5m=bars_5m,
        ref_price=100.3,
        cfg={"min_day_gain_pct": 3.0, "min_relative_volume": 1.71, "max_entry_spread_pct": 3.0},
        symbol="AMZN",
        news_score=0,
        catalyst_score=0.0,
        premarket_rank=2,
        current_time=datetime(2026, 6, 11, 10, 38, tzinfo=ZoneInfo("America/New_York")),
        is_dynamic=True,
    )

    assert not ok
    assert msg == "relative_volume 0.28 < 0.50"
    assert "CATALYST_ENTRY_RVOL_RELAX_BLOCKED symbol=AMZN reason=relative_volume_below_floor" in caplog.text


def test_catalyst_entry_rvol_relax_weak_symbol_keeps_original_floor() -> None:
    bars_1m, bars_5m = _entry_rvol_relax_bars()

    ok, msg = du.dynamic_momentum_entry_passes(
        gain_pct=7.3,
        relative_volume=0.64,
        vwap_above=True,
        spread_pct=0.38,
        bars_1m=bars_1m,
        bars_5m=bars_5m,
        ref_price=100.3,
        cfg={"min_day_gain_pct": 3.0, "min_relative_volume": 1.71, "max_entry_spread_pct": 3.0},
        symbol="WEAK",
        news_score=0,
        catalyst_score=0.0,
        current_time=datetime(2026, 6, 11, 10, 38, tzinfo=ZoneInfo("America/New_York")),
        is_dynamic=True,
    )

    assert not ok
    assert msg == "relative_volume 0.64 < 1.71"


def test_catalyst_entry_rvol_relax_still_fails_spread(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    bars_1m, bars_5m = _entry_rvol_relax_bars()

    ok, msg = du.dynamic_momentum_entry_passes(
        gain_pct=7.3,
        relative_volume=0.64,
        vwap_above=True,
        spread_pct=3.5,
        bars_1m=bars_1m,
        bars_5m=bars_5m,
        ref_price=100.3,
        cfg={"min_day_gain_pct": 3.0, "min_relative_volume": 1.71, "max_entry_spread_pct": 3.0},
        symbol="DDOG",
        news_score=10,
        catalyst_score=0.91,
        premarket_rank=4,
        current_time=datetime(2026, 6, 11, 10, 38, tzinfo=ZoneInfo("America/New_York")),
        is_dynamic=True,
    )

    assert not ok
    assert msg == "spread_pct 3.500% >= 3.00%"
    assert "CATALYST_ENTRY_RVOL_RELAX_BLOCKED symbol=DDOG reason=spread_too_wide" in caplog.text


def test_catalyst_entry_rvol_relax_outside_window_uses_original_floor(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    bars_1m, bars_5m = _entry_rvol_relax_bars()

    ok, msg = du.dynamic_momentum_entry_passes(
        gain_pct=7.3,
        relative_volume=0.64,
        vwap_above=True,
        spread_pct=0.38,
        bars_1m=bars_1m,
        bars_5m=bars_5m,
        ref_price=100.3,
        cfg={"min_day_gain_pct": 3.0, "min_relative_volume": 1.71, "max_entry_spread_pct": 3.0},
        symbol="INTC",
        news_score=10,
        catalyst_score=0.91,
        premarket_rank=4,
        current_time=datetime(2026, 6, 11, 11, 1, tzinfo=ZoneInfo("America/New_York")),
        is_dynamic=True,
    )

    assert not ok
    assert msg == "relative_volume 0.64 < 1.71"
    assert "CATALYST_ENTRY_RVOL_RELAX_BLOCKED symbol=INTC reason=outside_window" in caplog.text


def _vwap_reclaim_bars() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "high": [5.0, 5.1, 5.2, 5.45, 5.6],
            "low": [4.8, 4.9, 5.0, 5.25, 5.4],
            "close": [4.9, 4.95, 5.05, 5.48, 5.55],
            "volume": [50_000, 50_000, 50_000, 80_000, 80_000],
        }
    )


def _entry_aligned_scanner_cfg(**entry_overrides: object) -> dict:
    entry_cfg = {
        "enabled": True,
        "min_day_gain_pct": 15.0,
        "min_relative_volume": 2.0,
        "max_entry_spread_pct": 3.0,
        "require_above_vwap": True,
        "opening_range_breakout": {"enabled": False},
    }
    entry_cfg.update(entry_overrides)
    return _scanner_cfg(
        min_day_gain_pct=6.0,
        max_day_gain_pct=120.0,
        min_relative_volume=1.0,
        min_rel_volume=1.0,
        max_spread_pct=5.0,
        require_above_vwap=False,
        dynamic_momentum_entry=entry_cfg,
    )


def _entry_pass_bars() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [9.6, 9.8, 10.0],
            "high": [9.8, 10.0, 10.5],
            "low": [9.4, 9.6, 9.8],
            "close": [9.7, 9.9, 10.4],
            "volume": [50_000, 60_000, 80_000],
        }
    )


def _entry_confirmation_fail_bars() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [10.3, 10.4],
            "high": [10.8, 10.7],
            "low": [9.8, 10.0],
            "close": [10.4, 10.2],
            "volume": [50_000, 50_000],
        }
    )


def _alignment_bypass_bars() -> pd.DataFrame:
    rows: list[dict[str, float]] = []
    for _ in range(25):
        rows.append({"open": 10.10, "high": 10.20, "low": 10.00, "close": 10.15, "volume": 50_000})
    for _ in range(4):
        rows.append({"open": 10.45, "high": 10.80, "low": 9.30, "close": 10.35, "volume": 80_000})
    rows.append({"open": 10.50, "high": 10.70, "low": 9.20, "close": 10.30, "volume": 80_000})
    return pd.DataFrame(rows)


class _EntryBarsMarket(_OneMoverMarket):
    def __init__(
        self,
        symbol: str,
        *,
        price: float,
        avg_volume: float,
        relative_volume: float,
        day_gain_pct: float = 20.0,
        bid: float | None = None,
        ask: float | None = None,
        bars_1m: pd.DataFrame | None = None,
        bars_5m: pd.DataFrame | None = None,
    ) -> None:
        super().__init__(
            symbol,
            price=price,
            avg_volume=avg_volume,
            relative_volume=relative_volume,
            day_gain_pct=day_gain_pct,
            bid=bid,
            ask=ask,
        )
        self.bars_1m = bars_1m if bars_1m is not None else _entry_pass_bars()
        self.bars_5m = bars_5m if bars_5m is not None else pd.DataFrame({"high": [9.6, 10.0]})

    def get_bars_batch(self, symbols, timeframe: str = "1Min", limit: int = 60):
        bars = self.bars_5m if timeframe == "5Min" else self.bars_1m
        return {self.symbol: bars for _s in symbols}


def _alignment_bypass_cfg(**entry_overrides: object) -> dict:
    cfg = _entry_aligned_scanner_cfg(**entry_overrides)
    cfg["min_avg_volume"] = 5_000
    cfg["min_atr_expansion_ratio"] = 0.25
    return cfg


def test_dynamic_alignment_bypass_accepts_blze_like_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})

    with caplog.at_level(logging.INFO, logger="src.dynamic_universe"):
        out = du.scan_candidates_batch(
            _EntryBarsMarket(
                "BLZE",
                price=10.0,
                avg_volume=6_000,
                relative_volume=1.35,
                day_gain_pct=18.0,
                bars_1m=_alignment_bypass_bars(),
                bars_5m=pd.DataFrame({"high": [10.9, 10.8]}),
            ),
            [],
            _alignment_bypass_cfg(),
            emit_logs=False,
        )

    assert out.selected == ["BLZE"]
    assert "DYNAMIC_ALIGNMENT_BYPASS symbol=BLZE gain=18.00 rel=1.350 avg=6000 spread=" in caplog.text


def test_dynamic_alignment_bypass_accepts_sndq_like_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})

    with caplog.at_level(logging.INFO, logger="src.dynamic_universe"):
        out = du.scan_candidates_batch(
            _EntryBarsMarket(
                "SNDQ",
                price=10.0,
                avg_volume=8_500,
                relative_volume=1.55,
                day_gain_pct=16.2,
                bars_1m=_alignment_bypass_bars(),
                bars_5m=pd.DataFrame({"high": [10.7, 10.6]}),
            ),
            [],
            _alignment_bypass_cfg(),
            emit_logs=False,
        )

    assert out.selected == ["SNDQ"]
    assert "DYNAMIC_ALIGNMENT_BYPASS symbol=SNDQ gain=16.20 rel=1.550 avg=8500 spread=" in caplog.text


def test_dynamic_alignment_bypass_keeps_low_price_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})

    out = du.scan_candidates_batch(
        _EntryBarsMarket(
            "LOWP",
            price=1.5,
            avg_volume=8_500,
            relative_volume=1.55,
            day_gain_pct=18.0,
            bars_1m=_alignment_bypass_bars(),
        ),
        [],
        _alignment_bypass_cfg(),
        emit_logs=False,
    )

    assert out.selected == []
    assert out.rejected[0].rejection_reason == "below_min_price"


def test_dynamic_alignment_bypass_keeps_unstable_quote_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})

    out = du.scan_candidates_batch(
        _EntryBarsMarket(
            "UNST",
            price=10.0,
            avg_volume=8_500,
            relative_volume=1.55,
            day_gain_pct=18.0,
            bid=8.0,
            ask=12.0,
            bars_1m=_alignment_bypass_bars(),
        ),
        [],
        _alignment_bypass_cfg(),
        emit_logs=False,
    )

    assert out.selected == []
    assert out.rejected[0].rejection_reason == "unstable quote"


def test_dynamic_alignment_bypass_keeps_unsafe_spread_and_liquidity_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})

    wide = du.scan_candidates_batch(
        _EntryBarsMarket(
            "WIDE",
            price=10.0,
            avg_volume=8_500,
            relative_volume=1.55,
            day_gain_pct=18.0,
            bid=9.7,
            ask=10.3,
            bars_1m=_alignment_bypass_bars(),
        ),
        [],
        _alignment_bypass_cfg(),
        emit_logs=False,
    )
    thin = du.scan_candidates_batch(
        _EntryBarsMarket(
            "THIN",
            price=10.0,
            avg_volume=4_999,
            relative_volume=1.55,
            day_gain_pct=18.0,
            bars_1m=_alignment_bypass_bars(),
        ),
        [],
        _alignment_bypass_cfg(),
        emit_logs=False,
    )

    assert wide.selected == []
    assert wide.rejected[0].rejection_reason == "spread too wide"
    assert thin.selected == []
    assert thin.rejected[0].rejection_reason == "below_min_avg_volume"


def test_dynamic_alignment_bypass_keeps_normal_alignment_path_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})

    with caplog.at_level(logging.INFO, logger="src.dynamic_universe"):
        out = du.scan_candidates_batch(
            _EntryBarsMarket(
                "NORM",
                price=10.0,
                avg_volume=8_500,
                relative_volume=1.19,
                day_gain_pct=18.0,
                bars_1m=_alignment_bypass_bars(),
                bars_5m=pd.DataFrame({"high": [10.7, 10.6]}),
            ),
            [],
            _alignment_bypass_cfg(),
            emit_logs=False,
        )

    assert out.selected == []
    assert out.rejected[0].rejection_reason.startswith("entry_alignment:")
    assert "DYNAMIC_ALIGNMENT_BYPASS symbol=NORM" not in caplog.text


def test_dynamic_scan_rejects_entry_spread_before_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})

    out = du.scan_candidates_batch(
        _EntryBarsMarket(
            "CMND",
            price=10.0,
            avg_volume=1_000_000,
            relative_volume=1.19,
            bid=9.8,
            ask=10.2,
        ),
        [],
        _entry_aligned_scanner_cfg(min_relative_volume=1.0),
        emit_logs=False,
    )

    assert out.selected == []
    assert out.rejected[0].rejection_reason.startswith("entry_alignment: spread_pct")


def test_dynamic_scan_rejects_entry_relative_volume_before_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})

    out = du.scan_candidates_batch(
        _EntryBarsMarket("SOXS", price=10.5, avg_volume=1_000_000, relative_volume=1.19),
        [],
        _entry_aligned_scanner_cfg(),
        emit_logs=False,
    )

    assert out.selected == []
    assert out.rejected[0].rejection_reason.startswith("entry_alignment: relative_volume")


def test_dynamic_scan_near_entry_relative_volume_passes_with_catalyst(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})
    artifact = {
        "ABAT": {
            "symbol": "ABAT",
            "news_score": 4,
            "event_score": 5.0,
            "catalyst_score": 0.8,
            "headline": "ABAT announces battery materials update",
            "source": "alpaca",
            "catalyst_type": "production",
            "age_minutes": 18.0,
        }
    }

    out = du.scan_candidates_batch(
        _EntryBarsMarket("ABAT", price=10.5, avg_volume=1_000_000, relative_volume=1.76),
        [],
        _entry_aligned_scanner_cfg(min_relative_volume=1.80),
        emit_logs=False,
        premarket_artifacts=artifact,
    )

    assert out.selected == ["ABAT"]
    assert out.accepted[0].catalyst_score == pytest.approx(0.8)
    assert out.accepted[0].relative_volume == pytest.approx(1.76)


def test_dynamic_scan_near_entry_relative_volume_rejects_without_catalyst(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})

    out = du.scan_candidates_batch(
        _EntryBarsMarket("ABAT", price=10.5, avg_volume=1_000_000, relative_volume=1.19),
        [],
        _entry_aligned_scanner_cfg(min_relative_volume=1.80),
        emit_logs=False,
    )

    assert out.selected == []
    assert out.rejected[0].rejection_reason.startswith("entry_alignment: relative_volume")


def test_dynamic_scan_rejects_missing_entry_confirmation_before_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})

    out = du.scan_candidates_batch(
        _EntryBarsMarket(
            "IREZ",
            price=10.5,
            avg_volume=1_000_000,
            relative_volume=1.19,
            bars_1m=_entry_confirmation_fail_bars(),
            bars_5m=pd.DataFrame({"high": [10.8]}),
        ),
        [],
        _entry_aligned_scanner_cfg(min_relative_volume=1.0),
        emit_logs=False,
    )

    assert out.selected == []
    assert out.rejected[0].rejection_reason.startswith("entry_alignment: need 5m breakout")


def test_dynamic_scan_strong_news_override_keeps_low_rvol_when_safety_passes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})
    artifact = {
        "NVTS": {
            "symbol": "NVTS",
            "news_score": 8,
            "event_score": 7.0,
            "catalyst_score": 0.8,
            "headline": "NVTS major contract",
            "catalyst_type": "deal",
            "age_minutes": 45.0,
        }
    }

    out = du.scan_candidates_batch(
        _EntryBarsMarket("NVTS", price=10.5, avg_volume=1_000_000, relative_volume=0.42),
        [],
        _entry_aligned_scanner_cfg(),
        emit_logs=False,
        premarket_artifacts=artifact,
    )

    assert out.selected == ["NVTS"]
    assert out.accepted[0].relative_volume == pytest.approx(0.42)


def test_dynamic_scan_strong_news_override_still_rejects_entry_spread_safety(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})
    artifact = {
        "WIDE": {
            "symbol": "WIDE",
            "news_score": 8,
            "event_score": 7.0,
            "catalyst_score": 0.8,
            "headline": "wide spread name",
            "catalyst_type": "deal",
            "age_minutes": 20.0,
        }
    }

    out = du.scan_candidates_batch(
        _EntryBarsMarket(
            "WIDE",
            price=10.0,
            avg_volume=1_000_000,
            relative_volume=0.42,
            bid=9.8,
            ask=10.2,
        ),
        [],
        _entry_aligned_scanner_cfg(),
        emit_logs=False,
        premarket_artifacts=artifact,
    )

    assert out.selected == []
    assert out.rejected[0].rejection_reason.startswith("entry_alignment: spread_pct")


def test_strong_news_override_passes_low_rel_volume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn_override.json")
    du.save_state({"cooldowns": {}, "active": {}})
    artifact = {
        "NVTS": {
            "symbol": "NVTS",
            "news_score": 8,
            "event_score": 7.0,
            "catalyst_score": 0.8,
            "headline": "NVTS major contract",
            "catalyst_type": "deal",
            "age_minutes": 45.0,
        }
    }

    class _Market(_OneMoverMarket):
        def get_bars_batch(self, symbols, timeframe: str = "1Min", limit: int = 60):
            return {self.symbol: _vwap_reclaim_bars() for _s in symbols}

    out = du.scan_candidates_batch(
        _Market("NVTS", price=5.5, avg_volume=500_000, relative_volume=0.42, day_gain_pct=25.0),
        [],
        _scanner_cfg(min_relative_volume=1.0, min_rel_volume=1.0, require_above_vwap=True),
        emit_logs=True,
        premarket_artifacts=artifact,
    )

    assert out.selected == ["NVTS"]
    assert "DYNAMIC_NEWS_OVERRIDE_APPLIED symbol=NVTS" in caplog.text


def test_strong_news_penny_stock_still_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    artifact = {
        "PENN": {
            "symbol": "PENN",
            "news_score": 8,
            "event_score": 7.0,
            "catalyst_score": 0.8,
            "headline": "penny runner",
            "catalyst_type": "deal",
            "age_minutes": 30.0,
        }
    }
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})
    out = du.scan_candidates_batch(
        _OneMoverMarket("PENN", price=1.25, avg_volume=500_000, relative_volume=0.5),
        [],
        _scanner_cfg(),
        emit_logs=True,
        premarket_artifacts=artifact,
    )
    assert out.selected == []
    assert out.rejected[0].rejection_reason == "below_min_price"
    assert "DYNAMIC_NEWS_OVERRIDE_BLOCKED symbol=PENN" in caplog.text


def test_strong_news_wide_spread_still_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.INFO)
    artifact = {
        "WIDE": {
            "symbol": "WIDE",
            "news_score": 8,
            "event_score": 7.0,
            "catalyst_score": 0.8,
            "headline": "wide spread name",
            "catalyst_type": "deal",
            "age_minutes": 20.0,
        }
    }
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})
    out = du.scan_candidates_batch(
        _OneMoverMarket("WIDE", price=8.0, avg_volume=500_000, relative_volume=0.5, bid=7.6, ask=8.4),
        [],
        _scanner_cfg(max_spread_pct=2.5),
        emit_logs=True,
        premarket_artifacts=artifact,
    )
    assert out.selected == []
    assert out.rejected[0].rejection_reason in {
        "unstable quote",
        "spread too wide",
        "below_min_relative_volume",
    }
    assert "DYNAMIC_OVERRIDE_DEBUG symbol=WIDE" in caplog.text
    assert "override_active=True" in caplog.text
    assert "override_active=True" in capsys.readouterr().out
    assert "DYNAMIC_NEWS_OVERRIDE_BLOCKED symbol=WIDE" in caplog.text
    assert "spread_pct" in caplog.text


def test_xos_like_high_gain_below_vwap_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    below_vwap_bars = pd.DataFrame(
        {
            "high": [12.0, 12.1, 12.0],
            "low": [11.5, 11.6, 11.4],
            "close": [11.7, 11.65, 11.55],
            "volume": [100_000, 100_000, 100_000],
        }
    )

    class _Market(_OneMoverMarket):
        def get_bars_batch(self, symbols, timeframe: str = "1Min", limit: int = 60):
            return {self.symbol: below_vwap_bars for _s in symbols}

    artifact = {
        "XOS": {
            "symbol": "XOS",
            "news_score": 8,
            "event_score": 7.0,
            "catalyst_score": 0.8,
            "headline": "extended runner",
            "catalyst_type": "deal",
            "age_minutes": 60.0,
        }
    }
    out = du.scan_candidates_batch(
        _Market("XOS", price=11.6, avg_volume=800_000, relative_volume=1.2, day_gain_pct=169.0),
        [],
        _scanner_cfg(require_above_vwap=True),
        emit_logs=True,
        premarket_artifacts=artifact,
    )
    assert out.selected == []
    reasons = {row.rejection_reason for row in out.rejected}
    assert "gain filter" in reasons or "not above VWAP" in reasons
    assert "DYNAMIC_NEWS_OVERRIDE_BLOCKED symbol=XOS" in caplog.text


def test_strong_news_rvol_override_from_news_published_at(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """NVTS-like: news_score=8, rel_volume=0.57, no premarket artifact — age from headline time."""
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    published = datetime.now(timezone.utc) - timedelta(minutes=30)

    class _Market(_OneMoverMarket):
        def get_bars_batch(self, symbols, timeframe: str = "1Min", limit: int = 60):
            return {self.symbol: _vwap_reclaim_bars() for _s in symbols}

    monkeypatch.setattr(
        du,
        "fetch_recent_news_catalysts",
        lambda *_a, **_kw: {
            "NVTS": NewsCatalyst(
                "NVTS",
                8,
                "NVTS wins deal",
                published_at=published,
                catalyst_type="deal",
            )
        },
    )

    out = du.scan_candidates_batch(
        _Market("NVTS", price=5.5, avg_volume=500_000, relative_volume=0.57, day_gain_pct=25.0),
        [],
        _scanner_cfg(min_relative_volume=1.0, min_rel_volume=1.0, require_above_vwap=True),
        emit_logs=True,
    )

    assert out.selected == ["NVTS"]
    assert "DYNAMIC_OVERRIDE symbol=NVTS" in capsys.readouterr().out
    assert "DYNAMIC_OVERRIDE symbol=NVTS" in caplog.text
    assert "DYNAMIC_OVERRIDE_DEBUG symbol=NVTS" in caplog.text
    assert "base_min_rel=1.000" in caplog.text
    assert "effective_min_rel=0.250" in caplog.text or "effective_min_rel=0.350" in caplog.text
    assert "DYNAMIC_OVERRIDE_ACTIVE symbol=NVTS" in caplog.text
    assert "effective_min_rel_volume=0.250" in caplog.text or "effective_min_rel_volume=0.350" in caplog.text


def test_strong_news_override_keeps_effective_min_relative_volume_across_scans(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")

    artifact = {
        "NVTS": {
            "symbol": "NVTS",
            "news_score": 8,
            "event_score": 7.0,
            "catalyst_score": 0.8,
            "headline": "NVTS major contract",
            "catalyst_type": "deal",
            "age_minutes": 45.0,
        }
    }

    class _AboveVWAPMarket(_OneMoverMarket):
        def get_bars_batch(self, symbols, timeframe: str = "1Min", limit: int = 60):
            return {self.symbol: _vwap_reclaim_bars() for _s in symbols}

    class _BelowVWAPMarket(_OneMoverMarket):
        def get_bars_batch(self, symbols, timeframe: str = "1Min", limit: int = 60):
            return {
                self.symbol: pd.DataFrame(
                    {
                        "high": [6.0, 5.9, 5.8],
                        "low": [5.7, 5.6, 5.5],
                        "close": [5.75, 5.65, 5.55],
                        "volume": [50_000, 50_000, 50_000],
                    }
                )
                for _s in symbols
            }

    out1 = du.scan_candidates_batch(
        _AboveVWAPMarket("NVTS", price=5.5, avg_volume=500_000, relative_volume=0.68, day_gain_pct=25.0),
        [],
        _scanner_cfg(min_relative_volume=1.0, min_rel_volume=1.0, require_above_vwap=False),
        emit_logs=True,
        premarket_artifacts=artifact,
    )
    out2 = du.scan_candidates_batch(
        _BelowVWAPMarket("NVTS", price=5.5, avg_volume=500_000, relative_volume=0.70, day_gain_pct=25.0),
        [],
        _scanner_cfg(min_relative_volume=1.0, min_rel_volume=1.0, require_above_vwap=False),
        emit_logs=True,
        premarket_artifacts=artifact,
    )

    assert out1.selected == ["NVTS"]
    assert out2.selected == ["NVTS"]
    assert caplog.text.count("DYNAMIC_OVERRIDE symbol=NVTS") >= 2
    assert caplog.text.count("effective_min_rel=0.250") + caplog.text.count("effective_min_rel=0.350") >= 2


def test_strong_news_override_threshold_7_qualifies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    artifact = {
        "INTC": {
            "symbol": "INTC",
            "news_score": 7,
            "event_score": 6.5,
            "catalyst_score": 0.7,
            "headline": "INTC catalyst",
            "catalyst_type": "deal",
            "age_minutes": 60.0,
        }
    }

    class _Market(_OneMoverMarket):
        def get_bars_batch(self, symbols, timeframe: str = "1Min", limit: int = 60):
            return {self.symbol: _vwap_reclaim_bars() for _s in symbols}

    out = du.scan_candidates_batch(
        _Market("INTC", price=35.0, avg_volume=1_000_000, relative_volume=0.55, day_gain_pct=12.0),
        [],
        _scanner_cfg(min_relative_volume=1.0, min_rel_volume=1.0, require_above_vwap=False),
        emit_logs=True,
        premarket_artifacts=artifact,
    )

    assert out.selected == ["INTC"]
    assert "DYNAMIC_OVERRIDE_DEBUG symbol=INTC" in caplog.text
    assert "override_active=True" in caplog.text
    assert "effective_min_rel=0.250" in caplog.text or "effective_min_rel=0.350" in caplog.text


def test_catalyst_rvol_relax_allows_intc_like_stale_180_min_news(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})
    artifact = {
        "INTC": {
            "symbol": "INTC",
            "news_score": 10,
            "event_score": 4.0,
            "catalyst_score": 0.6,
            "headline": "INTC premarket catalyst",
            "catalyst_type": "deal",
            "age_minutes": 184.0,
            "premarket_rank": 11,
        }
    }

    class _Market(_OneMoverMarket):
        def get_bars_batch(self, symbols, timeframe: str = "1Min", limit: int = 60):
            return {self.symbol: _vwap_reclaim_bars() for _s in symbols}

    out = du.scan_candidates_batch(
        _Market("INTC", price=35.0, avg_volume=1_000_000, relative_volume=0.34, day_gain_pct=12.0),
        [],
        _scanner_cfg(min_relative_volume=1.0, min_rel_volume=1.0, require_above_vwap=True),
        emit_logs=True,
        premarket_artifacts=artifact,
        now=datetime(2026, 6, 11, 9, 45, tzinfo=ZoneInfo("America/New_York")),
    )

    assert out.selected == ["INTC"]
    assert out.accepted[0].relative_volume == pytest.approx(0.34)
    assert (
        "CATALYST_RVOL_RELAXED symbol=INTC rel=0.340 old_min=1.00 new_min=0.25 age=184.0"
        in caplog.text
    )


def test_catalyst_rvol_relax_keeps_weak_low_rvol_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})

    class _Market(_OneMoverMarket):
        def get_bars_batch(self, symbols, timeframe: str = "1Min", limit: int = 60):
            return {self.symbol: _vwap_reclaim_bars() for _s in symbols}

    out = du.scan_candidates_batch(
        _Market("WEAK", price=35.0, avg_volume=1_000_000, relative_volume=0.34, day_gain_pct=12.0),
        [],
        _scanner_cfg(min_relative_volume=1.0, min_rel_volume=1.0, require_above_vwap=True),
        emit_logs=True,
        now=datetime(2026, 6, 11, 9, 45, tzinfo=ZoneInfo("America/New_York")),
    )

    assert out.selected == []
    assert out.rejected[0].rejection_reason == "below_min_relative_volume"
    assert "CATALYST_RVOL_RELAX_BLOCKED symbol=WEAK reason=weak_catalyst" in caplog.text


def test_catalyst_rvol_relax_does_not_bypass_spread_or_unstable_quote(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})
    artifact = {
        "WIDE": {
            "symbol": "WIDE",
            "news_score": 10,
            "event_score": 7.0,
            "catalyst_score": 0.8,
            "headline": "WIDE catalyst",
            "catalyst_type": "deal",
            "age_minutes": 184.0,
        },
        "UNST": {
            "symbol": "UNST",
            "news_score": 10,
            "event_score": 7.0,
            "catalyst_score": 0.8,
            "headline": "UNST catalyst",
            "catalyst_type": "deal",
            "age_minutes": 184.0,
        },
    }

    class _WideMarket(_OneMoverMarket):
        def get_bars_batch(self, symbols, timeframe: str = "1Min", limit: int = 60):
            return {self.symbol: _vwap_reclaim_bars() for _s in symbols}

    wide = du.scan_candidates_batch(
        _WideMarket(
            "WIDE",
            price=35.0,
            avg_volume=1_000_000,
            relative_volume=1.2,
            day_gain_pct=12.0,
            bid=34.0,
            ask=36.0,
        ),
        [],
        _scanner_cfg(min_relative_volume=1.0, min_rel_volume=1.0, max_spread_pct=2.5, require_above_vwap=True),
        emit_logs=True,
        premarket_artifacts=artifact,
        now=datetime(2026, 6, 11, 9, 45, tzinfo=ZoneInfo("America/New_York")),
    )
    unstable = du.scan_candidates_batch(
        _WideMarket(
            "UNST",
            price=35.0,
            avg_volume=1_000_000,
            relative_volume=1.2,
            day_gain_pct=12.0,
            bid=29.0,
            ask=41.0,
        ),
        [],
        _scanner_cfg(min_relative_volume=1.0, min_rel_volume=1.0, max_spread_pct=2.5, require_above_vwap=True),
        emit_logs=True,
        premarket_artifacts=artifact,
        now=datetime(2026, 6, 11, 9, 45, tzinfo=ZoneInfo("America/New_York")),
    )

    assert wide.selected == []
    assert wide.rejected[0].rejection_reason == "spread too wide"
    assert unstable.selected == []
    assert unstable.rejected[0].rejection_reason == "unstable quote"
    assert "CATALYST_RVOL_RELAX_BLOCKED symbol=WIDE reason=spread_too_wide" in caplog.text
    assert "CATALYST_RVOL_RELAX_BLOCKED symbol=UNST reason=unstable_quote" in caplog.text


def test_catalyst_rvol_relax_does_not_bypass_min_price(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(du, "STATE_FILE", tmp_path / "dyn.json")
    monkeypatch.setattr(du, "fetch_recent_news_catalysts", lambda *_a, **_kw: {})
    artifact = {
        "LOWP": {
            "symbol": "LOWP",
            "news_score": 10,
            "event_score": 7.0,
            "catalyst_score": 0.8,
            "headline": "LOWP catalyst",
            "catalyst_type": "deal",
            "age_minutes": 184.0,
        }
    }

    out = du.scan_candidates_batch(
        _OneMoverMarket("LOWP", price=1.5, avg_volume=1_000_000, relative_volume=0.34, day_gain_pct=12.0),
        [],
        _scanner_cfg(min_relative_volume=1.0, min_rel_volume=1.0, min_price=2, require_above_vwap=False),
        emit_logs=True,
        premarket_artifacts=artifact,
        now=datetime(2026, 6, 11, 9, 45, tzinfo=ZoneInfo("America/New_York")),
    )

    assert out.selected == []
    assert out.rejected[0].rejection_reason == "below_min_price"
    assert "CATALYST_RVOL_RELAX_BLOCKED symbol=LOWP reason=below_min_price" in caplog.text


def test_dynamic_momentum_entry_strong_news_rel_volume_floor_is_lower(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    bars_1m = pd.DataFrame(
        {
            "high": [100.0, 100.1, 100.2, 100.3],
            "low": [99.8, 99.9, 100.0, 100.1],
            "open": [99.9, 100.0, 100.1, 100.2],
            "close": [100.05, 100.08, 100.15, 100.2],
            "volume": [50_000, 50_000, 50_000, 50_000],
        }
    )
    ok, msg = du.dynamic_momentum_entry_passes(
        gain_pct=20.0,
        relative_volume=1.16,
        vwap_above=True,
        spread_pct=1.0,
        bars_1m=bars_1m,
        bars_5m=None,
        ref_price=100.25,
        news_score=8,
        catalyst_age_minutes=5.3,
        is_dynamic=True,
    )

    assert ok
    assert msg in {"ok", "ok news_catalyst", "ok high_momentum_bypass"}
    assert "DYNAMIC_RVOL_GUARD symbol=" in caplog.text
    assert "effective_min=0.750" in caplog.text


def test_dynamic_momentum_entry_weak_news_keeps_hard_rvol_floor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    bars_1m = pd.DataFrame(
        {
            "high": [100.0, 100.1, 100.2, 100.3],
            "low": [99.8, 99.9, 100.0, 100.1],
            "open": [99.9, 100.0, 100.1, 100.2],
            "close": [100.05, 100.08, 100.15, 100.2],
            "volume": [50_000, 50_000, 50_000, 50_000],
        }
    )
    ok, msg = du.dynamic_momentum_entry_passes(
        gain_pct=20.0,
        relative_volume=1.16,
        vwap_above=True,
        spread_pct=1.0,
        bars_1m=bars_1m,
        bars_5m=None,
        ref_price=100.25,
        news_score=6,
        catalyst_age_minutes=5.3,
        is_dynamic=True,
    )

    assert not ok
    assert "relative_volume" in msg
    assert "DYNAMIC_RVOL_GUARD symbol=" in caplog.text
    assert "effective_min=2.000" in caplog.text


def test_batch_scan_skips_occ_symbols_for_snapshots() -> None:
    class _Market:
        def get_top_movers(self):
            return [
                {"symbol": "AI260618C00005000"},
                {"symbol": "REAL"},
            ]

        def get_snapshots_batch(self, symbols):
            return {s: {"price": 10.0, "day_gain_pct": 12.0, "volume": 2_000_000, "bid": 9.9, "ask": 10.1} for s in symbols}

        def get_avg_volumes(self, symbols):
            return {s: 1_000_000.0 for s in symbols}

        def get_bars_batch(self, symbols, timeframe: str = "1Min", limit: int = 60):
            return {s: pd.DataFrame() for s in symbols}

    out = du.scan_candidates_batch(_Market(), [], _scanner_cfg(), emit_logs=False)
    assert "AI260618C00005000" not in {row.symbol for row in out.rejected + out.accepted}
    assert out.selected == ["REAL"] or out.selected == []


def test_aggressive_dynamic_scan_settings_overlay() -> None:
    settings = du._dynamic_scan_settings(
        {
            "min_price": 5,
            "max_price": 150,
            "min_day_gain_pct": 2,
            "min_relative_volume": 1.5,
            "max_symbols": 10,
            "max_spread_pct": 0.5,
            "aggressive_mode": {
                "enabled": True,
                "minimum_price": 2,
                "maximum_price": 300,
                "minimum_day_gain_pct": 0.5,
                "catalyst_minimum_day_gain_pct": -2,
                "minimum_relative_volume": 0.75,
                "catalyst_minimum_relative_volume": 0.40,
                "max_symbols": 50,
                "max_spread_by_tier": {"normal": 3.0},
            },
        }
    )

    assert settings["min_price"] == pytest.approx(2.0)
    assert settings["max_price"] == pytest.approx(300.0)
    assert settings["min_gain"] == pytest.approx(0.5)
    assert settings["catalyst_min_gain"] == pytest.approx(-2.0)
    assert settings["min_rel_vol"] == pytest.approx(0.75)
    assert settings["catalyst_min_rel_vol"] == pytest.approx(0.40)
    assert settings["max_symbols"] == 50
    assert settings["require_above_vwap"] is False


def test_aggressive_dynamic_entry_accepts_market_vwap_failure(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    bars = pd.DataFrame(
        {
            "high": [10.0, 10.2, 10.4, 10.5, 10.7, 10.8],
            "low": [9.8, 10.0, 10.1, 10.2, 10.4, 10.5],
            "open": [9.9, 10.1, 10.2, 10.3, 10.5, 10.6],
            "close": [10.0, 10.2, 10.3, 10.4, 10.6, 10.7],
            "volume": [10_000, 20_000, 30_000, 40_000, 50_000, 60_000],
        }
    )

    ok, reason = du.dynamic_momentum_entry_passes(
        gain_pct=5.0,
        relative_volume=2.5,
        vwap_above=False,
        spread_pct=1.0,
        bars_1m=bars,
        bars_5m=bars,
        ref_price=10.7,
        cfg={"aggressive_mode": {"enabled": True, "normal_threshold": 60, "fast_lane_threshold": 50, "max_noncritical_failures": 3}},
        is_dynamic=True,
        news_score=3,
        catalyst_score=0.25,
    )

    assert ok is True
    assert "aggressive_dynamic" in reason
    assert "DYNAMIC_AGGRESSIVE_ENTRY_ACCEPT" in caplog.text


def test_aggressive_dynamic_entry_keeps_unstable_quote_hard_gate() -> None:
    bars = pd.DataFrame(
        {
            "high": [10.0, 10.2, 10.4, 10.5, 10.7, 10.8],
            "low": [9.8, 10.0, 10.1, 10.2, 10.4, 10.5],
            "open": [9.9, 10.1, 10.2, 10.3, 10.5, 10.6],
            "close": [10.0, 10.2, 10.3, 10.4, 10.6, 10.7],
            "volume": [10_000, 20_000, 30_000, 40_000, 50_000, 60_000],
        }
    )

    ok, reason = du.dynamic_momentum_entry_passes(
        gain_pct=8.0,
        relative_volume=5.0,
        vwap_above=True,
        spread_pct=1.0,
        bars_1m=bars,
        bars_5m=bars,
        ref_price=10.7,
        cfg={"aggressive_mode": {"enabled": True, "normal_threshold": 60, "fast_lane_threshold": 50}},
        is_dynamic=True,
        news_score=5,
        quote_unstable=True,
    )

    assert ok is False
    assert reason == "unstable_quote"
