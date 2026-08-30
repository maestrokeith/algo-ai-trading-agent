#!/usr/bin/env python3
"""
Run the trading engine with Alpaca as broker.

Uses config broker section (broker.firm: alpaca, broker.paper).
Environment: APCA_API_KEY_ID, APCA_API_SECRET_KEY.
CLI: --live or --paper to override config.
"""
import argparse
import sys
from pathlib import Path
from datetime import datetime
import pytz

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import load_config
from src.trading_engine import TradingEngine
from src.brokers.alpaca_client import AlpacaBroker
from src.strategy import _atr
from src.loop_helpers import entries_insufficient_buying_power
from src.portfolio_allocation import effective_buying_power_for_entries


def main() -> None:
    parser = argparse.ArgumentParser(description="Run trading engine once with Alpaca")
    parser.add_argument("--live", action="store_true", help="Use live account (real money)")
    parser.add_argument("--paper", action="store_true", help="Use paper account (default)")
    args = parser.parse_args()
    if args.live and args.paper:
        parser.error("Use only one of --live or --paper")

    config_path = PROJECT_ROOT / "config" / "default.yaml"
    config = load_config(config_path)
    if args.live:
        config.setdefault("broker", {})["paper"] = False
    elif args.paper:
        config.setdefault("broker", {})["paper"] = True

    broker_cfg = config.get("broker", {})
    if broker_cfg.get("firm") != "alpaca":
        print("Config broker.firm is not 'alpaca'. Set broker.firm: alpaca and broker.paper: true|false.")
        sys.exit(1)

    broker = AlpacaBroker(config)
    print("Broker mode:", "PAPER" if broker.paper else "LIVE (real money)")
    engine = TradingEngine(config=config)

    account_equity = broker.get_equity()
    engine.update_equity(account_equity)
    engine.state.pdt.equity = account_equity

    api = broker
    positions = api.get_positions()
    _bp_eff = effective_buying_power_for_entries(
        buying_power=broker.get_buying_power(),
        equity=float(account_equity),
        config=config,
    )
    if entries_insufficient_buying_power(_bp_eff, config.get("entries")):
        print("skip — insufficient buying power")
        sys.exit(0)
    current_positions = {p["symbol"]: {"notional": p["market_value"], "stop_pct": 1.5} for p in positions}
    sector_exposure_pct = {}  # Optional: map sector -> current exposure %

    symbol = config.get("universe", {}).get("symbols", ["SPY"])[0]
    df = broker.get_bars(symbol, timeframe="1Day", limit=220)
    if df.empty or len(df) < 200:
        print(f"Not enough bars for {symbol} (got {len(df)}). Skip.")
        sys.exit(0)

    quote = broker.get_latest_quote(symbol)
    spread_pct = quote.spread_pct if quote else 0.15
    skip_spread = quote is None or bool(getattr(quote, "skip_spread_check", False))
    atr = _atr(df["high"], df["low"], df["close"], 14)
    atr_pct = (atr.iloc[-1] / df["close"].iloc[-1]) * 100 if len(atr) else None
    volume_atr_ratio = 1.5  # Placeholder; compute from bars if needed

    et = pytz.timezone("America/New_York")
    dt = datetime.now(et)
    decision = engine.run_entry_gates(
        symbol=symbol,
        dt=dt,
        account_equity=account_equity,
        current_positions=current_positions,
        sector_exposure_pct=sector_exposure_pct,
        spread_pct=spread_pct,
        volume_atr_ratio=volume_atr_ratio,
        atr_pct=atr_pct,
        ohlcv_df=df,
        symbol_sector=None,
        skip_spread_check=skip_spread,
    )

    print("Entry decision:", decision.allowed, "—", decision.reason)
    if not decision.allowed:
        sys.exit(0)

    if decision.order_request:
        notional = (decision.position_sizing.notional if decision.position_sizing else 0) or 0
        buying_power = effective_buying_power_for_entries(
            buying_power=broker.get_buying_power(),
            equity=float(account_equity),
            config=config,
        )
        if notional > buying_power:
            print(
                "Skip — insufficient buying power (need $%.0f, have $%.0f after min cash reserve)"
                % (notional, buying_power)
            )
            sys.exit(0)
        order = broker.submit_order(decision.order_request)
        print("Order submitted:", getattr(order, "id", order))
        if getattr(decision, "decision_report", None):
            print(decision.decision_report)
        if getattr(order, "limit_price", None) is not None:
            print("  Limit price:", order.limit_price, "Qty:", order.qty)


if __name__ == "__main__":
    main()
