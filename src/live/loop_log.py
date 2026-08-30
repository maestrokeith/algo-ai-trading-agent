"""Shared skip / state print helpers for the live Alpaca loop."""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.brokers.alpaca_client import QuoteInfo


def _skip_reason_label(reason: str) -> str:
    r = str(reason or "").strip().lower()
    if "cooldown" in r or "last fill" in r:
        return "cooldown"
    if "already held" in r or "already in positions" in r or "in tracked state" in r:
        return "already held"
    if (
        "cap" in r
        or "max allowed" in r
        or "allocation" in r
        or "position size" in r
    ) and "max new positions" not in r:
        return "cap reached"
    if "max trade" in r or "trade limit" in r or "max new positions" in r:
        return "trade limit"
    if "size = 0" in r or "size=0" in r or "notional" in r or "qty" in r:
        return "size = 0"
    return str(reason or "unknown")


def quote_skip_spread_check(q: "QuoteInfo | None") -> bool:
    """No NBBO or one-sided quote — do not compare spread_pct to caps or execution spread gate."""
    return q is None or bool(getattr(q, "skip_spread_check", False))


def log_entry_skip(
    dt: datetime,
    symbol: str,
    reason: str,
    *,
    verbose: bool,
    force: bool = False,
) -> None:
    """Print ``SYMBOL skip — reason``. If *force*, always print; else when *verbose* or symbol is SQQQ."""
    sym_u = str(symbol).upper()
    label = _skip_reason_label(reason)
    detail = str(reason or "").strip()
    if detail and detail != label:
        print(f"SKIP {sym_u}: reason={label} detail={detail}", flush=True)
    else:
        print(f"SKIP {sym_u}: reason={label}", flush=True)
    if force or verbose or sym_u == "SQQQ":
        print(dt.strftime("%H:%M ET"), f"{sym_u} skip — {reason}")


def log_inverse_state_line(
    dt: datetime,
    symbol: str,
    *,
    shares: int,
    scale_count: int,
    num_scale_steps: int,
    avg_entry: float | None,
    last_entry: float | None,
    unrealized_pnl: float | None,
) -> None:
    """One-line inverse pyramid state (e.g. SQQQ scale_count vs steps)."""
    sym_u = str(symbol).upper()
    if num_scale_steps > 0:
        sc = "%d/%d" % (scale_count, num_scale_steps)
    else:
        sc = str(scale_count)
    ae = "%.2f" % float(avg_entry) if avg_entry is not None else "n/a"
    le = "%.2f" % float(last_entry) if last_entry is not None else "n/a"
    if unrealized_pnl is not None:
        up = "%.2f" % float(unrealized_pnl)
    else:
        up = "n/a"
    print(
        dt.strftime("%H:%M ET"),
        "%s state — shares=%d scale_count=%s avg_entry=%s last_entry=%s unrealized_pnl=%s"
        % (sym_u, int(shares), sc, ae, le, up),
        flush=True,
    )
