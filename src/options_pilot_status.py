"""Read-only live options pilot status and recent log summarization."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from src.config_loader import deep_merge, load_config
from src.options_config import options_live_pilot_enabled, options_mode


OPTION_LANE_MARKERS = (
    "OPTIONS_ENTRY_LANE",
    "OPTIONS_SCAN_RESULT",
    "OPTIONS_CHAIN_SUMMARY",
    "PAPER_ONLY_OPTIONS_SKIPPED",
    "OPTIONS_NO_TRADE",
)
OPTION_ORDER_MARKERS = (
    "OPTIONS_ORDER_INTENT",
    "OPTIONS_ORDER_SUBMITTED",
    "OPTIONS_LIVE_PILOT_PLACED",
    "OPTION_ORDER_INTENT",
    "OPTION_ORDER_SUBMITTED",
)


@dataclass(frozen=True)
class OptionsPilotStatus:
    """Resolved options pilot status plus recent production log evidence."""

    config_enabled: bool
    live_pilot_enabled: bool
    mode: str
    exposure_limit: str
    max_positions: str
    max_contracts: str
    allowed_symbols: list[str]
    latest_entry_lane_logs: list[str]
    latest_options_order_logs: list[str]
    reason_if_no_orders: str


def _fmt_bool(value: bool) -> str:
    return str(bool(value)).lower()


def _fmt_raw(value: Any, *, default: str = "unset") -> str:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip()


def load_effective_options_config(root: str | Path, *, env_name: str = "live", user_id: str | None = None) -> dict[str, Any]:
    """Load effective options config without resolving broker credentials."""
    repo_root = Path(root)
    try:
        config = load_config(repo_root / "config" / "default.yaml")
    except Exception:
        return {}
    users_path = repo_root / "config" / "users.yaml"
    selected = user_id or ("live_bot" if env_name == "live" else "paper_bot")
    if users_path.exists():
        payload = yaml.safe_load(users_path.read_text(encoding="utf-8")) or {}
        entries = payload.get("users") if isinstance(payload, Mapping) else []
        for entry in entries or []:
            if not isinstance(entry, Mapping):
                continue
            if str(entry.get("id") or "") != selected:
                continue
            overrides = entry.get("overrides") if isinstance(entry.get("overrides"), Mapping) else {}
            config = deep_merge(config, dict(overrides))
            break
    options = config.get("options") if isinstance(config, Mapping) else {}
    return options if isinstance(options, dict) else {}


def _collect_journal_logs(root: Path, *, env_name: str, since: str) -> str:
    service = "algo.service" if env_name == "live" else "paper.service"
    try:
        proc = subprocess.run(
            ("journalctl", "-u", service, "--since", since, "--no-pager"),
            cwd=str(root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=8,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout


def _latest_matching_lines(log_text: str, markers: Sequence[str], *, limit: int = 5) -> list[str]:
    lines = [
        line.strip()
        for line in str(log_text or "").splitlines()
        if any(marker in line for marker in markers)
    ]
    return lines[-limit:]


def _reason_from_lane_logs(lines: Sequence[str]) -> str:
    if not lines:
        return "no_options_lane_logs"
    text = "\n".join(lines).lower()
    if "no_contract" in text or "selected=none" in text:
        return "no_contract_selected"
    if "filter" in text or "reject" in text or "reason_codes=" in text:
        return "filter_rejected"
    if "action=skip" in text:
        for token in ("reason=", "reason_codes="):
            idx = text.rfind(token)
            if idx >= 0:
                value = text[idx + len(token) :].split()[0].strip(",")
                if value:
                    return value
        return "lane_skipped"
    if "action=attempt" in text:
        return "lane_active_no_order"
    return "no_options_order_logs"


def build_options_pilot_status(
    *,
    root: str | Path,
    env_name: str = "live",
    user_id: str | None = None,
    log_text: str | None = None,
    since: str = "2 hours ago",
) -> OptionsPilotStatus:
    """Build a read-only options pilot status from config and recent logs."""
    repo_root = Path(root).resolve()
    options = load_effective_options_config(repo_root, env_name=env_name, user_id=user_id)
    config = {"options": options}
    logs = log_text if log_text is not None else _collect_journal_logs(repo_root, env_name=env_name, since=since)
    lane = _latest_matching_lines(str(logs or ""), OPTION_LANE_MARKERS)
    orders = _latest_matching_lines(str(logs or ""), OPTION_ORDER_MARKERS)
    reason = "orders_seen" if orders else _reason_from_lane_logs(lane)
    nested_pilot = options.get("live_pilot") if isinstance(options.get("live_pilot"), Mapping) else {}
    pilot_enabled = bool(nested_pilot.get("enabled")) if "enabled" in nested_pilot else bool(options.get("live_pilot_enabled"))
    return OptionsPilotStatus(
        config_enabled=bool(options.get("enabled")),
        live_pilot_enabled=bool(options_live_pilot_enabled(config)) and pilot_enabled,
        mode=options_mode(config),
        exposure_limit=_fmt_raw(options.get("total_exposure_limit", options.get("max_total_options_exposure_pct"))),
        max_positions=_fmt_raw(options.get("max_option_positions", options.get("max_positions"))),
        max_contracts=_fmt_raw(options.get("max_contracts_per_trade", options.get("v1_max_contracts_per_trade"))),
        allowed_symbols=[
            str(item or "").strip().upper()
            for item in (options.get("allowed_underlyings") or [])
            if str(item or "").strip()
        ],
        latest_entry_lane_logs=lane,
        latest_options_order_logs=orders,
        reason_if_no_orders=reason,
    )


def format_options_pilot_status(status: OptionsPilotStatus) -> list[str]:
    """Format status for operator CLI output."""
    return [
        f"OPTIONS_CONFIG enabled={_fmt_bool(status.config_enabled)}",
        f"OPTIONS_LIVE_PILOT enabled={_fmt_bool(status.live_pilot_enabled)}",
        f"mode={status.mode}",
        f"exposure_limit={status.exposure_limit}",
        f"max_positions={status.max_positions}",
        f"max_contracts={status.max_contracts}",
        f"allowed_symbols={','.join(status.allowed_symbols) if status.allowed_symbols else 'none'}",
        f"latest_entry_lane_logs={' | '.join(status.latest_entry_lane_logs) if status.latest_entry_lane_logs else 'none'}",
        f"latest_options_order_logs={' | '.join(status.latest_options_order_logs) if status.latest_options_order_logs else 'none'}",
        f"reason_if_no_orders={status.reason_if_no_orders}",
    ]
