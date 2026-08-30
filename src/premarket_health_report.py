"""Daily pre-market readiness report for production trading."""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.daily_report_notify import send_smtp_email, send_telegram
from src.exposure import SYMBOL_SECTOR, ExposureSnapshot, compute_exposures
from src.news_catalyst import premarket_artifact_paths


@dataclass(frozen=True)
class PremarketReportSection:
    """One readiness section in the pre-market health report."""

    name: str
    ok: bool
    reason: str
    details: Mapping[str, Any]


@dataclass(frozen=True)
class PremarketHealthReport:
    """Complete pre-market readiness report."""

    generated_at: datetime
    ok: bool
    sections: list[PremarketReportSection]


def _float_value(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bool_config(config: Mapping[str, Any], path: Sequence[str], default: bool = False) -> bool:
    cur: Any = config
    for key in path:
        if not isinstance(cur, Mapping):
            return default
        cur = cur.get(key)
    if cur is None:
        return default
    return bool(cur)


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _read_json_mapping(path: Path) -> Mapping[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("payload_not_mapping")
    return payload


def _artifact_age_minutes(path: Path, payload: Mapping[str, Any], now: datetime) -> float:
    generated_at = _parse_dt(payload.get("generated_at"))
    if generated_at is None:
        generated_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return max(0.0, (now.astimezone(timezone.utc) - generated_at).total_seconds() / 60.0)


def _sequence_len(value: Any) -> int:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return len(value)
    return 0


def check_account_status(broker: Any) -> PremarketReportSection:
    """Read account state and mark blocked or unreadable accounts as not ready."""

    try:
        if callable(getattr(broker, "get_account_snapshot", None)):
            snapshot = dict(broker.get_account_snapshot() or {})
        elif callable(getattr(broker, "get_account", None)):
            account = broker.get_account()
            snapshot = {
                "equity": getattr(account, "equity", None),
                "cash": getattr(account, "cash", None),
                "buying_power": getattr(account, "buying_power", None),
                "status": getattr(account, "status", None),
                "trading_blocked": getattr(account, "trading_blocked", None),
            }
        else:
            snapshot = {"equity": broker.get_equity()}
    except Exception as exc:
        return PremarketReportSection("account", False, f"account_read_failed:{type(exc).__name__}", {})

    equity = _float_value(snapshot.get("equity"))
    blocked = bool(snapshot.get("trading_blocked") or snapshot.get("account_blocked"))
    status = str(snapshot.get("status") or "unknown")
    ok = equity > 0.0 and not blocked
    reason = "ready" if ok else ("trading_blocked" if blocked else "equity_missing")
    return PremarketReportSection(
        "account",
        ok,
        reason,
        {
            "status": status,
            "equity": equity,
            "cash": _float_value(snapshot.get("cash")),
            "buying_power": _float_value(snapshot.get("buying_power"), _float_value(snapshot.get("cash"))),
        },
    )


def check_news_status(
    config: Mapping[str, Any],
    project_root: Path,
    *,
    now: datetime,
    max_artifact_age_hours: float = 6.0,
) -> PremarketReportSection:
    """Validate pre-market news artifacts when the news job is enabled."""

    required = _bool_config(config, ("premarket_intelligence", "enabled"), True)
    if not required:
        return PremarketReportSection("news", True, "disabled", {})

    details: dict[str, Any] = {}
    missing: list[str] = []
    stale: list[str] = []
    newest_age = 0.0
    total_items = 0
    for kind, path in premarket_artifact_paths(project_root).items():
        if not path.exists():
            missing.append(kind)
            continue
        try:
            payload = _read_json_mapping(path)
            age = _artifact_age_minutes(path, payload, now)
        except Exception as exc:
            return PremarketReportSection("news", False, f"artifact_unreadable:{kind}:{type(exc).__name__}", details)
        newest_age = max(newest_age, age)
        if age > max_artifact_age_hours * 60.0:
            stale.append(kind)
        count = max(
            _sequence_len(payload.get("events")),
            _sequence_len(payload.get("catalysts")),
            _sequence_len(payload.get("rankings")),
            _sequence_len(payload.get("symbols")),
        )
        total_items += count
        details[kind] = {"path": str(path), "age_minutes": round(age, 1), "items": count}

    details["total_items"] = total_items
    details["max_age_minutes"] = round(newest_age, 1)
    if missing:
        return PremarketReportSection("news", False, "artifacts_missing", {**details, "missing": missing})
    if stale:
        return PremarketReportSection("news", False, "artifacts_stale", {**details, "stale": stale})
    if total_items <= 0:
        return PremarketReportSection("news", False, "artifacts_empty", details)
    return PremarketReportSection("news", True, "ready", details)


def check_dynamic_scan_status(
    config: Mapping[str, Any],
    project_root: Path,
    *,
    now: datetime,
    max_artifact_age_hours: float = 6.0,
) -> PremarketReportSection:
    """Validate latest dynamic pre-market rankings consumed by the scanner."""

    if not _bool_config(config, ("dynamic_universe", "enabled"), True):
        return PremarketReportSection("dynamic_scan", True, "disabled", {})

    rankings_path = project_root / "data" / "premarket" / "latest_rankings.json"
    if not rankings_path.exists():
        return PremarketReportSection("dynamic_scan", False, "rankings_missing", {"path": str(rankings_path)})
    try:
        payload = _read_json_mapping(rankings_path)
        age = _artifact_age_minutes(rankings_path, payload, now)
    except Exception as exc:
        return PremarketReportSection("dynamic_scan", False, f"rankings_unreadable:{type(exc).__name__}", {"path": str(rankings_path)})
    rankings = payload.get("rankings")
    count = _sequence_len(rankings)
    details = {"path": str(rankings_path), "age_minutes": round(age, 1), "rankings": count}
    if age > max_artifact_age_hours * 60.0:
        return PremarketReportSection("dynamic_scan", False, "rankings_stale", details)
    if count <= 0:
        return PremarketReportSection("dynamic_scan", False, "rankings_empty", details)
    return PremarketReportSection("dynamic_scan", True, "ready", details)


def check_open_orders(broker: Any) -> PremarketReportSection:
    """Summarize open broker orders."""

    if not callable(getattr(broker, "get_open_orders", None)):
        return PremarketReportSection("open_orders", True, "unsupported", {"count": 0})
    try:
        orders = list(broker.get_open_orders() or [])
    except Exception as exc:
        return PremarketReportSection("open_orders", False, f"orders_read_failed:{type(exc).__name__}", {})
    by_side: dict[str, int] = {}
    symbols: list[str] = []
    for order in orders:
        side = str(order.get("side") if isinstance(order, Mapping) else "").strip().lower() or "unknown"
        by_side[side] = by_side.get(side, 0) + 1
        symbol = str(order.get("symbol") if isinstance(order, Mapping) else "").strip().upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    reason = "none" if not orders else "open_orders_present"
    return PremarketReportSection("open_orders", True, reason, {"count": len(orders), "by_side": by_side, "symbols": symbols[:20]})


def check_exposure_summary(
    broker: Any,
    *,
    equity: float,
    default_sector: str = "unknown",
) -> PremarketReportSection:
    """Summarize current exposure from broker positions."""

    if not callable(getattr(broker, "get_positions", None)):
        return PremarketReportSection("exposure", True, "unsupported", {"gross": 0.0, "net": 0.0})
    try:
        positions = [dict(row) for row in (broker.get_positions() or []) if isinstance(row, Mapping)]
    except Exception as exc:
        return PremarketReportSection("exposure", False, f"positions_read_failed:{type(exc).__name__}", {})
    normalized = []
    for row in positions:
        if "market_value" not in row:
            row["market_value"] = _float_value(row.get("market_value_usd"))
        normalized.append(row)
    snapshot: ExposureSnapshot = compute_exposures(float(equity), normalized, SYMBOL_SECTOR, default_sector=default_sector)
    return PremarketReportSection(
        "exposure",
        True,
        "ready",
        {
            "positions": len(normalized),
            "gross": round(snapshot.gross_pct, 2),
            "net": round(snapshot.net_pct, 2),
            "sector": {k: round(v, 2) for k, v in snapshot.sector_pct.items()},
            "etf": round(snapshot.etf_pct, 2),
            "inverse_etf": round(snapshot.inverse_etf_pct, 2),
        },
    )


def build_premarket_health_report(
    *,
    broker: Any,
    config: Mapping[str, Any],
    project_root: Path,
    now: datetime | None = None,
    max_artifact_age_hours: float = 6.0,
) -> PremarketHealthReport:
    """Collect all pre-market readiness sections."""

    generated_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    account = check_account_status(broker)
    equity = _float_value(account.details.get("equity")) if account.details else 0.0
    default_sector = str((config.get("sector") or {}).get("default_sector") or "unknown") if isinstance(config.get("sector"), Mapping) else "unknown"
    sections = [
        account,
        check_news_status(config, project_root, now=generated_at, max_artifact_age_hours=max_artifact_age_hours),
        check_dynamic_scan_status(config, project_root, now=generated_at, max_artifact_age_hours=max_artifact_age_hours),
        check_open_orders(broker),
        check_exposure_summary(broker, equity=equity, default_sector=default_sector),
    ]
    return PremarketHealthReport(generated_at=generated_at, ok=all(section.ok for section in sections), sections=sections)


def render_premarket_health_text(report: PremarketHealthReport, *, user_label: str = "default") -> str:
    """Render a compact text report for logs and notifications."""

    status = "READY" if report.ok else "NOT READY"
    lines = [
        f"AlgoSphere pre-market health: {status}",
        f"User: {user_label}",
        f"Generated: {report.generated_at.strftime('%Y-%m-%d %H:%M UTC')}",
    ]
    for section in report.sections:
        marker = "OK" if section.ok else "FAIL"
        lines.append(f"{marker} {section.name}: {section.reason}")
        if section.name == "account":
            lines.append(
                "  equity=${equity:,.2f} cash=${cash:,.2f} buying_power=${buying_power:,.2f} status={status}".format(
                    **section.details
                )
            )
        elif section.name == "open_orders":
            lines.append(f"  count={section.details.get('count', 0)} symbols={','.join(section.details.get('symbols') or []) or '-'}")
        elif section.name == "exposure":
            lines.append(
                "  positions={positions} gross={gross:.2f}% net={net:.2f}% inverse_etf={inverse_etf:.2f}%".format(
                    **section.details
                )
            )
        elif section.name in ("news", "dynamic_scan"):
            compact = {k: v for k, v in section.details.items() if k not in ("path",)}
            lines.append(f"  {compact}")
    return "\n".join(lines)


def render_premarket_health_html(report: PremarketHealthReport, *, user_label: str = "default") -> str:
    """Render the readiness report as HTML."""

    def esc(value: Any) -> str:
        return html.escape(str(value), quote=True)

    rows = []
    for section in report.sections:
        rows.append(
            "<tr>"
            f"<td>{esc(section.name)}</td>"
            f"<td class=\"{'ok' if section.ok else 'fail'}\">{'OK' if section.ok else 'FAIL'}</td>"
            f"<td>{esc(section.reason)}</td>"
            f"<td><pre>{esc(json.dumps(dict(section.details), sort_keys=True, default=str, indent=2))}</pre></td>"
            "</tr>"
        )
    status = "READY" if report.ok else "NOT READY"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>Pre-market health - {esc(user_label)}</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif; margin: 1.25rem; color: #172033; }}
    h1 {{ margin-bottom: 0.25rem; }}
    .sub {{ color: #526173; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
    th, td {{ border: 1px solid #d8dee8; padding: 0.55rem; text-align: left; vertical-align: top; }}
    th {{ background: #f3f6fa; }}
    pre {{ white-space: pre-wrap; margin: 0; font-size: 0.82rem; }}
    .ok {{ color: #047857; font-weight: 700; }}
    .fail {{ color: #b91c1c; font-weight: 700; }}
  </style>
</head>
<body>
  <h1>Pre-market health: {esc(status)}</h1>
  <div class="sub">User: {esc(user_label)} · Generated: {esc(report.generated_at.strftime('%Y-%m-%d %H:%M UTC'))}</div>
  <table>
    <thead><tr><th>Section</th><th>Status</th><th>Reason</th><th>Details</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</body>
</html>
"""


def save_premarket_health_report(report: PremarketHealthReport, path: str | Path, *, user_label: str = "default") -> Path:
    """Write the HTML pre-market report and return the path."""

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_premarket_health_html(report, user_label=user_label), encoding="utf-8")
    return out


def deliver_premarket_health_report(
    report: PremarketHealthReport,
    *,
    html_path: Path | None = None,
    user_label: str = "default",
) -> None:
    """Best-effort notification delivery for the pre-market health report."""

    summary = render_premarket_health_text(report, user_label=user_label)
    subject = f"AlgoSphere pre-market health - {user_label} - {'READY' if report.ok else 'NOT READY'}"
    send_telegram(summary, document=html_path)
    send_smtp_email(subject, summary, html_attachment=html_path)
