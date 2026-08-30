"""Scan-only Alpaca options chain diagnostics for top dynamic stock candidates."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from src.brokers.alpaca_client import OptionContractCandidate
from src.options_selector import SelectedOptionContract, select_option_contract
from src.live.options_chain import log_options_disabled_non_paper, option_chain_for_underlying, options_runtime_enabled

log = logging.getLogger(__name__)

_PREMARKET_RANKINGS_PATH = Path("data") / "premarket" / "latest_rankings.json"
_PREMARKET_MIN_OPTIONS_SCORE = 7.0
_PREMARKET_CATALYST_PRIORITY = {
    "earnings": 4,
    "analyst": 3,
    "deal": 2,
    "ai": 1,
}
_CATALYST_TYPE_BONUS = {
    "earnings": 4.0,
    "guidance": 3.5,
    "fda": 3.5,
    "approval": 3.0,
    "deal": 2.75,
    "partnership": 2.5,
    "analyst": 2.0,
    "ai": 2.0,
    "sec_filing": 1.0,
}


@dataclass(frozen=True)
class OptionsScanResult:
    symbol: str
    right: str
    selected: SelectedOptionContract | None
    reason: str | None
    reason_codes: tuple[str, ...]
    chain_rows: int


def options_scan_only_active(config: Mapping[str, Any] | None) -> bool:
    opts = (config or {}).get("options") if isinstance(config, Mapping) else {}
    if not isinstance(opts, Mapping):
        return False
    return str(opts.get("mode") or "").strip().lower() in ("scan_only", "paper_only")


def _candidate_symbol(row: Any) -> str:
    return str(getattr(row, "symbol", None) or (row.get("symbol") if isinstance(row, Mapping) else "") or "").strip().upper()


def _candidate_spot(row: Any) -> float | None:
    raw = getattr(row, "price", None)
    if raw is None and isinstance(row, Mapping):
        raw = row.get("price")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _premarket_rankings_path(project_root: Path | None) -> Path | None:
    if project_root is None:
        return None
    return Path(project_root) / _PREMARKET_RANKINGS_PATH


def _candidate_float(raw: Mapping[str, Any], key: str) -> float:
    try:
        return float(raw.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _row_float(row: Any, *keys: str) -> float:
    for key in keys:
        raw = getattr(row, key, None)
        if raw is None and isinstance(row, Mapping):
            raw = row.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return 0.0


def _row_text(row: Any, *keys: str) -> str:
    for key in keys:
        raw = getattr(row, key, None)
        if raw is None and isinstance(row, Mapping):
            raw = row.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        return str(raw).strip().lower()
    return ""


def _options_catalyst_priority_score(row: Any) -> float:
    """Score a symbol-level options opportunity by news/event catalyst strength."""
    news = _row_float(row, "news_score", "dynamic_news_score", "catalyst_news_score")
    event = _row_float(row, "event_score", "event_strength")
    rank = _row_float(row, "rank_score", "score")
    catalyst_type = _row_text(row, "catalyst_type", "news_catalyst_type", "event_type", "source")
    type_bonus = _CATALYST_TYPE_BONUS.get(catalyst_type, 0.0)
    return max(news, event, rank) + (0.50 * news) + (0.35 * event) + type_bonus


def _load_premarket_options_candidates(
    project_root: Path | None,
    *,
    min_score: float = _PREMARKET_MIN_OPTIONS_SCORE,
) -> list[Any]:
    path = _premarket_rankings_path(project_root)
    if path is None or not path.exists():
        return []
    try:
        payload = json.loads(path.read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, Mapping):
        return []
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raw_items = payload.get("rankings")
    if not isinstance(raw_items, list):
        return []

    rows: list[tuple[int, float, str, Any]] = []
    seen: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            continue
        symbol = str(raw.get("symbol") or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        score = _candidate_float(raw, "score")
        if score < float(min_score):
            continue
        catalyst_type = str(raw.get("catalyst_type") or raw.get("source") or "").strip().lower()
        priority = _PREMARKET_CATALYST_PRIORITY.get(catalyst_type)
        if priority is None:
            continue
        seen.add(symbol)
        rows.append(
            (
                priority,
                score,
                symbol,
                SimpleNamespace(
                    symbol=symbol,
                    price=None,
                    score=score,
                    rank_score=score,
                    news_score=_candidate_float(raw, "news_score"),
                    event_score=_candidate_float(raw, "event_score"),
                    catalyst_type=catalyst_type,
                    source="premarket_rankings",
                ),
            )
        )
    rows.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [row for _, _, _, row in rows]


def _options_watchlist_rows(dynamic_candidates: Sequence[Any], premarket_candidates: Sequence[Any], top_n: int) -> list[Any]:
    cap = max(0, int(top_n))
    if cap <= 0:
        return []
    by_symbol: dict[str, Any] = {}
    for row in list(premarket_candidates or []) + list(dynamic_candidates or []):
        symbol = _candidate_symbol(row)
        if not symbol:
            continue
        current = by_symbol.get(symbol)
        if current is None or _options_catalyst_priority_score(row) > _options_catalyst_priority_score(current):
            by_symbol[symbol] = row
    ranked = sorted(
        by_symbol.values(),
        key=lambda row: (
            -_options_catalyst_priority_score(row),
            -_row_float(row, "rank_score", "score"),
            _candidate_symbol(row),
        ),
    )
    return ranked[:cap]


def _snapshot_spot(broker: Any, symbol: str) -> float | None:
    fn = getattr(broker, "get_snapshot", None)
    if fn is None:
        return None
    try:
        snap = fn(symbol)
    except Exception:
        return None
    if not isinstance(snap, Mapping):
        return None
    for key in ("price", "last", "last_price", "latest_price", "close"):
        try:
            value = float(snap.get(key) or 0.0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    try:
        bid = float(snap.get("bid") or 0.0)
        ask = float(snap.get("ask") or 0.0)
    except (TypeError, ValueError):
        return None
    if bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    return None


def _selected_reason_codes(selected: SelectedOptionContract, chain: Sequence[OptionContractCandidate]) -> tuple[str, ...]:
    oi_known = any(int(getattr(c, "open_interest", 0) or 0) > 0 for c in chain)
    codes = ["dte_ok", "spread_ok", "liquidity_ok", "atm_rank"]
    if oi_known:
        codes.append("open_interest_ok")
    else:
        codes.append("open_interest_unknown")
    return tuple(codes)


def _error_reason_codes(reason: str | None) -> tuple[str, ...]:
    text = str(reason or "").strip().lower()
    if not text:
        return ("unknown",)
    if "candidates is none" in text or "chain not passed" in text:
        return ("no_chain",)
    if "candidates is empty" in text or "0 rows" in text:
        return ("no_chain_rows",)
    if "no contracts in dte window" in text:
        return ("dte_window",)
    if "invalid option right" in text or "invalid" in text and "right" in text:
        return ("invalid_right",)
    if "underlying spot missing" in text or "non-positive" in text:
        return ("no_spot",)
    if "liquidity" in text:
        return ("liquidity",)
    if "spread" in text:
        return ("spread",)
    if "greeks" in text or "delta" in text:
        return ("delta",)
    return ("no_qualifying_contract",)


def scan_dynamic_candidates_option_chains(
    broker: Any,
    config: Mapping[str, Any] | None,
    dynamic_candidates: Sequence[Any],
    *,
    log_dt: datetime,
    top_n: int = 3,
    project_root: Path | None = None,
) -> list[OptionsScanResult]:
    """
    Scan top dynamic stock candidates for option contracts in read-only mode.

    No orders are placed. This only logs the best call and put candidates per symbol.
    """
    if not options_runtime_enabled(broker, dict(config or {})):
        log_options_disabled_non_paper()
        return []
    if not options_scan_only_active(config):
        return []
    premarket_candidates = _load_premarket_options_candidates(project_root)
    rows = _options_watchlist_rows(dynamic_candidates, premarket_candidates, top_n)
    if not rows:
        return []

    out: list[OptionsScanResult] = []
    as_of = log_dt.date()
    for row in rows:
        symbol = _candidate_symbol(row)
        if not symbol:
            continue
        spot = _candidate_spot(row)
        if spot is None:
            spot = _snapshot_spot(broker, symbol)
        if str(getattr(row, "source", "") or "") == "premarket_rankings":
            log.info(
                "OPTIONS_CANDIDATE_FROM_PREMARKET symbol=%s rank_score=%.2f catalyst_type=%s",
                symbol,
                float(getattr(row, "rank_score", 0.0) or 0.0),
                str(getattr(row, "catalyst_type", "") or ""),
            )
        log.info(
            "OPTIONS_CATALYST_PRIORITY symbol=%s score=%.2f news_score=%.2f event_score=%.2f catalyst_type=%s",
            symbol,
            _options_catalyst_priority_score(row),
            _row_float(row, "news_score", "dynamic_news_score", "catalyst_news_score"),
            _row_float(row, "event_score", "event_strength"),
            _row_text(row, "catalyst_type", "news_catalyst_type", "event_type", "source") or "none",
        )
        chain = option_chain_for_underlying(broker, dict(config or {}), symbol, log_dt)
        log.info(
            "OPTIONS_SCAN_START symbol=%s spot=%s chain_rows=%d mode=scan_only",
            symbol,
            "n/a" if spot is None else f"{spot:.2f}",
            len(chain),
        )
        for right in ("call", "put"):
            selected, err = select_option_contract(
                dict(config or {}),
                symbol,
                right,
                candidates=chain,
                underlying_spot=spot,
                as_of=as_of,
                signal=row,
            )
            if selected is None:
                codes = _error_reason_codes(err)
                log.info(
                    "OPTIONS_SCAN_RESULT symbol=%s right=%s selected=none chain_rows=%d reason_codes=%s reason=%s",
                    symbol,
                    right,
                    len(chain),
                    ",".join(codes),
                    err or "no qualifying contract",
                )
                out.append(
                    OptionsScanResult(
                        symbol=symbol,
                        right=right,
                        selected=None,
                        reason=err,
                        reason_codes=codes,
                        chain_rows=len(chain),
                    )
                )
                continue
            codes = _selected_reason_codes(selected, chain)
            dte = max(0, (selected.expiration - as_of).days)
            log.info(
                "OPTIONS_SCAN_RESULT symbol=%s right=%s selected=%s dte=%d bid=%.2f ask=%.2f spread_pct=%.2f volume=%d open_interest=%d reason_codes=%s chain_rows=%d",
                symbol,
                right,
                selected.symbol,
                dte,
                float(selected.bid),
                float(selected.ask),
                float(selected.spread_pct),
                int(selected.volume),
                int(selected.open_interest),
                ",".join(codes),
                len(chain),
            )
            out.append(
                OptionsScanResult(
                    symbol=symbol,
                    right=right,
                    selected=selected,
                    reason=None,
                    reason_codes=codes,
                    chain_rows=len(chain),
                )
            )
    return out
