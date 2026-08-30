"""
Score-tiered entry policy: SQQQ (inverse) sizing / gates vs trend longs.

Maps ``market_regime`` score 0–5 to practical risk stance (see ``entry_policy`` in YAML).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RegimeEntryPolicy:
    """Derived each entry pass from regime score + config."""

    score: int | None
    # SQQQ stock (initial sizing); controlled scaling uses separate config.
    sqqq_blocked: bool
    sqqq_requires_severe_breakdown: bool
    sqqq_notional_fraction: float
    # Trend long stock / gates path
    long_entries_blocked: bool
    long_notional_fraction: float
    long_require_ma_stack: bool  # MA_fast > MA_slow + close > MA_fast (stricter trend)


def _ep_cfg(config: dict[str, Any]) -> dict[str, Any]:
    mr = config.get("market_regime") or {}
    return mr.get("entry_policy") or {}


def severe_breakdown_ok(
    *,
    qqq_price: float | None,
    qqq_ma50: float | None,
    min_pct_below_ma: float,
    require_fresh_cross: bool,
    qqq_fresh_cross_ma50: bool | None,
) -> bool:
    """Very clear QQQ breakdown vs 50D MA (distance + optional fresh cross)."""
    if qqq_price is None or qqq_ma50 is None or float(qqq_ma50) <= 0:
        return False
    px = float(qqq_price)
    ma = float(qqq_ma50)
    pct = (ma - px) / ma * 100.0
    if pct < float(min_pct_below_ma):
        return False
    if require_fresh_cross and qqq_fresh_cross_ma50 is not True:
        return False
    return True


def compute_regime_entry_policy(
    config: dict[str, Any],
    *,
    regime_score: int | None,
    regime_scorer_enabled: bool,
) -> RegimeEntryPolicy:
    """
    Build policy from ``market_regime.entry_policy`` and *regime_score*.

    If ``market_regime.enabled`` is off, use :func:`compute_regime_entry_policy` only
    when ``entry_policy.enabled`` is true (see early return).

    If the **scorer** is disabled: fully permissive (no SQQQ / long tiering).

    If the scorer is enabled but *regime_score* is None: use ``*_when_regime_unavailable`` keys.
    """
    ep = _ep_cfg(config)
    if not bool(ep.get("enabled", True)):
        return RegimeEntryPolicy(
            score=regime_score,
            sqqq_blocked=False,
            sqqq_requires_severe_breakdown=False,
            sqqq_notional_fraction=1.0,
            long_entries_blocked=False,
            long_notional_fraction=1.0,
            long_require_ma_stack=False,
        )

    if not regime_scorer_enabled:
        return RegimeEntryPolicy(
            score=regime_score,
            sqqq_blocked=False,
            sqqq_requires_severe_breakdown=False,
            sqqq_notional_fraction=1.0,
            long_entries_blocked=False,
            long_notional_fraction=1.0,
            long_require_ma_stack=False,
        )

    if regime_score is None:
        # Scorer on but no score this pass (e.g. bar fetch gap)
        return RegimeEntryPolicy(
            score=regime_score,
            sqqq_blocked=bool(ep.get("block_sqqq_when_regime_score_unavailable", False)),
            sqqq_requires_severe_breakdown=bool(
                ep.get("require_severe_breakdown_when_regime_unavailable", True)
            ),
            sqqq_notional_fraction=float(ep.get("sqqq_fraction_when_regime_unavailable", 0.35)),
            long_entries_blocked=False,
            long_notional_fraction=float(ep.get("long_fraction_when_regime_unavailable", 0.5)),
            long_require_ma_stack=True,
        )

    s = int(regime_score)
    if s >= 4:
        return RegimeEntryPolicy(
            score=s,
            sqqq_blocked=False,
            sqqq_requires_severe_breakdown=False,
            sqqq_notional_fraction=float(ep.get("sqqq_notional_fraction_score_4_5", 1.0)),
            long_entries_blocked=False,
            long_notional_fraction=float(ep.get("long_notional_fraction_score_4_5", 1.0)),
            long_require_ma_stack=False,
        )
    if s == 3:
        return RegimeEntryPolicy(
            score=s,
            sqqq_blocked=False,
            sqqq_requires_severe_breakdown=False,
            sqqq_notional_fraction=float(ep.get("sqqq_notional_fraction_score_3", 0.5)),
            long_entries_blocked=False,
            long_notional_fraction=float(ep.get("long_notional_fraction_score_3", 0.75)),
            long_require_ma_stack=False,
        )
    if s == 2:
        return RegimeEntryPolicy(
            score=s,
            sqqq_blocked=False,
            sqqq_requires_severe_breakdown=False,
            sqqq_notional_fraction=float(ep.get("sqqq_notional_fraction_score_2", 0.35)),
            long_entries_blocked=False,
            long_notional_fraction=float(ep.get("long_notional_fraction_score_2", 0.5)),
            long_require_ma_stack=True,
        )
    if s == 1:
        return RegimeEntryPolicy(
            score=s,
            sqqq_blocked=False,
            sqqq_requires_severe_breakdown=True,
            sqqq_notional_fraction=float(ep.get("sqqq_notional_fraction_score_0_1", 0.25)),
            long_entries_blocked=bool(ep.get("score_1_block_all_longs", False)),
            long_notional_fraction=float(ep.get("long_notional_fraction_score_1", 0.2)),
            long_require_ma_stack=True,
        )
    # score <= 0
    return RegimeEntryPolicy(
        score=s,
        sqqq_blocked=False,
        sqqq_requires_severe_breakdown=True,
        sqqq_notional_fraction=float(ep.get("sqqq_notional_fraction_score_0_1", 0.25)),
        long_entries_blocked=bool(ep.get("score_0_block_all_longs", True)),
        long_notional_fraction=float(ep.get("long_notional_fraction_score_0", 0.0)),
        long_require_ma_stack=True,
    )


def policy_blocks_sqqq_entry(
    policy: RegimeEntryPolicy,
    *,
    severe_ok: bool,
) -> tuple[bool, str | None]:
    if policy.sqqq_blocked:
        return True, "regime entry policy: SQQQ blocked (no regime score)"
    if policy.sqqq_requires_severe_breakdown and not severe_ok:
        return (
            True,
            "regime entry policy: SQQQ needs severe breakdown (score %s)"
            % (policy.score if policy.score is not None else "?"),
        )
    return False, None
