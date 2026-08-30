"""Bounded adaptive sensitivity for dynamic stock entries."""
from __future__ import annotations

import json
import logging
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median
from typing import Any

log = logging.getLogger(__name__)

MODES = {"tight", "normal", "relaxed"}
MINOR_RULES = {
    "slightly_below_rvol",
    "slightly_above_vwap_distance",
    "marginal_ema_slope",
    "marginal_sector_confirmation",
    "entry_alignment",
}
HARD_RULE_TOKENS = (
    "daily_loss",
    "portfolio",
    "exposure",
    "position_limit",
    "stale",
    "bad_quote",
    "spread",
    "market_closed",
    "unsupported",
    "broker",
    "risk_off",
    "cooldown",
    "duplicate",
    "buying_power",
    "order_size",
)


@dataclass(frozen=True)
class AdaptiveSensitivityState:
    enabled: bool
    mode: str
    reason: str
    lookback_trading_days: int
    trades_per_day: float | None
    win_rate: float | None
    drawdown_pct: float | None
    relaxed_underperformance: bool
    normal_min_quality_score: float
    effective_quality_score: float
    effective_rvol: float
    max_vwap_distance_atr: float
    one_minor_rule_exception: bool
    relaxed_size_multiplier: float
    exception_size_multiplier: float


@dataclass(frozen=True)
class DynamicCandidateDecision:
    allowed: bool
    reason: str
    setup: str
    failed_minor_rule: str | None
    size_multiplier: float
    sensitivity_mode: str


FLEXIBLE_SETUP_MULTIPLIERS = {
    "vwap_reclaim": "vwap_reclaim_multiplier",
    "ema9_or_ema20_pullback": "ema_pullback_multiplier",
    "higher_low_continuation": "higher_low_multiplier",
    "consolidation_break": "consolidation_break_multiplier",
}


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def _cfg(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    c = config if isinstance(config, Mapping) else {}
    return c.get("adaptive_sensitivity") if isinstance(c.get("adaptive_sensitivity"), Mapping) else {}


def adaptive_enabled(config: Mapping[str, Any] | None) -> bool:
    cfg = _cfg(config)
    return bool(cfg.get("enabled", False))


def flexible_entries_cfg(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    c = config if isinstance(config, Mapping) else {}
    flex = c.get("flexible_entries") if isinstance(c.get("flexible_entries"), Mapping) else {}
    return flex


def flexible_entries_enabled(config: Mapping[str, Any] | None) -> bool:
    return bool(flexible_entries_cfg(config).get("enabled", False))


def dynamic_feature_readiness(
    *,
    bars_1m_count: int,
    bars_5m_count: int,
    vwap: Any,
    ema20: Any,
    ema50: Any,
    atr: Any,
    momentum_score: Any,
    trend_5m: Any,
    config: Mapping[str, Any] | None,
) -> dict[str, Any]:
    flex = flexible_entries_cfg(config)
    dq = flex.get("data_quality") if isinstance(flex.get("data_quality"), Mapping) else {}

    def ready(value: Any) -> bool:
        return _safe_float(value) is not None if not isinstance(value, bool) else True

    checks = {
        "vwap_ready": (not bool(dq.get("require_vwap", True))) or ready(vwap),
        "ema20_ready": (not bool(dq.get("require_short_ema", True))) or ready(ema20),
        "ema50_ready": (not bool(dq.get("require_short_ema", True))) or ready(ema50),
        "atr_ready": (not bool(dq.get("require_atr", True))) or ready(atr),
        "momentum_ready": ready(momentum_score),
        "trend_5m_ready": (not bool(dq.get("require_5m_trend", True))) or trend_5m is not None,
    }
    missing = [
        name.replace("_ready", "").replace("trend_5m", "trend_5m")
        for name, ok in checks.items()
        if not ok
    ]
    return {
        "bars_1m": int(bars_1m_count or 0),
        "bars_5m": int(bars_5m_count or 0),
        **checks,
        "missing_features": missing,
        "final_status": "ready" if not missing else "not_ready",
    }


def _target_min(cfg: Mapping[str, Any]) -> float:
    target = cfg.get("target_entries_per_day") if isinstance(cfg.get("target_entries_per_day"), Mapping) else {}
    return float(_safe_float(target.get("minimum"), 2.0) or 2.0)


def production_auto_apply_enabled(config: Mapping[str, Any] | None) -> bool:
    """Return whether adaptive relaxation may alter production filters."""

    c = config if isinstance(config, Mapping) else {}
    tc = c.get("trading_control") if isinstance(c.get("trading_control"), Mapping) else {}
    ar = tc.get("adaptive_relaxation") if isinstance(tc.get("adaptive_relaxation"), Mapping) else {}
    if "production_auto_apply" in ar:
        return bool(ar.get("production_auto_apply"))
    cfg = _cfg(config)
    if "production_auto_apply" in cfg:
        return bool(cfg.get("production_auto_apply"))
    return False


def _recent_metrics_ok(
    cfg: Mapping[str, Any],
    metrics: Mapping[str, Any] | None,
    context: Mapping[str, Any] | None,
    *,
    production_auto_apply: bool,
) -> tuple[str, str]:
    if not bool(cfg.get("enabled", False)):
        return "normal", "disabled"
    default = str(cfg.get("default_mode") or "normal").strip().lower()
    if default not in MODES:
        default = "normal"
    forced = str(cfg.get("force_mode") or "").strip().lower()
    if forced in MODES:
        return forced, "force_mode"
    m = metrics if isinstance(metrics, Mapping) else {}
    c = context if isinstance(context, Mapping) else {}
    obs = int(_safe_float(m.get("observations"), 0.0) or 0)
    min_obs = int(_safe_float(cfg.get("minimum_observations"), 5.0) or 5)
    if obs < min_obs:
        return default, "insufficient_observations"
    safety = cfg.get("safety") if isinstance(cfg.get("safety"), Mapping) else {}
    if bool(c.get("daily_loss_lockout")):
        return "tight", "daily_loss_lockout"
    if bool(c.get("data_quality_bad")):
        return "tight", "data_quality_bad"
    if bool(c.get("spread_liquidity_bad")):
        return "tight", "spread_liquidity_bad"
    if bool(safety.get("disable_in_risk_off_regime", True)) and str(c.get("market_regime") or "").lower() in {
        "risk_off",
        "bearish",
        "defensive",
    }:
        return "tight", "risk_off_regime"
    exposure = _safe_float(c.get("gross_exposure_pct"), 0.0) or 0.0
    exposure_cap = _safe_float(c.get("gross_exposure_cap_pct"), 100.0) or 100.0
    if exposure_cap > 0 and exposure >= exposure_cap:
        return "tight", "portfolio_exposure_cap"
    drawdown = abs(_safe_float(m.get("max_drawdown_pct"), 0.0) or 0.0)
    max_dd = abs(_safe_float(safety.get("max_rolling_drawdown_pct"), 2.0) or 2.0)
    if drawdown > max_dd:
        return "tight", "rolling_drawdown"
    loss_rate = _safe_float(m.get("loss_rate"), 0.0) or 0.0
    max_loss_rate = _safe_float(safety.get("max_relaxed_loss_rate"), 0.60) or 0.60
    if loss_rate > max_loss_rate:
        return "tight", "rolling_loss_rate"
    if bool(m.get("relaxed_underperformance")):
        return "normal", "relaxed_underperformance"
    trades_per_day = _safe_float(m.get("trades_per_day"), 0.0) or 0.0
    if trades_per_day < _target_min(cfg):
        production_context = str(c.get("environment") or c.get("mode") or "").lower() in {"live", "production"} or bool(c.get("production"))
        if production_context and not production_auto_apply:
            return "normal", "low_trade_frequency_informational_only"
        return "relaxed", "low_trade_frequency_safe_drawdown"
    return default, "target_frequency_met"


def resolve_adaptive_sensitivity(
    config: Mapping[str, Any] | None,
    *,
    metrics: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
    base_min_rvol: float | None = None,
) -> AdaptiveSensitivityState:
    cfg = _cfg(config)
    mode, reason = _recent_metrics_ok(
        cfg,
        metrics,
        context,
        production_auto_apply=production_auto_apply_enabled(config),
    )
    q = cfg.get("quality_score") if isinstance(cfg.get("quality_score"), Mapping) else {}
    rvol_cfg = cfg.get("rvol") if isinstance(cfg.get("rvol"), Mapping) else {}
    chase = cfg.get("no_chase") if isinstance(cfg.get("no_chase"), Mapping) else {}
    sizing = cfg.get("sizing") if isinstance(cfg.get("sizing"), Mapping) else {}
    normal_quality = _safe_float(q.get("normal_min_quality_score"), 80.0) or 80.0
    reduction = _safe_float(q.get("relaxed_reduction"), 5.0) or 5.0
    floor = _safe_float(q.get("absolute_floor"), 70.0) or 70.0
    effective_quality = normal_quality
    if mode == "relaxed":
        effective_quality = max(floor, normal_quality - reduction)
    elif mode == "tight":
        effective_quality = normal_quality + max(0.0, reduction)
    base_rvol = _safe_float(base_min_rvol, None)
    if base_rvol is None:
        base_rvol = _safe_float(rvol_cfg.get("normal_min"), None)
    if base_rvol is None:
        base_rvol = 1.5
    effective_rvol = float(base_rvol)
    if mode == "relaxed":
        large_cap = bool((context or {}).get("large_cap")) if isinstance(context, Mapping) else False
        relaxed_key = "relaxed_large_cap_min" if large_cap else "relaxed_other_min"
        relaxed_floor = _safe_float(rvol_cfg.get(relaxed_key), 1.50) or 1.50
        effective_rvol = min(effective_rvol, relaxed_floor)
    normal_atr = _safe_float(chase.get("normal_max_vwap_distance_atr"), 1.0) or 1.0
    relaxed_atr = _safe_float(chase.get("relaxed_max_vwap_distance_atr"), 1.25) or 1.25
    absolute_atr = _safe_float(chase.get("absolute_max_vwap_distance_atr"), 1.50) or 1.50
    max_vwap_atr = min(absolute_atr, relaxed_atr if mode == "relaxed" else normal_atr)
    one = cfg.get("one_minor_rule_exception") if isinstance(cfg.get("one_minor_rule_exception"), Mapping) else {}
    return AdaptiveSensitivityState(
        enabled=bool(cfg.get("enabled", False)),
        mode=mode,
        reason=reason,
        lookback_trading_days=int(_safe_float(cfg.get("lookback_trading_days"), 10.0) or 10),
        trades_per_day=_safe_float((metrics or {}).get("trades_per_day")) if isinstance(metrics, Mapping) else None,
        win_rate=_safe_float((metrics or {}).get("win_rate")) if isinstance(metrics, Mapping) else None,
        drawdown_pct=_safe_float((metrics or {}).get("max_drawdown_pct")) if isinstance(metrics, Mapping) else None,
        relaxed_underperformance=bool((metrics or {}).get("relaxed_underperformance")) if isinstance(metrics, Mapping) else False,
        normal_min_quality_score=normal_quality,
        effective_quality_score=effective_quality,
        effective_rvol=effective_rvol,
        max_vwap_distance_atr=max_vwap_atr,
        one_minor_rule_exception=bool(one.get("enabled", True)),
        relaxed_size_multiplier=_safe_float(sizing.get("relaxed_multiplier"), 0.50) or 0.50,
        exception_size_multiplier=_safe_float(sizing.get("exception_multiplier"), 0.35) or 0.35,
    )


def classify_setup(
    *,
    breakout: bool = False,
    higher_high: bool = False,
    strong_green: bool = False,
    orb: bool = False,
    price_above_vwap: bool = False,
    five_min_trend: bool = False,
    vwap_distance_atr: float | None = None,
    ema9_reclaim: bool = False,
) -> str:
    if breakout or higher_high or strong_green or orb:
        return "fresh_breakout"
    if price_above_vwap and five_min_trend and (vwap_distance_atr is None or vwap_distance_atr <= 0.35):
        return "vwap_reclaim"
    if ema9_reclaim and five_min_trend:
        return "ema9_or_ema20_pullback"
    if price_above_vwap and five_min_trend:
        return "higher_low_continuation"
    return "none"


def classify_flexible_setup(
    *,
    price_above_vwap: bool,
    five_min_trend: bool,
    vwap_distance_atr: float | None,
    momentum_score: float | None,
    volume_confirmation: bool,
    higher_low: bool = False,
    consolidation_break: bool = False,
) -> str:
    momentum_ok = momentum_score is not None and momentum_score > 0.0
    if price_above_vwap and five_min_trend and momentum_ok and (vwap_distance_atr is None or -0.15 <= vwap_distance_atr <= 0.50):
        return "vwap_reclaim"
    if five_min_trend and momentum_ok and volume_confirmation and (vwap_distance_atr is None or vwap_distance_atr <= 1.0):
        return "ema9_or_ema20_pullback"
    if five_min_trend and higher_low and volume_confirmation:
        return "higher_low_continuation"
    if consolidation_break and momentum_ok and volume_confirmation:
        return "consolidation_break"
    return "none"


def flexible_setup_enabled(config: Mapping[str, Any] | None, setup: str) -> bool:
    if setup == "fresh_breakout":
        return True
    flex = flexible_entries_cfg(config)
    setup_types = flex.get("setup_types") if isinstance(flex.get("setup_types"), Mapping) else {}
    key = "ema_pullback" if setup == "ema9_or_ema20_pullback" else setup
    return bool(flex.get("enabled", False)) and bool(setup_types.get(key, False))


def flexible_size_multiplier(config: Mapping[str, Any] | None, setup: str, *, minor_exception: bool = False) -> float:
    flex = flexible_entries_cfg(config)
    sizing = flex.get("sizing") if isinstance(flex.get("sizing"), Mapping) else {}
    if minor_exception:
        return _safe_float(sizing.get("minor_exception_multiplier"), 0.35) or 0.35
    key = FLEXIBLE_SETUP_MULTIPLIERS.get(setup)
    if key is None:
        return 1.0
    return _safe_float(sizing.get(key), 0.50) or 0.50


def approved_minor_rule(reason: str) -> str | None:
    text = str(reason or "").lower()
    if any(token in text for token in HARD_RULE_TOKENS):
        return None
    if "relative_volume" in text or "rvol" in text:
        return "slightly_below_rvol"
    if "vwap" in text and "distance" in text:
        return "slightly_above_vwap_distance"
    if "ema" in text and "slope" in text:
        return "marginal_ema_slope"
    if "sector" in text:
        return "marginal_sector_confirmation"
    if "need 5m breakout" in text or "entry_alignment" in text:
        return "entry_alignment"
    return None


def one_minor_rule_exception_allowed(
    state: AdaptiveSensitivityState,
    *,
    failed_rules: Sequence[str],
    hard_rules: Sequence[str] = (),
    quality_score: float | None = None,
) -> tuple[bool, str | None]:
    if not state.enabled or not state.one_minor_rule_exception:
        return False, None
    if state.mode != "relaxed":
        return False, None
    if hard_rules:
        return False, None
    minor = [rule for rule in failed_rules if rule in MINOR_RULES]
    if len(minor) != 1 or len(failed_rules) != 1:
        return False, minor[0] if minor else None
    if quality_score is not None and float(quality_score) < state.effective_quality_score:
        return False, minor[0]
    return True, minor[0]


def dynamic_size_multiplier(state: AdaptiveSensitivityState, *, exception: bool = False) -> float:
    if exception:
        return min(state.relaxed_size_multiplier, state.exception_size_multiplier)
    if state.mode == "relaxed":
        return min(1.0, state.relaxed_size_multiplier)
    return 1.0


def rank_dynamic_candidates(candidates: Sequence[Mapping[str, Any]], *, top_n: int) -> list[dict[str, Any]]:
    def score(row: Mapping[str, Any]) -> float:
        return (
            (_safe_float(row.get("quality_score"), 0.0) or 0.0)
            + (_safe_float(row.get("relative_strength"), _safe_float(row.get("day_gain_pct"), 0.0)) or 0.0)
            + (_safe_float(row.get("relative_volume"), 0.0) or 0.0) * 3.0
            + (5.0 if row.get("sector_confirmed") else 0.0)
            + (5.0 if row.get("spy_qqq_aligned") else 0.0)
            + (3.0 if row.get("spread_ok") else 0.0)
            - abs(_safe_float(row.get("vwap_distance_atr"), 0.0) or 0.0)
            + (_safe_float(row.get("catalyst_score"), 0.0) or 0.0) * 5.0
        )

    ranked = sorted((dict(row) for row in candidates), key=score, reverse=True)
    out = []
    for idx, row in enumerate(ranked[: max(0, int(top_n or 0))], start=1):
        row["adaptive_rank"] = idx
        row["adaptive_rank_score"] = score(row)
        out.append(row)
    return out


def load_recent_dynamic_metrics(
    *,
    data_dir: Path | str,
    user_id: str,
    lookback_trading_days: int,
) -> dict[str, Any]:
    root = Path(data_dir)
    files = sorted((root / "profitability_attribution" / "daily").glob(f"*_{{user}}.json".replace("{user}", user_id)))
    files = files[-max(1, int(lookback_trading_days or 10)) :]
    trades = 0
    wins = 0
    losses = 0
    pnls: list[float] = []
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        routes = payload.get("route_stats") if isinstance(payload.get("route_stats"), Mapping) else {}
        for route, row in routes.items():
            if "dynamic" not in str(route).lower() or not isinstance(row, Mapping):
                continue
            n = int(_safe_float(row.get("trades"), 0.0) or 0)
            trades += n
            wins += int(_safe_float(row.get("wins"), row.get("winning_trades", 0.0)) or 0)
            pnl = _safe_float(row.get("pnl"), _safe_float(row.get("realized_pnl"), 0.0)) or 0.0
            if n:
                pnls.append(pnl)
                if pnl < 0:
                    losses += 1
    observations = len(files)
    tpd = trades / observations if observations else 0.0
    win_rate = wins / trades if trades else None
    loss_rate = losses / len(pnls) if pnls else 0.0
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnls:
        cumulative += pnl
        peak = max(peak, cumulative)
        max_dd = min(max_dd, cumulative - peak)
    return {
        "observations": observations,
        "trades": trades,
        "trades_per_day": tpd,
        "win_rate": win_rate,
        "loss_rate": loss_rate,
        "max_drawdown_pct": abs(max_dd),
        "relaxed_underperformance": False,
    }


def build_dynamic_entry_baseline(
    *,
    data_dir: Path | str,
    user_id: str,
    lookback_trading_days: int = 20,
) -> dict[str, Any]:
    root = Path(data_dir)
    metric_dirs = sorted((root / "research_metrics").glob("20*"))[-max(1, lookback_trading_days) :]
    candidates = accepted = rejected = 0
    rejection_counts: Counter[str] = Counter()
    quality_scores: list[float] = []
    for day_dir in metric_dirs:
        funnel = day_dir / "dynamic_funnel_live.json"
        if funnel.exists():
            try:
                payload = json.loads(funnel.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {}
            scanner = payload.get("scanner") if isinstance(payload.get("scanner"), Mapping) else {}
            entry = payload.get("entry") if isinstance(payload.get("entry"), Mapping) else {}
            candidates += int(_safe_float(scanner.get("accepted"), 0.0) or 0)
            accepted += int(_safe_float(entry.get("passed"), 0.0) or 0)
            rejected += int(_safe_float(entry.get("failed"), 0.0) or 0)
            reasons = entry.get("reasons") if isinstance(entry.get("reasons"), Mapping) else {}
            rejection_counts.update({str(k): int(v) for k, v in reasons.items()})
        align = day_dir / "dynamic_entry_alignment.json"
        if align.exists():
            try:
                payload = json.loads(align.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {}
            for row in payload.get("events") or []:
                if isinstance(row, Mapping):
                    score = _safe_float(row.get("momentum_score"))
                    if score is not None:
                        quality_scores.append(score)
                    if row.get("raw_reason"):
                        rejection_counts[str(row.get("raw_reason")).split(":")[0]] += 1
                        rejected += 1
    metrics = load_recent_dynamic_metrics(data_dir=root, user_id=user_id, lookback_trading_days=lookback_trading_days)
    tpd = metrics.get("trades_per_day")
    return {
        "lookback_trading_days": len(metric_dirs),
        "candidates_evaluated": candidates if candidates else "unavailable",
        "entries_accepted": accepted if accepted else "unavailable",
        "entries_rejected": rejected if rejected else "unavailable",
        "acceptance_rate": (accepted / (accepted + rejected)) if (accepted + rejected) else "unavailable",
        "top_rejection_reasons": dict(rejection_counts.most_common(10)),
        "average_quality_score": mean(quality_scores) if quality_scores else "unavailable",
        "median_quality_score": median(quality_scores) if quality_scores else "unavailable",
        "trades_per_day": tpd if tpd is not None else "unavailable",
        "win_rate": metrics.get("win_rate") if metrics.get("win_rate") is not None else "unavailable",
        "profit_factor": "unavailable",
        "average_return": "unavailable",
        "max_drawdown": metrics.get("max_drawdown_pct", "unavailable"),
    }


def build_dynamic_entry_adaptive_report(
    *,
    data_dir: Path | str,
    user_id: str,
    report_date: str,
    config: Mapping[str, Any],
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    dme = dict(config.get("dynamic_momentum_entry") or {})
    dyn = config.get("dynamic_entry") if isinstance(config.get("dynamic_entry"), Mapping) else {}
    if isinstance(dyn.get("adaptive_sensitivity"), Mapping):
        dme["adaptive_sensitivity"] = dict(dyn["adaptive_sensitivity"])
    adaptive_cfg = dme.get("adaptive_sensitivity") if isinstance(dme.get("adaptive_sensitivity"), Mapping) else {}
    lookback = int(_safe_float(adaptive_cfg.get("lookback_trading_days"), 10.0) or 10)
    metrics = load_recent_dynamic_metrics(data_dir=data_dir, user_id=user_id, lookback_trading_days=lookback)
    state = resolve_adaptive_sensitivity(
        dme,
        metrics=metrics,
        context=context or {"market_regime": "normal"},
        base_min_rvol=_safe_float(dme.get("min_relative_volume")),
    )
    baseline = build_dynamic_entry_baseline(data_dir=data_dir, user_id=user_id, lookback_trading_days=20)
    target = adaptive_cfg.get("target_entries_per_day") if isinstance(adaptive_cfg.get("target_entries_per_day"), Mapping) else {}
    return {
        "date": report_date,
        "user": user_id,
        "baseline": baseline,
        "current_mode": state.mode,
        "reason_for_mode": state.reason,
        "effective_thresholds": {
            "quality_score": state.effective_quality_score,
            "relative_volume": state.effective_rvol,
            "max_vwap_distance_atr": state.max_vwap_distance_atr,
        },
        "trade_frequency_target": dict(target),
        "actual_trade_frequency": metrics.get("trades_per_day"),
        "normal_entries": "unavailable",
        "relaxed_entries": "unavailable",
        "one_rule_exceptions": "unavailable",
        "win_rate_by_mode": {"normal": metrics.get("win_rate"), "relaxed": "unavailable"},
        "profit_factor_by_mode": "unavailable",
        "mfe_mae_by_mode": "unavailable",
        "top_rejection_reasons": baseline.get("top_rejection_reasons"),
        "candidates_eligible_under_relaxed_mode": "available_when_row_level_candidate_history_exists",
        "safety_trigger_status": state.reason if state.mode != "relaxed" else "clear_for_relaxed",
        "backtest_comparison": {
            "current_baseline": "reported",
            "bounded_relaxed_mode": "framework_only",
            "one_minor_rule_exception_only": "framework_only",
            "pullback_entries_only": "framework_only",
            "combined_adaptive": "framework_only",
        },
        "data_limitations": [
            "recent live attribution lacks complete row-level dynamic entry MFE/MAE and exit-return fields",
            "relaxed-vs-normal performance comparison remains disabled until attributed relaxed entries exist",
        ],
        "config_line": render_adaptive_config(state),
    }


def render_dynamic_entry_adaptive_report(report: Mapping[str, Any]) -> str:
    baseline = report.get("baseline") if isinstance(report.get("baseline"), Mapping) else {}
    thresholds = report.get("effective_thresholds") if isinstance(report.get("effective_thresholds"), Mapping) else {}
    lines = [
        f"Dynamic Entry Adaptive Report {report.get('date')} user={report.get('user')}",
        "",
        "DYNAMIC_ENTRY_BASELINE "
        + " ".join(f"{key}={value}" for key, value in baseline.items()),
        str(report.get("config_line") or ""),
        f"current_mode={report.get('current_mode')}",
        f"reason_for_mode={report.get('reason_for_mode')}",
        f"effective_quality_threshold={thresholds.get('quality_score')}",
        f"effective_rvol_threshold={thresholds.get('relative_volume')}",
        f"effective_max_vwap_distance_atr={thresholds.get('max_vwap_distance_atr')}",
        f"trade_frequency_target={report.get('trade_frequency_target')}",
        f"actual_trade_frequency={report.get('actual_trade_frequency')}",
        f"normal_entries={report.get('normal_entries')}",
        f"relaxed_entries={report.get('relaxed_entries')}",
        f"one_rule_exceptions={report.get('one_rule_exceptions')}",
        f"win_rate_by_mode={report.get('win_rate_by_mode')}",
        f"profit_factor_by_mode={report.get('profit_factor_by_mode')}",
        f"MFE_MAE_by_mode={report.get('mfe_mae_by_mode')}",
        f"top_rejection_reasons={report.get('top_rejection_reasons')}",
        f"candidates_eligible_under_relaxed_mode={report.get('candidates_eligible_under_relaxed_mode')}",
        f"safety_trigger_status={report.get('safety_trigger_status')}",
        f"backtest_comparison={report.get('backtest_comparison')}",
        f"data_limitations={report.get('data_limitations')}",
        "",
    ]
    return "\n".join(lines)


def write_dynamic_entry_adaptive_report(
    *,
    data_dir: Path | str,
    user_id: str,
    report_date: str,
    config: Mapping[str, Any],
    context: Mapping[str, Any] | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    report = build_dynamic_entry_adaptive_report(
        data_dir=data_dir,
        user_id=user_id,
        report_date=report_date,
        config=config,
        context=context,
    )
    out_dir = Path(data_dir) / "research_metrics" / report_date
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "dynamic_entry_adaptive.json"
    text_path = out_dir / "dynamic_entry_adaptive.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    text_path.write_text(render_dynamic_entry_adaptive_report(report), encoding="utf-8")
    return json_path, text_path, report


def render_adaptive_config(state: AdaptiveSensitivityState) -> str:
    return (
        "DYNAMIC_ENTRY_ADAPTIVE_CONFIG enabled=%s mode=%s quality_threshold=%.2f "
        "rvol_threshold=%.3f max_vwap_distance_atr=%.2f one_minor_rule_exception=%s "
        "relaxed_size_multiplier=%.2f final_status=%s"
        % (
            str(state.enabled).lower(),
            state.mode,
            state.effective_quality_score,
            state.effective_rvol,
            state.max_vwap_distance_atr,
            str(state.one_minor_rule_exception).lower(),
            state.relaxed_size_multiplier,
            "active" if state.enabled else "inactive",
        )
    )
