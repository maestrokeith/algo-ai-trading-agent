"""Daily portfolio dashboard: HTML + optional Plotly charts under ``reports/`` (UTF-8)."""
from __future__ import annotations

import argparse
import datetime
import html
import importlib.util
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np

from src.config_loader import load_config
from src.daily_trading_report import collect_daily_trading_report_data
from src.trade_postmortem import build_daily_postmortem
from src.user_manager import UserManager

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_HTML_PATH = _PROJECT_ROOT / "reports" / "daily.html"

_CHART_LAYOUT = dict(
    template="plotly_dark",
    paper_bgcolor="#0f1419",
    plot_bgcolor="#141c28",
    font=dict(color="#e7ecf3", size=11),
    height=320,
    margin=dict(l=55, r=30, t=48, b=45),
    showlegend=False,
    xaxis=dict(gridcolor="#2a3545", zeroline=False),
    yaxis=dict(gridcolor="#2a3545", zeroline=True, zerolinecolor="#4a5568"),
)


def plotly_available() -> bool:
    """True when ``plotly`` is installed (charts enabled)."""
    return importlib.util.find_spec("plotly") is not None


def save_html(report_html: str, path: str | Path | None = None) -> Path:
    """Write *report_html* to ``reports/daily.html`` under the project root (unless *path* is set)."""
    out = Path(path) if path is not None else _DEFAULT_HTML_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report_html, encoding="utf-8")
    return out


def _drawdown_pct_series(equity: Sequence[float]) -> list[float]:
    eq = np.asarray(equity, dtype=float)
    peak = np.maximum.accumulate(eq)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peak > 0, (eq - peak) / peak * 100.0, 0.0)
    return [float(x) for x in dd]


def _coerce_history(
    portfolio_history: Mapping[str, Any] | None,
    *,
    today_equity: float,
    today_pnl: float,
    today: datetime.date,
) -> tuple[list[str], list[float], list[float]]:
    """Return (dates_iso, equity, daily_pnl_usd) for charts (at least two points when possible)."""
    if portfolio_history:
        dates = [str(d) for d in portfolio_history.get("dates") or []]
        eq_raw = portfolio_history.get("equity") or []
        pnl_raw = portfolio_history.get("daily_pnl")
        try:
            equity = [float(x) for x in eq_raw]
        except (TypeError, ValueError):
            equity = []
        if len(dates) == len(equity) and len(equity) >= 1:
            if pnl_raw is not None and len(pnl_raw) == len(equity):
                try:
                    daily = [float(x) for x in pnl_raw]
                except (TypeError, ValueError):
                    daily = [0.0] + [
                        equity[i] - equity[i - 1] for i in range(1, len(equity))
                    ]
            else:
                daily = [0.0] + [equity[i] - equity[i - 1] for i in range(1, len(equity))]
            return dates, equity, daily

    y = today - datetime.timedelta(days=1)
    return (
        [y.isoformat(), today.isoformat()],
        [float(today_equity) - float(today_pnl), float(today_equity)],
        [0.0, float(today_pnl)],
    )


def _charts_html_fragment(
    dates: list[str],
    equity: list[float],
    daily_pnl: list[float],
) -> str:
    if not plotly_available():
        return (
            '<p class="muted">Install plotly (&gt;=5.18) from requirements.txt for interactive '
            "equity, daily PnL, and drawdown charts.</p>"
        )

    import plotly.graph_objects as go

    dd_pct = _drawdown_pct_series(equity)
    if len(daily_pnl) != len(dates):
        daily_pnl = [0.0] + [equity[i] - equity[i - 1] for i in range(1, len(equity))]

    fig_eq = go.Figure(
        data=[
            go.Scatter(
                x=dates,
                y=equity,
                mode="lines",
                name="Equity",
                line=dict(color="#3d8bfd", width=2),
                fill="tozeroy",
                fillcolor="rgba(61,139,253,0.12)",
            )
        ]
    )
    fig_eq.update_layout(title="Equity curve", yaxis_title="USD", **_CHART_LAYOUT)

    colors = ["#3ecf8e" if v >= 0 else "#f87171" for v in daily_pnl]
    fig_pnl = go.Figure(
        data=[go.Bar(x=dates, y=daily_pnl, marker_color=colors, name="Daily PnL")]
    )
    fig_pnl.update_layout(title="Daily PnL ($)", yaxis_title="USD", **_CHART_LAYOUT)

    fig_dd = go.Figure(
        data=[
            go.Scatter(
                x=dates,
                y=dd_pct,
                mode="lines",
                name="Drawdown",
                line=dict(color="#f87171", width=1.5),
                fill="tozeroy",
                fillcolor="rgba(248,113,113,0.2)",
            )
        ]
    )
    fig_dd.update_layout(title="Drawdown from peak", yaxis_title="%", **_CHART_LAYOUT)

    parts: list[str] = []
    for i, fig in enumerate((fig_eq, fig_pnl, fig_dd)):
        parts.append(
            '<div class="chart-wrap">'
            + fig.to_html(
                full_html=False,
                include_plotlyjs="inline" if i == 0 else False,
                config={"displayModeBar": True, "responsive": True},
            )
            + "</div>"
        )
    return "".join(parts)


def _coerce_total_contributed(account: Mapping[str, Any]) -> float | None:
    """Return configured lifetime contributed capital, when available."""
    candidates = (
        account.get("total_contributed_usd"),
        account.get("total_contributed"),
        account.get("capital_contributed_usd"),
        account.get("capital_contributed"),
    )
    for raw in candidates:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _is_dynamic_trade(trade: Mapping[str, Any]) -> bool:
    marker = " ".join(
        str(trade.get(key) or "")
        for key in ("strategy", "source", "entry_source", "signal_source", "client_order_id")
    ).lower()
    return "dynamic" in marker


def _float_or_none(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dynamic_trade_return_pct(trade: Mapping[str, Any]) -> float | None:
    for key in ("return_pct", "pnl_pct", "realized_return_pct", "profit_loss_pct"):
        value = _float_or_none(trade.get(key))
        if value is not None:
            return value
    pnl = _float_or_none(trade.get("pnl"))
    qty = _float_or_none(trade.get("qty"))
    price = _float_or_none(trade.get("filled_avg_price"))
    if pnl is None or qty is None or price is None:
        return None
    notional = abs(qty * price)
    if notional <= 0:
        return None
    return (pnl / notional) * 100.0


def _dynamic_news_score_bucket(score: float | None) -> str:
    if score is None:
        return "Unknown"
    if score >= 7:
        return "7+"
    if score >= 4:
        return "4-6"
    if score >= 1:
        return "1-3"
    return "0"


def _dynamic_performance_summary(
    trades: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    dynamic_trades = [t for t in trades if _is_dynamic_trade(t)]
    wins = [t for t in dynamic_trades if float(t.get("pnl") or 0.0) > 0.0]
    losses = [t for t in dynamic_trades if float(t.get("pnl") or 0.0) < 0.0]
    returns = [
        ret
        for ret in (_dynamic_trade_return_pct(t) for t in dynamic_trades)
        if ret is not None
    ]
    sorted_by_pnl = sorted(dynamic_trades, key=lambda t: float(t.get("pnl") or 0.0))
    news_distribution = {"0": 0, "1-3": 0, "4-6": 0, "7+": 0, "Unknown": 0}
    for trade in dynamic_trades:
        news_distribution[_dynamic_news_score_bucket(_float_or_none(trade.get("news_score")))] += 1
    return {
        "trades": dynamic_trades,
        "count": len(dynamic_trades),
        "pnl": sum(float(t.get("pnl") or 0.0) for t in dynamic_trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": (len(wins) / len(dynamic_trades) * 100.0) if dynamic_trades else 0.0,
        "avg_return_pct": (sum(returns) / len(returns)) if returns else 0.0,
        "return_sample_count": len(returns),
        "best": sorted_by_pnl[-1] if sorted_by_pnl else None,
        "worst": sorted_by_pnl[0] if sorted_by_pnl else None,
        "news_distribution": news_distribution,
    }


def _trade_label(trade: Mapping[str, Any] | None) -> str:
    if not trade:
        return "-"
    symbol = str(trade.get("symbol") or "").strip().upper() or "-"
    pnl = float(trade.get("pnl") or 0.0)
    ret = _dynamic_trade_return_pct(trade)
    ret_s = f", {ret:,.2f}%" if ret is not None else ""
    return f"{symbol} (${pnl:,.2f}{ret_s})"


def generate_report_html(
    account: Mapping[str, Any],
    positions: list[Mapping[str, Any]],
    trades: list[Mapping[str, Any]],
    exposure: Mapping[str, Any],
    portfolio_history: Mapping[str, Any] | None = None,
) -> str:
    today = datetime.date.today()
    equity = float(account["equity"])
    pnl = float(account["pnl_today"])
    total_contributed = _coerce_total_contributed(account)
    lifetime_profit = (
        float(equity) - float(total_contributed)
        if total_contributed is not None
        else None
    )
    total_return_pct = (
        (float(lifetime_profit) / float(total_contributed)) * 100.0
        if total_contributed is not None and float(total_contributed) > 0
        else None
    )
    total_contributed_display = (
        f"${float(total_contributed):,.2f}" if total_contributed is not None else "Not set"
    )
    lifetime_profit_value = float(lifetime_profit) if lifetime_profit is not None else 0.0
    lifetime_profit_display = (
        f"${lifetime_profit_value:,.2f}" if total_contributed is not None else "Not set"
    )
    total_return_value = float(total_return_pct) if total_return_pct is not None else 0.0
    total_return_display = (
        f"{total_return_value:,.2f}%" if total_contributed is not None else "Not set"
    )
    gross = float(exposure["gross"])
    net = float(exposure["net"])
    sector = exposure.get("sector") or {}

    strat_pnl: defaultdict[str, float] = defaultdict(float)
    for t in trades:
        strat_pnl[str(t["strategy"])] += float(t["pnl"])

    wins = [t for t in trades if float(t.get("pnl") or 0.0) > 0]
    losses = [t for t in trades if float(t.get("pnl") or 0.0) < 0]
    dynamic_summary = _dynamic_performance_summary(trades)
    dynamic_trades = dynamic_summary["trades"]
    dynamic_pnl = float(dynamic_summary["pnl"])

    alerts: list[tuple[str, float]] = []
    for sec, pct in sector.items():
        if float(pct) > 35:
            alerts.append((str(sec), float(pct)))

    def esc(s: Any) -> str:
        return html.escape(str(s), quote=True)

    rows_pos = []
    for p in positions:
        sym = esc(p.get("symbol", ""))
        mv = float(p.get("market_value") or 0)
        pnl_r = float(p.get("pnl") or 0)
        rows_pos.append(
            f"<tr><td>{sym}</td><td class=\"num\">${mv:,.0f}</td><td class=\"num\">{pnl_r:,.0f}</td></tr>"
        )

    rows_strat = []
    for k, v in sorted(strat_pnl.items(), key=lambda kv: kv[0]):
        rows_strat.append(
            f"<tr><td>{esc(k)}</td><td class=\"num\">${v:,.2f}</td></tr>"
        )

    rows_trades = []
    for t in trades:
        pnl_v = float(t.get("pnl") or 0.0)
        price = t.get("filled_avg_price")
        price_s = f"${float(price):,.2f}" if price is not None and str(price).strip() != "" else "-"
        qty = float(t.get("qty") or 0.0)
        qty_s = f"{qty:,.4f}".rstrip("0").rstrip(".")
        rows_trades.append(
            "<tr>"
            f"<td>{esc(t.get('symbol', ''))}</td>"
            f"<td>{esc(t.get('side', ''))}</td>"
            f"<td>{esc(t.get('strategy', ''))}</td>"
            f"<td class=\"num\">{qty_s}</td>"
            f"<td class=\"num\">{price_s}</td>"
            f"<td class=\"num {'pnl-pos' if pnl_v >= 0 else 'pnl-neg'}\">${pnl_v:,.2f}</td>"
            "</tr>"
        )

    dynamic_news_rows = []
    for bucket in ("0", "1-3", "4-6", "7+", "Unknown"):
        dynamic_news_rows.append(
            f"<tr><td>{esc(bucket)}</td><td class=\"num\">{int(dynamic_summary['news_distribution'][bucket])}</td></tr>"
        )

    dynamic_best = _trade_label(dynamic_summary["best"])
    dynamic_worst = _trade_label(dynamic_summary["worst"])
    postmortem = build_daily_postmortem(trades)
    postmortem_winner_rows = [
        f"<tr><td>{esc(row.symbol)}</td><td class=\"num\">${row.pnl:,.2f}</td><td>{esc(row.explanation)}</td></tr>"
        for row in postmortem.winners
    ]
    postmortem_loser_rows = [
        f"<tr><td>{esc(row.symbol)}</td><td class=\"num\">${row.pnl:,.2f}</td><td>{esc(row.explanation)}</td></tr>"
        for row in postmortem.losers
    ]
    postmortem_suggestions = "".join(
        f"<li>{esc(item)}</li>" for item in postmortem.suggestions
    )

    alert_block = ""
    if alerts:
        items = "".join(f"<li>{esc(s)} exposure high: {pct:.1f}%</li>" for s, pct in alerts)
        alert_block = f"<section class=\"alerts\"><h2>Risk alerts</h2><ul>{items}</ul></section>"
    else:
        alert_block = "<section class=\"alerts muted\"><p>No sector exposure above 35%.</p></section>"

    pnl_class = "pnl-pos" if pnl >= 0 else "pnl-neg"

    d_ch, e_ch, pnl_ch = _coerce_history(
        portfolio_history, today_equity=equity, today_pnl=pnl, today=today
    )
    charts_html = _charts_html_fragment(d_ch, e_ch, pnl_ch)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Daily report — {esc(today)}</title>
  <style>
    :root {{
      --bg: #0f1419;
      --card: #1a2332;
      --text: #e7ecf3;
      --muted: #8b9cb3;
      --accent: #3d8bfd;
      --pos: #3ecf8e;
      --neg: #f87171;
    }}
    body {{
      font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      margin: 0;
      padding: 1.25rem;
      line-height: 1.45;
    }}
    h1 {{ font-size: 1.35rem; margin: 0 0 0.25rem; }}
    .sub {{ color: var(--muted); font-size: 0.9rem; margin-bottom: 1.25rem; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr));
      gap: 0.75rem;
      margin-bottom: 1.25rem;
    }}
    .card {{
      background: var(--card);
      border-radius: 10px;
      padding: 0.85rem 1rem;
      border: 1px solid #2a3545;
    }}
    .card label {{
      display: block;
      font-size: 0.75rem;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .card .val {{ font-size: 1.25rem; font-weight: 600; margin-top: 0.2rem; }}
    .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    section {{ margin-bottom: 1.5rem; }}
    h2 {{
      font-size: 0.95rem;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.06em;
      margin: 0 0 0.5rem;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--card);
      border-radius: 10px;
      overflow: hidden;
      border: 1px solid #2a3545;
    }}
    th, td {{ padding: 0.55rem 0.75rem; }}
    th {{
      text-align: left;
      font-size: 0.72rem;
      color: var(--muted);
      text-transform: uppercase;
      background: #141c28;
    }}
    tr:not(:last-child) td {{ border-bottom: 1px solid #2a3545; }}
    .pnl-pos {{ color: var(--pos); }}
    .pnl-neg {{ color: var(--neg); }}
    .alerts ul {{ margin: 0; padding-left: 1.1rem; }}
    .alerts li {{ margin: 0.25rem 0; }}
    .muted {{ color: var(--muted); font-size: 0.9rem; }}
    footer {{ color: var(--muted); font-size: 0.8rem; margin-top: 2rem; }}
    .chart-wrap {{
      background: var(--card);
      border: 1px solid #2a3545;
      border-radius: 10px;
      margin-bottom: 1rem;
      overflow: hidden;
    }}
    .chart-wrap .plotly-graph-div {{ width: 100% !important; }}
  </style>
</head>
<body>
  <header>
    <h1>Daily dashboard</h1>
    <p class="sub">{esc(today)} · AlgoSphere</p>
  </header>
  <div class="grid">
    <div class="card"><label>Equity</label><div class="val">${equity:,.2f}</div></div>
    <div class="card"><label>Daily PnL</label><div class="val {pnl_class}">${pnl:,.2f}</div></div>
    <div class="card"><label>Total contributed</label><div class="val">{total_contributed_display}</div></div>
    <div class="card"><label>Lifetime profit</label><div class="val {'pnl-pos' if lifetime_profit_value >= 0 else 'pnl-neg'}">{lifetime_profit_display}</div></div>
    <div class="card"><label>Total return</label><div class="val {'pnl-pos' if total_return_value >= 0 else 'pnl-neg'}">{total_return_display}</div></div>
    <div class="card"><label>Gross exposure</label><div class="val">{gross:.1f}%</div></div>
    <div class="card"><label>Net exposure</label><div class="val">{net:.1f}%</div></div>
    <div class="card"><label>Trades</label><div class="val">{len(trades)}</div></div>
    <div class="card"><label>Winners / losers</label><div class="val">{len(wins)} / {len(losses)}</div></div>
    <div class="card"><label>Dynamic PnL</label><div class="val {'pnl-pos' if dynamic_pnl >= 0 else 'pnl-neg'}">${dynamic_pnl:,.2f}</div></div>
  </div>
  <section>
    <h2>Performance</h2>
    {charts_html}
  </section>
  <section>
    <h2>Positions</h2>
    <table>
      <thead><tr><th>Symbol</th><th class="num">Market value</th><th class="num">PnL</th></tr></thead>
      <tbody>{"".join(rows_pos) or "<tr><td colspan=\"3\" class=\"muted\">No open positions</td></tr>"}</tbody>
    </table>
  </section>
  <section>
    <h2>PnL by strategy</h2>
    <table>
      <thead><tr><th>Strategy</th><th class="num">PnL</th></tr></thead>
      <tbody>{"".join(rows_strat) or "<tr><td colspan=\"2\" class=\"muted\">No trades</td></tr>"}</tbody>
    </table>
  </section>
  <section>
    <h2>Trades</h2>
    <p>Total: {len(trades)} · Winners: {len(wins)} · Losers: {len(losses)} · Dynamic: {len(dynamic_trades)} · Dynamic PnL: ${dynamic_pnl:,.2f}</p>
    <table>
      <thead><tr><th>Symbol</th><th>Side</th><th>Strategy</th><th class="num">Qty</th><th class="num">Fill</th><th class="num">PnL</th></tr></thead>
      <tbody>{"".join(rows_trades) or "<tr><td colspan=\"6\" class=\"muted\">No trades</td></tr>"}</tbody>
    </table>
  </section>
  <section>
    <h2>Dynamic performance dashboard</h2>
    <div class="grid">
      <div class="card"><label>Dynamic trades</label><div class="val">{int(dynamic_summary['count'])}</div></div>
      <div class="card"><label>Dynamic win rate</label><div class="val">{float(dynamic_summary['win_rate_pct']):,.1f}%</div></div>
      <div class="card"><label>Average dynamic return</label><div class="val">{float(dynamic_summary['avg_return_pct']):,.2f}%</div></div>
      <div class="card"><label>Dynamic PnL</label><div class="val {'pnl-pos' if dynamic_pnl >= 0 else 'pnl-neg'}">${dynamic_pnl:,.2f}</div></div>
      <div class="card"><label>Best dynamic trade</label><div class="val">{esc(dynamic_best)}</div></div>
      <div class="card"><label>Worst dynamic trade</label><div class="val">{esc(dynamic_worst)}</div></div>
    </div>
    <table>
      <thead><tr><th>News score</th><th class="num">Dynamic trades</th></tr></thead>
      <tbody>{"".join(dynamic_news_rows)}</tbody>
    </table>
    <p class="muted">Average return sample: {int(dynamic_summary['return_sample_count'])} dynamic trades.</p>
  </section>
  <section>
    <h2>Trade postmortem</h2>
    <table>
      <thead><tr><th colspan="3">Winners</th></tr></thead>
      <tbody>{"".join(postmortem_winner_rows) or "<tr><td colspan=\"3\" class=\"muted\">No winning realized trades</td></tr>"}</tbody>
    </table>
    <table>
      <thead><tr><th colspan="3">Losers</th></tr></thead>
      <tbody>{"".join(postmortem_loser_rows) or "<tr><td colspan=\"3\" class=\"muted\">No losing realized trades</td></tr>"}</tbody>
    </table>
    <div class="card"><label>Parameter review</label><ul>{postmortem_suggestions}</ul></div>
  </section>
  {alert_block}
  <footer>Generated {esc(datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))}</footer>
</body>
</html>
"""


def generate_report(
    account: Mapping[str, Any],
    positions: list[Mapping[str, Any]],
    trades: list[Mapping[str, Any]],
    exposure: Mapping[str, Any],
    output_path: str | Path | None = None,
    portfolio_history: Mapping[str, Any] | None = None,
) -> Path:
    """Build HTML and save (default ``reports/daily.html``); returns the written path."""
    report_html = generate_report_html(
        account, positions, trades, exposure, portfolio_history=portfolio_history
    )
    return save_html(report_html, path=output_path)


def _demo() -> None:
    days = 40
    base = 98_000.0
    rng = np.random.default_rng(42)
    walk = np.cumsum(rng.normal(0, 400, size=days))
    eq_s = base + walk
    eq_s = np.maximum(eq_s, 85_000.0)
    d0 = datetime.date.today() - datetime.timedelta(days=days - 1)
    dates = [(d0 + datetime.timedelta(days=i)).isoformat() for i in range(days)]
    dpnl = [0.0] + [float(eq_s[i] - eq_s[i - 1]) for i in range(1, days)]
    p = generate_report(
        account={"equity": float(eq_s[-1]), "pnl_today": float(dpnl[-1])},
        positions=[
            {"symbol": "SPY", "market_value": 25_000.0, "pnl": 120},
            {"symbol": "QQQ", "market_value": 15_000.0, "pnl": -40},
        ],
        trades=[
            {"strategy": "trend_long", "pnl": 180.0},
            {"strategy": "trend_long", "pnl": -30.0},
            {"strategy": "bear_hedge", "pnl": 100.0},
        ],
        exposure={
            "gross": 42.0,
            "net": 38.0,
            "sector": {"technology": 28.0, "broad_market": 40.0},
        },
        portfolio_history={
            "dates": dates,
            "equity": [float(x) for x in eq_s],
            "daily_pnl": dpnl,
        },
    )
    print("Wrote", p)


def _today_et() -> datetime.date:
    return datetime.datetime.now(ZoneInfo("America/New_York")).date()


def _parse_report_date(raw: str | None) -> datetime.date:
    if raw is None or not str(raw).strip():
        return _today_et()
    try:
        return datetime.date.fromisoformat(str(raw).strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def _slug(value: str) -> str:
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in str(value))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate the daily AlgoSphere HTML trading report."
    )
    parser.add_argument("--date", type=_parse_report_date, default=None, help="Trading date YYYY-MM-DD; defaults to today in US/Eastern.")
    parser.add_argument("--user", default="default", help="User id from config/users.yaml; defaults to default.")
    parser.add_argument("--config", type=Path, default=_PROJECT_ROOT / "config" / "default.yaml", help="Path to default config YAML.")
    parser.add_argument("--users", type=Path, default=_PROJECT_ROOT / "config" / "users.yaml", help="Path to users YAML.")
    parser.add_argument("--output", type=Path, default=None, help="Output HTML path. Defaults to reports/daily.html for default user, otherwise reports/daily_USER.html.")
    parser.add_argument("--demo", action="store_true", help="Generate a demo report with synthetic data.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.demo:
        _demo()
        return 0

    report_date = args.date or _today_et()
    config = load_config(args.config)
    user_manager = UserManager(config, users_path=args.users)
    broker = user_manager.get_broker(str(args.user))
    user_ctx = user_manager.get_user(str(args.user))
    report_data = collect_daily_trading_report_data(
        broker=broker,
        config=user_ctx.config,
        trade_date=report_date,
    )
    output_path = args.output
    if output_path is None and str(args.user) != "default":
        output_path = _PROJECT_ROOT / "reports" / f"daily_{_slug(str(args.user))}.html"
    written = generate_report(
        account=report_data.account,
        positions=report_data.positions,
        trades=report_data.trades,
        exposure=report_data.exposure,
        output_path=output_path,
        portfolio_history=report_data.portfolio_history,
    )
    print("Wrote", written)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
