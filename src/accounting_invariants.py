"""Hard accounting invariants for trading records."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from src.trading_diagnostics import run_trading_audit
from src.trading_control import persist_integrity_incident


@dataclass(frozen=True)
class InvariantFailure:
    code: str
    detail: str
    context: dict[str, Any]


def invariant_failures_from_audit(problems: Sequence[Mapping[str, Any]]) -> list[InvariantFailure]:
    mapping = {
        "unmatched_submissions": "ENTRY_BLOCKED_INTEGRITY_FAILURE",
        "unmatched_fills": "FILL_RECONCILIATION_FAILURE",
        "fills_without_decisions": "FILL_RECONCILIATION_FAILURE",
        "positions_without_fills": "POSITION_RECONCILIATION_FAILURE",
        "exits_without_positions": "POSITION_RECONCILIATION_FAILURE",
        "duplicate_fills": "FILL_RECONCILIATION_FAILURE",
        "duplicate_orders": "FILL_RECONCILIATION_FAILURE",
        "missing_exit_records": "LEARNING_SKIPPED_UNRECONCILED_DATA",
    }
    out: list[InvariantFailure] = []
    for problem in problems:
        kind = str(problem.get("kind") or "unknown")
        out.append(
            InvariantFailure(
                code=mapping.get(kind, "ENTRY_BLOCKED_INTEGRITY_FAILURE"),
                detail=str(problem.get("detail") or kind),
                context=dict(problem),
            )
        )
    return out


def enforce_daily_invariants(*, root, day: str, user: str) -> list[InvariantFailure]:
    audit = run_trading_audit(root=root, day=day, user=user)
    failures = invariant_failures_from_audit(audit.problems)
    for failure in failures:
        persist_integrity_incident(
            root / "data",
            user_id=user,
            reason_code=failure.code,
            detail=failure.detail,
            context=failure.context,
        )
    return failures
