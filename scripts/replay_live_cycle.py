#!/usr/bin/env python3
"""Offline replay of saved live-cycle artifacts with a mock broker only."""
from __future__ import annotations

import argparse
import ast
import contextlib
import io
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.app.live_cycle import build_core_rebuild_candidates, _inject_premarket_ranked_candidates
from src.capital_allocator_loop import execute_capital_allocator_pass
from src.config_loader import deep_merge, load_config
from src.execution import ExecutionManager
from src.news_catalyst import load_premarket_artifacts as load_fresh_premarket_artifacts
from src.portfolio.allocator_planner import parse_capital_allocator_cfg
from src.position_tracker import load as load_tracked
from src.trade_attribution import attribution_daily_path, load_daily_artifact


log = logging.getLogger(__name__)


@dataclass
class ReplayQuote:
    mid: float
    spread_pct: float
    skip_spread_check: bool = False

    @property
    def bid(self) -> float:
        half = max(0.0, float(self.spread_pct)) / 200.0
        return max(0.01, float(self.mid) * (1.0 - half))

    @property
    def ask(self) -> float:
        half = max(0.0, float(self.spread_pct)) / 200.0
        return max(0.01, float(self.mid) * (1.0 + half))

    def is_stale(self, _max_age: float) -> bool:
        return False

    def reference_mid(self, fallback: float) -> float:
        return float(self.mid or fallback)


@dataclass
class ReplayMockBroker:
    quotes: dict[str, ReplayQuote]
    positions: list[dict[str, Any]] = field(default_factory=list)
    submitted_orders: list[dict[str, Any]] = field(default_factory=list)
    submit_called: bool = False

    def get_open_orders(self) -> list[dict[str, Any]]:
        return []

    def get_positions(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.positions]

    def get_latest_quote(self, symbol: str) -> ReplayQuote | None:
        sym = str(symbol or "").strip().upper()
        return self.quotes.get(sym) or ReplayQuote(mid=100.0, spread_pct=0.1)

    def get_avg_volume(self, _symbol: str) -> float:
        return 1_000_000.0

    def submit_order(self, request: Any) -> dict[str, Any]:
        self.submit_called = True
        row = {
            "id": f"replay-{len(self.submitted_orders) + 1}",
            "symbol": str(getattr(request, "symbol", "") or "").upper(),
            "side": str(getattr(request, "side", "") or "").lower(),
            "quantity": getattr(request, "quantity", None),
            "notional": getattr(request, "notional", None),
            "order_type": str(getattr(getattr(request, "order_type", None), "value", getattr(request, "order_type", ""))),
            "limit_price": getattr(request, "limit_price", None),
        }
        self.submitted_orders.append(row)
        return row


class _ReplayEngine:
    def __init__(self, config: Mapping[str, Any]) -> None:
        self.execution = ExecutionManager(dict(config))
        self.strategy = type("ReplayStrategy", (), {"stop_loss_pct": 1.5})()
        self.market_quality = type(
            "ReplayMarketQuality",
            (),
            {"_max_spread_for_symbol": staticmethod(lambda _symbol: 3.5)},
        )()


class _ReplayLogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(self.format(record))


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _load_replay_config(project_root: Path, user: str) -> dict[str, Any]:
    config = load_config(project_root / "config" / "default.yaml")
    users_path = project_root / "config" / "users.yaml"
    if not users_path.exists():
        return config
    try:
        payload = yaml.safe_load(users_path.read_text(encoding="utf-8")) or {}
    except Exception:
        log.warning("REPLAY_USERS_CONFIG_UNREADABLE path=%s", users_path, exc_info=True)
        return config
    users = payload.get("users") if isinstance(payload, Mapping) else None
    if not isinstance(users, list):
        return config
    requested = str(user or "").strip()
    for row in users:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("id") or "").strip() != requested:
            continue
        overrides = row.get("overrides")
        if isinstance(overrides, Mapping):
            return deep_merge(config, dict(overrides))
        return config
    return config


def _latest_exit_timestamp(project_root: Path, user: str) -> datetime | None:
    daily_dir = project_root / "data" / "trade_attribution" / "daily"
    if not daily_dir.exists():
        return None
    latest: datetime | None = None
    for path in daily_dir.glob(f"*_{user}.json"):
        payload = _read_json(path)
        exits = payload.get("exits") if isinstance(payload, Mapping) else None
        if not isinstance(exits, list):
            continue
        for row in exits:
            if not isinstance(row, Mapping):
                continue
            raw = row.get("timestamp")
            if not isinstance(raw, str) or not raw.strip():
                continue
            try:
                ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if latest is None or ts > latest:
                latest = ts
    return latest


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True, default=str)
        fh.write("\n")


def _date_token(path: Path) -> str:
    match = re.match(r"(\d{8})T", path.name)
    return match.group(1) if match else ""


def find_dynamic_scan_history(
    *,
    project_root: Path,
    user: str,
    date: str,
) -> Path | None:
    history_dir = project_root / "data" / "dynamic_scan_history"
    if not history_dir.exists():
        return None
    user_suffix = f"_{user}.json"
    rows = [p for p in history_dir.glob("*.json") if p.name.endswith(user_suffix)]
    if date != "latest":
        token = date.replace("-", "")
        rows = [p for p in rows if _date_token(p) == token]
    if not rows:
        return None
    return max(rows, key=lambda p: p.stat().st_mtime)


def load_premarket_artifacts(project_root: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    premarket_dir = project_root / "data" / "premarket"
    for name in ("latest_event_feed.json", "latest_rankings.json", "latest_catalysts.json"):
        path = premarket_dir / name
        if path.exists():
            out[name] = _read_json(path)
    return out


def _candidate_symbol(row: Mapping[str, Any]) -> str:
    return str(row.get("symbol") or row.get("sym_u") or "").strip().upper()


def _float(row: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    try:
        value = float(row.get(key, default) or default)
    except (TypeError, ValueError):
        return float(default)
    return value


def build_replay_signals(scan_payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    accepted = scan_payload.get("accepted")
    if not isinstance(accepted, list):
        candidates = scan_payload.get("candidates") if isinstance(scan_payload.get("candidates"), list) else []
        accepted = [row for row in candidates if isinstance(row, Mapping) and bool(row.get("accepted"))]
    signals: list[dict[str, Any]] = []
    for row in accepted:
        if not isinstance(row, Mapping):
            continue
        sym = _candidate_symbol(row)
        if not sym:
            continue
        quality = row.get("quality") if isinstance(row.get("quality"), Mapping) else {}
        signals.append(
            {
                "sym_u": sym,
                "symbol": sym,
                "final": True,
                "source": row.get("source") or "dynamic_universe",
                "route": row.get("route") or "dynamic_replay",
                "dynamic_candidate": True,
                "score": _float(row, "score"),
                "composite_score": _float(row, "score"),
                "strength_eff": _float(row, "score") / 100.0,
                "news_score": _float(row, "news_score"),
                "event_score": _float(row, "event_score"),
                "catalyst_score": _float(row, "catalyst_score"),
                "relative_volume": _float(row, "relative_volume"),
                "price_above_vwap": bool(quality.get("price_above_vwap", True)),
                "vwap_above": bool(quality.get("price_above_vwap", True)),
                "age_minutes": row.get("catalyst_age_minutes"),
                "catalyst_age_minutes": row.get("catalyst_age_minutes"),
            }
        )
    return signals


def _premarket_replay_signal(row: Mapping[str, Any]) -> dict[str, Any]:
    sym = str(row.get("symbol") or row.get("sym_u") or "").strip().upper()
    score = _float(row, "score", max(_float(row, "news_score"), _float(row, "event_score"), _float(row, "catalyst_score") * 10.0))
    return {
        "sym_u": sym,
        "symbol": sym,
        "final": True,
        "source": row.get("source") or "premarket",
        "route": "premarket_catalyst_replay",
        "dynamic_candidate": True,
        "dynamic_symbol": True,
        "premarket_injected": True,
        "score": score,
        "composite_score": score,
        "strength_eff": max(0.0, min(1.0, score / 100.0)),
        "news_score": _float(row, "news_score"),
        "event_score": _float(row, "event_score"),
        "catalyst_score": _float(row, "catalyst_score"),
        "relative_volume": _float(row, "relative_volume"),
        "price_above_vwap": True,
        "vwap_above": True,
        "headline": row.get("headline"),
        "catalyst_type": row.get("catalyst_type"),
        "age_minutes": row.get("catalyst_age_minutes"),
        "catalyst_age_minutes": row.get("catalyst_age_minutes"),
    }


def build_replay_quotes(scan_payload: Mapping[str, Any], signals: list[Mapping[str, Any]]) -> dict[str, ReplayQuote]:
    quotes: dict[str, ReplayQuote] = {}
    rows = scan_payload.get("candidates") if isinstance(scan_payload.get("candidates"), list) else []
    rows = list(rows) + list(scan_payload.get("accepted", []) if isinstance(scan_payload.get("accepted"), list) else [])
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        sym = _candidate_symbol(row)
        if not sym:
            continue
        price = max(0.01, _float(row, "price", 100.0))
        spread = max(0.0, _float(row, "spread_pct", 0.1))
        quotes[sym] = ReplayQuote(mid=price, spread_pct=spread, skip_spread_check=bool(row.get("unstable_quote")))
    for signal in signals:
        sym = _candidate_symbol(signal)
        quotes.setdefault(sym, ReplayQuote(mid=100.0, spread_pct=0.1))
    return quotes


def configured_core_symbols(config: Mapping[str, Any]) -> list[str]:
    universe = config.get("universe") if isinstance(config.get("universe"), Mapping) else {}
    raw_symbols = universe.get("symbols") if isinstance(universe, Mapping) else None
    if isinstance(raw_symbols, list) and raw_symbols:
        paused = {
            str(sym or "").strip().upper()
            for sym in universe.get("paused_symbols", [])
            if str(sym or "").strip()
        }
        return [
            str(sym or "").strip().upper()
            for sym in raw_symbols
            if str(sym or "").strip() and str(sym or "").strip().upper() not in paused
        ]
    return ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL"]


def build_replay_core_rebuild_candidates(
    *,
    config: Mapping[str, Any],
    broker: ReplayMockBroker,
    positions: list[dict[str, Any]],
    signals: list[Mapping[str, Any]],
    project_root: Path,
    user: str,
    now: datetime,
    account_equity: float,
    cash: float,
    regime_score: int | None,
    regime_condition: str | None,
) -> list[dict[str, Any]]:
    dynamic_symbols = [
        str(row.get("symbol") or row.get("sym_u") or "").strip().upper()
        for row in signals
        if str(row.get("symbol") or row.get("sym_u") or "").strip()
        and bool(row.get("dynamic_candidate", True))
    ]
    try:
        rows = build_core_rebuild_candidates(
            config=dict(config),
            core_symbols=configured_core_symbols(config),
            dynamic_symbols=dynamic_symbols,
            existing_candidates=signals,
            positions=positions,
            equity=float(account_equity),
            cash=float(cash),
            broker=broker,
            open_order_symbols=[],
            cooldown_symbols=[],
            max_positions=int(
                (
                    (config.get("portfolio") or {}).get("capital_allocator") or {}
                ).get("max_positions", 10)
                if isinstance((config.get("portfolio") or {}).get("capital_allocator"), Mapping)
                else 10
            ),
            regime_score=regime_score,
            regime_condition=regime_condition,
            spread_cap_fn=lambda _sym: 3.5,
            user_id=user,
            data_dir=project_root / "data",
            now=now,
        )
    except Exception:
        log.warning("REPLAY_CORE_REBUILD_FAILED user=%s", user, exc_info=True)
        return []
    return [dict(row) for row in rows]


def rejected_candidates(scan_payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = scan_payload.get("candidates") if isinstance(scan_payload.get("candidates"), list) else []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping) or bool(row.get("accepted")):
            continue
        out.append(
            {
                "symbol": _candidate_symbol(row),
                "reason": row.get("rejection_reason") or row.get("reason") or "unknown",
                "score": row.get("score"),
                "price": row.get("price"),
                "relative_volume": row.get("relative_volume"),
                "spread_pct": row.get("spread_pct"),
            }
        )
    return out


def _tracked_positions(tracked: Mapping[str, Any]) -> list[dict[str, Any]]:
    positions: list[dict[str, Any]] = []
    for sym, row in tracked.items():
        if not isinstance(row, Mapping):
            continue
        try:
            qty = float(row.get("qty") or 0.0)
        except (TypeError, ValueError):
            qty = 0.0
        try:
            price = float(row.get("entry_price") or row.get("avg_price") or 0.0)
        except (TypeError, ValueError):
            price = 0.0
        try:
            notional = float(row.get("notional") or 0.0)
        except (TypeError, ValueError):
            notional = 0.0
        market_value = notional if notional > 0 else max(0.0, qty * price)
        if qty > 0 or market_value > 0:
            positions.append({"symbol": str(sym).upper(), "qty": qty, "market_value": market_value, "avg_price": price})
    return positions


def _extract_allocator_actions(stdout_text: str) -> list[str]:
    return [line.strip() for line in stdout_text.splitlines() if line.startswith("ALLOCATOR ACTIONS:")]


def _parse_key_value_log(line: str) -> dict[str, str]:
    return {
        str(match.group(1)): str(match.group(2))
        for match in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)=([^ ]+)", str(line or ""))
    }


def _parse_allocator_action_rows(stdout_text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in stdout_text.splitlines():
        if not line.startswith("ALLOCATOR ACTIONS:"):
            continue
        raw = line.split(":", 1)[1].strip()
        try:
            parsed = ast.literal_eval(raw)
        except (SyntaxError, ValueError):
            continue
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, Mapping):
                    rows.append(dict(item))
    return rows


def _symbol_index(rows: list[Mapping[str, Any]], *, key: str = "symbol") -> dict[str, list[Mapping[str, Any]]]:
    out: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        sym = str(row.get(key) or row.get("sym_u") or "").strip().upper()
        if sym:
            out.setdefault(sym, []).append(row)
    return out


def _extract_log_rows(log_lines: list[str], marker: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in log_lines:
        if marker not in line:
            continue
        row = _parse_key_value_log(line)
        row["_line"] = line
        if row:
            rows.append(row)
    return rows


def _trace_reason(*values: Any, default: str = "none") -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() not in {"none", "n/a"}:
            return text
    return default


def _build_replay_diagnostics(
    *,
    signals: list[Mapping[str, Any]],
    core_rebuild_candidates: list[Mapping[str, Any]],
    scan_payload: Mapping[str, Any],
    stdout_text: str,
    log_lines: list[str],
    attribution_payload: Mapping[str, Any] | None,
    submitted_orders: list[Mapping[str, Any]],
) -> dict[str, Any]:
    selected = [
        {
            "symbol": str(s.get("sym_u") or s.get("symbol") or "").strip().upper(),
            "score": s.get("score"),
            "route": s.get("route"),
            "source": s.get("source"),
        }
        for s in signals
    ]
    selected = [row for row in selected if row["symbol"]]
    rejected_before = rejected_candidates(scan_payload)
    allocator_action_rows = _parse_allocator_action_rows(stdout_text)
    action_created_logs = _extract_log_rows(log_lines, "ALLOCATOR_ACTION_CREATED")
    allocator_reject_logs = (
        _extract_log_rows(log_lines, "ALLOCATOR_FILTER_REJECT")
        + _extract_log_rows(log_lines, "ALLOCATOR_ACTION_BLOCKED")
        + _extract_log_rows(log_lines, "ALLOCATOR_NO_ACTION_DETAIL")
        + _extract_log_rows(log_lines, "ALLOCATOR_SKIP_REASON")
    )
    order_reject_logs = _extract_log_rows(log_lines, "ORDER_BUILD_REJECT")
    order_skip_logs = _extract_log_rows(log_lines, "ORDER_SKIP")
    trade_cycle_logs = _extract_log_rows(log_lines, "TRADE_CYCLE_GATE")
    submitted_idx = _symbol_index(list(submitted_orders))
    action_idx = _symbol_index(allocator_action_rows)
    created_idx = _symbol_index(action_created_logs)
    allocator_reject_idx = _symbol_index(allocator_reject_logs)
    order_reject_idx = _symbol_index(order_reject_logs)
    order_skip_idx = _symbol_index(order_skip_logs)
    trade_cycle_idx = _symbol_index(trade_cycle_logs)

    attr_alloc_idx: dict[str, list[Mapping[str, Any]]] = {}
    attr_order_idx: dict[str, list[Mapping[str, Any]]] = {}
    if isinstance(attribution_payload, Mapping):
        alloc_rows = attribution_payload.get("allocator_candidates")
        order_rows = attribution_payload.get("orders")
        attr_alloc_idx = _symbol_index(alloc_rows if isinstance(alloc_rows, list) else [])
        attr_order_idx = _symbol_index(order_rows if isinstance(order_rows, list) else [])

    per_symbol: list[dict[str, Any]] = []
    core_rebuild_idx = _symbol_index(core_rebuild_candidates)
    for signal in signals:
        sym = str(signal.get("sym_u") or signal.get("symbol") or "").strip().upper()
        if not sym:
            continue
        core_rebuild_yes = sym in core_rebuild_idx or bool(signal.get("core_rebuild"))
        attr_alloc = (attr_alloc_idx.get(sym) or [{}])[-1]
        attr_orders = attr_order_idx.get(sym) or []
        attr_order = attr_orders[-1] if attr_orders else {}
        alloc_reject = (allocator_reject_idx.get(sym) or [{}])[-1]
        order_reject = (order_reject_idx.get(sym) or [{}])[-1]
        order_skip = (order_skip_idx.get(sym) or [{}])[-1]
        trade_cycle = (trade_cycle_idx.get(sym) or [{}])[-1]
        action_rows_for_sym = action_idx.get(sym) or []
        created_rows_for_sym = created_idx.get(sym) or []
        submitted_rows_for_sym = submitted_idx.get(sym) or []

        entry_final = bool(signal.get("final", True))
        entry_reason = _trace_reason(signal.get("reason"), "ok" if entry_final else "entry_eval_rejected")
        allocator_candidate_yes = bool(attr_alloc_idx.get(sym)) or bool(created_rows_for_sym) or bool(action_rows_for_sym)
        if allocator_reject_idx.get(sym):
            allocator_candidate_reason = _trace_reason(alloc_reject.get("reason"))
        elif allocator_candidate_yes:
            allocator_candidate_reason = "accepted"
        else:
            allocator_candidate_reason = "not_seen_by_allocator"

        action_yes = bool(action_rows_for_sym) or bool(created_rows_for_sym) or bool(attr_alloc.get("action_created"))
        if action_yes:
            action_reason = "created"
        else:
            action_reason = _trace_reason(attr_alloc.get("no_action_reason"), alloc_reject.get("reason"), default="no_allocator_action")

        order_status = str(attr_order.get("order_build_status") or "").strip().lower()
        order_skip_yes = bool(order_skip_idx.get(sym))
        order_skip_reason = _trace_reason(order_skip.get("reason"), default="none")
        order_build_yes = bool(submitted_rows_for_sym) or order_status == "built"
        if order_build_yes:
            order_reason = "built"
        else:
            order_reason = _trace_reason(
                attr_order.get("reject_reason"),
                order_skip.get("reason"),
                order_reject.get("reason"),
                alloc_reject.get("reason") if not action_yes else None,
                default="not_reached",
            )

        submit_yes = bool(submitted_rows_for_sym) or bool(attr_order.get("submitted"))
        if submit_yes:
            submit_reason = "submitted"
        elif not order_build_yes:
            submit_reason = order_reason
        else:
            submit_reason = _trace_reason(
                attr_order.get("reject_reason"),
                order_skip.get("reason"),
                default="not_submitted",
            )

        final_stage = "order_submitted" if submit_yes else "order_skip" if order_skip_yes else "order_build"
        final_reason = submit_reason if submit_yes else order_skip_reason if order_skip_yes else order_reason

        per_symbol.append(
            {
                "symbol": sym,
                "route": signal.get("route"),
                "source": signal.get("source"),
                "scan_selected": not core_rebuild_yes,
                "core_rebuild_candidate": {
                    "result": core_rebuild_yes,
                    "reason": _trace_reason(signal.get("reason"), default="not_core_rebuild"),
                },
                "entry_eval": {"result": entry_final, "reason": entry_reason},
                "allocator_candidate": {
                    "result": allocator_candidate_yes,
                    "reason": allocator_candidate_reason,
                },
                "allocator_action": {"result": action_yes, "reason": action_reason},
                "trade_cycle_allowed": {
                    "result": str(trade_cycle.get("trade_cycle_allowed") or "").strip().lower()
                    in {"true", "1", "yes"},
                    "reason": _trace_reason(trade_cycle.get("skip_reason"), default="none"),
                    "line": trade_cycle.get("_line"),
                },
                "order_build": {"result": order_build_yes, "reason": order_reason},
                "order_skip": {
                    "result": order_skip_yes,
                    "reason": order_skip_reason,
                    "source_stage": "order_dispatch" if order_skip_yes else "none",
                    "line": order_skip.get("_line"),
                },
                "simulated_submit": {"result": submit_yes, "reason": submit_reason},
                "final_stage": final_stage,
                "final_reason": final_reason,
            }
        )

    rejected_by_allocator = [
        {
            "symbol": str(row.get("symbol") or "?").strip().upper(),
            "reason": _trace_reason(row.get("reason")),
            "line": row.get("_line"),
        }
        for row in allocator_reject_logs
        if str(row.get("symbol") or "").strip()
    ]
    rejected_by_order_builder = [
        {
            "symbol": str(row.get("symbol") or "?").strip().upper(),
            "reason": _trace_reason(row.get("reason")),
            "line": row.get("_line"),
        }
        for row in order_reject_logs
        if str(row.get("symbol") or "").strip()
    ]
    return {
        "selected_candidates": selected,
        "core_rebuild_candidates": [dict(row) for row in core_rebuild_candidates],
        "rejected_before_allocator": rejected_before,
        "rejected_by_allocator": rejected_by_allocator,
        "allocator_actions_created": allocator_action_rows,
        "rejected_by_order_builder": rejected_by_order_builder,
        "order_skips": [
            {
                "symbol": str(row.get("symbol") or "?").strip().upper(),
                "reason": _trace_reason(row.get("reason")),
                "source": _trace_reason(row.get("source"), default="unknown"),
                "line": row.get("_line"),
            }
            for row in order_skip_logs
            if str(row.get("symbol") or "").strip()
        ],
        "per_symbol_trace": per_symbol,
    }


def run_replay(
    *,
    project_root: Path = PROJECT_ROOT,
    date: str = "latest",
    user: str = "default",
    history_user: str | None = None,
    broker_mock: bool = False,
    broker_mode: str | None = None,
    summary_dir: Path | None = None,
    regime_score: int | None = 3,
    regime_condition: str | None = "neutral",
    history_path_override: Path | None = None,
    now_override: datetime | None = None,
) -> dict[str, Any]:
    mode = (broker_mode or os.environ.get("BROKER_MODE") or os.environ.get("ALPACA_BROKER_MODE") or "MOCK").strip().upper()
    if mode == "LIVE" and not broker_mock:
        raise RuntimeError("unsafe_live_broker_mode: replay requires --broker-mock when broker mode is LIVE")
    if not broker_mock:
        raise RuntimeError("broker_mock_required: replay never runs without --broker-mock")

    config_user = str(user or "default")
    history_lookup_user = str(history_user or config_user)
    log.info("REPLAY_USER_CONFIG user=%s", config_user)
    log.info("REPLAY_HISTORY_USER user=%s", history_lookup_user)

    history_path = history_path_override or find_dynamic_scan_history(
        project_root=project_root,
        user=history_lookup_user,
        date=date,
    )
    if history_path is None:
        raise RuntimeError(
            f"replay_data_missing: no dynamic scan history for user={history_lookup_user!r} date={date!r}"
        )
    scan_payload = _read_json(history_path)
    reports_dir = project_root / "data" / "reports"
    reports = sorted(str(p.relative_to(project_root)) for p in reports_dir.glob("*") if p.is_file()) if reports_dir.exists() else []
    tracked = load_tracked(config_user, data_dir=project_root / "data")
    positions = _tracked_positions(tracked)
    signals = build_replay_signals(scan_payload if isinstance(scan_payload, Mapping) else {})
    quotes = build_replay_quotes(scan_payload if isinstance(scan_payload, Mapping) else {}, signals)

    config = _load_replay_config(project_root, config_user)
    config.setdefault("options", {})["enabled"] = False
    config["_replay_mode"] = "offline_replay"
    config["_broker_mock"] = bool(broker_mock)
    config["_market_open"] = "replay_not_evaluated"
    ca_cfg = parse_capital_allocator_cfg(config.get("portfolio") if isinstance(config.get("portfolio"), Mapping) else {})
    broker = ReplayMockBroker(quotes=quotes, positions=positions)
    engine = _ReplayEngine(config)
    account_equity = 28_000.0
    cash = 10_000.0
    now = now_override or _latest_exit_timestamp(project_root, config_user) or datetime.now(timezone.utc)
    premarket = load_premarket_artifacts(project_root)
    capture = _ReplayLogCapture()
    capture.setFormatter(logging.Formatter("%(message)s"))
    root_logger = logging.getLogger()
    root_logger.addHandler(capture)
    old_level = root_logger.level
    root_logger.setLevel(logging.INFO)
    premarket_summary = load_fresh_premarket_artifacts(project_root, now=now, emit_log=False)
    injected_premarket = _inject_premarket_ranked_candidates(
        config=config,
        project_root=project_root,
        now=now,
        artifact_summary=premarket_summary,
        existing_symbols=[
            str(row.get("symbol") or row.get("sym_u") or "").strip().upper()
            for row in signals
            if str(row.get("symbol") or row.get("sym_u") or "").strip()
        ],
        dynamic_symbols=[
            str(row.get("symbol") or row.get("sym_u") or "").strip().upper()
            for row in signals
            if str(row.get("symbol") or row.get("sym_u") or "").strip()
            and bool(row.get("dynamic_candidate", True))
        ],
        paused_symbols=set(),
    )
    if injected_premarket:
        for row in injected_premarket:
            sig = _premarket_replay_signal(row)
            if sig["symbol"]:
                signals.append(sig)
                quotes.setdefault(sig["symbol"], ReplayQuote(mid=100.0, spread_pct=0.1))
    core_rebuild_candidates = build_replay_core_rebuild_candidates(
        config=config,
        broker=broker,
        positions=positions,
        signals=signals,
        project_root=project_root,
        user=config_user,
        now=now,
        account_equity=account_equity,
        cash=cash,
        regime_score=regime_score,
        regime_condition=regime_condition,
    )
    if core_rebuild_candidates:
        signals.extend(core_rebuild_candidates)
        for row in core_rebuild_candidates:
            sym = str(row.get("symbol") or row.get("sym_u") or "").strip().upper()
            if sym:
                quotes.setdefault(sym, ReplayQuote(mid=100.0, spread_pct=0.1))
    _replay_symbols = sorted(
        {
            str(row.get("symbol") or row.get("sym_u") or "").strip().upper()
            for row in signals
            if str(row.get("symbol") or row.get("sym_u") or "").strip()
        }
    )
    _option_skip_reason = (
        "options_disabled_by_replay_live_cycle"
        if not bool((config.get("options") or {}).get("enabled"))
        else "offline_allocator_replay_does_not_run_options_selector"
    )
    for _sym in _replay_symbols:
        log.info(
            "ENTRY_PIPELINE_STAGE symbol=%s stage=replay_live_cycle result=skipped "
            "reason=offline_allocator_replay_does_not_run_live_entry_eval",
            _sym,
        )
        log.info(
            "OPTION_PIPELINE_STAGE symbol=%s stage=replay_live_cycle result=skipped reason=%s",
            _sym,
            _option_skip_reason,
        )
    stdout = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout):
            execute_capital_allocator_pass(
                signals=signals,
                broker=broker,
                engine=engine,
                config=config,
                dt=now,
                positions=positions,
                tracked=dict(tracked),
                current_positions={},
                eligible_active=[row["symbol"] for row in positions],
                account_equity=account_equity,
                cash=cash,
                ca_cfg=ca_cfg,
                user_id=config_user,
                data_dir=project_root / "data",
                stale_quote_max_age=999_999.0,
                strength_jitter_max=0.0,
                et_date_iso=now.date().isoformat(),
                cycle_risk_state={"daily_loss_lockout": False},
                verbose=False,
                allow_allocator_buys=True,
                gross_exposure_pct=0.0,
            )
    finally:
        root_logger.removeHandler(capture)
        root_logger.setLevel(old_level)

    log_lines = capture.records
    selected = [{"symbol": s["sym_u"], "score": s.get("score"), "route": s.get("route")} for s in signals]
    replay_day = now.date().isoformat()
    attribution_path = attribution_daily_path(
        data_dir=project_root / "data",
        user_id=config_user,
        day=replay_day,
    )
    attribution_payload = load_daily_artifact(attribution_path) if attribution_path.exists() else None
    diagnostics = _build_replay_diagnostics(
        signals=signals,
        core_rebuild_candidates=core_rebuild_candidates,
        scan_payload=scan_payload if isinstance(scan_payload, Mapping) else {},
        stdout_text=stdout.getvalue(),
        log_lines=log_lines,
        attribution_payload=attribution_payload if isinstance(attribution_payload, Mapping) else None,
        submitted_orders=broker.submitted_orders,
    )
    summary = {
        "ok": True,
        "mode": "offline_replay",
        "broker_mock": True,
        "broker_mode": mode,
        "user": config_user,
        "history_user": history_lookup_user,
        "date": date,
        "history_path": str(history_path.relative_to(project_root)),
        "premarket_artifacts": sorted(premarket.keys()),
        "reports": reports[:20],
        "selected_candidates": diagnostics["selected_candidates"],
        "core_rebuild_candidates": diagnostics["core_rebuild_candidates"],
        "rejected_before_allocator": diagnostics["rejected_before_allocator"],
        "rejected_by_allocator": diagnostics["rejected_by_allocator"],
        "allocator_actions_created": diagnostics["allocator_actions_created"],
        "rejected_by_order_builder": diagnostics["rejected_by_order_builder"],
        "simulated_submitted_orders": broker.submitted_orders,
        "per_symbol_trace": diagnostics["per_symbol_trace"],
        "rejected_candidates": diagnostics["rejected_before_allocator"],
        "allocator_actions": _extract_allocator_actions(stdout.getvalue()),
        "order_build_rejects": [line for line in log_lines if "ORDER_BUILD_REJECT" in line],
        "allocator_blocks": [line for line in log_lines if "ALLOCATOR_ACTION_BLOCKED" in line],
        "log_lines": log_lines,
        "trade_attribution_path": (
            str(attribution_path.relative_to(project_root))
            if attribution_path.exists() and attribution_path.is_relative_to(project_root)
            else (str(attribution_path) if attribution_path.exists() else None)
        ),
        "trade_attribution_summary": (
            attribution_payload.get("summary")
            if isinstance(attribution_payload, Mapping)
            else None
        ),
        "stdout": stdout.getvalue().splitlines(),
    }
    out_dir = summary_dir or (project_root / "data" / "replay")
    day = now.date().isoformat()
    out_path = out_dir / f"{day}_{config_user}.json"
    summary["summary_path"] = str(out_path.relative_to(project_root) if out_path.is_relative_to(project_root) else out_path)
    _write_json(out_path, summary)
    return summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline replay of saved live-cycle artifacts.")
    parser.add_argument("--date", default="latest", help="YYYY-MM-DD or latest")
    parser.add_argument("--user", default="default")
    parser.add_argument(
        "--history-user",
        default=None,
        help="Optional user id for dynamic scan history lookup. Defaults to --user.",
    )
    parser.add_argument("--broker-mock", action="store_true", help="Required. Force mock broker.")
    parser.add_argument("--broker-mode", default=None, help="Override broker mode for safety validation.")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--regime-score", type=int, default=3)
    parser.add_argument("--regime-condition", default="neutral")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        summary = run_replay(
            project_root=args.project_root,
            date=args.date,
            user=args.user,
            history_user=args.history_user,
            broker_mock=bool(args.broker_mock),
            broker_mode=args.broker_mode,
            regime_score=args.regime_score,
            regime_condition=args.regime_condition,
        )
    except Exception as exc:
        print(f"REPLAY_FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({k: summary[k] for k in ("ok", "user", "date", "history_path", "summary_path")}, indent=2))
    print("selected candidates:", len(summary["selected_candidates"]))
    print("rejected candidates:", len(summary["rejected_candidates"]))
    print("simulated submitted orders:", len(summary["simulated_submitted_orders"]))
    print("order build rejects:", len(summary["order_build_rejects"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
