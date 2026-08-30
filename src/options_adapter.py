"""
v1: bullish stock signal → long ATM call; bearish → long ATM put (long_premium_only).

Routing mirrors::

    if bullish:
        use_call_spread()   # v1: long call; ``call_spread`` / ``call`` in YAML
    elif bearish:
        use_put_spread()    # v1: long put; ``put_spread`` / ``put``

Uses config ``options.entry_mapping`` and ``allowed_underlyings``. Multileg vertical
execution is not wired yet; ``call_spread`` / ``put_spread`` map to the same single-leg
selection as ``call`` / ``put``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

OptionRight = Literal["call", "put"]


def use_call_spread() -> OptionRight:
    """Bullish regime: v1 long premium **call** (vertical call spread = same long-call leg for now)."""
    return "call"


def use_put_spread() -> OptionRight:
    """Bearish regime: v1 long premium **put** (vertical put spread = same long-put leg for now)."""
    return "put"


@dataclass(frozen=True)
class OptionIntent:
    """What to buy: long call or long put on underlying (premium debit only for v1)."""

    underlying: str
    right: OptionRight
    source: str
    stock_symbol: str | None = None


def adapt_stock_signal_to_option_intent(
    config: dict[str, Any],
    *,
    underlying: str,
    direction: str,
    source: str,
    stock_symbol: str | None = None,
) -> tuple[OptionIntent | None, str | None]:
    """
    Map bullish/bearish stock signal to call/put per config.

    Returns (intent, None) on success, or (None, reason) for logs when routing skips.
    """
    opts = config.get("options") or {}
    if not bool(opts.get("enabled")):
        return None, "options.enabled is false"
    mode = str(opts.get("mode") or "").strip().lower()
    if mode not in ("long_premium_only", "paper_only", "shadow_live", "live", "live_long_premium"):
        return None, "options.mode is %r (need long_premium_only, paper_only, shadow_live, live, or live_long_premium)" % (
            opts.get("mode"),
        )

    d = str(direction or "").strip().lower()
    mapping = opts.get("entry_mapping") or {}
    if d == "bullish":
        leg = str(mapping.get("bullish_signal") or "call").strip().lower()
        if leg in ("call", "calls", "call_spread", "vertical_call"):
            right: OptionRight = use_call_spread()
        else:
            return None, (
                "entry_mapping.bullish_signal must be call, call_spread, or vertical_call (got %r)"
                % mapping.get("bullish_signal")
            )
    elif d == "bearish":
        leg = str(mapping.get("bearish_signal") or "put").strip().lower()
        if leg in ("put", "puts", "put_spread", "vertical_put"):
            right = use_put_spread()
        else:
            return None, (
                "entry_mapping.bearish_signal must be put, put_spread, or vertical_put (got %r)"
                % mapping.get("bearish_signal")
            )
    else:
        return None, "direction %r not bullish/bearish" % (direction,)

    u = str(underlying or "").strip().upper()
    if not u:
        return None, "empty underlying"
    allowed = {str(x).upper() for x in (opts.get("allowed_underlyings") or [])}
    if not allowed:
        return None, "allowed_underlyings is empty"
    if u not in allowed:
        return None, "underlying %s not in allowed_underlyings %s" % (u, ",".join(sorted(allowed)))

    return (
        OptionIntent(
            underlying=u,
            right=right,
            source=str(source or ""),
            stock_symbol=str(stock_symbol).upper() if stock_symbol else None,
        ),
        None,
    )
