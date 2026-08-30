"""Pre-filter universe symbols using ScoringEngine (config ``scoring`` section)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pandas as pd

from src.portfolio_allocation import is_high_cash_deploy
from src.scoring_engine import ScoreBreakdown, ScoringEngine

logger = logging.getLogger(__name__)


def scoring_max_bucket_n(work_scoring: dict[str, Any]) -> int:
    """
    Effective scored-symbol bucket size.

    Prefers ``top_n_candidates`` over legacy ``max_candidates`` (same role: top-N / cap for
    ``selection_mode``). Returns ``0`` when unset/invalid (caller may treat as disabled slice).
    """
    for key in ("top_n_candidates", "max_candidates"):
        raw = work_scoring.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            continue
    return 5


def merge_scoring_config_for_cash(
    config: dict[str, Any],
    *,
    account_cash: float | None,
    account_equity: float,
) -> dict[str, Any]:
    """
    Return the active ``scoring`` dict for this pass (no ``when_high_cash`` key).

    When :func:`is_high_cash_deploy` is true, overlays ``scoring.when_high_cash`` fields
    ``min_score``, ``top_n_candidates``, ``max_candidates``, and ``selection_mode`` when present.
    """
    base = dict(config.get("scoring") or {})
    out = {k: v for k, v in base.items() if k != "when_high_cash"}
    if not is_high_cash_deploy(config, cash=account_cash, equity=float(account_equity)):
        return out
    wh = base.get("when_high_cash")
    if not isinstance(wh, dict):
        return out
    for key in ("min_score", "top_n_candidates", "max_candidates", "selection_mode"):
        if key not in wh:
            continue
        val = wh[key]
        if val is None:
            continue
        if isinstance(val, str) and not str(val).strip():
            continue
        out[key] = val
    return out


def build_scoring_allowlist_from_ranked(
    ranked: Sequence[tuple[str, float, Any]],
    *,
    min_score: float,
    max_candidates: int,
    selection_mode: str = "top_n",
) -> frozenset[str]:
    """
    Build the allowlist from a score-sorted list (highest ``total`` first).

    * ``top_n`` — consider only the first ``max_candidates`` rows, keep those with
      ``total >= min_score`` (legacy behavior).
    * ``threshold`` — keep every row with ``total >= min_score``, in rank order,
      up to ``max_candidates`` symbols.
    * ``ranked_top_n`` — the top ``max_candidates`` symbols by score only; ``min_score`` is ignored
      (use for a fixed-size ranked slice each scan, e.g. 10–15 names).
    """
    mode = (selection_mode or "top_n").strip().lower()
    max_c = max(0, int(max_candidates))
    if max_c == 0:
        return frozenset()
    ms = float(min_score)
    if mode == "ranked_top_n":
        return frozenset(sym for sym, _, _ in ranked[:max_c])
    if mode == "threshold":
        passing = [(sym, total, bd) for sym, total, bd in ranked if total >= ms]
        return frozenset(sym for sym, _, _ in passing[:max_c])
    if mode == "top_n":
        return frozenset(sym for sym, total, _ in ranked[:max_c] if total >= ms)
    logger.warning("unknown scoring.selection_mode %r, using top_n", selection_mode)
    return frozenset(sym for sym, total, _ in ranked[:max_c] if total >= ms)


def _scoring_min_history_config(
    work_scoring: Mapping[str, Any],
    *,
    is_dynamic_candidate: bool = False,
) -> tuple[int, bool]:
    """Return required daily bars and whether dynamic short-history scoring is enabled."""
    raw = work_scoring.get("min_history_bars") if isinstance(work_scoring, Mapping) else None
    cfg = raw if isinstance(raw, Mapping) else {}
    try:
        core_min = int(cfg.get("core", 200) or 200)
    except (TypeError, ValueError):
        core_min = 200
    try:
        dynamic_min = int(cfg.get("dynamic", core_min) or core_min)
    except (TypeError, ValueError):
        dynamic_min = core_min
    dynamic_override_enabled = bool(cfg.get("enable_dynamic_override", False))
    if is_dynamic_candidate and dynamic_override_enabled:
        return max(50, dynamic_min), True
    return max(200, core_min), False


def _missing_scoring_indicators(bars_count: int, *, required_bars: int) -> list[str]:
    missing: list[str] = []
    if bars_count < 20:
        missing.extend(["ma20", "avg_volume_20"])
    if bars_count < 50:
        missing.append("ma50")
    if bars_count < 200:
        missing.extend(["ma200", "ma50_gt_ma200"])
    if bars_count < required_bars:
        missing.append("configured_min_history")
    return list(dict.fromkeys(missing))


def scoring_inputs_from_daily_bars(
    df: pd.DataFrame,
    *,
    min_bars: int = 200,
    allow_short_ma200: bool = False,
) -> dict[str, float] | None:
    """
    Build the ``data`` dict for :meth:`ScoringEngine.score_symbol` from daily OHLCV.

    Returns ``None`` if history is insufficient or required columns are missing.
    """
    if df is None or df.empty or "close" not in df.columns:
        return None
    required = max(1, int(min_bars or 200))
    if len(df) < required:
        return None
    if "volume" not in df.columns:
        return None
    close = df["close"].astype(float)
    price = float(close.iloc[-1])
    ma200_window = 200
    if len(close) < 200:
        if not allow_short_ma200:
            return None
        ma200_window = len(close)
    ma20 = float(close.rolling(20).mean().iloc[-1])
    ma50 = float(close.rolling(50).mean().iloc[-1])
    ma200 = float(close.rolling(ma200_window).mean().iloc[-1])
    vol = df["volume"].astype(float)
    latest_volume = float(vol.iloc[-1])
    tail = vol.tail(20)
    if len(tail) < 20:
        return None
    avg_volume_20 = float(tail.mean())
    return {
        "price": price,
        "ma20": ma20,
        "ma50": ma50,
        "ma200": ma200,
        "volume": latest_volume,
        "avg_volume": avg_volume_20,
    }


def compute_scoring_allowed_symbols(
    config: dict[str, Any],
    symbols: Sequence[str],
    get_bars: Callable[[str], pd.DataFrame],
    regime_score: int | None,
    *,
    account_cash: float | None = None,
    account_equity: float | None = None,
    dynamic_symbols: Sequence[str] | None = None,
) -> frozenset[str] | None:
    """
    When ``scoring.enabled`` is false, return ``None`` (caller: no pre-filter).

    Otherwise rank *symbols* by composite score (descending), then apply
    ``scoring.selection_mode`` (after optional ``when_high_cash`` overlay; see
    :func:`merge_scoring_config_for_cash`).

    Slice size uses :func:`scoring_max_bucket_n` — set ``top_n_candidates`` (preferred) or
    legacy ``max_candidates`` (e.g. 10–15 for ``ranked_top_n``).

    * ``top_n`` (default) — first *N* by rank, then ``total >= min_score``.
    * ``threshold`` — all symbols with ``total >= min_score``, highest scores first, capped at *N*.
    * ``ranked_top_n`` — top *N* by composite score; ``min_score`` is not applied.

    Deprecated: ``allowlist_only: true`` is ignored; use ``selection_mode`` + ``top_n_candidates``.

    Pass ``account_cash`` and ``account_equity`` so high-cash mode can relax scoring when
    ``portfolio.high_cash_deploy_pct`` is met; omit them to skip that overlay.
    """
    sc = config.get("scoring") or {}
    if not bool(sc.get("enabled", False)):
        return None
    eq_for_merge = float(account_equity) if account_equity is not None else 0.0
    work_scoring = merge_scoring_config_for_cash(
        config, account_cash=account_cash, account_equity=eq_for_merge
    )
    if bool(work_scoring.get("allowlist_only", False)):
        logger.warning(
            "scoring.allowlist_only is deprecated (ignored). Use selection_mode and top_n_candidates "
            "(e.g. ranked_top_n + 12)."
        )
    cfg_for_scorer = {**config, "scoring": work_scoring}
    rs = int(regime_score) if regime_score is not None else 0
    scorer = ScoringEngine(cfg_for_scorer)
    ranked: list[tuple[str, float, ScoreBreakdown]] = []
    dynamic_set = {
        str(sym or "").strip().upper()
        for sym in (dynamic_symbols or [])
        if str(sym or "").strip()
    }
    for symbol in symbols:
        sym_u = str(symbol).upper()
        try:
            df = get_bars(symbol)
            bars_count = 0 if df is None or df.empty else len(df)
            is_dynamic = sym_u in dynamic_set
            min_bars, allow_short_ma200 = _scoring_min_history_config(
                work_scoring,
                is_dynamic_candidate=is_dynamic,
            )
            data = scoring_inputs_from_daily_bars(
                df,
                min_bars=min_bars,
                allow_short_ma200=allow_short_ma200,
            )
            if data is None:
                missing = _missing_scoring_indicators(bars_count, required_bars=min_bars)
                logger.info(
                    "SCORING_PREFILTER_SHORT_HISTORY symbol=%s candidate_type=%s bars=%d required_bars=%d missing_indicators=%s dynamic_override_enabled=%s reason=insufficient_scoring_inputs",
                    sym_u,
                    "dynamic" if is_dynamic else "core",
                    int(bars_count),
                    int(min_bars),
                    ",".join(missing) or "unknown",
                    bool(is_dynamic and allow_short_ma200),
                )
                continue
            if is_dynamic and allow_short_ma200 and bars_count < 200:
                logger.info(
                    "SCORING_PREFILTER_DYNAMIC_SHORT_HISTORY symbol=%s bars=%d required_bars=%d ma200_source=available_short_history",
                    sym_u,
                    int(bars_count),
                    int(min_bars),
                )
            breakdown = scorer.score_symbol(data, rs)
            ranked.append((sym_u, breakdown.total, breakdown))
        except Exception as exc:
            logger.debug("scoring skip %s: %s", sym_u, exc)
            continue
    ranked.sort(key=lambda x: x[1], reverse=True)
    min_score = float(work_scoring.get("min_score", 8))
    max_candidates = scoring_max_bucket_n(work_scoring)
    mode = str(work_scoring.get("selection_mode", "top_n")).strip()
    return build_scoring_allowlist_from_ranked(
        ranked,
        min_score=min_score,
        max_candidates=max_candidates,
        selection_mode=mode,
    )


def should_apply_scoring_gate(
    *,
    scoring_allowed: frozenset[str] | None,
    sym_upper: str,
    current_positions: set[str],
    tracked_keys_upper: set[str],
) -> bool:
    """True when *sym_upper* must pass the scored top-N / threshold set (new names only)."""
    if scoring_allowed is None:
        return False
    if sym_upper in current_positions:
        return False
    if sym_upper in tracked_keys_upper:
        return False
    return True
