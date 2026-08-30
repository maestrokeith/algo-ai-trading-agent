"""Medium-aggressive portfolio allocation profile helpers."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from typing import Any

log = logging.getLogger(__name__)

CORE_STOCK_SYMBOLS: frozenset[str] = frozenset(
    {
        "AAPL",
        "MSFT",
        "NVDA",
        "AMZN",
        "GOOGL",
        "META",
        "AVGO",
        "AMD",
        "CRWD",
        "PLTR",
        "ORCL",
        "TSM",
        "MU",
        "ARM",
        "ANET",
        "MRVL",
        "SMCI",
        "LLY",
    }
)

LEVERAGED_DYNAMIC_ETFS: frozenset[str] = frozenset(
    {"SOXS", "SOXL", "TQQQ", "SQQQ", "TNA", "TZA"}
)


def _parse_pct_fraction(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, str):
        text = value.strip()
        pct = text.endswith("%")
        if pct:
            text = text[:-1].strip()
        try:
            val = float(text)
        except (TypeError, ValueError):
            return None
        if pct or val > 1.0 + 1e-9:
            val /= 100.0
        return max(0.0, min(1.0, val))
    try:
        val = float(value)
    except (TypeError, ValueError):
        return None
    if val > 1.0 + 1e-9:
        val /= 100.0
    return max(0.0, min(1.0, val))


def allocation_target_fractions(config: Mapping[str, Any] | None) -> dict[str, float]:
    """Return core/dynamic/cash targets as fractions of equity."""
    portfolio = (config or {}).get("portfolio") if isinstance(config, Mapping) else {}
    portfolio = portfolio if isinstance(portfolio, Mapping) else {}

    def _frac(key: str, default_pct: float) -> float:
        raw = portfolio.get(key, default_pct)
        parsed = _parse_pct_fraction(raw)
        if parsed is None:
            return default_pct / 100.0
        return max(0.0, min(1.0, float(parsed)))

    return {
        "core": _frac("target_core_stock_pct", 65.0),
        "dynamic": _frac("target_dynamic_pct", 25.0),
        "cash": _frac("target_cash_pct", 10.0),
    }


def log_allocation_targets(config: Mapping[str, Any] | None) -> None:
    targets = allocation_target_fractions(config)
    log.info(
        "ALLOCATION_TARGETS core=%d dynamic=%d cash=%d",
        round(targets["core"] * 100),
        round(targets["dynamic"] * 100),
        round(targets["cash"] * 100),
    )


def is_core_stock(symbol: Any) -> bool:
    return str(symbol or "").strip().upper() in CORE_STOCK_SYMBOLS


def is_excluded_dynamic_etf(symbol: Any) -> bool:
    return str(symbol or "").strip().upper() in LEVERAGED_DYNAMIC_ETFS


def is_dynamic_candidate(row: Mapping[str, Any] | None) -> bool:
    if not isinstance(row, Mapping):
        return False
    for key in ("dynamic_candidate", "dynamic_symbol", "is_dynamic"):
        val = row.get(key)
        if isinstance(val, str):
            if val.strip().lower() not in ("", "0", "false", "no", "off"):
                return True
        elif bool(val):
            return True
    src = str(row.get("source") or "").strip().lower()
    return src in {"dynamic_universe", "dynamic_momentum_override", "news_catalyst"}


def normalize_strategy_route(*values: Any) -> str:
    """Return a stable reporting bucket for known strategy route/source values."""
    text = ""
    for value in values:
        if value is None:
            continue
        text = str(value).strip().lower()
        if text and text not in {"none", "null", "n/a", "unknown"}:
            break
    if not text or text in {"none", "null", "n/a"}:
        return "unknown"
    if text in {
        "dynamic",
        "dynamic_universe",
        "dynamic_momentum",
        "dynamic_momentum_entry",
        "dynamic_momentum_override",
        "news_catalyst",
        "premarket_catalyst_replay",
    }:
        return "dynamic_momentum"
    if text in {"core", "core_rebuild", "core_stock", "core_rebalance"}:
        return "core_rebuild"
    if text in {"trend", "trend_long", "trend_following"}:
        return "trend_long"
    return text


def tracked_row_is_dynamic(row: Mapping[str, Any] | None) -> bool:
    if not isinstance(row, Mapping):
        return False
    if is_dynamic_candidate(row):
        return True
    src = str(row.get("source") or row.get("entry_source") or "").strip().lower()
    return src in {"dynamic_universe", "dynamic_momentum_override", "news_catalyst"}


def _dynamic_quality_cfg(config: Mapping[str, Any] | None) -> dict[str, Any]:
    root = config if isinstance(config, Mapping) else {}
    portfolio = root.get("portfolio") if isinstance(root.get("portfolio"), Mapping) else {}
    cfg = portfolio.get("dynamic_quality") if isinstance(portfolio.get("dynamic_quality"), Mapping) else {}
    dme_cfg = (
        root.get("dynamic_momentum_entry")
        if isinstance(root.get("dynamic_momentum_entry"), Mapping)
        else {}
    )

    def _float(key: str, default: float) -> float:
        raw = cfg.get(key, default) if isinstance(cfg, Mapping) else default
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return float(default)
        return val if math.isfinite(val) else float(default)

    return {
        "enabled": bool(cfg.get("enabled", True)) if isinstance(cfg, Mapping) else True,
        "allow_event_news_fallback": (
            bool(cfg.get("allow_event_news_fallback", True)) if isinstance(cfg, Mapping) else True
        ),
        "allow_pure_momentum": bool(cfg.get("allow_pure_momentum", True)) if isinstance(cfg, Mapping) else True,
        "min_catalyst_score": _float("min_catalyst_score", 0.3),
        "min_event_score": _float("min_event_score", 3.0),
        "min_news_score": _float("min_news_score", 3.0),
        "pure_momentum_min_score": _float("pure_momentum_min_score", 35.0),
        "pure_momentum_min_rvol": _float("pure_momentum_min_rvol", 0.8),
        "pure_momentum_min_gain_pct": _float("pure_momentum_min_gain_pct", 3.0),
        "pure_momentum_max_spread_pct": _float("pure_momentum_max_spread_pct", 2.5),
        "allocator_allow_no_catalyst_if_scanner_selected": (
            bool(dme_cfg.get("allocator_allow_no_catalyst_if_scanner_selected", True))
            if isinstance(dme_cfg, Mapping)
            else True
        ),
        "scanner_selected_min_day_gain_pct": _config_float(
            dme_cfg,
            "min_day_gain_pct",
            _config_float(dme_cfg, "min_gain_pct", 2.0),
        ),
        "scanner_selected_min_relative_volume": _config_float(
            dme_cfg,
            "min_relative_volume",
            _config_float(dme_cfg, "min_rel_volume", 0.3),
        ),
    }


def _config_float(config: Mapping[str, Any] | None, key: str, default: float) -> float:
    if not isinstance(config, Mapping):
        return float(default)
    try:
        val = float(config.get(key, default) or default)
    except (TypeError, ValueError):
        return float(default)
    return val if math.isfinite(val) else float(default)


def _score(row: Mapping[str, Any], key: str) -> float:
    try:
        val = float(row.get(key, 0.0) or 0.0)
    except (TypeError, ValueError):
        val = 0.0
    return val if math.isfinite(val) else 0.0


def _allocation_profile_field(row: Mapping[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        val = row.get(key)
        if val is not None and str(val).strip() != "":
            return val
    return default


def _pure_momentum_route(row: Mapping[str, Any]) -> str:
    return str(
        _allocation_profile_field(row, "route", "source", "entry_route", "entry_source", default="")
    ).strip().lower()


def _pure_momentum_score(row: Mapping[str, Any]) -> float:
    return max(
        _score(row, "score"),
        _score(row, "signal_score"),
        _score(row, "dynamic_score"),
        _score(row, "strength_eff"),
    )


def _pure_momentum_rel_volume(row: Mapping[str, Any]) -> float | None:
    for key in ("relative_volume", "rel_volume", "rvol"):
        raw = row.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None
    return None


def _pure_momentum_gain_pct(row: Mapping[str, Any]) -> float | None:
    for key in ("gain_pct", "day_gain_pct"):
        raw = row.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None
    return None


def _quality_bool(row: Mapping[str, Any], *keys: str) -> bool:
    for key in keys:
        raw = row.get(key)
        if isinstance(raw, str):
            text = raw.strip().lower()
            if text in {"1", "true", "yes", "on", "y"}:
                return True
            if text in {"", "0", "false", "no", "off", "n", "none", "null"}:
                continue
        elif raw is not None and bool(raw):
            return True
    return False


def _effective_min_rel_volume_for_scanner_selected(
    row: Mapping[str, Any],
    default_min: float,
) -> float:
    for key in (
        "effective_min_rel_volume",
        "effective_min_relative_volume",
        "entry_effective_min_rel_volume",
        "entry_eval_effective_min_rel_volume",
        "scanner_effective_min_rel_volume",
        "scanner_effective_min_relative_volume",
    ):
        raw = row.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(val) and val > 0.0:
            return val
    return float(default_min)


def _scanner_selected_no_catalyst_ok(
    row: Mapping[str, Any],
    quality: Mapping[str, Any],
) -> bool:
    if not bool(quality.get("allocator_allow_no_catalyst_if_scanner_selected", False)):
        return False
    route = str(_allocation_profile_field(row, "route", "source", default="")).strip().lower()
    if route != "dynamic_momentum_override":
        return False
    if not _quality_bool(
        row,
        "scanner_selected",
        "dynamic_scanner_selected",
        "selected_by_dynamic_scanner",
        "dynamic_selected",
    ):
        return False
    rel = quality.get("pure_momentum_rel_volume")
    gain = quality.get("pure_momentum_gain_pct")
    if rel is None or gain is None:
        return False
    min_rel = _effective_min_rel_volume_for_scanner_selected(
        row,
        float(quality["scanner_selected_min_relative_volume"]),
    )
    if float(rel) < float(min_rel) - 1e-9:
        return False
    if float(gain) < float(quality["scanner_selected_min_day_gain_pct"]) - 1e-9:
        return False
    return _quality_bool(
        row,
        "vwap_above",
        "price_above_vwap",
        "scanner_vwap_above",
        "scanner_price_above_vwap",
        "entry_vwap_above",
        "entry_price_above_vwap",
        "entry_alignment_passed",
        "entry_alignment_ok",
        "alignment_passed",
        "alignment_ok",
    )


def _optional_quality_float(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return "%.3f" % float(value)
    except (TypeError, ValueError):
        return "n/a"


def _dynamic_quality_missing_fields(quality: Mapping[str, Any]) -> list[str]:
    missing: list[str] = []
    score = quality.get("pure_momentum_score")
    if score is None:
        missing.append("score")
    else:
        try:
            if float(score) <= 0.0:
                missing.append("score")
        except (TypeError, ValueError):
            missing.append("score")
    if quality.get("pure_momentum_rel_volume") is None:
        missing.append("rel")
    if quality.get("pure_momentum_gain_pct") is None:
        missing.append("gain")
    return missing


def _log_dynamic_allocator_input(sym: str, row: Mapping[str, Any], quality: Mapping[str, Any]) -> None:
    log.info(
        "DYNAMIC_ALLOCATOR_INPUT symbol=%s route=%s source=%s score=%.2f gain=%s rel=%s "
        "catalyst_score=%.2f news_score=%.2f event_score=%.2f",
        sym,
        str(_allocation_profile_field(row, "route", default="n/a")),
        str(_allocation_profile_field(row, "source", default="n/a")),
        float(quality["pure_momentum_score"]),
        _optional_quality_float(quality["pure_momentum_gain_pct"]),
        _optional_quality_float(quality["pure_momentum_rel_volume"]),
        float(quality["catalyst_score"]),
        float(quality["news_score"]),
        float(quality["event_score"]),
    )


def _log_dynamic_allocator_no_catalyst_reject(sym: str, quality: Mapping[str, Any]) -> None:
    missing_fields = _dynamic_quality_missing_fields(quality)
    if missing_fields:
        log.info(
            "DYNAMIC_ALLOCATOR_NO_CATALYST_REJECT symbol=%s missing_fields=%s",
            sym,
            ",".join(missing_fields),
        )
    log.info(
        "DYNAMIC_ALLOCATOR_NO_CATALYST_REJECT symbol=%s score=%.2f rel=%s gain=%s required_score=%.2f",
        sym,
        float(quality["pure_momentum_score"]),
        _optional_quality_float(quality["pure_momentum_rel_volume"]),
        _optional_quality_float(quality["pure_momentum_gain_pct"]),
        float(quality["pure_momentum_min_score"]),
    )


def _log_dynamic_allocator_pure_momentum_pass(sym: str, quality: Mapping[str, Any]) -> None:
    log.info(
        "DYNAMIC_ALLOCATOR_PURE_MOMENTUM_PASS symbol=%s score=%.2f rel=%s gain=%s",
        sym,
        float(quality["pure_momentum_score"]),
        _optional_quality_float(quality["pure_momentum_rel_volume"]),
        _optional_quality_float(quality["pure_momentum_gain_pct"]),
    )


def _log_dynamic_allocator_low_score_allowed(sym: str, quality: Mapping[str, Any]) -> None:
    if float(quality["pure_momentum_score"]) >= float(quality["pure_momentum_min_score"]):
        return
    log.info(
        "DYNAMIC_ALLOCATOR_LOW_SCORE_ALLOWED symbol=%s score=%.2f reason=scanner_selected",
        sym,
        float(quality["pure_momentum_score"]),
    )


def dynamic_quality_decision(
    row: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the dynamic catalyst-gate decision without changing the candidate row."""
    cfg = _dynamic_quality_cfg(config)
    news = _score(row, "news_score")
    event = _score(row, "event_score")
    catalyst = _score(row, "catalyst_score")
    if not bool(cfg["enabled"]):
        return {
            **cfg,
            "news_score": news,
            "event_score": event,
            "catalyst_score": catalyst,
            "passes": True,
            "path": "disabled",
            "reason": None,
        }
    if catalyst >= float(cfg["min_catalyst_score"]):
        path = "catalyst_score"
    elif bool(cfg["allow_event_news_fallback"]) and event >= float(cfg["min_event_score"]):
        path = "event_score"
    elif bool(cfg["allow_event_news_fallback"]) and news >= float(cfg["min_news_score"]):
        path = "news_score"
    else:
        path = "none"
    pure_route = _pure_momentum_route(row)
    pure_score = _pure_momentum_score(row)
    pure_rel = _pure_momentum_rel_volume(row)
    pure_gain = _pure_momentum_gain_pct(row)
    pure_route_ok = pure_route in {"dynamic_momentum", "dynamic_momentum_override"}
    pure_score_ok = pure_score >= float(cfg["pure_momentum_min_score"])
    pure_rel_ok = pure_rel is not None and pure_rel >= float(cfg["pure_momentum_min_rvol"])
    pure_gain_ok = pure_gain is not None and pure_gain >= float(cfg["pure_momentum_min_gain_pct"])
    pure_momentum_ok = bool(
        path == "none"
        and cfg["allow_pure_momentum"]
        and pure_route_ok
        and pure_score_ok
        and pure_rel_ok
        and pure_gain_ok
    )
    if pure_momentum_ok:
        path = "pure_momentum"
    scanner_quality = {
        **cfg,
        "pure_momentum_score": pure_score,
        "pure_momentum_rel_volume": pure_rel,
        "pure_momentum_gain_pct": pure_gain,
    }
    scanner_selected_ok = bool(
        path == "none" and _scanner_selected_no_catalyst_ok(row, scanner_quality)
    )
    if scanner_selected_ok:
        path = "scanner_selected"
    passes = path != "none"
    sym = str(row.get("symbol") or row.get("sym_u") or "UNKNOWN").strip().upper()
    if scanner_selected_ok:
        log.info(
            "DYNAMIC_ALLOCATOR_CATALYST_BYPASS symbol=%s reason=scanner_selected",
            sym,
        )
        _log_dynamic_allocator_low_score_allowed(sym, scanner_quality)
    elif path == "none" and pure_route == "dynamic_momentum_override":
        log.info(
            "DYNAMIC_ALLOCATOR_CATALYST_REQUIRED symbol=%s reason=no_catalyst",
            sym,
        )
    return {
        **cfg,
        "news_score": news,
        "event_score": event,
        "catalyst_score": catalyst,
        "pure_momentum_score": pure_score,
        "pure_momentum_rel_volume": pure_rel,
        "pure_momentum_gain_pct": pure_gain,
        "pure_momentum_route": pure_route,
        "pure_momentum_route_ok": pure_route_ok,
        "pure_momentum_score_ok": pure_score_ok,
        "pure_momentum_rel_volume_ok": pure_rel_ok,
        "pure_momentum_gain_pct_ok": pure_gain_ok,
        "scanner_selected_no_catalyst_ok": scanner_selected_ok,
        "passes": passes,
        "path": path,
        "reason": None if passes else "no_catalyst",
    }


def dynamic_quality_passes(row: Mapping[str, Any], config: Mapping[str, Any] | None = None) -> bool:
    """Dynamic candidates need an explicit news/event/catalyst reason."""
    return bool(dynamic_quality_decision(row, config=config)["passes"])


def dynamic_quality_reject_reason(row: Mapping[str, Any], config: Mapping[str, Any] | None = None) -> str | None:
    decision = dynamic_quality_decision(row, config=config)
    sym = str(row.get("symbol") or row.get("sym_u") or "UNKNOWN").strip().upper()
    _log_dynamic_allocator_input(sym, row, decision)
    if bool(decision["passes"]):
        if str(decision["path"]) == "pure_momentum":
            _log_dynamic_allocator_pure_momentum_pass(sym, decision)
        elif str(decision["path"]) == "scanner_selected":
            _log_dynamic_allocator_low_score_allowed(sym, decision)
        return None
    reason = str(decision["reason"] or "no_catalyst")
    if reason == "no_catalyst":
        _log_dynamic_allocator_no_catalyst_reject(sym, decision)
    return reason


def dynamic_position_value(
    portfolio: Sequence[Mapping[str, Any]],
    tracked: Mapping[str, Any] | None = None,
) -> float:
    total = 0.0
    tracked_map = tracked if isinstance(tracked, Mapping) else {}
    for row in portfolio:
        sym = str(row.get("symbol") or "").strip().upper()
        trow = tracked_map.get(sym) if isinstance(tracked_map, Mapping) else None
        if not (is_dynamic_candidate(row) or tracked_row_is_dynamic(trow if isinstance(trow, Mapping) else None)):
            continue
        try:
            total += max(0.0, float(row.get("value", 0.0) or 0.0))
        except (TypeError, ValueError):
            continue
    return total


def dynamic_position_count(
    portfolio: Sequence[Mapping[str, Any]],
    tracked: Mapping[str, Any] | None = None,
) -> int:
    tracked_map = tracked if isinstance(tracked, Mapping) else {}
    count = 0
    for row in portfolio:
        sym = str(row.get("symbol") or "").strip().upper()
        trow = tracked_map.get(sym) if isinstance(tracked_map, Mapping) else None
        if is_dynamic_candidate(row) or tracked_row_is_dynamic(trow if isinstance(trow, Mapping) else None):
            count += 1
    return count


def dynamic_lockout_reason(engine: Any, equity: float) -> str | None:
    """Return the reason new dynamic buys should be locked out for today, if any."""
    state = getattr(engine, "dynamic_risk_state", None)
    state = state if isinstance(state, Mapping) else {}

    stop_count = None
    for key in ("dynamic_stop_loss_count_today", "dynamic_stop_loss_count", "stop_loss_count"):
        raw = getattr(engine, key, state.get(key, None))
        if raw is None:
            continue
        try:
            stop_count = int(float(raw))
            break
        except (TypeError, ValueError):
            continue
    if stop_count is not None and stop_count >= 2:
        return "stop_loss_count"

    realized_loss = None
    for key in (
        "dynamic_realized_loss_today",
        "dynamic_realized_loss",
        "realized_dynamic_loss_today",
    ):
        raw = getattr(engine, key, state.get(key, None))
        if raw is None:
            continue
        try:
            realized_loss = max(0.0, float(raw))
            break
        except (TypeError, ValueError):
            continue
    if realized_loss is None:
        for key in ("dynamic_realized_pnl_today", "dynamic_realized_pnl"):
            raw = getattr(engine, key, state.get(key, None))
            if raw is None:
                continue
            try:
                pnl = float(raw)
            except (TypeError, ValueError):
                continue
            realized_loss = max(0.0, -pnl)
            break
    try:
        eq = max(0.0, float(equity))
    except (TypeError, ValueError):
        eq = 0.0
    if realized_loss is not None and eq > 0.0 and realized_loss >= eq * 0.015 - 1e-9:
        return "realized_loss_limit"
    return None


def filter_allocator_candidates_for_profile(
    candidates: Sequence[Mapping[str, Any]],
    *,
    config: Mapping[str, Any] | None,
    portfolio: Sequence[Mapping[str, Any]] | None = None,
    tracked: Mapping[str, Any] | None = None,
    equity: float = 0.0,
    engine: Any = None,
) -> list[dict[str, Any]]:
    """Apply medium-aggressive quality filters and core preference to allocator candidates."""
    targets = allocation_target_fractions(config)
    try:
        eq = max(0.0, float(equity))
    except (TypeError, ValueError):
        eq = 0.0
    port = list(portfolio or [])
    core_value = 0.0
    for row in port:
        if is_core_stock(row.get("symbol")):
            try:
                core_value += max(0.0, float(row.get("value", 0.0) or 0.0))
            except (TypeError, ValueError):
                pass
    core_underweight = eq > 0.0 and core_value < eq * targets["core"] - 1e-9
    dyn_lockout = dynamic_lockout_reason(engine, eq) if engine is not None else None
    if dyn_lockout is not None:
        log.info("DYNAMIC_LOCKOUT reason=%s", dyn_lockout)
    dyn_quality_cfg = _dynamic_quality_cfg(config)
    log.info(
        "DYNAMIC_QUALITY_THRESHOLDS required_catalyst_score=%.2f required_event_score=%.2f "
        "required_news_score=%.2f allow_event_news_fallback=%s enabled=%s",
        float(dyn_quality_cfg["min_catalyst_score"]),
        float(dyn_quality_cfg["min_event_score"]),
        float(dyn_quality_cfg["min_news_score"]),
        str(bool(dyn_quality_cfg["allow_event_news_fallback"])).lower(),
        str(bool(dyn_quality_cfg["enabled"])).lower(),
    )

    out: list[dict[str, Any]] = []
    for src in candidates:
        row = dict(src)
        sym = str(row.get("symbol") or "").strip().upper()
        if not sym:
            continue
        if sym in LEVERAGED_DYNAMIC_ETFS:
            log.info("ALLOCATOR_SKIP_ETF symbol=%s reason=etf_excluded", sym)
            continue
        if is_dynamic_candidate(row):
            if dyn_lockout is not None:
                continue
            log.info(
                "DYNAMIC_ALLOCATOR_INPUT symbol=%s route=%s source=%s score=%.2f gain=%s rel=%s "
                "catalyst_score=%.2f news_score=%.2f event_score=%.2f",
                sym,
                str(_allocation_profile_field(row, "route", default="n/a")),
                str(_allocation_profile_field(row, "source", default="n/a")),
                float(_pure_momentum_score(row)),
                (
                    "n/a"
                    if _pure_momentum_gain_pct(row) is None
                    else "%.3f" % float(_pure_momentum_gain_pct(row) or 0.0)
                ),
                (
                    "n/a"
                    if _pure_momentum_rel_volume(row) is None
                    else "%.3f" % float(_pure_momentum_rel_volume(row) or 0.0)
                ),
                _score(row, "catalyst_score"),
                _score(row, "news_score"),
                _score(row, "event_score"),
            )
            quality = dynamic_quality_decision(row, config=config)
            reason = None if bool(quality["passes"]) else str(quality["reason"] or "no_catalyst")
            if reason is not None:
                if reason == "no_catalyst":
                    missing_fields = [
                        name
                        for name, value in (
                            ("score", quality["pure_momentum_score"]),
                            ("rel", quality["pure_momentum_rel_volume"]),
                            ("gain", quality["pure_momentum_gain_pct"]),
                        )
                        if value is None or (name == "score" and float(value) <= 0.0)
                    ]
                    if missing_fields:
                        log.info(
                            "DYNAMIC_ALLOCATOR_NO_CATALYST_REJECT symbol=%s missing_fields=%s",
                            sym,
                            ",".join(missing_fields),
                        )
                    log.info(
                        "DYNAMIC_ALLOCATOR_NO_CATALYST_REJECT symbol=%s score=%.2f rel=%s gain=%s required_score=%.2f",
                        sym,
                        float(quality["pure_momentum_score"]),
                        (
                            "n/a"
                            if quality["pure_momentum_rel_volume"] is None
                            else "%.3f" % float(quality["pure_momentum_rel_volume"])
                        ),
                        (
                            "n/a"
                            if quality["pure_momentum_gain_pct"] is None
                            else "%.3f" % float(quality["pure_momentum_gain_pct"])
                        ),
                        float(quality["pure_momentum_min_score"]),
                    )
                    log.info(
                        "ALLOCATION_PROFILE_NO_CATALYST symbol=%s route=%s dynamic_score=%.2f "
                        "news_score=%.2f event_score=%.2f catalyst_score=%.2f catalyst_type=%s "
                        "catalyst_age_minutes=%s require_catalyst=%s config_key=%s threshold_keys=%s",
                        sym,
                        str(_allocation_profile_field(row, "route", "source", default="n/a")),
                        _score(row, "score"),
                        float(quality["news_score"]),
                        float(quality["event_score"]),
                        float(quality["catalyst_score"]),
                        str(_allocation_profile_field(row, "catalyst_type", default="none")),
                        str(_allocation_profile_field(row, "catalyst_age_minutes", "age_minutes", default="n/a")),
                        str(bool(quality["enabled"])).lower(),
                        "portfolio.dynamic_quality.enabled",
                        "portfolio.dynamic_quality.min_catalyst_score,portfolio.dynamic_quality.min_event_score,portfolio.dynamic_quality.min_news_score",
                    )
                log.info(
                    "DYNAMIC_REJECT symbol=%s reason=%s catalyst_score=%.2f event_score=%.2f news_score=%.2f "
                    "required_catalyst_score=%.2f required_event_score=%.2f required_news_score=%.2f catalyst_path=%s",
                    sym,
                    reason,
                    float(quality["catalyst_score"]),
                    float(quality["event_score"]),
                    float(quality["news_score"]),
                    float(quality["min_catalyst_score"]),
                    float(quality["min_event_score"]),
                    float(quality["min_news_score"]),
                    str(quality["path"]),
                )
                continue
            if str(quality["path"]) == "pure_momentum":
                log.info(
                    "DYNAMIC_ALLOCATOR_PURE_MOMENTUM_PASS symbol=%s score=%.2f rel=%s gain=%s",
                    sym,
                    float(quality["pure_momentum_score"]),
                    (
                        "n/a"
                        if quality["pure_momentum_rel_volume"] is None
                        else "%.3f" % float(quality["pure_momentum_rel_volume"])
                    ),
                    (
                        "n/a"
                        if quality["pure_momentum_gain_pct"] is None
                        else "%.3f" % float(quality["pure_momentum_gain_pct"])
                    ),
                )
            log.info(
                "DYNAMIC_QUALITY_PASS symbol=%s catalyst_score=%.2f event_score=%.2f news_score=%.2f "
                "required_catalyst_score=%.2f required_event_score=%.2f required_news_score=%.2f catalyst_path=%s",
                sym,
                float(quality["catalyst_score"]),
                float(quality["event_score"]),
                float(quality["news_score"]),
                float(quality["min_catalyst_score"]),
                float(quality["min_event_score"]),
                float(quality["min_news_score"]),
                str(quality["path"]),
            )
        if core_underweight and is_core_stock(sym):
            try:
                row["score"] = float(row.get("score", 0.0) or 0.0) * 1.20
            except (TypeError, ValueError):
                row["score"] = 0.0
            row["core_bucket_priority"] = True
        out.append(row)
    return out


def deployable_cash_after_reserve(
    *,
    cash: float,
    equity: float,
    config: Mapping[str, Any] | None,
) -> float:
    targets = allocation_target_fractions(config)
    try:
        c = max(0.0, float(cash))
        eq = max(0.0, float(equity))
    except (TypeError, ValueError):
        return 0.0
    reserve = eq * targets["cash"]
    deployable = max(0.0, c - reserve)
    if c > 0.0 and deployable <= 1e-9:
        log.info("CASH_RESERVE_BLOCKED reason=target_cash_pct")
    return deployable


def clip_actions_for_allocation_profile(
    actions: Sequence[Mapping[str, Any]],
    *,
    candidates: Sequence[Mapping[str, Any]],
    portfolio: Sequence[Mapping[str, Any]],
    tracked: Mapping[str, Any] | None,
    equity: float,
    config: Mapping[str, Any] | None,
    min_realloc_leg: float,
) -> list[dict[str, Any]]:
    """Cap new dynamic buys to sleeve, per-name, and concurrent-position limits."""
    if not actions:
        return []
    cand_by_symbol = {
        str(c.get("symbol") or "").strip().upper(): c
        for c in candidates
        if str(c.get("symbol") or "").strip()
    }
    targets = allocation_target_fractions(config)
    try:
        eq = max(0.0, float(equity))
    except (TypeError, ValueError):
        eq = 0.0
    dyn_headroom = max(0.0, eq * targets["dynamic"] - dynamic_position_value(portfolio, tracked))
    dyn_slots = max(0, 6 - dynamic_position_count(portfolio, tracked))
    single_dyn_cap = eq * 0.04 if eq > 0.0 else 0.0
    mleg = max(0.0, float(min_realloc_leg or 0.0))
    out: list[dict[str, Any]] = []

    def _single_order_cap() -> float:
        if not isinstance(config, Mapping):
            return 0.0
        portfolio_cfg = config.get("portfolio")
        if not isinstance(portfolio_cfg, Mapping):
            return 0.0
        allocator_cfg = portfolio_cfg.get("capital_allocator")
        if not isinstance(allocator_cfg, Mapping):
            return 0.0
        ceiling: float | None = None
        pct_raw = allocator_cfg.get("max_single_order_notional_pct")
        if pct_raw is not None and str(pct_raw).strip() != "":
            try:
                pct = float(pct_raw)
                if pct > 1.0 + 1e-9:
                    pct /= 100.0
                pct = max(0.0, pct)
                if pct > 0.0 and eq > 0.0:
                    ceiling = eq * pct
            except (TypeError, ValueError):
                pass
        abs_raw = allocator_cfg.get("max_single_order_notional")
        if abs_raw is not None and str(abs_raw).strip() != "":
            try:
                abs_cap = max(0.0, float(abs_raw))
                if abs_cap > 0.0:
                    ceiling = abs_cap if ceiling is None else min(ceiling, abs_cap)
            except (TypeError, ValueError):
                pass
        return float(ceiling or 0.0)

    single_order_cap = _single_order_cap()

    def _log_clip(
        *,
        row: Mapping[str, Any],
        cand: Mapping[str, Any],
        profile_rule: str,
        profile_threshold: float,
        rejection_reason: str,
    ) -> None:
        log.info(
            "ALLOCATION_PROFILE_CLIP_REASON symbol=%s action=%s route=%s is_dynamic=%s "
            "catalyst_score=%.2f event_score=%.2f news_score=%.2f profile_rule=%s "
            "profile_threshold=%.2f rejection_reason=%s",
            str(row.get("symbol") or "").strip().upper() or "?",
            str(row.get("action") or "").strip().lower() or "?",
            str(row.get("route") or row.get("source") or cand.get("route") or cand.get("source") or "n/a"),
            str(is_dynamic_candidate(cand)).lower(),
            _score(cand, "catalyst_score"),
            _score(cand, "event_score"),
            _score(cand, "news_score"),
            str(profile_rule),
            float(profile_threshold),
            str(rejection_reason),
        )

    for src in actions:
        row = dict(src)
        side = str(row.get("action") or "").strip().lower()
        sym = str(row.get("symbol") or "").strip().upper()
        cand = cand_by_symbol.get(sym, {})
        if side != "buy" or not is_dynamic_candidate(cand):
            out.append(row)
            continue
        if dyn_slots <= 0 or dyn_headroom <= 1e-9 or single_dyn_cap <= 1e-9:
            if dyn_slots <= 0:
                _log_clip(
                    row=row,
                    cand=cand,
                    profile_rule="dynamic_position_slots",
                    profile_threshold=0.0,
                    rejection_reason="dynamic_position_limit",
                )
            elif dyn_headroom <= 1e-9:
                _log_clip(
                    row=row,
                    cand=cand,
                    profile_rule="dynamic_sleeve_headroom",
                    profile_threshold=eq * targets["dynamic"],
                    rejection_reason="dynamic_sleeve_cap",
                )
            else:
                _log_clip(
                    row=row,
                    cand=cand,
                    profile_rule="single_dynamic_notional_cap",
                    profile_threshold=single_dyn_cap,
                    rejection_reason="single_dynamic_cap_zero",
                )
            continue
        try:
            n = max(0.0, float(row.get("notional", 0.0) or 0.0))
        except (TypeError, ValueError):
            _log_clip(
                row=row,
                cand=cand,
                profile_rule="action_notional",
                profile_threshold=0.0,
                rejection_reason="invalid_notional",
            )
            continue
        requested_notional = n
        dynamic_cap = min(single_dyn_cap, dyn_headroom)
        raw_clipped_notional = min(requested_notional, dynamic_cap)
        cap_floor_applied = (
            requested_notional > mleg + 1e-9
            and raw_clipped_notional < mleg - 1e-9
            and single_dyn_cap < mleg - 1e-9
            and dyn_headroom + 1e-9 >= mleg
            and single_dyn_cap <= dyn_headroom + 1e-9
        )
        n = mleg if cap_floor_applied else raw_clipped_notional
        log.info(
            "ALLOCATION_PROFILE_CLIP_DEBUG symbol=%s action=%s route=%s is_dynamic=%s "
            "requested_notional=%.2f clipped_notional=%.2f raw_clipped_notional=%.2f "
            "min_realloc_leg=%.2f dynamic_cap=%.2f single_dynamic_cap=%.2f "
            "single_order_cap=%.2f gross_headroom=%.2f dynamic_sleeve_headroom=%.2f "
            "final_post_planner_notional=%.2f cap_floor_applied=%s",
            sym or "?",
            side or "?",
            str(row.get("route") or row.get("source") or cand.get("route") or cand.get("source") or "n/a"),
            str(is_dynamic_candidate(cand)).lower(),
            requested_notional,
            n,
            raw_clipped_notional,
            mleg,
            dynamic_cap,
            single_dyn_cap,
            single_order_cap,
            dyn_headroom,
            dyn_headroom,
            n if n >= mleg - 1e-9 else 0.0,
            str(cap_floor_applied).lower(),
        )
        if n < mleg - 1e-9:
            rule = "min_realloc_leg_after_single_dynamic_cap"
            threshold = mleg
            if dyn_headroom < single_dyn_cap - 1e-9:
                rule = "min_realloc_leg_after_dynamic_sleeve_headroom"
                threshold = dyn_headroom
            _log_clip(
                row=row,
                cand=cand,
                profile_rule=rule,
                profile_threshold=threshold,
                rejection_reason="clipped_notional_below_min_realloc_leg",
            )
            continue
        row["notional"] = n
        out.append(row)
        dyn_headroom -= n
        dyn_slots -= 1
    return out


def dynamic_spread_cap_pct(row: Mapping[str, Any] | None) -> float:
    if not isinstance(row, Mapping):
        return 2.5
    vals = []
    for key in ("score", "news_score", "event_score"):
        try:
            vals.append(float(row.get(key, 0.0) or 0.0))
        except (TypeError, ValueError):
            vals.append(0.0)
    try:
        vals.append(float(row.get("catalyst_score", 0.0) or 0.0) * 10.0)
    except (TypeError, ValueError):
        vals.append(0.0)
    return 5.0 if max(vals or [0.0]) >= 8.0 else 2.5
