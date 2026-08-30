"""Global risk guardrails for self-managing trading operation."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from src.exposure import SYMBOL_SECTOR
from src.options_premium_risk import is_option_symbol
from src.risk_limits import parse_allocation_fraction

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RiskAlert:
    """One risk guardrail alert."""

    code: str
    severity: str
    message: str
    value: float | int | str | None = None
    limit: float | int | str | None = None


@dataclass(frozen=True)
class GlobalRiskStatus:
    """Global risk decision for the current account state."""

    allowed: bool
    kill_switch_active: bool
    alerts: list[RiskAlert] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking_alerts(self) -> list[RiskAlert]:
        """Alerts that should stop new risk."""
        return [alert for alert in self.alerts if alert.severity == "block"]

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot."""
        return {
            "allowed": self.allowed,
            "kill_switch_active": self.kill_switch_active,
            "alerts": [alert.__dict__ for alert in self.alerts],
            "metrics": dict(self.metrics),
        }


def kill_switch_path(user_id: str = "default", *, data_dir: Path | None = None) -> Path:
    """Path for manual kill-switch state."""
    root = data_dir or (Path(__file__).resolve().parents[1] / "data")
    return root / f"global_risk_kill_switch_{user_id}.json"


def set_kill_switch(
    active: bool,
    *,
    user_id: str = "default",
    reason: str = "",
    data_dir: Path | None = None,
) -> None:
    """Persist manual kill-switch state."""
    path = kill_switch_path(user_id, data_dir=data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "active": bool(active),
        "reason": str(reason or ""),
        "updated_date": date.today().isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def get_kill_switch(
    *,
    user_id: str = "default",
    data_dir: Path | None = None,
) -> tuple[bool, str]:
    """Load manual kill-switch state."""
    path = kill_switch_path(user_id, data_dir=data_dir)
    if not path.exists():
        return False, ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Unreadable kill switch state at %s", path, exc_info=True)
        return True, "kill_switch_state_unreadable"
    return bool(payload.get("active")), str(payload.get("reason") or "")


def _as_float(raw: Any, default: float = 0.0) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value != value:
        return default
    return value


def _position_symbol(position: Mapping[str, Any]) -> str:
    return str(position.get("symbol") or "").strip().upper()


def _position_market_value(position: Mapping[str, Any]) -> float:
    return abs(_as_float(position.get("market_value")))


def _side_sign(position: Mapping[str, Any]) -> float:
    return -1.0 if str(position.get("side") or "long").strip().lower() == "short" else 1.0


def _max_consecutive_losses(trades: Sequence[Mapping[str, Any]]) -> int:
    count = 0
    for trade in reversed(list(trades)):
        pnl = _as_float(trade.get("pnl"))
        if pnl < 0:
            count += 1
            continue
        if pnl > 0:
            break
    return count


def _config_limit(config: Mapping[str, Any] | None, key: str) -> float:
    raw = ((config or {}).get("global_risk") or {}).get(key)
    if raw is None:
        raw = ((config or {}).get("risk") or {}).get(key)
    return parse_allocation_fraction(raw)


def evaluate_global_risk(
    *,
    equity: float,
    peak_equity: float | None = None,
    last_equity: float | None = None,
    positions: Sequence[Mapping[str, Any]] = (),
    trades: Sequence[Mapping[str, Any]] = (),
    config: Mapping[str, Any] | None = None,
    user_id: str = "default",
    data_dir: Path | None = None,
) -> GlobalRiskStatus:
    """Evaluate account-level guardrails and return block/warn alerts."""
    equity_f = max(0.0, _as_float(equity))
    peak_f = _as_float(peak_equity, equity_f) if peak_equity is not None else equity_f
    last_f = _as_float(last_equity, equity_f) if last_equity is not None else equity_f
    alerts: list[RiskAlert] = []
    metrics: dict[str, Any] = {}

    active, kill_reason = get_kill_switch(user_id=user_id, data_dir=data_dir)
    if active:
        alerts.append(
            RiskAlert(
                "kill_switch",
                "block",
                kill_reason or "manual kill switch active",
                value="active",
                limit="inactive",
            )
        )

    daily_loss_limit = _config_limit(config, "daily_loss_limit_pct") or _config_limit(
        config, "max_daily_loss_pct"
    )
    daily_pnl_pct = 0.0
    if last_f > 0:
        daily_pnl_pct = ((equity_f - last_f) / last_f) * 100.0
    metrics["daily_pnl_pct"] = daily_pnl_pct
    if daily_loss_limit > 0 and daily_pnl_pct <= -(daily_loss_limit * 100.0):
        alerts.append(
            RiskAlert(
                "daily_loss_limit",
                "block",
                "daily loss limit breached",
                value=round(daily_pnl_pct, 4),
                limit=-(daily_loss_limit * 100.0),
            )
        )

    max_dd_limit = _config_limit(config, "max_drawdown_pct")
    drawdown_pct = 0.0
    if peak_f > 0:
        drawdown_pct = ((equity_f - peak_f) / peak_f) * 100.0
    metrics["drawdown_pct"] = drawdown_pct
    if max_dd_limit > 0 and drawdown_pct <= -(max_dd_limit * 100.0):
        alerts.append(
            RiskAlert(
                "max_drawdown",
                "block",
                "maximum drawdown breached",
                value=round(drawdown_pct, 4),
                limit=-(max_dd_limit * 100.0),
            )
        )

    gr = (config or {}).get("global_risk") or {}
    max_losses = int(_as_float(gr.get("consecutive_loss_lockout"), 0.0))
    consecutive_losses = _max_consecutive_losses(trades)
    metrics["consecutive_losses"] = consecutive_losses
    if max_losses > 0 and consecutive_losses >= max_losses:
        alerts.append(
            RiskAlert(
                "consecutive_loss_lockout",
                "block",
                "consecutive loss lockout active",
                value=consecutive_losses,
                limit=max_losses,
            )
        )

    symbol_cap = _config_limit(config, "max_symbol_exposure_pct")
    sector_cap = _config_limit(config, "max_sector_exposure_pct")
    options_cap = _config_limit(config, "max_options_exposure_pct")
    symbol_exposure: dict[str, float] = {}
    sector_exposure: dict[str, float] = {}
    options_exposure = 0.0
    if equity_f > 0:
        for position in positions:
            symbol = _position_symbol(position)
            if not symbol:
                continue
            mv = _position_market_value(position)
            pct = (mv / equity_f) * 100.0
            symbol_exposure[symbol] = symbol_exposure.get(symbol, 0.0) + pct
            sector = str(position.get("sector") or SYMBOL_SECTOR.get(symbol) or "unknown")
            sector_exposure[sector] = sector_exposure.get(sector, 0.0) + pct
            if is_option_symbol(symbol) or str(position.get("asset_class") or "").lower() == "option":
                options_exposure += pct
            metrics["net_exposure_pct"] = metrics.get("net_exposure_pct", 0.0) + (
                _side_sign(position) * pct
            )
    metrics["symbol_exposure_pct"] = symbol_exposure
    metrics["sector_exposure_pct"] = sector_exposure
    metrics["options_exposure_pct"] = options_exposure

    for symbol, pct in symbol_exposure.items():
        if symbol_cap > 0 and pct > symbol_cap * 100.0:
            alerts.append(
                RiskAlert(
                    "max_symbol_exposure",
                    "block",
                    f"{symbol} exposure exceeds cap",
                    value=round(pct, 4),
                    limit=symbol_cap * 100.0,
                )
            )
    for sector, pct in sector_exposure.items():
        if sector_cap > 0 and pct > sector_cap * 100.0:
            alerts.append(
                RiskAlert(
                    "max_sector_exposure",
                    "block",
                    f"{sector} exposure exceeds cap",
                    value=round(pct, 4),
                    limit=sector_cap * 100.0,
                )
            )
    if options_cap > 0 and options_exposure > options_cap * 100.0:
        alerts.append(
            RiskAlert(
                "max_options_exposure",
                "block",
                "options exposure exceeds cap",
                value=round(options_exposure, 4),
                limit=options_cap * 100.0,
            )
        )

    allowed = not any(alert.severity == "block" for alert in alerts)
    return GlobalRiskStatus(
        allowed=allowed,
        kill_switch_active=active,
        alerts=alerts,
        metrics=metrics,
    )


__all__ = [
    "GlobalRiskStatus",
    "RiskAlert",
    "evaluate_global_risk",
    "get_kill_switch",
    "kill_switch_path",
    "set_kill_switch",
]
