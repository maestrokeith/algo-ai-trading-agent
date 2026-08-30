"""Human-readable buy decision reports for trade logs and notifications."""

from __future__ import annotations

from typing import Any


def _pct_text(v: float | None, *, nd: int = 1) -> str | None:
    if v is None or v != v:
        return None
    return f"{float(v):.{nd}f}%"


def build_buy_decision_report(
    *,
    symbol: str,
    route: str,
    trend_ok: bool | None,
    pullback_ok: bool | None,
    momentum_ok: bool | None,
    volatility_ok: bool | None,
    regime_score: int | None,
    regime_condition: str | None,
    spread_pct: float | None,
    gross_exposure_pct: float | None,
    max_gross_exposure_frac: float | None,
    entry_signal: Any | None,
) -> str:
    sym = str(symbol or "").strip().upper()
    md = getattr(entry_signal, "metadata", None) or {}
    ma_fast = md.get("ma_fast")
    ma_slow = md.get("ma_slow")
    tp = getattr(entry_signal, "take_profit_pct", None)
    stop = getattr(entry_signal, "stop_pct", None)

    bullets: list[str] = []
    if route not in {"trend", "entry_override"}:
        bullets.append(f"route: {route}")
    if trend_ok is True:
        if ma_slow:
            bullets.append(f"price > {int(ma_slow)}D MA")
        else:
            bullets.append("trend filter passed")
    if pullback_ok is True:
        if ma_fast:
            bullets.append(f"{int(ma_fast)}D pullback confirmed")
        else:
            bullets.append("pullback confirmed")
    if momentum_ok is True:
        bullets.append("momentum positive")
    if volatility_ok is True:
        bullets.append("volatility acceptable")
    if regime_score is not None:
        if regime_condition:
            bullets.append(f"regime {str(regime_condition).lower()} score {int(regime_score)}")
        else:
            bullets.append(f"regime score {int(regime_score)}")
    elif regime_condition:
        bullets.append(f"regime {str(regime_condition).lower()}")
    spread_text = _pct_text(spread_pct, nd=2)
    if spread_text is not None:
        bullets.append(f"spread acceptable ({spread_text})")
    cap_text = _pct_text(float(max_gross_exposure_frac) * 100.0 if max_gross_exposure_frac is not None else None)
    gross_text = _pct_text(gross_exposure_pct)
    if gross_text is not None:
        if cap_text is not None:
            bullets.append(f"portfolio exposure under cap ({gross_text} < {cap_text})")
        else:
            bullets.append(f"portfolio exposure acceptable ({gross_text})")
    rr_line = None
    try:
        if tp is not None and stop is not None and float(stop) > 0:
            rr_line = f"expected risk/reward: {float(tp) / float(stop):.1f}x"
    except (TypeError, ValueError, ZeroDivisionError):
        rr_line = None
    if rr_line:
        bullets.append(rr_line)
    if not bullets:
        bullets.append("entry gates passed")
    return "BUY %s because:\n- %s" % (sym, "\n- ".join(bullets))

