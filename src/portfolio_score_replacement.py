"""
Score-based rotation at ``max_positions`` when ``portfolio.enable_replacement`` is on.

When ``portfolio.replacement.replacement_threshold`` is strictly between 0 and 1, rotation
compares jittered entry **strength** to the weakest hold score (tracked ``signal_strength`` by default,
``weakest_pick: pnl_momentum_trend`` (normalized composite mean), or ``weakest_pick: composite_position_score``
(raw ``momentum + pnl + trend`` sum on ``[0, 3]`` for both sides) — see
:func:`evaluate_strength_based_portfolio_swap`). Otherwise compares
:func:`~src.signal_scoring.score_signal` on the incoming entry gate snapshot to each eligible hold:

* **Classic** (default): :func:`~src.position_scoring.score_position` on a bar snapshot (0–100).
* **Weighted** (``replacement.swap_position_score: weighted_position``): weakest is
  ``min(symbol, key=weighted_composite_position_score)`` on ``[0, 1]``, scaled to 0–100 for the same
  ``score_signal`` vs ``weakest_score + swap_threshold`` rules as classic.

``portfolio.swap_threshold`` (default 10) applies on that 0–100 scale unless
``rotate_on_stronger_signal`` (strict ``new_score > weakest_score``).
"""
from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd

from src.portfolio_allocation import symbol_long_position_market_value_usd
from src.portfolio_replacement import (
    WEAKEST_PICK_COMPOSITE_POSITION_SCORE,
    WEAKEST_PICK_PNL_MOMENTUM_TREND,
    effective_signal_strength,
    parse_weakest_pick,
    replacement_hold_candidates_sorted_asc,
    replacement_min_market_value_to_replace_usd,
    replacement_min_notional_for_incoming_usd,
    replacement_size_ok,
    replacement_strength_gap_ok,
    replacement_strength_ok,
    replacement_weakest_min_hold_ok,
    strategy_bar_params_for_position_score,
    weakest_replacement_hold,
)
from src.position_scoring import composite_position_score, score_position, weighted_composite_position_score
from src.position_tracker import bars_held as tracker_bars_held
from src.signal_scoring import score_signal

log = logging.getLogger(__name__)

DEFAULT_SWAP_SCORE_THRESHOLD = 10

# ``portfolio.replacement.swap_position_score`` / ``portfolio.swap_position_score``
SWAP_POSITION_SCORE_CLASSIC = "classic"
SWAP_POSITION_SCORE_WEIGHTED_POSITION = "weighted_position"


def parse_swap_position_score_mode(
    rep_sub: Mapping[str, Any] | None,
    portfolio_cfg: Mapping[str, Any] | None = None,
) -> str:
    """
    How to score open longs for the **score-based** replacement path (``replacement_threshold`` not in (0, 1)).

    * ``classic`` — :func:`score_eligible_positions_for_swap` / :func:`~src.position_scoring.score_position`.
    * ``weighted_position`` — :func:`score_eligible_weighted_positions_for_swap` /
      :func:`~src.position_scoring.weighted_composite_position_score` (40% / 40% / 20% blend), weakest =
      ``min(..., key=score)``; scores are scaled to 0–100 to match :func:`~src.signal_scoring.score_signal`.
    """
    pc = portfolio_cfg if isinstance(portfolio_cfg, dict) else {}
    rep = rep_sub if isinstance(rep_sub, dict) else {}
    for raw in (
        rep.get("swap_position_score"),
        rep.get("position_swap_score"),
        pc.get("swap_position_score"),
        pc.get("replacement", {}).get("swap_position_score")
        if isinstance(pc.get("replacement"), dict)
        else None,
    ):
        if raw is None or str(raw).strip() == "":
            continue
        s = str(raw).strip().lower().replace("-", "_")
        if s in (
            "weighted",
            "weighted_position",
            "weighted_position_score",
            "position_score",
            "weighted_composite",
        ):
            return SWAP_POSITION_SCORE_WEIGHTED_POSITION
        if s in ("classic", "score_position", "legacy", "default"):
            return SWAP_POSITION_SCORE_CLASSIC
    return SWAP_POSITION_SCORE_CLASSIC


def swap_score_threshold(
    rep_sub: Mapping[str, Any] | None,
    portfolio_cfg: Mapping[str, Any] | None = None,
) -> int:
    """Resolve swap gap: ``portfolio.swap_threshold`` → ``portfolio.swap_score_threshold`` → ``replacement.swap_score_threshold``."""
    pc = portfolio_cfg or {}
    for raw in (
        pc.get("swap_threshold"),
        pc.get("swap_score_threshold"),
        (rep_sub or {}).get("swap_score_threshold"),
    ):
        if raw is None or str(raw).strip() == "":
            continue
        try:
            return max(0, int(float(raw)))
        except (TypeError, ValueError):
            continue
    return DEFAULT_SWAP_SCORE_THRESHOLD


def _regime_ok_for_signal(regime_result: Any) -> bool:
    if regime_result is None:
        return False
    c = str(getattr(regime_result, "condition", None) or "").strip().lower()
    if c in ("bullish", "neutral"):
        return True
    if c == "defensive":
        return False
    sc = getattr(regime_result, "score", None)
    if sc is None:
        return False
    try:
        return int(sc) >= 2
    except (TypeError, ValueError):
        return False


def _spread_ok_for_signal(quote: Any) -> bool:
    if quote is None:
        return True
    sp = getattr(quote, "spread_pct", None)
    if sp is None:
        return True
    try:
        return float(sp) <= 2.0
    except (TypeError, ValueError):
        return True


def build_entry_swap_signal_map(
    engine: Any,
    symbol: str,
    df: Any,
    spread_pct: float | None,
    atr_pct: float | None,
    *,
    regime_score: int | None,
    regime_result: Any,
    quote: Any,
) -> dict[str, bool]:
    sym_u = str(symbol).strip().upper()
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return {
            "trend": False,
            "pullback": False,
            "momentum": False,
            "volatility": False,
            "regime_ok": _regime_ok_for_signal(regime_result),
            "spread_ok": _spread_ok_for_signal(quote),
        }
    te, pe, me, ve = engine.strategy.entry_eval_components_for_log(
        sym_u, df, spread_pct, atr_pct, regime_score=regime_score
    )
    return {
        "trend": bool(te) if te is not None else False,
        "pullback": bool(pe) if pe is not None else False,
        "momentum": bool(me) if me is not None else False,
        "volatility": bool(ve) if ve is not None else False,
        "regime_ok": _regime_ok_for_signal(regime_result),
        "spread_ok": _spread_ok_for_signal(quote),
    }


def _last_close_ma_row(engine: Any, df: pd.DataFrame) -> dict[str, float] | None:
    if df.empty or "close" not in df.columns:
        return None
    close = df["close"]
    mf_i = int(getattr(engine.strategy, "ma_fast", 10) or 10)
    ms_i = int(getattr(engine.strategy, "ma_slow", 50) or 50)
    if len(close) < max(mf_i, ms_i):
        return None
    ma_f = float(close.rolling(mf_i).mean().iloc[-1])
    ma_s = float(close.rolling(ms_i).mean().iloc[-1])
    px = float(close.iloc[-1])
    return {"close": px, "ma_fast": ma_f, "ma_slow": ma_s}


def broker_position_dict(positions: Sequence[Mapping[str, Any]], sym: str) -> dict[str, Any] | None:
    su = sym.strip().upper()
    for p in positions:
        if str(p.get("symbol") or "").strip().upper() == su:
            return dict(p)
    return None


def position_row_for_score(
    sym: str,
    pos_row: Mapping[str, Any] | None,
    *,
    bars_held_val: int,
) -> dict[str, Any]:
    out: dict[str, Any] = {"symbol": sym, "bars_held": int(bars_held_val)}
    if not pos_row:
        return out
    for k in ("unrealized_plpc", "unrealized_intraday_plpc"):
        if k in pos_row and pos_row[k] is not None:
            out[k] = pos_row[k]
            return out
    ur = pos_row.get("unrealized_pl")
    mv = pos_row.get("market_value")
    try:
        if ur is not None and mv not in (None, 0, "", "0") and float(mv) != 0:
            out["unrealized_plpc"] = float(ur) / float(mv)
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    return out


def score_eligible_positions_for_swap(
    *,
    engine: Any,
    broker: Any,
    eligible_active: list[str],
    tracked: Mapping[str, Any],
    positions: Sequence[Mapping[str, Any]],
    dt: Any,
) -> list[tuple[str, int]]:
    market_data: dict[str, dict[str, float]] = {}
    scored: list[tuple[str, int]] = []
    for h in eligible_active:
        hu = str(h).strip().upper()
        try:
            df_h = broker.get_bars(hu, timeframe="1Day", limit=220)
        except Exception:
            df_h = None
        row = None
        if isinstance(df_h, pd.DataFrame) and not df_h.empty:
            row = _last_close_ma_row(engine, df_h)
        if row:
            market_data[hu] = row
        tr = (tracked or {}).get(hu) if isinstance(tracked, dict) else None
        et = (tr or {}).get("entry_time") if isinstance(tr, dict) else None
        bh = int(tracker_bars_held(str(et), dt)) if et else 0
        br = broker_position_dict(positions, hu)
        p_sc = position_row_for_score(hu, br, bars_held_val=bh)
        scored.append((hu, score_position(p_sc, market_data)))
    return scored


def score_eligible_weighted_positions_for_swap(
    *,
    engine: Any,
    broker: Any,
    eligible_active: list[str],
    positions: Sequence[Mapping[str, Any]],
) -> list[tuple[str, float]]:
    """
    For each eligible symbol, :func:`~src.position_scoring.weighted_composite_position_score`
    on daily bars (``min(positions, key=…)`` semantics for the caller).
    """
    scored: list[tuple[str, float]] = []
    ms, mb, vb = strategy_bar_params_for_position_score(engine)
    for h in eligible_active:
        hu = str(h).strip().upper()
        try:
            df_h = broker.get_bars(hu, timeframe="1Day", limit=220)
        except Exception:
            df_h = None
        if not isinstance(df_h, pd.DataFrame):
            df_h = None
        w, _bd = weighted_composite_position_score(
            hu,
            positions,
            df_h,
            ma_slow=ms,
            momentum_bars=mb,
            volume_bars=vb,
        )
        scored.append((hu, float(w)))
    return scored


def evaluate_score_based_portfolio_swap(
    *,
    incoming_sym_upper: str,
    engine: Any,
    broker: Any,
    df: Any,
    atr_pct: float | None,
    quote: Any,
    spread_pct: float | None,
    regime_result: Any,
    entry_regime_score: int | None,
    eligible_active: list[str],
    tracked: Mapping[str, Any],
    positions: Sequence[Mapping[str, Any]],
    dt: Any,
    rep_sub: Mapping[str, Any] | None,
    portfolio_cfg: Mapping[str, Any] | None = None,
) -> tuple[str | None, int, int, int, str | None]:
    """
    Returns ``(weakest_symbol_or_none, new_score, weakest_score, threshold, skip_reason)``.

    When *skip_reason* is set, do not rotate (caller should log and return). When
    *weakest_symbol_or_none* is set and *skip_reason* is ``None``, sell that symbol and proceed
    with the new entry.

    ``replacement.rotate_on_stronger_signal``: when true, rotate if ``new_score > weakest_score``
    only (ignore *swap_threshold* increment ``th``).

    ``replacement.swap_position_score`` / ``portfolio.swap_position_score``: set to
    ``weighted_position`` to use :func:`score_eligible_weighted_positions_for_swap` (weakest =
    lowest weighted composite on ``[0, 1]``, compared after scaling weakest to 0–100).
    """
    su = str(incoming_sym_upper).strip().upper()
    if not eligible_active:
        return None, 0, 0, swap_score_threshold(rep_sub, portfolio_cfg), "portfolio replacement: no eligible hold to rotate"

    sig_map = build_entry_swap_signal_map(
        engine,
        su,
        df,
        spread_pct,
        atr_pct,
        regime_score=entry_regime_score,
        regime_result=regime_result,
        quote=quote,
    )
    new_score = score_signal(sig_map)
    th = swap_score_threshold(rep_sub, portfolio_cfg)
    _swap_mode = parse_swap_position_score_mode(rep_sub, portfolio_cfg)
    if _swap_mode == SWAP_POSITION_SCORE_WEIGHTED_POSITION:
        scored_f = score_eligible_weighted_positions_for_swap(
            engine=engine,
            broker=broker,
            eligible_active=list(eligible_active),
            positions=positions,
        )
        if not scored_f:
            return None, new_score, 0, th, "portfolio replacement: no eligible hold to rotate"
        weakest_sym, weakest_f = min(scored_f, key=lambda x: (x[1], x[0]))
        weakest_score = int(max(0, min(100, round(weakest_f * 100.0))))
    else:
        scored = score_eligible_positions_for_swap(
            engine=engine,
            broker=broker,
            eligible_active=list(eligible_active),
            tracked=tracked,
            positions=positions,
            dt=dt,
        )
        if not scored:
            return None, new_score, 0, th, "portfolio replacement: no eligible hold to rotate"

        weakest_sym, weakest_score = min(scored, key=lambda x: (x[1], x[0]))
    if weakest_sym == su:
        return None, new_score, weakest_score, th, None

    w_row = (tracked or {}).get(weakest_sym) if isinstance(tracked, dict) else None
    w_entry_iso = (w_row or {}).get("entry_time") if isinstance(w_row, dict) else None

    _mh_raw = (rep_sub or {}).get("min_hold_minutes")
    _mh_override: float | None
    if _mh_raw is None or str(_mh_raw).strip() == "":
        _mh_override = None
    else:
        try:
            _mh_override = float(_mh_raw)
        except (TypeError, ValueError):
            _mh_override = None

    _ok_mh, _mh_reason = replacement_weakest_min_hold_ok(
        weakest_entry_time_iso=str(w_entry_iso) if w_entry_iso else None,
        now=dt,
        min_hold_minutes=_mh_override,
    )
    if not _ok_mh:
        return None, new_score, weakest_score, th, _mh_reason or "portfolio replacement: min hold not met"

    rep = rep_sub if isinstance(rep_sub, dict) else {}
    rotate_on_stronger = bool(rep.get("rotate_on_stronger_signal", False))

    if rotate_on_stronger:
        if new_score > weakest_score:
            log.info(
                "%s replacing %s new_score=%d weakest=%d (rotate_on_stronger_signal; score path%s)",
                su,
                weakest_sym,
                new_score,
                weakest_score,
                "; weighted_position" if _swap_mode == SWAP_POSITION_SCORE_WEIGHTED_POSITION else "",
            )
            return weakest_sym, new_score, weakest_score, th, None
    elif new_score > weakest_score + th:
        log.info(
            "%s replacing %s new_score=%d weakest=%d (threshold=%d)%s",
            su,
            weakest_sym,
            new_score,
            weakest_score,
            th,
            " weighted_position" if _swap_mode == SWAP_POSITION_SCORE_WEIGHTED_POSITION else "",
        )
        return weakest_sym, new_score, weakest_score, th, None

    return (
        None,
        new_score,
        weakest_score,
        th,
        "better positions already held (new_score=%d vs weakest %s=%d, need >+%d)"
        % (new_score, weakest_sym, weakest_score, th),
    )


def _holder_cmp_strength(
    wstr: float, *, is_composite: bool
) -> float:
    return float(wstr) / 3.0 if is_composite else float(wstr)


def plan_replace_losers_with_winners_stack(
    *,
    su: str,
    candidates_asc: list[tuple[str, float]],
    inc_cmp: float,
    is_composite: bool,
    tracked: Mapping[str, Any],
    positions: Sequence[Mapping[str, Any]],
    dt: Any,
    rep: dict[str, Any],
    strength_jitter_max: float,
    replace_if_weakest_older_than_bars: int | None,
    max_position_age_bars: int | None,
    allow_equal_replacement: bool,
    strength_gap: float,
    incoming_notional_usd: float,
) -> list[str] | None:
    """
    When ``replacement.replace_losers_with_winners.enabled`` is set, take **up to** *max_sells* weakest
    lines that each pass the same strength gates as a single-weak rotation, until cumulative market
    value (or *prefer_sell_count*) is satisfied, so a strong incoming can be funded by multiple
    laggards (e.g. XLF+WMT → NVDA) instead of a single name that might be the wrong size.

    Returns ``None`` to fall back to one-weakest behavior.
    """
    raw = rep.get("replace_losers_with_winners")
    if not isinstance(raw, dict) or not bool(raw.get("enabled", False)):
        return None
    try:
        max_sells = max(1, int(raw.get("max_sells", 2) or 2))
    except (TypeError, ValueError):
        max_sells = 2
    try:
        prefer_n = int(raw.get("prefer_sell_count", 0) or 0)
    except (TypeError, ValueError):
        prefer_n = 0
    prefer_n = max(0, min(max_sells, prefer_n))
    tr = dict(tracked) if tracked is not None else {}
    _mh_raw = rep.get("min_hold_minutes")
    if _mh_raw is None or str(_mh_raw).strip() == "":
        _mh_override = None
    else:
        try:
            _mh_override = float(_mh_raw)
        except (TypeError, ValueError):
            _mh_override = None
    _tiny_floor = replacement_min_market_value_to_replace_usd(rep)
    if _tiny_floor <= 0:
        _tiny_floor = 750.0
    min_incoming = float(
        max(
            float(incoming_notional_usd or 0.0) or 0.0,
            float(replacement_min_notional_for_incoming_usd(rep) or 0.0) or 0.0,
        )
    )
    if min_incoming <= 0.0 and prefer_n == 0:
        return None
    picked: list[str] = []
    cum_mv = 0.0
    rotate_on_stronger = bool(rep.get("rotate_on_stronger_signal", False))
    su_u = str(su).upper()

    for wsym, wstr in candidates_asc:
        if str(wsym).upper() == su_u or len(picked) >= max_sells:
            break
        w_mv = float(symbol_long_position_market_value_usd(list(positions), wsym))
        if w_mv < _tiny_floor:
            continue
        w_u = str(wsym).upper()
        w_row = (tr or {}).get(w_u) or {}
        w_entry_iso = w_row.get("entry_time")
        w_age: int | None = None
        if w_entry_iso:
            try:
                w_age = int(tracker_bars_held(str(w_entry_iso), dt))
            except (TypeError, ValueError):
                w_age = None
        if max_position_age_bars is not None and int(max_position_age_bars) > 0:
            if w_age is None or w_age < int(max_position_age_bars):
                continue
        _ok_mh, _ = replacement_weakest_min_hold_ok(
            weakest_entry_time_iso=str(w_entry_iso) if w_entry_iso else None,
            now=dt,
            min_hold_minutes=_mh_override,
        )
        if not _ok_mh:
            continue
        wstr_cmp = _holder_cmp_strength(wstr, is_composite=is_composite)
        if not replacement_strength_ok(
            inc_cmp,
            wstr_cmp,
            weakest_age_bars=w_age,
            replace_if_weakest_older_than_bars=replace_if_weakest_older_than_bars,
        ):
            continue
        stale_bypass = (
            replace_if_weakest_older_than_bars is not None
            and w_age is not None
            and int(w_age) > int(replace_if_weakest_older_than_bars)
        )
        if not stale_bypass and not rotate_on_stronger:
            _ok_gap, _ = replacement_strength_gap_ok(
                inc_cmp,
                wstr_cmp,
                threshold=float(strength_gap),
                allow_equal_replacement=allow_equal_replacement,
                strength_jitter_max=float(strength_jitter_max),
            )
            if not _ok_gap:
                continue
        picked.append(w_u)
        cum_mv += w_mv
        if prefer_n > 0 and len(picked) >= prefer_n:
            if min_incoming > 0.0 and cum_mv + 1.0e-6 < min_incoming and len(picked) < max_sells:
                continue
            log.info(
                "%s replace_losers_with_winners (prefer_sell_count=%d) — selling %s cum_mv~=%.0f",
                su,
                prefer_n,
                picked,
                cum_mv,
            )
            return picked
        if (
            prefer_n == 0
            and (min_incoming <= 0.0 or cum_mv + 1.0e-6 >= min_incoming)
        ):
            log.info(
                "%s replace_losers_with_winners (funded) — selling %s cum_mv~=%.0f min_in~=%.0f",
                su,
                picked,
                cum_mv,
                min_incoming,
            )
            return picked
    if not picked:
        return None
    if min_incoming > 0.0 and cum_mv + 1.0e-6 < min_incoming:
        return None
    log.info("%s replace_losers_with_winners — selling %s (cumulative_mv~=%.0f)", su, picked, cum_mv)
    return picked


def evaluate_strength_based_portfolio_swap(
    *,
    incoming_sym_upper: str,
    decision: Any,
    tracked: Mapping[str, Any],
    eligible_active: list[str],
    positions: Sequence[Mapping[str, Any]],
    dt: Any,
    rep_sub: Mapping[str, Any] | None,
    strength_jitter_max: float,
    replace_if_weakest_older_than_bars: int | None,
    max_position_age_bars: int | None,
    allow_equal_replacement: bool,
    strength_gap: float,
    incoming_notional_usd: float = 0.0,
    engine: Any | None = None,
    broker: Any | None = None,
    df: Any | None = None,
) -> tuple[list[str] | None, str | None]:
    """
    Rotation when ``portfolio.replacement.replacement_threshold`` is in (0, 1).

    With ``replace_losers_with_winners`` enabled, may return **multiple** symbols to sell
    (weakest laggards first) so a strong new name is funded from several smaller/weaker lines
    (``replace_losers_with_winners``). Otherwise one symbol as ``[weakest]``.

    Returns ``(symbols_to_sell, skip_reason)`` — *symbols_to_sell* is non-empty on success.
    """
    su = str(incoming_sym_upper).strip().upper()
    rep = rep_sub if isinstance(rep_sub, dict) else {}
    if not eligible_active:
        return None, "portfolio replacement: no eligible hold to rotate"

    def _gb_swap(s: str) -> Any:
        if broker is None:
            return None
        try:
            return broker.get_bars(s, timeframe="1Day", limit=220)
        except Exception:
            return None

    _wpick = parse_weakest_pick(rep_sub if isinstance(rep_sub, dict) else None)
    _need_bars = _wpick in (
        WEAKEST_PICK_PNL_MOMENTUM_TREND,
        WEAKEST_PICK_COMPOSITE_POSITION_SCORE,
    )
    tr_dict = dict(tracked) if isinstance(tracked, dict) else {}
    candidates_asc = replacement_hold_candidates_sorted_asc(
        tr_dict,
        list(eligible_active),
        positions=positions,
        get_bars=_gb_swap if broker is not None and _need_bars else None,
        engine=engine,
        rep_sub=rep_sub if isinstance(rep_sub, dict) else None,
    )
    if not candidates_asc:
        return None, "portfolio replacement: no eligible hold to rotate"
    wsym, wstr = candidates_asc[0]
    if wsym is None or not str(wsym).strip():
        return None, "portfolio replacement: no eligible hold to rotate"
    if wsym == su:
        return None, None

    _is_composite = _wpick == WEAKEST_PICK_COMPOSITE_POSITION_SCORE
    if _is_composite:
        if df is None or not isinstance(df, pd.DataFrame) or getattr(df, "empty", True):
            return (
                None,
                "portfolio replacement: composite_position_score swap needs daily OHLCV (df) for incoming symbol",
            )
        _ms_i, _mb_i, _vb_i = strategy_bar_params_for_position_score(engine)
        inc_total, _bd_i = composite_position_score(
            su,
            positions,
            df,
            ma_slow=_ms_i,
            momentum_bars=_mb_i,
            volume_bars=_vb_i,
        )
        inc_cmp = float(inc_total) / 3.0
    else:
        base_inc = float(getattr(decision.entry_signal, "strength", None) or 1.0) if decision else 1.0
        inc_cmp = effective_signal_strength(base_inc, strength_jitter_max)

    stack = plan_replace_losers_with_winners_stack(
        su=su,
        candidates_asc=candidates_asc,
        inc_cmp=inc_cmp,
        is_composite=bool(_is_composite),
        tracked=tracked,
        positions=positions,
        dt=dt,
        rep=rep,
        strength_jitter_max=float(strength_jitter_max or 0.0),
        replace_if_weakest_older_than_bars=replace_if_weakest_older_than_bars,
        max_position_age_bars=max_position_age_bars,
        allow_equal_replacement=bool(allow_equal_replacement),
        strength_gap=float(strength_gap),
        incoming_notional_usd=float(incoming_notional_usd or 0.0),
    )
    if stack:
        return stack, None

    w_mv = float(symbol_long_position_market_value_usd(list(positions), wsym))
    _ok_sz, _reason_sz = replacement_size_ok(
        weakest_market_value_usd=w_mv,
        incoming_notional_usd=float(incoming_notional_usd or 0.0),
        rep_cfg=rep,
    )
    if not _ok_sz:
        return None, _reason_sz

    wstr_cmp = _holder_cmp_strength(float(wstr), is_composite=bool(_is_composite))

    w_row = (tracked or {}).get(wsym) if isinstance(tracked, dict) else None
    w_entry_iso = (w_row or {}).get("entry_time") if isinstance(w_row, dict) else None
    w_age: int | None = None
    if w_entry_iso:
        try:
            w_age = int(tracker_bars_held(str(w_entry_iso), dt))
        except (TypeError, ValueError):
            w_age = None

    if max_position_age_bars is not None and max_position_age_bars > 0:
        if w_age is None or w_age < int(max_position_age_bars):
            ypo = rep.get("young_position_exceptional_override")
            ypo_cfg = ypo if isinstance(ypo, dict) else {}
            try:
                min_gap_raw = float(ypo_cfg.get("min_strength_gap", 0.50) or 0.50)
            except (TypeError, ValueError):
                min_gap_raw = 0.50
            gap_threshold = min_gap_raw / 100.0 if max(abs(inc_cmp), abs(wstr_cmp), abs(min_gap_raw)) > 2.0 else min_gap_raw
            try:
                min_incoming_raw = float(ypo_cfg.get("min_incoming_strength", 0.85) or 0.85)
            except (TypeError, ValueError):
                min_incoming_raw = 0.85
            min_incoming = min_incoming_raw / 100.0 if max(abs(inc_cmp), abs(min_incoming_raw)) > 2.0 else min_incoming_raw
            exceptional_override = bool(
                isinstance(ypo_cfg, dict)
                and bool(ypo_cfg.get("enabled", False))
                and inc_cmp >= min_incoming
                and (inc_cmp - wstr_cmp) >= gap_threshold - 1e-12
            )
            if exceptional_override:
                log.info(
                    "PORTFOLIO_REPLACEMENT_YOUNG_OVERRIDE incoming=%s weakest=%s bars_held=%s "
                    "incoming_strength=%.4f weakest_strength=%.4f min_gap=%.4f",
                    su,
                    wsym,
                    w_age,
                    inc_cmp,
                    wstr_cmp,
                    gap_threshold,
                )
            else:
                return None, (
                    "portfolio replacement: weakest %s bars_held=%s < max_position_age_bars=%d"
                    % (wsym, w_age, int(max_position_age_bars))
                )

    _mh_raw = rep.get("min_hold_minutes")
    if _mh_raw is None or str(_mh_raw).strip() == "":
        _mh_override = None
    else:
        try:
            _mh_override = float(_mh_raw)
        except (TypeError, ValueError):
            _mh_override = None

    _ok_mh, _mh_reason = replacement_weakest_min_hold_ok(
        weakest_entry_time_iso=str(w_entry_iso) if w_entry_iso else None,
        now=dt,
        min_hold_minutes=_mh_override,
    )
    if not _ok_mh:
        return None, _mh_reason or "portfolio replacement: min hold not met"

    if not replacement_strength_ok(
        inc_cmp,
        wstr_cmp,
        weakest_age_bars=w_age,
        replace_if_weakest_older_than_bars=replace_if_weakest_older_than_bars,
    ):
        return None, "portfolio replacement: strength vs weakest not ok (below gap / not stale)"

    stale_bypass = (
        replace_if_weakest_older_than_bars is not None
        and w_age is not None
        and int(w_age) > int(replace_if_weakest_older_than_bars)
    )
    rotate_on_stronger = bool(rep.get("rotate_on_stronger_signal", False))
    if not stale_bypass and not rotate_on_stronger:
        _ok_gap, _reason_gap = replacement_strength_gap_ok(
            inc_cmp,
            wstr_cmp,
            threshold=float(strength_gap),
            allow_equal_replacement=allow_equal_replacement,
            strength_jitter_max=float(strength_jitter_max),
        )
        if not _ok_gap:
            return None, _reason_gap

    log.info(
        "%s replacing %s (strength swap) incoming=%.4f weakest=%.4f gap=%.4f%s%s",
        su,
        wsym,
        inc_cmp,
        wstr_cmp,
        float(strength_gap),
        " rotate_on_stronger_signal" if rotate_on_stronger else "",
        " composite_position_score" if _is_composite else "",
    )
    return [str(wsym).upper()], None
