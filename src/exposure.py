"""Portfolio exposure snapshot: gross/net, sector and theme %, ETF / inverse-ETF % of equity.

**Gross long book** vs equity is::

    gross_usd = stock_notional_usd + abs(options_delta_adjusted_usd)

where *stock_notional_usd* is the sum of ``|market_value|`` over **non-option** rows (stocks and ETFs),
and *options_delta_adjusted_usd* is the **net** signed options delta in dollars (sum of per-line
signed contributions). Per option line, we use ``options_delta_adjusted`` when the broker supplies it;
otherwise ``sign(side) ×`` delta notional (``options_delta_notional`` or
``|delta|×|qty|×100×underlying``), with ``|market_value|`` (premium) as a last-resort magnitude.

``gross_pct`` is ``100 × gross_usd / equity`` on a 0–100+ percent scale. Sector and theme % still use
``|market_value|`` per row. *default_sector* — label when *symbol* is not listed in *symbol_sector*
(see app ``sector:``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.options_premium_risk import is_option_symbol

ETF_SYMBOLS = {
    "SPY",
    "QQQ",
    "IWM",
    "XLF",
    "XLK",
    "XLE",
    "SMH",
    "GLD",
    "SQQQ",
    "SPXS",
}
INVERSE_ETFS = {"SQQQ", "SPXS"}

SYMBOL_SECTOR: dict[str, str] = {
    "SPY": "broad_market",
    "QQQ": "technology",
    "IWM": "small_caps",
    "XLF": "financials",
    "XLK": "technology",
    "SMH": "technology",
    "XLE": "energy",
    "GLD": "commodities",
    "AAPL": "technology",
    "MSFT": "technology",
    "NVDA": "technology",
    "AMZN": "consumer",
    "META": "technology",
    "GOOGL": "technology",
    "AMD": "technology",
    "TSLA": "consumer",
    "BABA": "china",
    "JD": "china",
    "PDD": "china",
    "SQQQ": "hedge",
    "SPXS": "hedge",
}

THEME_MAP = {
    "SPY": "broad_index",
    "QQQ": "ai_growth",
    "XLK": "ai_growth",
    "AAPL": "ai_growth",
    "MSFT": "ai_growth",
    "NVDA": "ai_growth",
    "META": "ai_growth",
    "GOOGL": "ai_growth",
    "SMH": "semis",
    "AMD": "semis",
    "TSLA": "high_beta_growth",
    "XLF": "financials",
    "GLD": "defensive_alt",
    "SQQQ": "hedge",
    "SPXS": "hedge",
    "BABA": "china",
    "JD": "china",
    "PDD": "china",
}


@dataclass
class ExposureSnapshot:
    gross_pct: float
    net_pct: float
    sector_pct: dict[str, float]
    theme_pct: dict[str, float]
    etf_pct: float
    inverse_etf_pct: float


def _option_delta_notional_magnitude_usd(
    p: dict[str, Any], *, abs_market_value: float
) -> float:
    """
    Unsigned delta notional (dollar delta) for a US equity **option** row, or ``|market_value|``
    as fallback. Returns 0 if the symbol is not an option.
    """
    sym = str(p.get("symbol") or "").strip().upper()
    mv = max(0.0, float(abs_market_value))
    if not is_option_symbol(sym):
        return 0.0
    raw_oden = p.get("options_delta_notional")
    if raw_oden is not None and str(raw_oden).strip() != "":
        try:
            v = float(raw_oden)
        except (TypeError, ValueError):
            v = float("nan")
        if v == v:
            return max(0.0, abs(v))
    try:
        d = p.get("delta")
        u_raw = p.get("underlying_last") or p.get("underlying_price")
        if d is not None and u_raw is not None and str(u_raw).strip() != "":
            delta = float(d)
            u = float(u_raw)
            try:
                qty = int(float(p.get("qty", 0) or 0))
            except (TypeError, ValueError):
                qty = 0
            if abs(u) > 0.0 and abs(qty) > 0:
                return max(0.0, abs(delta * float(qty) * 100.0 * u))
    except (TypeError, ValueError):
        pass
    return mv


def stock_notional_dollars_for_position(p: dict[str, Any], *, abs_market_value: float) -> float:
    """Long stock/ETF ``|market_value|`` that counts toward gross; **zero** for option symbols."""
    sym = str(p.get("symbol") or "").strip().upper()
    mv = max(0.0, float(abs_market_value))
    if is_option_symbol(sym):
        return 0.0
    return mv


def signed_options_delta_adjusted_dollars_for_position(
    p: dict[str, Any], *, abs_market_value: float
) -> float:
    """
    Signed options delta in USD for one row (0 for non-options).

    If ``options_delta_adjusted`` is set, it is used as-is (broker-signed). Otherwise
    ``sign(side) ×`` :func:`_option_delta_notional_magnitude_usd` (long = positive sign multiplier).
    """
    sym = str(p.get("symbol") or "").strip().upper()
    mv = max(0.0, float(abs_market_value))
    if not is_option_symbol(sym):
        return 0.0
    raw_adj = p.get("options_delta_adjusted")
    if raw_adj is not None and str(raw_adj).strip() != "":
        try:
            v = float(raw_adj)
        except (TypeError, ValueError):
            v = float("nan")
        if v == v:
            return v
    mag = _option_delta_notional_magnitude_usd(p, abs_market_value=mv)
    side = str(p.get("side", "long")).lower()
    sign = -1.0 if side == "short" else 1.0
    return sign * mag


def gross_notional_dollars_for_position(p: dict[str, Any], *, abs_market_value: float) -> float:
    """
    Per-line **unsigned** economic weight used in legacy helpers: non-options use ``|market_value|``;
    options use delta notional magnitude when available, else ``|market_value|``.

    Portfolio **gross** in :func:`compute_exposures` follows
    ``stocks + abs(sum signed option deltas)``; see :func:`stock_notional_dollars_for_position` and
    :func:`signed_options_delta_adjusted_dollars_for_position`.
    """
    sym = str(p.get("symbol") or "").strip().upper()
    mv = max(0.0, float(abs_market_value))
    if not is_option_symbol(sym):
        return mv
    return _option_delta_notional_magnitude_usd(p, abs_market_value=mv)


def compute_exposures(
    account_equity: float,
    positions: list[dict[str, Any]],
    symbol_sector: dict[str, str],
    *,
    default_sector: str = "unknown",
) -> ExposureSnapshot:
    """Aggregate *positions* into % of *account_equity* buckets.

    **Gross** is ``(stock_notional + abs(net_options_delta_adjusted)) / equity`` in %% (see module
    docstring). Sector and theme %% still use ``|market_value|`` per row.
    """
    stocks_gross = 0.0
    options_delta_adjusted_net = 0.0
    net = 0.0
    etf = 0.0
    inverse_etf = 0.0
    sector_pct: dict[str, float] = {}
    theme_pct: dict[str, float] = {}

    if account_equity <= 0:
        return ExposureSnapshot(0.0, 0.0, {}, {}, 0.0, 0.0)

    for p in positions:
        symbol = str(p["symbol"]).upper()
        mv = abs(float(p["market_value"]))
        side = str(p.get("side", "long")).lower()
        signed_mv = -mv if side == "short" else mv

        stocks_gross += stock_notional_dollars_for_position(p, abs_market_value=mv)
        options_delta_adjusted_net += signed_options_delta_adjusted_dollars_for_position(
            p, abs_market_value=mv
        )
        net += signed_mv

        if symbol in ETF_SYMBOLS:
            etf += mv
        if symbol in INVERSE_ETFS:
            inverse_etf += mv

        raw_s = symbol_sector.get(symbol)
        if raw_s is not None and str(raw_s).strip() != "":
            sector = str(raw_s).strip()
        else:
            sector = default_sector
        sector_pct[sector] = sector_pct.get(sector, 0.0) + (mv / account_equity * 100.0)

        theme = THEME_MAP.get(symbol, symbol)
        theme_pct[theme] = theme_pct.get(theme, 0.0) + (mv / account_equity * 100.0)

    gross = stocks_gross + abs(options_delta_adjusted_net)
    return ExposureSnapshot(
        gross_pct=(gross / account_equity) * 100.0,
        net_pct=(net / account_equity) * 100.0,
        sector_pct=sector_pct,
        theme_pct=theme_pct,
        etf_pct=(etf / account_equity) * 100.0,
        inverse_etf_pct=(inverse_etf / account_equity) * 100.0,
    )
