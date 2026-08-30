"""End-to-end validation for paper-only options trading."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from src.execution import ExecutionManager, OrderRequest
from src.live.options_chain import broker_mode_is_paper
from src.live.options_paper import PaperOptionEntryResult, attempt_paper_option_entry
from src.options_position_manager import options_state_path, record_option_exit
from src.options_selector import OptionContractCandidate


@dataclass(frozen=True)
class OptionsPaperValidationStep:
    """One validation checkpoint."""

    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class OptionsPaperValidationReport:
    """Paper options validation result."""

    passed: bool
    symbol: str
    user_id: str
    order_symbol: str | None = None
    order_id: str | None = None
    steps: tuple[OptionsPaperValidationStep, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "symbol": self.symbol,
            "user_id": self.user_id,
            "order_symbol": self.order_symbol,
            "order_id": self.order_id,
            "steps": [
                {"name": s.name, "passed": s.passed, "detail": s.detail}
                for s in self.steps
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


class PaperValidationBroker:
    """Paper broker stub that records option order submissions."""

    paper = True

    def __init__(self, chain: Sequence[OptionContractCandidate]) -> None:
        self.chain = list(chain)
        self.submitted_orders: list[OrderRequest] = []

    def get_account_snapshot(self) -> dict[str, float]:
        return {"equity": 100_000.0, "last_equity": 100_000.0}

    def get_option_chain_candidates(
        self,
        underlying: str,
        *,
        expiration_date_gte: date | None = None,
        expiration_date_lte: date | None = None,
    ) -> list[OptionContractCandidate]:
        out: list[OptionContractCandidate] = []
        underlying_u = str(underlying or "").strip().upper()
        for row in self.chain:
            if not str(row.symbol).upper().startswith(underlying_u):
                continue
            if expiration_date_gte is not None and row.expiration < expiration_date_gte:
                continue
            if expiration_date_lte is not None and row.expiration > expiration_date_lte:
                continue
            out.append(row)
        return out

    def submit_order(self, request: OrderRequest) -> Any:
        self.submitted_orders.append(request)
        return SimpleNamespace(
            id="paper-validation-order-1",
            status="filled",
            filled_avg_price=request.limit_price or request.expected_price,
            limit_price=request.limit_price,
        )

    def resolve_entry_price_from_fill(self, order: Any, *, fallback: float) -> float:
        return float(getattr(order, "filled_avg_price", None) or fallback)


def sample_validation_chain(symbol: str, as_of: datetime | None = None) -> list[OptionContractCandidate]:
    """Return a liquid deterministic option chain for offline validation.

    The first row is the historical ATM-like mock contract that costs about
    $200/contract. Additional cheaper OTM rows let paper-only diagnostics prove
    that contract selection can step under a tight premium budget without
    relying on live option-chain access.
    """
    now = as_of or datetime.now(timezone.utc)
    exp = now.date() + timedelta(days=21)
    root = str(symbol or "QQQ").strip().upper()
    yymmdd = f"{exp.year % 100:02d}{exp.month:02d}{exp.day:02d}"
    return [
        OptionContractCandidate(
            symbol=f"{root}{yymmdd}C00350000",
            strike=350.0,
            expiration=exp,
            right="call",
            open_interest=2500,
            volume=900,
            bid=1.95,
            ask=2.05,
            delta=0.45,
            iv=0.32,
        ),
        OptionContractCandidate(
            symbol=f"{root}{yymmdd}C00360000",
            strike=360.0,
            expiration=exp,
            right="call",
            open_interest=1800,
            volume=650,
            bid=0.98,
            ask=1.02,
            delta=0.35,
            iv=0.34,
        ),
        OptionContractCandidate(
            symbol=f"{root}{yymmdd}C00370000",
            strike=370.0,
            expiration=exp,
            right="call",
            open_interest=1400,
            volume=500,
            bid=0.70,
            ask=0.80,
            delta=0.31,
            iv=0.36,
        ),
        OptionContractCandidate(
            symbol=f"{root}{yymmdd}P00340000",
            strike=340.0,
            expiration=exp,
            right="put",
            open_interest=1600,
            volume=550,
            bid=0.98,
            ask=1.02,
            delta=-0.35,
            iv=0.35,
        ),
    ]


def paper_validation_config(symbol: str = "QQQ") -> dict[str, Any]:
    """Minimal paper-only options config used by the validation CLI and tests."""
    sym = str(symbol or "QQQ").strip().upper()
    return {
        "broker": {"paper": True},
        "portfolio": {"max_options_capital_pct": 40},
        "execution": {
            "max_spread_pct": 20,
            "prefer_limit_orders": True,
            "limit_price_mode": "inside_spread",
            "inside_spread_fraction": 0.5,
        },
        "options": {
            "enabled": True,
            "allow_new_entries": True,
            "new_entries_enabled": True,
            "mode": "paper_only",
            "allowed_underlyings": [sym],
            "entry_mapping": {"bullish_signal": "call", "bearish_signal": "put"},
            "max_total_options_exposure_pct": 40,
            "risk_per_trade_pct": 2,
            "max_option_position_pct": 100,
            "max_open_option_positions": 5,
            "max_contracts_per_trade": 1,
            "v1_max_contracts_per_trade": 1,
            "max_daily_loss_pct": 2,
            "max_bid_ask_spread_pct": 12,
            "contract_selection": {
                "expiry_min_days": 14,
                "expiry_max_days": 35,
                "min_open_interest": 100,
                "min_volume": 50,
                "max_bid_ask_spread_pct": 12,
            },
        },
    }


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _step(name: str, passed: bool, detail: str) -> OptionsPaperValidationStep:
    return OptionsPaperValidationStep(name=name, passed=bool(passed), detail=detail)


def validate_options_paper_e2e(
    config: Mapping[str, Any],
    *,
    broker: Any,
    symbol: str = "QQQ",
    user_id: str = "default",
    data_dir: Path | None = None,
    now: datetime | None = None,
    current_price: float = 350.0,
    session_vwap: float = 349.0,
    account_equity: float = 100_000.0,
    positions: list[dict[str, Any]] | None = None,
    chain_candidates: Sequence[OptionContractCandidate] | None = None,
) -> OptionsPaperValidationReport:
    """
    Validate scan, contract selection, paper order submission, entry persistence, and exit handling.

    The function is paper-safe: it refuses non-paper brokers and only submits to the broker object
    passed by the caller.
    """
    symbol_u = str(symbol or "").strip().upper()
    dt = now or datetime.now(timezone.utc)
    steps: list[OptionsPaperValidationStep] = []
    if not broker_mode_is_paper(broker, dict(config)):
        steps.append(_step("paper_mode", False, "broker/config resolved to live mode"))
        return OptionsPaperValidationReport(False, symbol_u, user_id, steps=tuple(steps))
    steps.append(_step("paper_mode", True, "paper broker confirmed"))

    chain = list(chain_candidates) if chain_candidates is not None else []
    if not chain:
        getter = getattr(broker, "get_option_chain_candidates", None)
        if getter is not None:
            chain = list(getter(symbol_u))
    steps.append(_step("option_scan", bool(chain), f"{len(chain)} candidate(s) available"))
    if not chain:
        return OptionsPaperValidationReport(False, symbol_u, user_id, steps=tuple(steps))

    execution_manager = ExecutionManager(dict(config))
    setattr(execution_manager, "_options_data_dir", data_dir)
    setattr(execution_manager, "_sqlite_user_id", user_id)
    setattr(broker, "_sqlite_user_id", user_id)

    result: PaperOptionEntryResult = attempt_paper_option_entry(
        dict(config),
        broker=broker,
        execution_manager=execution_manager,
        symbol=symbol_u,
        dt=dt,
        current_price=float(current_price),
        session_vwap=float(session_vwap),
        account_equity=float(account_equity),
        positions=positions or [],
        source="options_paper_validation",
        conviction_score=0.9,
        news_score=4.0,
        event_score=3.0,
        relative_volume=2.0,
        chain_candidates=chain,
        enforce_dynamic_gate=False,
    )
    submitted = list(getattr(broker, "submitted_orders", []) or [])
    order = submitted[-1] if submitted else None
    steps.append(_step("contract_selection", result.direction is not None and result.right is not None, f"direction={result.direction} right={result.right}"))
    steps.append(_step("order_submission", bool(result.placed and order is not None), result.reason or "paper option order placed"))

    order_symbol = str(getattr(order, "symbol", "") or "") if order is not None else None
    if not result.placed or order is None or not order_symbol:
        return OptionsPaperValidationReport(False, symbol_u, user_id, order_symbol=order_symbol, steps=tuple(steps))

    state_path = options_state_path(user_id, data_dir=data_dir)
    state = _read_state(state_path)
    positions_map = state.get("positions") if isinstance(state.get("positions"), dict) else {}
    entry_open = order_symbol in positions_map
    steps.append(_step("entry_persistence", entry_open, f"state={state_path}"))
    if not entry_open:
        return OptionsPaperValidationReport(False, symbol_u, user_id, order_symbol=order_symbol, steps=tuple(steps))

    exit_price = float(getattr(order, "limit_price", None) or getattr(order, "expected_price", None) or 0.0) * 1.05
    record_option_exit(
        order_symbol,
        user_id=user_id,
        data_dir=data_dir,
        exit_reason="paper_validation_exit",
        exit_price=exit_price,
        realized_pl=25.0,
        now=dt + timedelta(minutes=30),
    )
    closed = _read_state(state_path)
    history = closed.get("history") if isinstance(closed.get("history"), list) else []
    still_open = order_symbol in (closed.get("positions") if isinstance(closed.get("positions"), dict) else {})
    exit_recorded = bool(history) and not still_open
    steps.append(_step("exit_handling", exit_recorded, f"history_records={len(history)}"))

    passed = all(s.passed for s in steps)
    return OptionsPaperValidationReport(
        passed,
        symbol_u,
        user_id,
        order_symbol=order_symbol,
        order_id="paper-validation-order-1",
        steps=tuple(steps),
    )
