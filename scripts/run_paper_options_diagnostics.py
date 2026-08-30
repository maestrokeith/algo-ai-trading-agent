#!/usr/bin/env python3
"""Run a safe paper-options diagnostic through entry gates and options routing."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import deep_merge, load_config
from src.execution import ExecutionManager
from src.live.options_paper import attempt_paper_option_entry, paper_only_options_active
from src.options_paper_validation import PaperValidationBroker, sample_validation_chain
from src.strategy import EntrySignal
from src.trading_engine import TradingEngine
from src.user_manager import load_user_ids


log = logging.getLogger(__name__)


def _load_user_config_without_credentials(project_root: Path, user_id: str) -> tuple[dict[str, Any], bool]:
    """Load base + selected user overrides without resolving broker credentials."""

    users_path = project_root / "config" / "users.yaml"
    available = load_user_ids(users_path)
    if user_id not in available:
        raise ValueError(
            f"Unknown user_id '{user_id}'. Available users in config/users.yaml: {', '.join(available)}"
        )

    config = load_config(project_root / "config" / "default.yaml")
    if not users_path.exists():
        return config, bool((config.get("broker") or {}).get("paper", True))

    payload = yaml.safe_load(users_path.read_text(encoding="utf-8")) or {}
    users = payload.get("users") if isinstance(payload, Mapping) else None
    if not isinstance(users, list):
        return config, bool((config.get("broker") or {}).get("paper", True))

    for row in users:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("id") or "").strip() != user_id:
            continue
        overrides = row.get("overrides")
        if isinstance(overrides, Mapping):
            config = deep_merge(config, dict(overrides))
        return config, bool(row.get("paper", False))

    return config, bool((config.get("broker") or {}).get("paper", True))


def _diagnostic_config(config: Mapping[str, Any], *, symbol: str) -> dict[str, Any]:
    """Apply safe diagnostic overlays: paper-only options on, live order path off."""

    symbol_u = str(symbol or "").strip().upper()
    cfg = deep_merge(dict(config or {}), {})
    broker_cfg = dict(cfg.get("broker") or {})
    broker_cfg["paper"] = True
    cfg["broker"] = broker_cfg

    opts = dict(cfg.get("options") or {})
    allowed = {str(s).strip().upper() for s in (opts.get("allowed_underlyings") or []) if str(s).strip()}
    allowed.add(symbol_u)
    opts.update(
        {
            "enabled": True,
            "allow_new_entries": True,
            "new_entries_enabled": True,
            "mode": "paper_only",
            "only_buy_options": True,
            "allowed_underlyings": sorted(allowed),
        }
    )
    opts.setdefault("entry_mapping", {"bullish_signal": "call", "bearish_signal": "put"})
    opts.setdefault("max_contracts_per_trade", 1)
    opts.setdefault("v1_max_contracts_per_trade", 1)
    opts.setdefault("max_bid_ask_spread_pct", 12)
    cs = dict(opts.get("contract_selection") or {})
    cs.setdefault("expiry_min_days", 14)
    cs.setdefault("expiry_max_days", 35)
    cs.setdefault("min_open_interest", 100)
    cs.setdefault("min_volume", 50)
    cs.setdefault("max_bid_ask_spread_pct", 12)
    opts["contract_selection"] = cs
    cfg["options"] = opts
    return cfg


def _sample_uptrend_ohlcv(*, price: float, bars: int = 60) -> pd.DataFrame:
    start = float(price) - (bars * 0.2)
    closes = [start + i * 0.2 for i in range(bars - 1)] + [float(price)]
    return pd.DataFrame(
        {
            "close": closes,
            "high": [c + 0.35 for c in closes],
            "low": [max(0.01, c - 0.35) for c in closes],
            "volume": [5_000_000.0] * bars,
        }
    )


def _option_stage(
    *,
    passed: bool,
    reason: str,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {"passed": bool(passed), "reason": str(reason or ""), "detail": dict(detail or {})}


def _chain_liquidity_ok(chain: list[Any], config: Mapping[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    opts = config.get("options") if isinstance(config, Mapping) else {}
    cs = (opts or {}).get("contract_selection") if isinstance(opts, Mapping) else {}
    min_oi = float((cs or {}).get("min_open_interest", 0) or 0)
    min_volume = float((cs or {}).get("min_volume", 0) or 0)
    max_spread = float((cs or {}).get("max_bid_ask_spread_pct", (opts or {}).get("max_bid_ask_spread_pct", 0)) or 0)
    for contract in chain:
        oi = float(getattr(contract, "open_interest", 0) or 0)
        vol = float(getattr(contract, "volume", 0) or 0)
        bid = float(getattr(contract, "bid", 0) or 0)
        ask = float(getattr(contract, "ask", 0) or 0)
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else 0.0
        spread = abs(ask - bid) / mid * 100.0 if mid > 0 else 0.0
        if oi >= min_oi and vol >= min_volume and (max_spread <= 0 or spread <= max_spread):
            return True, "liquid_contract_available", {
                "open_interest": oi,
                "volume": vol,
                "spread_pct": spread,
                "max_spread_pct": max_spread,
            }
    return False, "no_contract_met_liquidity_filters", {
        "chain_rows": len(chain),
        "min_open_interest": min_oi,
        "min_volume": min_volume,
        "max_spread_pct": max_spread,
    }


def run_paper_options_diagnostics(
    *,
    project_root: Path = PROJECT_ROOT,
    user_id: str = "paper_bot",
    symbol: str = "QQQ",
    price: float = 350.0,
    vwap: float = 349.0,
    account_equity: float = 100_000.0,
    data_dir: Path | None = None,
    now: datetime | None = None,
    chain_source: str = "mock",
) -> dict[str, Any]:
    """Run entry evaluation plus paper-only option selection against a mock broker."""

    symbol_u = str(symbol or "").strip().upper()
    if not symbol_u:
        raise ValueError("symbol is required")

    user_config, user_is_paper = _load_user_config_without_credentials(project_root, user_id)
    if not user_is_paper:
        raise RuntimeError(f"paper_options_diagnostics_requires_paper_user user={user_id}")

    config = _diagnostic_config(user_config, symbol=symbol_u)
    dt = now or datetime(2026, 6, 9, 14, 0, 0)
    data_root = data_dir or (project_root / "data" / "paper_options_diagnostics")
    requested_chain_source = str(chain_source or "mock").strip().lower()
    chain = sample_validation_chain(symbol_u, dt)
    chain_source_used = "mock"
    chain_source_reason = (
        "deterministic offline validation chain; real Alpaca option chain was not requested"
        if requested_chain_source in {"mock", ""}
        else "real_chain_unavailable_falling_back_to_mock"
    )
    broker = PaperValidationBroker(chain)
    broker.paper = True
    execution_manager = ExecutionManager(config)
    setattr(execution_manager, "_options_data_dir", data_root)
    setattr(execution_manager, "_sqlite_user_id", user_id)
    setattr(broker, "_sqlite_user_id", user_id)

    opts = config.get("options") or {}
    allowed_underlyings = {str(s).strip().upper() for s in (opts.get("allowed_underlyings") or []) if str(s).strip()}
    diagnostics: dict[str, dict[str, Any]] = {
        "signal": _option_stage(passed=False, reason="not_evaluated"),
        "allowed_underlying": _option_stage(
            passed=symbol_u in allowed_underlyings,
            reason="allowed" if symbol_u in allowed_underlyings else "underlying_not_allowlisted",
            detail={"allowed_underlyings": sorted(allowed_underlyings)},
        ),
        "contract_found": _option_stage(
            passed=bool(chain),
            reason="chain_rows_available" if chain else "no_option_chain_rows",
            detail={"chain_rows": len(chain)},
        ),
        "liquidity": _option_stage(passed=False, reason="not_evaluated"),
        "sizing": _option_stage(passed=False, reason="not_evaluated"),
        "risk_cap": _option_stage(passed=False, reason="not_evaluated"),
        "submit_attempted": _option_stage(passed=False, reason="not_evaluated"),
        "broker_response": _option_stage(passed=False, reason="not_evaluated"),
    }
    liquidity_ok, liquidity_reason, liquidity_detail = _chain_liquidity_ok(chain, config)
    diagnostics["liquidity"] = _option_stage(
        passed=liquidity_ok,
        reason=liquidity_reason,
        detail=liquidity_detail,
    )
    log.info(
        "OPTIONS_CONFIG enabled=%s mode=%s paper_only_active=%s",
        str(bool(opts.get("enabled"))).lower(),
        str(opts.get("mode") or "unset").strip().lower() or "unset",
        str(bool(paper_only_options_active(config))).lower(),
    )
    log.info(
        "MOCK_CHAIN_USED symbol=%s chain_rows=%d reason=%s",
        symbol_u,
        len(chain),
        chain_source_reason,
    )
    log.info(
        "ENTRY_PIPELINE_STAGE symbol=%s stage=entry_eval_start result=running reason=paper_options_diagnostic",
        symbol_u,
    )

    engine = TradingEngine(config)
    entry_override = EntrySignal(
        symbol=symbol_u,
        side="long",
        strength=0.9,
        stop_pct=2.0,
        take_profit_pct=4.0,
        time_bars_exit=20,
        metadata={
            "source": "paper_options_diagnostic",
            "alternate_entry": True,
            "news_score": 4.0,
            "event_score": 3.0,
            "catalyst_score": 0.9,
        },
    )
    decision = engine.run_entry_gates(
        symbol=symbol_u,
        dt=dt,
        account_equity=float(account_equity),
        current_positions={},
        sector_exposure_pct={},
        spread_pct=0.05,
        volume_atr_ratio=2.0,
        atr_pct=1.0,
        ohlcv_df=_sample_uptrend_ohlcv(price=float(price)),
        log_strategy_context=True,
        entry_override=entry_override,
        regime_score=3,
        skip_spread_check=True,
        regime_condition="neutral",
        gross_exposure_pct=0.0,
        net_exposure_pct=0.0,
        theme_exposure_pct={},
        strategy_winrate=0.55,
        dynamic_symbols=[symbol_u],
        entry_route="paper_options_diagnostic",
    )
    entry_allowed = bool(getattr(decision, "allowed", False))
    entry_reason = str(getattr(decision, "reason", "") or "ok")
    log.info(
        "ENTRY_PIPELINE_STAGE symbol=%s stage=entry_eval result=%s reason=%s",
        symbol_u,
        "allowed" if entry_allowed else "blocked",
        entry_reason,
    )
    if not entry_allowed:
        diagnostics["signal"] = _option_stage(passed=False, reason=entry_reason)
        log.info(
            "PAPER_OPTIONS_DIAGNOSTIC symbol=%s signal=%s allowed_underlying=%s contract_found=%s liquidity=%s sizing=%s risk_cap=%s submit_attempted=%s broker_response=%s reason=%s",
            symbol_u,
            diagnostics["signal"]["passed"],
            diagnostics["allowed_underlying"]["passed"],
            diagnostics["contract_found"]["passed"],
            diagnostics["liquidity"]["passed"],
            diagnostics["sizing"]["passed"],
            diagnostics["risk_cap"]["passed"],
            diagnostics["submit_attempted"]["passed"],
            diagnostics["broker_response"]["passed"],
            entry_reason,
        )
        log.info(
            "OPTION_PIPELINE_STAGE symbol=%s stage=options_route result=skipped reason=entry_eval_not_allowed:%s",
            symbol_u,
            entry_reason,
        )
        return {
            "ok": False,
            "user": user_id,
            "symbol": symbol_u,
            "entry_allowed": False,
            "entry_reason": entry_reason,
            "options_attempted": False,
            "chain_source": chain_source_used,
            "chain_source_reason": chain_source_reason,
            "option_diagnostics": diagnostics,
            "paper_mock_orders": [],
        }
    diagnostics["signal"] = _option_stage(passed=True, reason=entry_reason or "entry_allowed")

    log.info(
        "OPTION_PIPELINE_STAGE symbol=%s stage=options_route result=running reason=entry_eval_allowed",
        symbol_u,
    )
    result = attempt_paper_option_entry(
        config,
        broker=broker,
        execution_manager=execution_manager,
        symbol=symbol_u,
        dt=dt,
        current_price=float(price),
        session_vwap=float(vwap),
        account_equity=float(account_equity),
        positions=[],
        source="paper_options_diagnostic",
        conviction_score=0.9,
        news_score=4.0,
        event_score=3.0,
        relative_volume=2.0,
        chain_candidates=chain,
        enforce_dynamic_gate=False,
    )
    log.info(
        "OPTION_PIPELINE_STAGE symbol=%s stage=options_route result=%s reason=%s",
        symbol_u,
        "placed" if result.placed else "blocked",
        result.reason or "ok",
    )
    submitted_orders = list(broker.submitted_orders)
    diagnostics["risk_cap"] = _option_stage(
        passed="daily_loss_block" not in result.reason_codes,
        reason=result.reason if "daily_loss_block" in result.reason_codes else "risk_cap_passed",
        detail={"reason_codes": list(result.reason_codes)},
    )
    diagnostics["submit_attempted"] = _option_stage(
        passed=bool(submitted_orders),
        reason="submit_order_called" if submitted_orders else (result.reason or "submit_not_attempted"),
    )
    diagnostics["broker_response"] = _option_stage(
        passed=bool(result.placed),
        reason="paper_order_placed" if result.placed else (result.reason or "broker_no_order"),
        detail={"submitted_orders": len(submitted_orders), "right": result.right, "direction": result.direction},
    )
    diagnostics["sizing"] = _option_stage(
        passed=bool(submitted_orders),
        reason="sized_order_created" if submitted_orders else (result.reason or "no_sized_order"),
        detail={"submitted_orders": len(submitted_orders)},
    )
    log.info(
        "PAPER_OPTIONS_DIAGNOSTIC symbol=%s signal=%s allowed_underlying=%s contract_found=%s liquidity=%s sizing=%s risk_cap=%s submit_attempted=%s broker_response=%s reason=%s",
        symbol_u,
        diagnostics["signal"]["passed"],
        diagnostics["allowed_underlying"]["passed"],
        diagnostics["contract_found"]["passed"],
        diagnostics["liquidity"]["passed"],
        diagnostics["sizing"]["passed"],
        diagnostics["risk_cap"]["passed"],
        diagnostics["submit_attempted"]["passed"],
        diagnostics["broker_response"]["passed"],
        result.reason or "ok",
    )
    return {
        "ok": bool(entry_allowed),
        "user": user_id,
        "symbol": symbol_u,
        "entry_allowed": entry_allowed,
        "entry_reason": entry_reason,
        "options_attempted": True,
        "options_placed": bool(result.placed),
        "options_reason": result.reason,
        "chain_source": chain_source_used,
        "chain_source_reason": chain_source_reason,
        "option_diagnostics": diagnostics,
        "paper_mock_orders": [
            {
                "symbol": str(getattr(order, "symbol", "") or ""),
                "side": str(getattr(order, "side", "") or ""),
                "limit_price": getattr(order, "limit_price", None),
            }
            for order in submitted_orders
        ],
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default="paper_bot", help="Paper user config to load")
    parser.add_argument("--symbol", default="QQQ", help="Underlying symbol to evaluate")
    parser.add_argument("--price", type=float, default=350.0)
    parser.add_argument("--vwap", type=float, default=349.0)
    parser.add_argument("--account-equity", type=float, default=100_000.0)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument(
        "--chain-source",
        choices=("mock", "auto", "real"),
        default="mock",
        help="Option chain source. Default mock is offline-safe and prints MOCK_CHAIN_USED.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON result summary")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        result = run_paper_options_diagnostics(
            project_root=args.project_root,
            user_id=args.user,
            symbol=args.symbol,
            price=args.price,
            vwap=args.vwap,
            account_equity=args.account_equity,
            data_dir=args.data_dir,
            chain_source=args.chain_source,
        )
    except Exception as exc:
        print(f"PAPER_OPTIONS_DIAGNOSTICS_FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        status = "PASS" if result.get("ok") else "FAIL"
        print(
            f"{status} paper options diagnostics user={result.get('user')} "
            f"symbol={result.get('symbol')} chain_source={result.get('chain_source')} "
            f"options_placed={result.get('options_placed')}",
            flush=True,
        )
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
