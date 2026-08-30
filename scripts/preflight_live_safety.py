#!/usr/bin/env python3
"""Live pre-market safety validator.

This script exercises the live configuration, broker reads, premarket artifacts,
dynamic scan, entry dry-run, and allocator dry-run without submitting orders.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import load_app_config
from src.dynamic_universe import (
    DynamicScanBatchResult,
    DynamicScanCandidate,
    dynamic_scan_cfg_with_entry_alignment,
    scan_candidates_batch,
)
from src.news_catalyst import premarket_artifact_paths

MAX_ARTIFACT_AGE_HOURS = 6.0
SUBMIT_METHOD_NAMES = (
    "submit_order",
    "submit_notional_market_day",
    "submit_market_sell",
    "submit_market_buy",
    "place_order",
    "close_position",
    "close_all_positions",
)


@dataclass(frozen=True)
class NewsArtifactLoad:
    """Premarket artifact summary used by the preflight dynamic scanner."""

    summary: dict[str, dict[str, Any]]
    top_symbols: list[str]
    newest_generated_at: datetime
    age_minutes: float


@dataclass(frozen=True)
class PreflightResult:
    """Top-level preflight status."""

    ok: bool
    reason: str | None = None


def _emit(message: str) -> None:
    print(message, flush=True)


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


def _float_value(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def install_submit_guards(broker: Any) -> None:
    """Replace broker submit/close methods with fail-closed guards."""

    def _blocked(name: str) -> Callable[..., Any]:
        def _raise(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError(f"PREFLIGHT blocked broker submit method {name}")

        return _raise

    for name in SUBMIT_METHOD_NAMES:
        if callable(getattr(broker, name, None)):
            setattr(broker, name, _blocked(name))


def load_live_config(config_path: Path | None = None) -> dict[str, Any]:
    """Load the same base config as live mode and force live broker mode."""

    cfg = load_app_config(config_path or PROJECT_ROOT / "config" / "default.yaml")
    cfg = copy.deepcopy(cfg)
    broker_cfg = cfg.setdefault("broker", {})
    broker_cfg["paper"] = False
    return cfg


def create_live_broker(config: Mapping[str, Any]) -> Any:
    """Create the live Alpaca broker lazily so tests can import this module."""

    from src.brokers.alpaca_client import AlpacaBroker

    return AlpacaBroker(dict(config), paper=False)


def _artifact_items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    items: list[Mapping[str, Any]] = []
    for key in ("rankings", "catalysts", "events"):
        raw = payload.get(key)
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            items.extend(item for item in raw if isinstance(item, Mapping))
    return items


def _artifact_generated_at(path: Path, payload: Mapping[str, Any]) -> datetime:
    generated = _parse_dt(payload.get("generated_at"))
    if generated is not None:
        return generated
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _merge_artifact_item(
    summary: dict[str, dict[str, Any]],
    item: Mapping[str, Any],
    *,
    generated_at: datetime,
    age_minutes: float,
    source_kind: str,
) -> None:
    symbol = str(item.get("symbol") or "").strip().upper()
    if not symbol:
        return
    score = max(
        _float_value(item.get("news_score")),
        _float_value(item.get("score")),
        _float_value(item.get("event_score")),
    )
    event_score = max(_float_value(item.get("event_score")), score)
    row = summary.setdefault(
        symbol,
        {
            "symbol": symbol,
            "news_score": 0,
            "event_score": 0.0,
            "catalyst_score": 0.0,
            "article_count": 0,
            "sentiment": 0.0,
            "headline": "",
            "source": str(item.get("source") or source_kind),
            "catalyst_type": str(item.get("catalyst_type") or item.get("rank_source") or ""),
            "generated_at": generated_at,
            "age_seconds": age_minutes * 60.0,
            "age_minutes": age_minutes,
            "artifact_kind": source_kind,
        },
    )
    row["news_score"] = int(max(_float_value(row.get("news_score")), score))
    row["event_score"] = max(_float_value(row.get("event_score")), event_score)
    row["catalyst_score"] = max(_float_value(row.get("catalyst_score")), max(score, event_score) / 10.0)
    row["article_count"] = max(int(_float_value(row.get("article_count"))), int(_float_value(item.get("article_count"))))
    row["sentiment"] = max(_float_value(row.get("sentiment")), _float_value(item.get("sentiment")))
    headline = str(item.get("headline") or item.get("reason") or "").strip()
    if headline and (not row.get("headline") or score >= _float_value(row.get("news_score"))):
        row["headline"] = headline
    if item.get("catalyst_type"):
        row["catalyst_type"] = str(item.get("catalyst_type"))
    if item.get("source"):
        row["source"] = str(item.get("source"))
    row["generated_at"] = generated_at
    row["age_seconds"] = age_minutes * 60.0
    row["age_minutes"] = age_minutes


def load_and_validate_news_artifacts(
    project_root: Path,
    *,
    now: datetime | None = None,
    max_age_hours: float = MAX_ARTIFACT_AGE_HOURS,
) -> NewsArtifactLoad:
    """Load latest premarket artifacts and fail when missing or older than max age."""

    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    payloads: list[tuple[str, Path, Mapping[str, Any], datetime]] = []
    missing: list[str] = []
    for kind, path in premarket_artifact_paths(project_root).items():
        if not path.exists():
            missing.append(str(path))
            continue
        try:
            payload = json.loads(path.read_text())
        except Exception as exc:
            raise RuntimeError(f"news_artifact_unreadable path={path} error={exc}") from exc
        if not isinstance(payload, Mapping):
            raise RuntimeError(f"news_artifact_invalid path={path}")
        payloads.append((kind, path, payload, _artifact_generated_at(path, payload)))
    if missing:
        raise RuntimeError(f"news_artifacts_missing paths={','.join(missing)}")

    newest = max(generated_at for _kind, _path, _payload, generated_at in payloads)
    age_minutes = max(0.0, (now_utc - newest).total_seconds() / 60.0)
    if age_minutes > max_age_hours * 60.0:
        raise RuntimeError(f"news_artifacts_stale age_minutes={age_minutes:.1f}")

    summary: dict[str, dict[str, Any]] = {}
    for kind, _path, payload, generated_at in payloads:
        item_age = max(0.0, (now_utc - generated_at).total_seconds() / 60.0)
        for item in _artifact_items(payload):
            _merge_artifact_item(summary, item, generated_at=generated_at, age_minutes=item_age, source_kind=kind)
        raw_symbols = payload.get("symbols")
        if isinstance(raw_symbols, Sequence) and not isinstance(raw_symbols, (str, bytes)):
            for raw in raw_symbols:
                symbol = str(raw or "").strip().upper()
                if symbol and symbol not in summary:
                    summary[symbol] = {
                        "symbol": symbol,
                        "news_score": 0,
                        "event_score": 0.0,
                        "catalyst_score": 0.0,
                        "article_count": 0,
                        "sentiment": 0.0,
                        "headline": "",
                        "source": kind,
                        "catalyst_type": "",
                        "generated_at": generated_at,
                        "age_seconds": item_age * 60.0,
                        "age_minutes": item_age,
                        "artifact_kind": kind,
                    }
    if not summary:
        missing_text = ",".join(missing[:3])
        raise RuntimeError(f"news_artifacts_empty missing={missing_text}")

    top = sorted(
        summary.values(),
        key=lambda row: (
            _float_value(row.get("news_score")),
            _float_value(row.get("event_score")),
            _float_value(row.get("catalyst_score")),
        ),
        reverse=True,
    )
    return NewsArtifactLoad(
        summary=summary,
        top_symbols=[str(row["symbol"]) for row in top[:10]],
        newest_generated_at=newest,
        age_minutes=age_minutes,
    )


def _fetch_account_snapshot(broker: Any) -> dict[str, Any]:
    if callable(getattr(broker, "get_account_snapshot", None)):
        snapshot = broker.get_account_snapshot()
        if isinstance(snapshot, Mapping):
            return dict(snapshot)
    account = broker.get_account() if callable(getattr(broker, "get_account", None)) else None
    return {
        "equity": _float_value(getattr(account, "equity", None)),
        "buying_power": _float_value(getattr(account, "buying_power", None), _float_value(getattr(account, "cash", None))),
        "cash": _float_value(getattr(account, "cash", None)),
    }


def validate_broker_account(broker: Any) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch live account, positions, and open orders."""

    snapshot = _fetch_account_snapshot(broker)
    if "buying_power" not in snapshot and callable(getattr(broker, "get_buying_power", None)):
        snapshot["buying_power"] = broker.get_buying_power()
    positions = broker.get_positions() if callable(getattr(broker, "get_positions", None)) else []
    open_orders = broker.get_open_orders() if callable(getattr(broker, "get_open_orders", None)) else []
    _emit(
        "BROKER_ACCOUNT equity=%.2f buying_power=%.2f open_order_count=%d"
        % (
            _float_value(snapshot.get("equity")),
            _float_value(snapshot.get("buying_power"), _float_value(snapshot.get("cash"))),
            len(open_orders or []),
        )
    )
    return snapshot, list(positions or []), list(open_orders or [])


def run_dynamic_scan(
    broker: Any,
    config: Mapping[str, Any],
    artifacts: Mapping[str, Mapping[str, Any]],
) -> DynamicScanBatchResult:
    """Run the configured dynamic universe scan without placing orders."""

    core_symbols = [str(sym).strip().upper() for sym in (config.get("universe", {}).get("symbols") or []) if str(sym).strip()]
    dyn_cfg = dynamic_scan_cfg_with_entry_alignment(config.get("dynamic_universe") or {}, config)
    result = scan_candidates_batch(
        broker,
        core_symbols,
        dyn_cfg,
        emit_logs=False,
        news_config=config,
        premarket_artifacts=artifacts,
    )
    _emit(
        "DYNAMIC_PREFLIGHT_SCAN accepted=%d rejected=%d selected=%s"
        % (len(result.accepted), len(result.rejected), ",".join(result.selected))
    )
    return result


def evaluate_dynamic_entries(
    selected_symbols: Sequence[str],
    accepted: Sequence[DynamicScanCandidate],
    config: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Dry-run entry evaluation for selected dynamic symbols."""

    dyn_cfg = config.get("dynamic_universe") or {}
    min_price = _float_value(dyn_cfg.get("min_price"), 0.0)
    hard_spread = _float_value(dyn_cfg.get("execution_max_spread_pct"), _float_value(dyn_cfg.get("max_spread_pct"), 100.0))
    accepted_by_symbol = {row.symbol: row for row in accepted}
    decisions: list[dict[str, Any]] = []
    for symbol in selected_symbols:
        row = accepted_by_symbol.get(str(symbol).strip().upper())
        if row is None:
            decision = {"symbol": symbol, "would_buy": False, "reason": "not_accepted"}
        elif row.price <= min_price:
            decision = {"symbol": row.symbol, "would_buy": False, "reason": "below_min_price"}
        elif row.spread_pct > hard_spread:
            decision = {"symbol": row.symbol, "would_buy": False, "reason": "spread_over_hard_max"}
        else:
            decision = {"symbol": row.symbol, "would_buy": True, "reason": "dynamic_scan_selected"}
        decisions.append(decision)
        _emit(
            "ENTRY_PREFLIGHT symbol=%s would_%s reason=%s"
            % (decision["symbol"], "buy" if decision["would_buy"] else "skip", decision["reason"])
        )
    return decisions


def build_allocator_plan(
    entry_decisions: Sequence[Mapping[str, Any]],
    *,
    equity: float,
    buying_power: float,
    config: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Build a dry-run allocator plan. This function must not call broker submit methods."""

    dyn_cfg = config.get("dynamic_universe") or {}
    max_symbol_pct = _float_value(dyn_cfg.get("max_symbol_exposure_pct"), 12.0)
    if max_symbol_pct <= 0:
        max_symbol_pct = 12.0
    max_notional = max(0.0, float(equity) * max_symbol_pct / 100.0)
    available = max(0.0, float(buying_power))
    buys = [row for row in entry_decisions if bool(row.get("would_buy"))]
    if not buys or max_notional <= 0 or available <= 0:
        return []
    per_symbol = min(max_notional, available / max(1, len(buys)))
    plan = [
        {"action": "buy", "symbol": str(row.get("symbol")).upper(), "notional": round(per_symbol, 2)}
        for row in buys
        if per_symbol > 0
    ]
    for action in plan:
        _emit(
            "ALLOCATOR_PREFLIGHT would_buy symbol=%s notional=%.2f"
            % (action["symbol"], float(action["notional"]))
        )
    return plan


def run_preflight(
    *,
    config_path: Path | None = None,
    project_root: Path = PROJECT_ROOT,
    broker: Any | None = None,
    now: datetime | None = None,
) -> PreflightResult:
    """Run the full pre-market safety validation."""

    try:
        config = load_live_config(config_path)
        live_broker = broker if broker is not None else create_live_broker(config)
        install_submit_guards(live_broker)

        snapshot, _positions, _open_orders = validate_broker_account(live_broker)

        artifacts = load_and_validate_news_artifacts(project_root, now=now)
        _emit(
            "NEWS_PREFLIGHT age_minutes=%.1f top10=%s"
            % (artifacts.age_minutes, ",".join(artifacts.top_symbols))
        )

        scan = run_dynamic_scan(live_broker, config, artifacts.summary)
        decisions = evaluate_dynamic_entries(scan.selected, scan.accepted, config)
        plan = build_allocator_plan(
            decisions,
            equity=_float_value(snapshot.get("equity")),
            buying_power=_float_value(snapshot.get("buying_power"), _float_value(snapshot.get("cash"))),
            config=config,
        )
        _emit(
            "ALLOCATOR_PREFLIGHT actions=%d symbols=%s"
            % (len(plan), ",".join(str(row.get("symbol")) for row in plan))
        )
        return PreflightResult(True)
    except Exception as exc:
        return PreflightResult(False, str(exc))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run live-mode pre-market safety validation without orders.")
    parser.add_argument("--config", type=Path, default=None, help="Config path; defaults to config/default.yaml")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT, help="Project root for premarket artifacts")
    args = parser.parse_args(argv)

    result = run_preflight(config_path=args.config, project_root=args.project_root)
    if result.ok:
        _emit("PREFLIGHT_PASS")
        return 0
    _emit(f"PREFLIGHT_FAIL reason={result.reason}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
