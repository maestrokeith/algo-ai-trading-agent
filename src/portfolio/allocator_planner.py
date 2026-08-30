"""
Drop-in capital allocator: rank candidates vs held book, emit buy/sell actions by notional.

Used as a planning helper and, when ``portfolio.capital_allocator.enabled`` is true, as the post-scan
stock path in ``scripts/run_alpaca_loop.py`` (see ``src/capital_allocator_loop.py``).
Mutates *only* shallow copies of *portfolio* and a running *cash* float so multiple candidates in one
pass see a coherent state.

Migration note: because this repo already has ``src/portfolio/allocator.py``, the split allocator
surfaces live as ``src/portfolio/allocator_*.py`` modules rather than a sibling
``src/portfolio/allocator/`` package. This file remains the compatibility entry point while callers
move over gradually.
"""
from __future__ import annotations

import copy
import logging
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from src.exposure_gates import parse_equity_fraction_optional
from src.portfolio_allocation import _regime_leg_for_cash_reserve
from src.risk_limits import effective_symbol_allocation_cap_pct, parse_allocation_fraction

log = logging.getLogger(__name__)

# Skip the minimum-cash-to-deploy buy only when ``trade_size + buffer < min_deploy_dollars`` (dollars).
MINIMUM_CASH_TO_DEPLOY_SKIP_BUFFER_USD = 5.0


def _print_allocator_skip(symbol: str, reason: str, *, detail: str | None = None) -> None:
    sym_u = str(symbol or "").strip().upper()
    if not sym_u:
        return
    msg = f"SKIP {sym_u}: reason={reason}"
    if detail:
        msg += f" detail={detail}"
    print(msg, flush=True)


def allocator_candidate_book_score(
    candidate: Mapping[str, Any] | None, *, rank_score: float
) -> float:
    """
    Score used vs :meth:`rotate_capital` / book rows from :func:`~src.capital_allocator_loop.build_allocator_portfolio`
    (tracker strength, 0–1 scale). When the signal row carries ``strength_eff``, that value is used;
    otherwise *rank_score* (often composite) is the fallback.
    """
    if not isinstance(candidate, Mapping):
        return float(rank_score)
    raw = candidate.get("strength_eff")
    if raw is not None and str(raw).strip() != "":
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    return float(rank_score)


def parse_capital_allocator_cfg(portfolio_cfg: Mapping[str, Any] | None) -> dict[str, Any]:
    """Read ``portfolio.capital_allocator`` with safe defaults (see ``config/default.yaml``).

    When ``capital_allocator.max_positions`` is omitted, uses ``portfolio.allocator.max_positions`` if set.
    ``soft_cap_mode`` / ``cap_penalty_multiplier`` / ``allow_cross_bucket_rebalance`` are read from
    ``capital_allocator`` when the key is present, else from ``portfolio.allocator``.
    ``net_reduction_max_buy_to_sell_ratio`` and ``net_reduction_near_cap_relative_to_max`` control the
    optional stricter buy cap when the book is near the effective max gross (see
    :func:`gross_book_near_effective_max_for_net_reduction`).
    ``min_gross_deployment_pct`` sets the gross fraction below which allocator **mode** is
    ``deploy`` (under-invested) when not in ``risk_control``. Set to ``0`` to disable.
    ``deploy_top_n_signals`` (default 4, allowed 3–5) limits deploy-mode plans to the top *N*
    candidates by ``score`` after diversification reorder.
    ``empty_alloc_top_n`` (default 5, clamped 1–20) is how many top ``score`` names get an
    equal cash split when the allocator plan is empty and ``fallback_on_empty_alloc`` is on.
    ``if_no_actions_cycles`` + ``fallback_pick_top_n`` + ``fallback_size_pct`` optionally enable an
    idle-cycle fallback: after N consecutive empty allocator passes, try fixed-size BUYs in the top
    ranked names before giving up for the cycle. ``idle_fallback.max_gross_pct`` caps those fallback
    BUYs so they cannot crowd out later dynamic-scan entries.
    ``fallback_enforce_diversity`` + ``correlation_max_per_group`` + ``correlation_groups`` optionally
    cap how many selected names come from the same configured correlation bucket.
    ``concentration_bias`` (optional): after ranking candidates by ``score`` (see
    :func:`src.capital_allocator_loop.rank_allocator_candidates`), scale the **dollar tranche**
    (``min_trade_size``) for the top ``top_n`` vs the rest.
    ``bullish_force_minimum_deploy`` (default on): with ``regime_score==4`` and gross **below**
    ``min_gross_deployment_pct``, the live pass sets *force_allocate* and skips
    ``require_net_sell_gte_buy`` trimming of buy-only plans.
    ``rebalance_fund_from_weakest`` / ``rebalance_weakest_trim_fraction``: when true, the allocator
    may sell a fraction of a held line to top up *cash* before a buy (strongest-first lines in sort
    order), and use the full trim fraction in rotation (not capped at ``min_trade_size`` for the sell leg).
    With ``sell_only_if_needed`` (default true), that cash-raise sell only executes when the candidate
    strictly outranks the sourced line (:meth:`CapitalAllocator.rotate_capital`) — same as rotation swaps.
    ``replace_weakest_with_stronger`` (default true): when the book is full, only sell-trim+swap when
    the candidate passes :meth:`CapitalAllocator.rotate_capital` (incoming vs weakest ×
    ``replacement_strength_ratio``, default strict ``>`` when ratio is ``1.0``).
    ``single_pass_per_cycle`` (default true): live loop runs one allocator execute per heartbeat;
    skips the legacy second ``post_sell_reallocation`` execute (remainder equal-split defers to next cycle).
    ``max_gross_increase_per_cycle`` (optional, ``capital_allocator`` or ``portfolio.allocator``): max
    **additional** gross book vs equity in one allocator pass —
    ``min(effective max gross USD, current gross USD + equity × fraction)``. Omitted or ``0`` leaves only the book cap.
    ``symbol_caps`` (optional):
    - **Soft / hard (two-line cap):** ``soft`` and ``hard`` (fractions of equity). In the allocator,
      between soft and hard, adds use the penalty tranche; at/above **hard** no new adds. Optional
      ``bullish_soft`` / ``bullish_hard`` apply when the regime leg is *bullish* (same as cash reserves;
      score ≥ 4 or ``regime_condition: bullish``); otherwise use ``soft``/``hard``.
    - **Per score:** ``regime_<n>`` (e.g. ``regime_4: 0.15``) sets the per-name **hard** line for that
      exact ``regime_score`` (soft unchanged unless it would exceed the new hard); also relaxes
      :func:`src.risk_limits.effective_symbol_allocation_cap_pct` vs ``risk.max_symbol_allocation_pct``.
    - **Legacy table:** ``base`` and/or ``bullish_regime`` — a single per-name line (treated as both
      soft and hard) via :func:`effective_capital_allocator_symbol_cap_soft_hard`, then min with
      the risk per-name cap.
    - **Tier buckets:** ``leaders`` / ``core`` / ``defensive`` (and optional nested ``tiers:``) map
      tickers to **hard** caps (same fraction rules). First bucket wins if a symbol is duplicated.
      Per-ticker soft/hard are derived from the global dual band ratio; unlisted symbols use the
      global soft/hard only (see :func:`effective_capital_allocator_symbol_caps_by_symbol`).
    """
    port = portfolio_cfg if isinstance(portfolio_cfg, dict) else {}
    raw = port.get("capital_allocator")
    sub = raw if isinstance(raw, dict) else {}
    al = port.get("allocator")
    al = al if isinstance(al, dict) else {}

    def _bool_cfg(key: str, default: bool) -> bool:
        value = sub.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() not in ("0", "false", "no", "off", "")
        return bool(value) if value is not None else bool(default)

    _mp_raw = sub.get("max_positions")
    if _mp_raw is None or str(_mp_raw).strip() == "":
        _mp_raw = al.get("max_positions")
    try:
        max_pos = int(_mp_raw) if _mp_raw is not None and str(_mp_raw).strip() != "" else 10
    except (TypeError, ValueError):
        max_pos = 10
    _sym_caps_raw = sub.get("symbol_caps")
    _sym_caps: dict[str, Any] = {}
    if isinstance(_sym_caps_raw, dict) and _sym_caps_raw:
        _sym_caps = {str(k): v for k, v in _sym_caps_raw.items()}
    _tier_max = 0.0
    _tier_map = collect_symbol_cap_tier_hard_fractions(_sym_caps) if _sym_caps else {}
    if _tier_map:
        _tier_max = max(float(x) for x in _tier_map.values())
    _has_sh = bool(
        isinstance(_sym_caps, dict)
        and "soft" in _sym_caps
        and "hard" in _sym_caps
        and _sym_caps.get("soft") is not None
        and str(_sym_caps.get("soft", "")).strip() != ""
        and _sym_caps.get("hard") is not None
        and str(_sym_caps.get("hard", "")).strip() != ""
    )
    try:
        if _has_sh:
            sym_cap = float(parse_allocation_fraction(_sym_caps.get("hard")))
        elif (
            "base" in _sym_caps
            and _sym_caps.get("base") is not None
            and str(_sym_caps.get("base", "")).strip() != ""
        ):
            sym_cap = float(parse_allocation_fraction(_sym_caps.get("base")))
        else:
            sym_cap = float(sub.get("symbol_cap", 0.25))
    except (TypeError, ValueError):
        sym_cap = 0.25
    _explicit_symbol_cap = bool(
        sub.get("symbol_cap") is not None
        and str(sub.get("symbol_cap", "")).strip() != ""
    )
    if _tier_max > 0.0 and not _has_sh and not (
        "base" in _sym_caps
        and _sym_caps.get("base") is not None
        and str(_sym_caps.get("base", "")).strip() != ""
    ):
        if _explicit_symbol_cap:
            sym_cap = max(sym_cap, min(1.0, _tier_max))
        else:
            sym_cap = min(1.0, _tier_max)
    try:
        min_trade = float(sub.get("min_trade_size", 1000.0))
    except (TypeError, ValueError):
        min_trade = 1000.0
    try:
        rot = float(sub.get("rotate_trim_fraction", 0.30))
    except (TypeError, ValueError):
        rot = 0.30
    if "soft_cap_mode" in sub:
        sc_b = sub.get("soft_cap_mode", False)
    else:
        sc_b = al.get("soft_cap_mode", False)
    if "cap_penalty_multiplier" in sub:
        try:
            cpm = float(sub.get("cap_penalty_multiplier", 0.5))
        except (TypeError, ValueError):
            cpm = 0.5
    else:
        try:
            cpm = float(al.get("cap_penalty_multiplier", 0.5))
        except (TypeError, ValueError):
            cpm = 0.5
    if "allow_cross_bucket_rebalance" in sub:
        cbr = sub.get("allow_cross_bucket_rebalance", False)
    else:
        cbr = al.get("allow_cross_bucket_rebalance", False)
    _mrl_raw = sub.get("min_realloc_leg")
    if _mrl_raw is None or str(_mrl_raw).strip() == "":
        _mrl_raw = sub.get("min_trade_notional")
    if _mrl_raw is None or str(_mrl_raw).strip() == "":
        _mrl_raw = al.get("min_trade_notional")
    if _mrl_raw is None or str(_mrl_raw).strip() == "":
        _mrl_raw = 300.0
    try:
        min_realloc_leg = float(_mrl_raw)
    except (TypeError, ValueError):
        min_realloc_leg = 300.0
    min_realloc_leg = max(0.0, min_realloc_leg)
    _mss_raw = sub.get("min_signal_strength")
    if _mss_raw is None or str(_mss_raw).strip() == "":
        _mss_raw = al.get("min_signal_strength")
    if _mss_raw is None or str(_mss_raw).strip() == "":
        min_signal_strength = 0.0
    else:
        try:
            min_signal_strength = float(_mss_raw)
        except (TypeError, ValueError):
            min_signal_strength = 0.0
    min_signal_strength = max(0.0, min(1.0, min_signal_strength))
    _rnet = sub.get("require_net_sell_gte_buy", True)
    if isinstance(_rnet, str):
        _net = str(_rnet).strip().lower() not in ("0", "false", "no", "off", "")
    else:
        _net = bool(_rnet) if _rnet is not None else True
    _rcf_raw = sub.get("risk_control_gross_frac", 0.95)
    try:
        _rcf = float(_rcf_raw) if _rcf_raw is not None and str(_rcf_raw).strip() != "" else 0.95
    except (TypeError, ValueError):
        _rcf = 0.95
    if _rcf > 1.0 + 1e-9:
        _rcf = _rcf / 100.0
    _rcf = max(0.0, min(1.0, _rcf))
    _rcb = sub.get("risk_control_block_buys", True)
    if isinstance(_rcb, str):
        _rcb_b = str(_rcb).strip().lower() not in ("0", "false", "no", "off", "")
    else:
        _rcb_b = bool(_rcb) if _rcb is not None else True
    _pdv = sub.get("prioritize_diversification", False)
    if isinstance(_pdv, str):
        pri_div = str(_pdv).strip().lower() not in ("0", "false", "no", "off", "")
    else:
        pri_div = bool(_pdv) if _pdv is not None else False
    try:
        _dr = float(sub.get("diversification_reentry_scale", 0.55))
    except (TypeError, ValueError):
        _dr = 0.55
    _dr = max(0.0, min(1.0, _dr))
    try:
        _dsw = float(sub.get("diversification_sector_weight", 0.35))
    except (TypeError, ValueError):
        _dsw = 0.35
    _dsw = max(0.0, min(1.0, _dsw))
    try:
        _dtw = float(sub.get("diversification_theme_weight", 0.35))
    except (TypeError, ValueError):
        _dtw = 0.35
    _dtw = max(0.0, min(1.0, _dtw))
    try:
        _dref = float(
            sub.get("diversification_reference_exposure_pct", 30.0)
        )
    except (TypeError, ValueError):
        _dref = 30.0
    _dref = max(1.0, min(200.0, _dref))
    _nrr_raw = sub.get("net_reduction_max_buy_to_sell_ratio", 0.5)
    try:
        _nrr = (
            float(_nrr_raw)
            if _nrr_raw is not None and str(_nrr_raw).strip() != ""
            else 0.5
        )
    except (TypeError, ValueError):
        _nrr = 0.5
    _nrr = max(0.0, min(1.0, _nrr))
    _nrel_raw = sub.get("net_reduction_near_cap_relative_to_max", 0.9)
    try:
        _nrel = (
            float(_nrel_raw)
            if _nrel_raw is not None and str(_nrel_raw).strip() != ""
            else 0.9
        )
    except (TypeError, ValueError):
        _nrel = 0.9
    _nrel = max(0.0, min(1.0, _nrel))
    _mgd_raw = sub.get("min_gross_deployment_pct", 0.85)
    try:
        _mgd = (
            float(_mgd_raw)
            if _mgd_raw is not None and str(_mgd_raw).strip() != ""
            else 0.85
        )
    except (TypeError, ValueError):
        _mgd = 0.85
    if _mgd > 1.0 + 1e-9:
        _mgd = _mgd / 100.0
    _mgd = max(0.0, min(1.0, _mgd))
    _dtn_raw = sub.get("deploy_top_n_signals", 4)
    try:
        _dtn = int(_dtn_raw) if _dtn_raw is not None and str(_dtn_raw).strip() != "" else 4
    except (TypeError, ValueError):
        _dtn = 4
    _dtn = max(3, min(5, _dtn))
    _eatn_raw = sub.get("empty_alloc_top_n", 5)
    try:
        _eatn = int(_eatn_raw) if _eatn_raw is not None and str(_eatn_raw).strip() != "" else 5
    except (TypeError, ValueError):
        _eatn = 5
    _eatn = max(1, min(20, _eatn))
    _idle_raw = sub.get("idle_fallback")
    _idle = _idle_raw if isinstance(_idle_raw, dict) else {}
    _idle_enabled_raw = _idle.get("enabled", True)
    if isinstance(_idle_enabled_raw, str):
        _idle_enabled = (
            _idle_enabled_raw.strip().lower() not in ("0", "false", "no", "off", "")
        )
    else:
        _idle_enabled = bool(_idle_enabled_raw) if _idle_enabled_raw is not None else True
    _inac_raw = sub.get("if_no_actions_cycles", 0)
    try:
        _inac = int(_inac_raw) if _inac_raw is not None and str(_inac_raw).strip() != "" else 0
    except (TypeError, ValueError):
        _inac = 0
    _inac = max(0, _inac)
    _fptn_raw = sub.get("fallback_pick_top_n", 0)
    if (_fptn_raw is None or str(_fptn_raw).strip() == "") and "pick_top_n" in _idle:
        _fptn_raw = _idle.get("pick_top_n")
    try:
        _fptn = int(_fptn_raw) if _fptn_raw is not None and str(_fptn_raw).strip() != "" else 0
    except (TypeError, ValueError):
        _fptn = 0
    _fptn = max(0, min(20, _fptn))
    _fsp_raw = sub.get("fallback_size_pct")
    if (_fsp_raw is None or str(_fsp_raw).strip() == "") and "size_pct" in _idle:
        _fsp_raw = _idle.get("size_pct")
    _fsp = parse_equity_fraction_optional(_fsp_raw)
    _idle_max_gross = parse_equity_fraction_optional(_idle.get("max_gross_pct"))
    if _idle_max_gross is None:
        _idle_max_gross = parse_equity_fraction_optional(sub.get("idle_fallback_max_gross_pct"))
    _etf_fallback_enabled_raw = sub.get("etf_fallback_enabled", False)
    if isinstance(_etf_fallback_enabled_raw, str):
        _etf_fallback_enabled = _etf_fallback_enabled_raw.strip().lower() not in (
            "0",
            "false",
            "no",
            "off",
            "",
        )
    else:
        _etf_fallback_enabled = bool(_etf_fallback_enabled_raw)
    _etf_fallback_max_notional_pct = _parse_percent_points_fraction(
        sub.get("etf_fallback_max_notional_pct")
    )
    _etf_only_no_news_raw = sub.get("etf_fallback_only_when_no_news_candidates", True)
    if isinstance(_etf_only_no_news_raw, str):
        _etf_only_no_news = _etf_only_no_news_raw.strip().lower() not in (
            "0",
            "false",
            "no",
            "off",
            "",
        )
    else:
        _etf_only_no_news = (
            bool(_etf_only_no_news_raw) if _etf_only_no_news_raw is not None else True
        )
    _fed_raw = sub.get("fallback_enforce_diversity", False)
    if "enforce_diversity" in _idle:
        _fed_raw = _idle.get("enforce_diversity")
    if isinstance(_fed_raw, str):
        _fed = _fed_raw.strip().lower() not in ("0", "false", "no", "off", "")
    else:
        _fed = bool(_fed_raw)
    _cmpg_raw = sub.get("correlation_max_per_group", 0)
    try:
        _cmpg = int(_cmpg_raw) if _cmpg_raw is not None and str(_cmpg_raw).strip() != "" else 0
    except (TypeError, ValueError):
        _cmpg = 0
    _cmpg = max(0, _cmpg)
    _cgroups_raw = sub.get("correlation_groups")
    _cgroups = _cgroups_raw if isinstance(_cgroups_raw, dict) else {}
    _bfm = sub.get("bullish_force_minimum_deploy", True)
    if isinstance(_bfm, str):
        bfm = str(_bfm).strip().lower() not in ("0", "false", "no", "off", "")
    else:
        bfm = bool(_bfm) if _bfm is not None else True
    _rbf = sub.get("rebalance_fund_from_weakest", False)
    if isinstance(_rbf, str):
        rbf = str(_rbf).strip().lower() not in ("0", "false", "no", "off", "")
    else:
        rbf = bool(_rbf) if _rbf is not None else False
    _rws = sub.get("replace_weakest_with_stronger", True)
    if isinstance(_rws, str):
        rws = str(_rws).strip().lower() not in ("0", "false", "no", "off", "")
    else:
        rws = bool(_rws) if _rws is not None else True
    _son_in = sub.get("sell_only_if_needed", True)
    if isinstance(_son_in, str):
        son_in = str(_son_in).strip().lower() not in ("0", "false", "no", "off", "")
    else:
        son_in = bool(_son_in) if _son_in is not None else True
    _rsr_raw = sub.get("replacement_strength_ratio", 1.0)
    if _rsr_raw is None or str(_rsr_raw).strip() == "":
        _rsr_raw = 1.0
    try:
        rsr = float(_rsr_raw)
    except (TypeError, ValueError):
        rsr = 1.0
    rsr = max(1e-9, rsr)
    _iscm_raw = sub.get("ignore_soft_caps_after_sell_minutes", 0)
    try:
        iscm = (
            float(_iscm_raw)
            if _iscm_raw is not None and str(_iscm_raw).strip() != ""
            else 0.0
        )
    except (TypeError, ValueError):
        iscm = 0.0
    iscm = max(0.0, iscm)
    _rwt = sub.get("rebalance_weakest_trim_fraction", 0.30)
    if _rwt is None or str(_rwt).strip() == "":
        _rwt = 0.30
    try:
        rwt = float(_rwt)
    except (TypeError, ValueError):
        rwt = 0.30
    rwt = max(0.0, min(1.0, rwt))
    _cb_raw = sub.get("concentration_bias")
    _cbe = False
    _ctn = 0
    _cts = 1.0
    _crs = 1.0
    if isinstance(_cb_raw, dict) and _cb_raw:
        _cbe_raw = _cb_raw.get("enabled", False)
        if isinstance(_cbe_raw, str):
            _cbe = _cbe_raw.strip().lower() not in ("0", "false", "no", "off", "")
        else:
            _cbe = bool(_cbe_raw)
        if _cbe:
            try:
                _ctn = int(
                    float(_cb_raw.get("top_n", 0) or 0)
                )
            except (TypeError, ValueError, OverflowError):
                _ctn = 0
            _ctn = max(0, min(20, _ctn))
            try:
                _cts = float(
                    _cb_raw.get("top_tranche_scale", 1.0) or 1.0
                )
            except (TypeError, ValueError):
                _cts = 1.0
            try:
                _crs = float(
                    _cb_raw.get("rest_tranche_scale", 1.0) or 1.0
                )
            except (TypeError, ValueError):
                _crs = 1.0
            _cts = max(0.0, min(5.0, _cts))
            _crs = max(0.0, min(5.0, _crs))
    _sppc_raw = sub.get("single_pass_per_cycle", True)
    if isinstance(_sppc_raw, str):
        single_pass_cycle = (
            str(_sppc_raw).strip().lower() not in ("0", "false", "no", "off", "")
        )
    else:
        single_pass_cycle = bool(_sppc_raw) if _sppc_raw is not None else True
    _msop: float | None = None
    _mson: float | None = None
    _raw_msop = sub.get("max_single_order_notional_pct")
    if _raw_msop is not None and str(_raw_msop).strip() != "":
        try:
            _msop = float(_raw_msop)
        except (TypeError, ValueError):
            _msop = None
    _raw_mson = sub.get("max_single_order_notional")
    if _raw_mson is not None and str(_raw_mson).strip() != "":
        try:
            _mson = float(_raw_mson)
        except (TypeError, ValueError):
            _mson = None
    _refuse_gross = parse_equity_fraction_optional(
        sub.get("refuse_to_allocate_if_gross_above")
    )
    if _refuse_gross is None:
        _refuse_gross = parse_equity_fraction_optional(
            al.get("refuse_to_allocate_if_gross_above")
        )
    _mgipc = parse_equity_fraction_optional(sub.get("max_gross_increase_per_cycle"))
    if _mgipc is None:
        _mgipc = parse_equity_fraction_optional(al.get("max_gross_increase_per_cycle"))
    _mcd = parse_equity_fraction_optional(sub.get("minimum_cash_to_deploy_pct"))
    if _mcd is None:
        _mcd = parse_equity_fraction_optional(al.get("minimum_cash_to_deploy_pct"))
    if _mcd is None:
        _mcd = 0.0
    return {
        "enabled": bool(sub.get("enabled", False)),
        "max_positions": max(1, max_pos),
        "symbol_cap": max(0.0, min(1.0, sym_cap)),
        "symbol_caps": _sym_caps,
        "min_trade_size": max(0.0, min_trade),
        "min_realloc_leg": min_realloc_leg,
        "min_signal_strength": min_signal_strength,
        "rotate_trim_fraction": max(0.0, min(1.0, rot)),
        "soft_cap_mode": bool(sc_b),
        "cap_penalty_multiplier": max(0.0, min(1.0, cpm)),
        "allow_cross_bucket_rebalance": bool(cbr),
        "require_net_sell_gte_buy": _net,
        "risk_control_gross_frac": _rcf,
        "risk_control_block_buys": _rcb_b,
        "prioritize_diversification": pri_div,
        "diversification_reentry_scale": _dr,
        "diversification_sector_weight": _dsw,
        "diversification_theme_weight": _dtw,
        "diversification_reference_exposure_pct": _dref,
        "net_reduction_max_buy_to_sell_ratio": _nrr,
        "net_reduction_near_cap_relative_to_max": _nrel,
        "min_gross_deployment_pct": _mgd,
        "deploy_top_n_signals": _dtn,
        "empty_alloc_top_n": _eatn,
        "if_no_actions_cycles": _inac if _idle_enabled else 0,
        "fallback_pick_top_n": _fptn,
        "fallback_size_pct": _fsp,
        "idle_fallback_enabled": _idle_enabled,
        "idle_fallback_max_gross_pct": _idle_max_gross,
        "idle_fallback_prefer_dynamic_symbols": (
            str(_idle.get("prefer_dynamic_symbols", False)).strip().lower()
            not in ("0", "false", "no", "off", "")
            if isinstance(_idle.get("prefer_dynamic_symbols", False), str)
            else bool(_idle.get("prefer_dynamic_symbols", False))
        ),
        "fallback_enforce_diversity": _fed,
        "correlation_max_per_group": _cmpg,
        "correlation_groups": _cgroups,
        "fallback_on_empty_alloc": bool(sub.get("fallback_on_empty_alloc", True)),
        "etf_fallback_enabled": _etf_fallback_enabled,
        "etf_fallback_max_notional_pct": _etf_fallback_max_notional_pct,
        "etf_fallback_only_when_no_news_candidates": _etf_only_no_news,
        "bullish_force_minimum_deploy": bfm,
        "rebalance_fund_from_weakest": rbf,
        "rebalance_weakest_trim_fraction": rwt,
        "replace_weakest_with_stronger": rws,
        "sell_only_if_needed": son_in,
        "replacement_strength_ratio": rsr,
        "ignore_soft_caps_after_sell_minutes": iscm,
        "concentration_bias_enabled": _cbe and _ctn > 0,
        "concentration_top_n": _ctn if _cbe else 0,
        "concentration_top_tranche_scale": _cts,
        "concentration_rest_tranche_scale": _crs,
        "single_pass_per_cycle": single_pass_cycle,
        "max_single_order_notional_pct": _msop,
        "max_single_order_notional": _mson,
        "refuse_to_allocate_if_gross_above": _refuse_gross,
        "max_gross_increase_per_cycle": _mgipc,
        "minimum_cash_to_deploy_pct": max(0.0, min(1.0, float(_mcd))),
        "allow_no_trade_cycles": _bool_cfg("allow_no_trade_cycles", False),
        "selected_must_execute": _bool_cfg("selected_must_execute", False),
        "force_deploy_when_candidates_exist": _bool_cfg(
            "force_deploy_when_candidates_exist",
            False,
        ),
        "force_minimum_trade_single_candidate": _bool_cfg(
            "force_minimum_trade_single_candidate",
            True,
        ),
    }


def effective_capital_allocator_symbol_cap_soft_hard(
    config: dict[str, Any] | None,
    ca_cfg: Mapping[str, Any] | None,
    *,
    regime_score: int | None = None,
    regime_condition: str | None = None,
    account_equity: float | None = None,
) -> tuple[float, float]:
    """
    Effective (soft, hard) per-name **fractions of equity** for :class:`CapitalAllocator`.

    When ``symbol_caps`` includes both ``soft`` and ``hard``, these define the tranche: below soft,
    full ``min_trade_size`` adds; from soft up to hard, penalty tranche; at/above **hard** no new
    adds. Optional ``bullish_soft`` / ``bullish_hard`` override the pair when the regime leg is
    *bullish* (``bullish`` when score ≥ 4 or ``regime_condition`` is ``bullish``).

    With ``base``/``bullish_regime`` only, both return values are the same single line (per regime).

    Merges with :func:`src.risk_limits.effective_symbol_allocation_cap_pct` when that cap is set
    (stricter / lower value applies to *both* soft and hard, with soft still ≤ hard).
    """
    ca = ca_cfg if isinstance(ca_cfg, Mapping) else {}
    scaps = ca.get("symbol_caps")
    if isinstance(scaps, dict) and scaps and "soft" in scaps and "hard" in scaps:
        s0 = scaps.get("soft")
        h0 = scaps.get("hard")
        if s0 is not None and h0 is not None and str(s0).strip() and str(h0).strip():
            bsd, bhd = scaps.get("bullish_soft"), scaps.get("bullish_hard")
            leg = _regime_leg_for_cash_reserve(regime_condition, regime_score)
            if (
                leg == "bullish"
                and bsd is not None
                and bhd is not None
                and str(bsd).strip() != ""
                and str(bhd).strip() != ""
            ):
                s_raw, h_raw = bsd, bhd
            else:
                s_raw, h_raw = s0, h0
            soft = max(0.0, min(1.0, parse_allocation_fraction(s_raw)))
            hard = max(0.0, min(1.0, parse_allocation_fraction(h_raw)))
            if soft > hard:
                soft, hard = hard, soft
        else:
            soft, hard = _parse_legacy_symbol_cap_pair(ca, scaps, regime_score, regime_condition)
    else:
        soft, hard = _parse_legacy_symbol_cap_pair(ca, scaps, regime_score, regime_condition)
    if isinstance(scaps, dict) and regime_score is not None:
        _rk = "regime_%d" % int(regime_score)
        _rraw = scaps.get(_rk)
        if _rraw is not None and str(_rraw).strip() != "":
            _rv = max(0.0, min(1.0, parse_allocation_fraction(_rraw)))
            if (
                "soft" in scaps
                and "hard" in scaps
                and str(scaps.get("soft", "")).strip() != ""
                and str(scaps.get("hard", "")).strip() != ""
            ):
                hard = min(1.0, _rv)
                soft = min(soft, hard)
            else:
                soft, hard = _rv, _rv
    risk_pp = effective_symbol_allocation_cap_pct(
        config, account_equity=account_equity, regime_score=regime_score
    )
    if risk_pp > 0.0 + 1e-12:
        r = max(0.0, min(1.0, risk_pp / 100.0))
        soft = min(soft, r)
        hard = min(hard, r)
    if soft > hard:
        soft = hard
    return max(0.0, min(1.0, soft)), max(0.0, min(1.0, hard))


def _parse_legacy_symbol_cap_pair(
    ca: Mapping[str, Any],
    scaps: Any,
    regime_score: int | None,
    regime_condition: str | None,
) -> tuple[float, float]:
    """Single-line ``base``/``bullish`` table or ``symbol_cap``; soft == hard."""
    if isinstance(scaps, dict) and scaps and (
        "base" in scaps or "bullish_regime" in scaps
    ):
        b_raw = scaps.get("base", ca.get("symbol_cap", 0.25))
        br_raw = scaps.get("bullish_regime", b_raw)
        base_frac = max(0.0, min(1.0, parse_allocation_fraction(b_raw)))
        bull_frac = max(0.0, min(1.0, parse_allocation_fraction(br_raw)))
        leg = _regime_leg_for_cash_reserve(regime_condition, regime_score)
        c = bull_frac if leg == "bullish" else base_frac
        return c, c
    try:
        c = float(ca.get("symbol_cap", 0.25) or 0.25)
    except (TypeError, ValueError):
        c = 0.25
    c = max(0.0, min(1.0, c))
    return c, c


# Keys that are not tier buckets (symbol -> cap) under ``symbol_caps``.
_SYMBOL_CAPS_NON_TIER_KEYS = frozenset({
    "soft",
    "hard",
    "bullish_soft",
    "bullish_hard",
    "base",
    "bullish_regime",
    "default",
    "default_hard",
    "default_soft",
    "tiers",
})


def _symbol_caps_regime_override_key(k: str) -> bool:
    if not k.startswith("regime_"):
        return False
    tail = k[7:]
    return tail.isdigit()


def _tier_bucket_ordered_names(keys: Iterable[str]) -> list[str]:
    """Stable order: *leaders* → *core* → *defensive*, then remaining names sorted."""
    preferred = ("leaders", "core", "defensive")
    pref_set = set(preferred)
    seen: set[str] = set()
    out: list[str] = []
    kl = [str(k) for k in keys]
    for p in preferred:
        if p in kl:
            out.append(p)
            seen.add(p)
    rest = sorted(x for x in kl if x not in seen)
    out.extend(rest)
    return out


def _iter_symbol_cap_tier_bucket_dicts(
    symbol_caps: Mapping[str, Any],
) -> list[tuple[str, Mapping[str, Any]]]:
    """
    Collect tier buckets: ``tiers.leaders`` / top-level ``leaders`` / etc.

    Bucket order: inner ``tiers`` keys first (ordered), then top-level dict keys
    not in :data:`_SYMBOL_CAPS_NON_TIER_KEYS`, skipping duplicates (first wins for
    symbols — see :func:`collect_symbol_cap_tier_hard_fractions`).
    """
    out: list[tuple[str, Mapping[str, Any]]] = []
    seen_bucket_lower: set[str] = set()
    wrap = symbol_caps.get("tiers")
    if isinstance(wrap, dict):
        for name in _tier_bucket_ordered_names(wrap.keys()):
            blk = wrap.get(name)
            if isinstance(blk, dict) and blk:
                nm = str(name)
                out.append((nm, blk))
                seen_bucket_lower.add(nm.lower())
    for name in _tier_bucket_ordered_names(symbol_caps.keys()):
        if name == "tiers":
            continue
        if name in _SYMBOL_CAPS_NON_TIER_KEYS:
            continue
        if _symbol_caps_regime_override_key(name):
            continue
        blk = symbol_caps.get(name)
        if not isinstance(blk, dict) or not blk:
            continue
        nm = str(name)
        if nm.lower() in seen_bucket_lower:
            continue
        out.append((nm, blk))
        seen_bucket_lower.add(nm.lower())
    return out


def collect_symbol_cap_tier_hard_fractions(symbol_caps: Mapping[str, Any]) -> dict[str, float]:
    """
    Parse tier buckets (``leaders`` / ``core`` / ``defensive`` or nested under ``tiers``).

    Each value is a per-symbol **hard** cap as a fraction of equity (same parsing as
    :func:`src.risk_limits.parse_allocation_fraction`). **First bucket wins** if the
    same ticker appears twice (``leaders`` before ``core``).

    Returns uppercase symbol → hard fraction ``[0, 1]``.
    """
    hard_by_symbol: dict[str, float] = {}
    for _bucket_name, blk in _iter_symbol_cap_tier_bucket_dicts(symbol_caps):
        for sym_raw, raw_cap in blk.items():
            sym_u = str(sym_raw).strip().upper()
            if not sym_u:
                continue
            if sym_u in hard_by_symbol:
                continue
            h = parse_allocation_fraction(raw_cap)
            if h <= 0.0:
                continue
            hard_by_symbol[sym_u] = max(0.0, min(1.0, float(h)))
    return hard_by_symbol


def symbol_caps_define_tier_buckets(symbol_caps: Mapping[str, Any] | None) -> bool:
    """True when ``symbol_caps`` contains at least one tier bucket with a symbol entry."""
    if not isinstance(symbol_caps, dict) or not symbol_caps:
        return False
    return bool(collect_symbol_cap_tier_hard_fractions(symbol_caps))


def effective_capital_allocator_symbol_caps_by_symbol(
    config: dict[str, Any] | None,
    ca_cfg: Mapping[str, Any] | None,
    *,
    regime_score: int | None = None,
    regime_condition: str | None = None,
    account_equity: float | None = None,
) -> dict[str, tuple[float, float]] | None:
    """
    When ``portfolio.capital_allocator.symbol_caps`` uses **tier buckets** (e.g. ``leaders``,
    ``core`` ``defensive``), return per-ticker ``(soft_frac, hard_frac)`` of equity for
    :class:`CapitalAllocator`.

    Each tier value is treated as a **hard** ceiling; soft is scaled from the global
    dual-band ratio :func:`effective_capital_allocator_symbol_cap_soft_hard` when
    soft < hard globally, otherwise soft = hard per symbol.

    Merges each hard with :func:`src.risk_limits.effective_symbol_allocation_cap_pct`
    (stricter / lower wins), same as the non-tiered allocator cap path.
    """
    ca = ca_cfg if isinstance(ca_cfg, Mapping) else {}
    scaps = ca.get("symbol_caps")
    if not isinstance(scaps, dict):
        return None
    tier_hards = collect_symbol_cap_tier_hard_fractions(scaps)
    if not tier_hards:
        return None
    g_soft, g_hard = effective_capital_allocator_symbol_cap_soft_hard(
        config,
        ca_cfg,
        regime_score=regime_score,
        regime_condition=regime_condition,
        account_equity=account_equity,
    )
    ratio = g_soft / g_hard if g_hard > 1e-12 else 1.0
    ratio = max(0.0, min(1.0, ratio))
    out: dict[str, tuple[float, float]] = {}
    for sym_u, h_tier in tier_hards.items():
        sym_risk_pp = effective_symbol_allocation_cap_pct(
            config,
            account_equity=account_equity,
            regime_score=regime_score,
            symbol_upper=sym_u,
        )
        risk_hard = max(0.0, min(1.0, float(sym_risk_pp) / 100.0)) if sym_risk_pp > 0 else 0.0
        h_eff = min(h_tier, risk_hard) if risk_hard > 0.0 else h_tier
        h_eff = max(0.0, min(1.0, h_eff))
        if (g_hard - g_soft) > 1e-9:
            s_eff = max(0.0, min(1.0, h_eff * ratio))
            if s_eff > h_eff + 1e-12:
                s_eff = h_eff
        else:
            s_eff = h_eff
        out[sym_u] = (s_eff, h_eff)
    return out if out else None


def effective_capital_allocator_symbol_cap_frac(
    config: dict[str, Any] | None,
    ca_cfg: Mapping[str, Any] | None,
    *,
    regime_score: int | None = None,
    regime_condition: str | None = None,
    account_equity: float | None = None,
) -> float:
    """
    Per-name **hard** fraction of equity (second value from
    :func:`effective_capital_allocator_symbol_cap_soft_hard`).

    Kept for callers that only need the upper ceiling.
    """
    _s, h = effective_capital_allocator_symbol_cap_soft_hard(
        config,
        ca_cfg,
        regime_score=regime_score,
        regime_condition=regime_condition,
        account_equity=account_equity,
    )
    return h


ActionType = Literal["buy", "sell"]


def allocator_book_sector_theme_pct(
    portfolio: list[Mapping[str, Any]],
    equity: float,
    symbol_sector: Mapping[str, str],
    theme_map: Mapping[str, str],
    default_sector: str,
) -> tuple[dict[str, float], dict[str, float]]:
    """
    Book load by **sector** and **theme** (exposure “theme” key, see :data:`exposure.THEME_MAP`).

    Returns the same 0–100+ **percent of equity** scale as :func:`exposure.compute_exposures`.
    """
    sector_pct: dict[str, float] = {}
    theme_pct: dict[str, float] = {}
    eq = max(0.0, float(equity))
    if eq <= 1e-12:
        return sector_pct, theme_pct
    ds = str(default_sector or "unknown").strip() or "unknown"
    for row in portfolio:
        sym = str(row.get("symbol") or "").strip().upper()
        if not sym:
            continue
        try:
            v = max(0.0, float(row.get("value", 0) or 0))
        except (TypeError, ValueError):
            v = 0.0
        if v <= 0.0:
            continue
        raw = symbol_sector.get(sym)
        sec = str(raw).strip() if raw is not None and str(raw).strip() else ds
        thk = str(theme_map.get(sym, sym)).strip() or sym
        p = (v / eq) * 100.0
        sector_pct[sec] = sector_pct.get(sec, 0.0) + p
        theme_pct[thk] = theme_pct.get(thk, 0.0) + p
    return sector_pct, theme_pct


def parse_defensive_drift_cfg(ca_cfg: Mapping[str, Any] | None) -> dict[str, Any]:
    """Read ``capital_allocator.defensive_drift`` (optional). See :func:`apply_allocator_defensive_drift_scores`."""
    dflt: dict[str, Any] = {
        "enabled": False,
        "regime_min_score": 4,
        "priority_scale": 0.15,
        "symbols": frozenset(),
        "sector_substrings": ("staple", "defensive", "utilities", "utility"),
        "theme_substrings": ("defensive", "staple", "utilities", "utility", "consumer_staples"),
    }
    raw = (ca_cfg or {}).get("defensive_drift")
    if not isinstance(raw, dict):
        return dict(dflt)
    en = raw.get("enabled", False)
    if isinstance(en, str):
        enabled = str(en).strip().lower() not in ("0", "false", "no", "off", "")
    else:
        enabled = bool(en) if en is not None else False
    try:
        rmin = int(float(raw.get("regime_min_score", dflt["regime_min_score"]) or 4))
    except (TypeError, ValueError):
        rmin = 4
    rmin = max(0, min(5, rmin))
    try:
        ps = float(raw.get("priority_scale", dflt["priority_scale"]) or 0.15)
    except (TypeError, ValueError):
        ps = 0.15
    ps = max(0.0, min(1.0, ps))
    syms: set[str] = set()
    raw_syms = raw.get("symbols")
    if isinstance(raw_syms, (list, tuple, set)):
        for x in raw_syms:
            if x is not None and str(x).strip():
                syms.add(str(x).strip().upper())

    def _tok_tuple(key: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
        v = raw.get(key)
        if isinstance(v, (list, tuple)):
            out = tuple(
                str(t).strip().lower()
                for t in v
                if t is not None and str(t).strip()
            )
            return out if out else fallback
        return fallback

    return {
        "enabled": enabled,
        "regime_min_score": rmin,
        "priority_scale": ps,
        "symbols": frozenset(syms),
        "sector_substrings": _tok_tuple("match_sector_substrings", dflt["sector_substrings"]),  # type: ignore[arg-type]
        "theme_substrings": _tok_tuple("match_theme_substrings", dflt["theme_substrings"]),  # type: ignore[arg-type]
    }


def allocator_bullish_regime_for_defensive_drift(
    regime_score: int | None,
    regime_condition: str | None,
    *,
    regime_min_score: int,
) -> bool:
    """
    True when we treat the tape as **risk-on enough** that defensive/staples names should not
    compete with growth/cyclicals for allocator dollars.
    """
    c = str(regime_condition or "").strip().lower()
    if c == "defensive":
        return False
    if c == "bullish":
        return True
    try:
        rsi = int(regime_score) if regime_score is not None else None
    except (TypeError, ValueError):
        rsi = None
    if rsi is not None and rsi >= int(regime_min_score):
        return True
    return False


def allocator_symbol_is_defensive_drift_name(
    sym_upper: str,
    *,
    sector: str,
    theme: str,
    cfg: Mapping[str, Any],
) -> bool:
    su = str(sym_upper or "").strip().upper()
    if su in cfg["symbols"]:
        return True
    s_low = str(sector or "").strip().lower()
    t_low = str(theme or "").strip().lower()
    for tok in cfg["sector_substrings"]:
        if tok and tok in s_low:
            return True
    for tok in cfg["theme_substrings"]:
        if tok and tok in t_low:
            return True
    return False


def apply_allocator_defensive_drift_scores(
    candidates: list[dict[str, Any]],
    *,
    regime_score: int | None,
    regime_condition: str | None,
    symbol_sector: Mapping[str, str],
    theme_map: Mapping[str, str],
    default_sector: str,
    ca_cfg: Mapping[str, Any],
    user_id: str = "default",
) -> list[dict[str, Any]]:
    """
    In **bullish / high-score** regimes, multiply ``score`` on defensive/staples candidates by
    ``defensive_drift.priority_scale`` (default **0.15**) so the allocator rarely leads with
    KO/XLP-style names — reduces "defensive drift" when staples pass gates but should not soak deploy.
    """
    drift = parse_defensive_drift_cfg(ca_cfg)
    if not drift["enabled"] or not candidates:
        return candidates
    if not allocator_bullish_regime_for_defensive_drift(
        regime_score,
        regime_condition,
        regime_min_score=int(drift["regime_min_score"]),
    ):
        return candidates
    scale = float(drift["priority_scale"])
    ds = str(default_sector or "unknown").strip() or "unknown"
    n_scaled = 0
    for row in candidates:
        su = str(row.get("symbol") or "").strip().upper()
        if not su:
            continue
        raw_sec = symbol_sector.get(su)
        sec = str(raw_sec).strip() if raw_sec is not None and str(raw_sec).strip() else ds
        thk = str(theme_map.get(su, su)).strip() or su
        if not allocator_symbol_is_defensive_drift_name(su, sector=sec, theme=thk, cfg=drift):
            continue
        try:
            sc0 = float(row.get("score", 0.0) or 0.0)
        except (TypeError, ValueError):
            sc0 = 0.0
        row["score"] = sc0 * scale
        row["defensive_drift_scaled"] = True
        n_scaled += 1
    if n_scaled:
        log.info(
            "[%s] capital_allocator: defensive_drift — scaled score × %.2f for %d bullish-regime defensive/staples candidate(s)",
            str(user_id),
            scale,
            n_scaled,
        )
    return candidates


def reorder_allocator_candidates_diversification(
    candidates: list[dict[str, Any]],
    portfolio: list[Mapping[str, Any]],
    equity: float,
    ca_cfg: Mapping[str, Any],
    symbol_sector: Mapping[str, str],
    theme_map: Mapping[str, str],
    default_sector: str,
) -> list[dict[str, Any]]:
    """
    Reorder allocator candidates to favor **underweight** sector/theme sleeves and
    **non-overlapping** (low theme book load) names over **re-entry** adds (same symbol already
    in *portfolio*), before the allocator’s default score pass.

    Uses a combined priority (higher = processed first)::

        base_score × reentry_mult × (1 + w_s × sector_uw + w_t × theme_uw)

    where *sector_uw* / *theme_uw* are ``1 − min(1, current_% / ref_% )`` and *reentry_mult* is
    :confval:`diversification_reentry_scale` when the symbol is already in the book.
    """
    if not candidates:
        return []
    held = {str(p.get("symbol", "")).strip().upper() for p in portfolio if str(p.get("symbol", "")).strip()}
    try:
        ref = float(
            ca_cfg.get("diversification_reference_exposure_pct", 30.0) or 30.0
        )
    except (TypeError, ValueError):
        ref = 30.0
    ref = max(1.0, min(200.0, ref))
    w_s = max(0.0, min(1.0, float(ca_cfg.get("diversification_sector_weight", 0.35) or 0.0)))
    w_t = max(0.0, min(1.0, float(ca_cfg.get("diversification_theme_weight", 0.35) or 0.0)))
    re_sc = max(0.0, min(1.0, float(ca_cfg.get("diversification_reentry_scale", 0.55) or 0.0)))
    sec_p, th_p = allocator_book_sector_theme_pct(
        list(portfolio), float(equity), symbol_sector, theme_map, default_sector
    )

    def _uw(cur_pct: float) -> float:
        return max(0.0, 1.0 - min(1.0, float(cur_pct) / ref))

    out = [dict(c) for c in candidates]

    def _key(row: dict[str, Any]) -> tuple[float, str]:
        su = str(row.get("symbol") or "").strip().upper()
        try:
            sc = float(row.get("score", 0.0) or 0.0)
        except (TypeError, ValueError):
            sc = 0.0
        rmul = re_sc if su in held else 1.0
        raw = symbol_sector.get(su)
        sec = str(raw).strip() if raw is not None and str(raw).strip() else default_sector
        thk = str(theme_map.get(su, su)).strip() or su
        s_cur = float(sec_p.get(sec, 0.0) or 0.0)
        t_cur = float(th_p.get(thk, 0.0) or 0.0)
        suw = _uw(s_cur)
        tuw = _uw(t_cur)
        prio = sc * rmul * (1.0 + w_s * suw + w_t * tuw)
        return (prio, su)

    out.sort(key=lambda r: _key(r), reverse=True)
    if log.isEnabledFor(logging.DEBUG):
        _top = [str(c.get("symbol", "")) for c in out[:5]]
        log.debug("reorder_allocator_candidates_diversification first=%s (held=%s)", _top, sorted(held))
    return out


def trim_allocator_actions_for_max_buy_to_sell_ratio(
    actions: list[Mapping[str, Any]],
    *,
    min_realloc_leg: float,
    max_buy_to_sell_ratio: float,
) -> list[dict[str, Any]]:
    """
    Enforce **aggregate** notional: ``sum(buys) <= max_buy_to_sell_ratio × sum(sells)``.

    With ``0.5``, aggregate buys are at most half of aggregate sells. If there are
    no sells, any positive buy notional is trimmed away from the end.

    Trims or drops **buy** lines from the end (same *min_realloc_leg* rules as
    :func:`trim_allocator_actions_for_net_sell_gte_buy`).
    """
    if not actions:
        return []
    try:
        r = float(max_buy_to_sell_ratio)
    except (TypeError, ValueError):
        r = 1.0
    r = max(0.0, min(1.0, r))
    mleg = max(0.0, float(min_realloc_leg or 0.0))
    act: list[dict[str, Any]] = [dict(x) for x in actions]

    def sums() -> tuple[float, float]:
        ts = tb = 0.0
        for a in act:
            side = str(a.get("action", "")).lower()
            try:
                n = max(0.0, float(a.get("notional", 0) or 0))
            except (TypeError, ValueError):
                n = 0.0
            if side == "sell":
                ts += n
            elif side == "buy":
                tb += n
        return ts, tb

    while act:
        ts, tb = sums()
        if ts < 1e-9 and tb < 1e-9:
            return act
        if ts < 1e-9 and tb > 1e-9:
            need = tb
        else:
            cap = r * ts
            if tb <= cap + 1e-9:
                return act
            need = tb - cap
        idx = -1
        for i in range(len(act) - 1, -1, -1):
            if str(act[i].get("action", "")).lower() == "buy":
                idx = i
                break
        if idx < 0:
            log.warning(
                "CapitalAllocator: max_buy_to_sell_ratio needs trim of $%.2f but no buy action left; returning sells only",
                need,
            )
            return act
        n = 0.0
        try:
            n = max(0.0, float(act[idx].get("notional", 0) or 0))
        except (TypeError, ValueError):
            n = 0.0
        if n < 1e-9:
            act.pop(idx)
            continue
        if n <= need + 1e-9:
            act.pop(idx)
            continue
        n_new = n - need
        if mleg > 0 and 0 < n_new < mleg:
            act.pop(idx)
            continue
        if n_new < 1e-9:
            act.pop(idx)
            continue
        act[idx]["notional"] = n_new
        return act
    return act


def gross_book_near_effective_max_for_net_reduction(
    gross_exposure_pct: float | None,
    config: Mapping[str, Any] | None,
    *,
    relative_to_max_frac: float,
    regime_score: int | None = None,
    regime_condition: str | None = None,
    entry_wave_strong_signal_count: int | None = None,
) -> bool:
    """
    **Near cap** (for allocator net reduction): long gross (fraction of equity) is at least
    *relative_to_max_frac* times the **effective** max total exposure fraction from
    :func:`src.adaptive.adaptive_effective_max_total_exposure` (uses ``portfolio.exposure_gates`` base).
    """
    if gross_exposure_pct is None or relative_to_max_frac <= 1e-12:
        return False
    from src.adaptive import adaptive_effective_max_total_exposure
    from src.exposure_gates import parse_portfolio_exposure_gates

    try:
        rel = max(0.0, min(1.0, float(relative_to_max_frac)))
    except (TypeError, ValueError):
        return False
    if rel <= 0.0:
        return False
    eg = parse_portfolio_exposure_gates(config)
    try:
        b = float(eg.get("max_total_exposure_frac", 0.92) or 0.92)
    except (TypeError, ValueError):
        b = 0.92
    meff = float(
        adaptive_effective_max_total_exposure(
            dict(config) if config is not None else None,
            base_max_total_exposure_frac=b,
            regime_score=regime_score,
            regime_condition=regime_condition,
            entry_wave_strong_signal_count=entry_wave_strong_signal_count,
        )
    )
    if meff < 1e-12:
        return False
    g = max(0.0, float(gross_exposure_pct)) / 100.0
    return g + 1e-12 >= meff * rel


def clip_buy_actions_to_gross_headroom_dollars(
    actions: list[dict[str, Any]],
    *,
    gross_headroom_dollars: float,
    min_realloc_leg: float,
) -> list[dict[str, Any]]:
    """
    If cumulative buy notional would exceed *gross_headroom_dollars* (``max_gross*equity −
    current_gross``), lower each buy to ``headroom / n`` so the sum does not pass the cap.
    Drops all buys if that per-line amount is below *min_realloc_leg* or headroom is not positive.
    """
    h = max(0.0, float(gross_headroom_dollars))
    if not actions or not math.isfinite(h):
        return [dict(x) for x in actions]
    if h < 1e-9:
        return [a for a in actions if str(a.get("action", "")).lower() != "buy"]
    mleg = max(0.0, float(min_realloc_leg or 0.0))
    buys: list[dict[str, Any]] = [
        a
        for a in actions
        if str(a.get("action", "")).lower() == "buy"
    ]
    if not buys:
        return [dict(x) for x in actions]
    try:
        tot = sum(
            max(0.0, float(x.get("notional", 0) or 0)) for x in buys
        )
    except (TypeError, ValueError):
        return [dict(x) for x in actions]
    if tot <= h + 1e-6:
        return [dict(x) for x in actions]
    n = float(len(buys))
    per = h / n
    if per < mleg - 1e-9:
        return [a for a in actions if str(a.get("action", "")).lower() != "buy"]
    out: list[dict[str, Any]] = []
    for a in actions:
        s = str(a.get("action", "")).lower()
        if s != "buy":
            out.append(dict(a))
        else:
            b = dict(a)
            b["notional"] = float(per)
            out.append(b)
    return out


def trim_allocator_actions_for_net_sell_gte_buy(
    actions: list[Mapping[str, Any]],
    *,
    min_realloc_leg: float,
) -> list[dict[str, Any]]:
    """
    Enforce **net non-increasing** book: sum(sell notional) ≥ sum(buy notional).

    Trims or drops **buy** lines from the **end** of the plan (preserving interleaved order
    of earlier rows) until the inequality holds. If a partial buy would be positive but
    below *min_realloc_leg* (and ``min_realloc_leg > 0``), the **entire** that buy is removed
    (stronger de-risking to avoid sub-min notional).
    """
    if not actions:
        return []
    mleg = max(0.0, float(min_realloc_leg or 0.0))
    act: list[dict[str, Any]] = [dict(x) for x in actions]

    def sums() -> tuple[float, float]:
        ts = tb = 0.0
        for a in act:
            side = str(a.get("action", "")).lower()
            try:
                n = max(0.0, float(a.get("notional", 0) or 0))
            except (TypeError, ValueError):
                n = 0.0
            if side == "sell":
                ts += n
            elif side == "buy":
                tb += n
        return ts, tb

    while act:
        ts, tb = sums()
        if tb <= ts + 1e-9:
            return act
        need = tb - ts
        idx = -1
        for i in range(len(act) - 1, -1, -1):
            if str(act[i].get("action", "")).lower() == "buy":
                idx = i
                break
        if idx < 0:
            log.warning(
                "CapitalAllocator: net_sell_gte_buy needs trim of $%.2f but no buy action left; returning sells only",
                need,
            )
            return act
        n = 0.0
        try:
            n = max(0.0, float(act[idx].get("notional", 0) or 0))
        except (TypeError, ValueError):
            n = 0.0
        if n < 1e-9:
            act.pop(idx)
            continue
        if n <= need + 1e-9:
            act.pop(idx)
            continue
        n_new = n - need
        if mleg > 0 and 0 < n_new < mleg:
            act.pop(idx)
            continue
        if n_new < 1e-9:
            act.pop(idx)
            continue
        act[idx]["notional"] = n_new
        return act
    return act


def clip_allocator_buy_notionals_to_single_order_caps(
    actions: Sequence[Mapping[str, Any]],
    *,
    account_equity: float,
    max_single_order_notional_pct: float | None = None,
    max_single_order_notional: float | None = None,
) -> list[dict[str, Any]]:
    """
    Cap each **buy** leg by ``min(equity × pct, abs_cap)`` when those knobs are set (either or both).

    Percent may be a fraction (``0.08``) or percent points (``8``). Sells are unchanged.
    """
    pct_raw = max_single_order_notional_pct
    abs_raw = max_single_order_notional
    if pct_raw is None and (abs_raw is None or str(abs_raw).strip() == ""):
        return [dict(x) for x in actions]
    eq = max(0.0, float(account_equity or 0.0))
    ceiling: float | None = None
    if pct_raw is not None and str(pct_raw).strip() != "":
        try:
            p = float(pct_raw)
            if p > 1.0 + 1e-9:
                p = p / 100.0
            p = max(0.0, p)
            if p > 0.0 and eq > 0.0:
                ceiling = eq * p
        except (TypeError, ValueError):
            pass
    if abs_raw is not None and str(abs_raw).strip() != "":
        try:
            a = max(0.0, float(abs_raw))
            if a > 0.0:
                ceiling = a if ceiling is None else min(ceiling, a)
        except (TypeError, ValueError):
            pass
    if ceiling is None or ceiling <= 0.0:
        return [dict(x) for x in actions]
    out: list[dict[str, Any]] = []
    for a in actions:
        row = dict(a)
        if str(row.get("action", "")).lower() != "buy":
            out.append(row)
            continue
        try:
            n = float(row.get("notional", 0) or 0)
        except (TypeError, ValueError):
            out.append(row)
            continue
        if n > ceiling + 1e-9:
            row["notional"] = float(ceiling)
        out.append(row)
    return out


def consolidate_allocator_actions_net_by_symbol(
    actions: Sequence[Mapping[str, Any]],
    *,
    min_abs_net_notional: float = 0.0,
) -> list[dict[str, Any]]:
    """
    Net signed USD notionals per symbol (buy → ``+``, sell → ``−``) and return one leg per symbol.

    Rows with the same *symbol* that would wash to near-zero are dropped; remaining legs use
    ``|net|`` as notional. When *min_abs_net_notional* is positive, symbols whose ``|net|`` is
    strictly below that threshold are omitted (no sub-min residual orders).
    """
    net: dict[str, float] = {}
    order_syms: list[str] = []
    for a in actions:
        sym = str(a.get("symbol") or "").strip().upper()
        if not sym:
            continue
        side = str(a.get("action") or "").strip().lower()
        if side not in ("buy", "sell"):
            continue
        try:
            n = float(a.get("notional", 0) or 0)
        except (TypeError, ValueError):
            continue
        n = max(0.0, n)
        if n <= 0.0:
            continue
        signed = n if side == "buy" else -n
        if sym not in net:
            order_syms.append(sym)
            net[sym] = 0.0
        net[sym] += signed
    floor = max(0.0, float(min_abs_net_notional or 0.0))
    out: list[dict[str, Any]] = []
    for sym in order_syms:
        amt = net.get(sym, 0.0)
        if abs(amt) < 1e-9:
            continue
        if floor > 0.0 and abs(amt) < floor:
            continue
        out.append(
            {
                "action": "buy" if amt > 0.0 else "sell",
                "symbol": sym,
                "notional": abs(amt),
            }
        )
    return out


def _parse_percent_points_fraction(raw: object) -> float | None:
    """Parse config keys named ``*_pct`` where ``1`` means 1%."""
    if raw is None or str(raw).strip() == "":
        return None
    s = str(raw).strip()
    if s.endswith("%"):
        s = s[:-1].strip()
    try:
        value = float(s)
    except (TypeError, ValueError):
        return None
    if value != value or value <= 0.0:
        return None
    return value / 100.0


@dataclass(frozen=True)
class AllocatorAction:
    action: ActionType
    symbol: str
    notional: float

    def as_dict(self) -> dict[str, Any]:
        return {"action": self.action, "symbol": self.symbol, "notional": float(self.notional)}


class CapitalAllocator:
    """
    Sort held names by score (weakest first) and candidates by effective rank (strongest signal
    first; with ``prioritize_diversification``, underweight sector/theme sleeves and non-overlapping
    exposures before re-entry adds), then:

    When a candidate cannot deploy via rotation (score not above weakest held, or sell trim infeasible),
    the loop **continues** to the next candidate so recycled cash and later adds are not all blocked
    by a single ineligible *strongest-first* name.

    * For **held** names: when ``symbol_cap`` equals ``symbol_cap_soft`` (single line), a full
      ``min_trade_size`` add is skipped at/above the line; with ``soft_cap_mode``, the penalty
      tranche is used, with partial headroom to the line when a full tranche would exceed it. When
      **soft** < **hard** (``symbol_cap_soft`` < ``symbol_cap``), below soft: full
      ``min_trade_size`` until soft; if a tranche would cross **soft** from below, the add is
      ``min(min_trade_size, soft*equity − v0)``; in ``[soft, hard)`` uses
      ``min(penalty*min_trade, hard*equity − v0)``; at/above **hard** no new adds.
    * If there is room for a new name (``max_positions``) or the candidate is already held,
      and *cash* ≥ the chosen trade *notional*, append a BUY and reduce *cash*. Any trade
      *notional* must be at least ``min_realloc_leg`` (default 300) or it is skipped.
    * Else if the book is non-empty, pop the weakest holding; if :meth:`replace_weakest_with_stronger` is
      on and :meth:`rotate_capital` is true (incoming book score above weakest × ``replacement_strength_ratio``),
      SELL a trim of the weakest (``rotate_trim_fraction`` × value, capped by ``min_trade_size`` when
      **rebalance** is off; or ``rebalance_weakest_trim_fraction`` × value when
      ``rebalance_fund_from_weakest`` is on), then BUY the same notional on the candidate.
    * If **rebalance** is on and *cash* is still short for a tranche, walk holds **weakest-score first**
      and sell a trim from the first line that qualifies: with ``sell_only_if_needed`` (default), the
      candidate must pass :meth:`rotate_capital` vs that line; otherwise hold (no selling a
      stronger line to fund a weaker buy). Set ``sell_only_if_needed: false`` for legacy weakest-first
      funding regardless of rank.
    * Rotation sell size is raised to at least ``min_realloc_leg`` when the weakest line is large
      enough; if the line is smaller than ``min_realloc_leg``, the full line is sold so the swap can
      still complete.
    * **Top-N concentration bias** (``concentration_bias`` in config): candidates are already ordered
      strongest first; index ``0 .. top_n-1`` use ``min_trade_size * top_tranche_scale``;
      the rest use ``min_trade_size * rest_tranche_scale`` (``allocate_more`` / ``allocate_less``).
    """

    def __init__(
        self,
        max_positions: int = 10,
        symbol_cap: float = 0.25,
        min_trade_size: float = 500.0,
        *,
        symbol_cap_soft: float | None = None,
        min_realloc_leg: float = 300.0,
        rotate_trim_fraction: float = 0.30,
        soft_cap_mode: bool = False,
        cap_penalty_multiplier: float = 0.5,
        rebalance_fund_from_weakest: bool = False,
        rebalance_weakest_trim_fraction: float = 0.30,
        replace_weakest_with_stronger: bool = True,
        sell_only_if_needed: bool = True,
        replacement_strength_ratio: float = 1.0,
        ignore_soft_caps: bool = False,
        concentration_top_n: int = 0,
        concentration_top_tranche_scale: float = 1.0,
        concentration_rest_tranche_scale: float = 1.0,
        symbol_cap_fractions: dict[str, tuple[float, float]] | None = None,
        minimum_cash_to_deploy_frac: float = 0.0,
        broker_mode: str = "live",
        paper_dynamic_min_deploy_experiment_enabled: bool = False,
        paper_dynamic_min_deploy_experiment_use_min_realloc_leg: bool = True,
    ) -> None:
        self.max_positions = int(max_positions)
        self.symbol_cap = float(symbol_cap)
        if symbol_cap_soft is None:
            s_soft = self.symbol_cap
        else:
            s_soft = float(symbol_cap_soft)
        if s_soft > self.symbol_cap + 1e-9:
            s_soft = self.symbol_cap
        self.symbol_cap_soft = max(0.0, min(1.0, s_soft))
        self.min_trade_size = float(min_trade_size)
        self.min_realloc_leg = max(0.0, float(min_realloc_leg))
        self.rotate_trim_fraction = max(0.0, min(1.0, float(rotate_trim_fraction)))
        self.soft_cap_mode = bool(soft_cap_mode)
        self.cap_penalty_multiplier = max(0.0, min(1.0, float(cap_penalty_multiplier)))
        self.rebalance_fund_from_weakest = bool(rebalance_fund_from_weakest)
        try:
            rbf = float(rebalance_weakest_trim_fraction)
        except (TypeError, ValueError):
            rbf = 0.30
        self.rebalance_weakest_trim_fraction = max(0.0, min(1.0, rbf))
        self.replace_weakest_with_stronger = bool(replace_weakest_with_stronger)
        self.sell_only_if_needed = bool(sell_only_if_needed)
        try:
            _rsr = float(replacement_strength_ratio)
        except (TypeError, ValueError):
            _rsr = 1.0
        self.replacement_strength_ratio = max(1e-9, _rsr)
        self.ignore_soft_caps = bool(ignore_soft_caps)
        try:
            _ctn = int(concentration_top_n)
        except (TypeError, ValueError):
            _ctn = 0
        self.concentration_top_n = max(0, min(20, _ctn))
        try:
            _cts = float(concentration_top_tranche_scale)
        except (TypeError, ValueError):
            _cts = 1.0
        try:
            _crs = float(concentration_rest_tranche_scale)
        except (TypeError, ValueError):
            _crs = 1.0
        self.concentration_top_tranche_scale = max(0.0, min(5.0, _cts))
        self.concentration_rest_tranche_scale = max(0.0, min(5.0, _crs))
        try:
            _mcdf = float(minimum_cash_to_deploy_frac)
        except (TypeError, ValueError):
            _mcdf = 0.0
        self.minimum_cash_to_deploy_frac = max(0.0, min(1.0, _mcdf))
        self.broker_mode = str(broker_mode or "live").strip().lower()
        self.paper_dynamic_min_deploy_experiment_enabled = bool(
            paper_dynamic_min_deploy_experiment_enabled
        )
        self.paper_dynamic_min_deploy_experiment_use_min_realloc_leg = bool(
            paper_dynamic_min_deploy_experiment_use_min_realloc_leg
        )
        self.last_skipped_symbols: set[str] = set()
        self.last_skip_reasons: dict[str, str] = {}
        self.last_no_action_details: dict[str, dict[str, Any]] = {}
        self.symbol_cap_fractions: dict[str, tuple[float, float]] | None = None
        if symbol_cap_fractions:
            tmp: dict[str, tuple[float, float]] = {}
            for k, pair in symbol_cap_fractions.items():
                ku = str(k).strip().upper()
                if not ku:
                    continue
                try:
                    sf = float(pair[0])
                    hf = float(pair[1])
                except (TypeError, ValueError, IndexError):
                    continue
                sf = max(0.0, min(1.0, sf))
                hf = max(0.0, min(1.0, hf))
                if sf > hf + 1e-12:
                    sf = hf
                tmp[ku] = (sf, hf)
            self.symbol_cap_fractions = tmp if tmp else None

    def _tranche_min_for_rank(self, rank_0_index: int) -> float:
        """
        Per-candidate tranche: rank ``0`` = best signal. Top ``concentration_top_n`` get
        ``min_trade_size * concentration_top_tranche_scale``; others get ``* rest_tranche_scale``.
        """
        m0 = self.min_trade_size
        n = int(self.concentration_top_n)
        if n <= 0:
            return m0
        if rank_0_index < n:
            return m0 * self.concentration_top_tranche_scale
        return m0 * self.concentration_rest_tranche_scale

    def rotate_capital(
        self,
        *,
        new_signal_score: float,
        weakest_position: Mapping[str, Any],
        replacement_strength_ratio: float | None = None,
    ) -> bool:
        """
        Weakest-here **replacement** gate: True when the new signal on a **book-comparable** scale
        (see :func:`allocator_candidate_book_score`) exceeds the weakest line’s score times
        ``replacement_strength_ratio`` (e.g. ``1.2`` ⇒ require roughly a 20% strength edge). :meth:`allocate` then
        issues a sell trim on the weakest and a buy on the candidate when the book is full or cash
        precludes a simple add. Ratio ``1.0`` recovers strict ``new > weakest``.
        """
        w = max(0.0, float(weakest_position.get("score", 0.0)))
        r = (
            float(replacement_strength_ratio)
            if replacement_strength_ratio is not None
            else float(self.replacement_strength_ratio)
        )
        floor = w * r
        return float(new_signal_score) > floor + 1e-12

    def allocate(
        self,
        *,
        portfolio: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
        equity: float,
        cash: float,
        max_total_gross_dollars: float | None = None,
        current_gross_dollars: float | None = None,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Keyword-only: ``allocate(portfolio=..., candidates=..., equity=..., cash=...)``.

        *portfolio* / *candidates* rows: ``symbol`` (str), ``score`` (float), portfolio rows also
        ``value`` (float USD market value for that symbol).

        If *max_total_gross_dollars* and *current_gross_dollars* are set (book vs equity cap, same
        units as :func:`src.exposure.compute_exposures` gross; typically ``effective_max*equity`` and
        ``(gross_pct/100)*equity``), each **net new** long buy is ``min(requested, headroom)`` with
        ``headroom = max(0, max - simulated_gross)``, updating simulated gross for sells and buys. Net
        rotation swaps (sell then buy equal size) are capped to fit under the same total so the pair
        does not exceed the cap on the add leg.

        Per name, the buy tranche is also ``min(tranche, hard_cap_usd - line_value)`` when the line
        is not already above the hard cap under ``soft_cap_mode`` (penalty tranche may still add while
        over the hard line).

        Returns a list of action dicts ``{"action": "buy"|"sell", "symbol": str, "notional": float}``.
        """
        eq = max(0.0, float(equity))
        self.last_skipped_symbols = set()
        self.last_skip_reasons = {}
        self.last_no_action_details = {}
        diag_ctx = diagnostics if isinstance(diagnostics, Mapping) else {}

        def _diag_float(key: str, default: float = 0.0) -> float:
            try:
                val = float(diag_ctx.get(key, default) or default)
            except (TypeError, ValueError, OverflowError):
                return default
            return val if math.isfinite(val) else default

        def _diag_str(key: str, default: str = "unknown") -> str:
            val = diag_ctx.get(key, default)
            text = str(val or default).strip()
            return text or default

        def _skip(symbol: str, reason: str, *, detail: str | None = None) -> None:
            sym_u = str(symbol or "").strip().upper()
            if sym_u:
                self.last_skipped_symbols.add(sym_u)
                self.last_skip_reasons[sym_u] = reason if detail is None else f"{reason}: {detail}"
            _print_allocator_skip(symbol, reason, detail=detail)

        def _caps_for(sym_u: str) -> tuple[float, float, bool]:
            if self.symbol_cap_fractions and sym_u in self.symbol_cap_fractions:
                sf, hf = self.symbol_cap_fractions[sym_u]
                cs = float(sf) * eq
                ch = float(hf) * eq
                dual = (hf - sf) > 1e-9
            else:
                cs = self.symbol_cap_soft * eq
                ch = self.symbol_cap * eq
                dual = (self.symbol_cap - self.symbol_cap_soft) > 1e-9
            if self.ignore_soft_caps and dual:
                # After a recent equity sell: **hard** line only (no soft→hard penalty band).
                dual = False
            return cs, ch, dual

        cash_run = float(cash)
        book = copy.deepcopy(portfolio)
        actions: list[dict[str, Any]] = []
        use_gross = max_total_gross_dollars is not None and current_gross_dollars is not None
        max_g: float | None
        gross_run: float
        if use_gross:
            try:
                _mgf = float(max_total_gross_dollars)  # type: ignore[arg-type]
                _cgf = float(current_gross_dollars)  # type: ignore[arg-type]
            except (TypeError, ValueError, OverflowError):
                use_gross = False
            else:
                if not (math.isfinite(_mgf) and math.isfinite(_cgf)):
                    use_gross = False
                else:
                    max_g = max(0.0, _mgf)
                    gross_run = max(0.0, _cgf)
        if not use_gross:
            max_g, gross_run = None, 0.0
        min_deploy_dollars = eq * self.minimum_cash_to_deploy_frac

        def _gross_headroom() -> float:
            if max_g is None:
                return float("inf")
            return max(0.0, max_g - gross_run)

        def _gross_sell(amt: float) -> None:
            nonlocal gross_run
            if max_g is None:
                return
            gross_run = max(0.0, gross_run - max(0.0, float(amt)))

        def _gross_buy(amt: float) -> None:
            nonlocal gross_run
            if max_g is None:
                return
            gross_run = max(0.0, gross_run + max(0.0, float(amt)))

        def _gross_cap_add_buy(amt: float) -> float:
            a = max(0.0, float(amt))
            if not math.isfinite(a):
                return 0.0
            if max_g is None:
                return a
            return min(a, _gross_headroom())

        def _sym(row: dict[str, Any]) -> str:
            return str(row.get("symbol") or "").strip().upper()

        def _find(sym_u: str) -> dict[str, Any] | None:
            for p in book:
                if _sym(p) == sym_u:
                    return p
            return None

        def _candidate_replacement_ratio(candidate: Mapping[str, Any]) -> tuple[float, bool]:
            if not bool(candidate.get("high_conviction_rotation_relaxed", False)):
                return float(self.replacement_strength_ratio), False
            raw = candidate.get("replacement_strength_ratio_override")
            try:
                ratio = float(raw)
            except (TypeError, ValueError, OverflowError):
                return float(self.replacement_strength_ratio), False
            if not math.isfinite(ratio) or ratio <= 0.0:
                return float(self.replacement_strength_ratio), False
            return max(1e-9, ratio), True

        def _candidate_float(candidate: Mapping[str, Any], key: str) -> float:
            try:
                val = float(candidate.get(key, 0.0) or 0.0)
            except (TypeError, ValueError, OverflowError):
                return 0.0
            return val if math.isfinite(val) else 0.0

        def _candidate_requested_notional(candidate: Mapping[str, Any]) -> float:
            requested = 0.0
            for key in (
                "candidate_notional_requested",
                "requested_notional",
                "candidate_notional_cap",
                "max_notional",
                "notional",
            ):
                requested = max(requested, _candidate_float(candidate, key))
            return requested

        def _candidate_notional_cap(candidate: Mapping[str, Any]) -> float:
            cap = 0.0
            for key in ("candidate_notional_cap", "max_notional", "notional"):
                cap = max(cap, _candidate_float(candidate, key))
            return cap

        def _candidate_is_dynamic(candidate: Mapping[str, Any]) -> bool:
            if bool(candidate.get("dynamic_candidate")) or bool(candidate.get("is_dynamic")):
                return True
            bucket = str(candidate.get("allocation_bucket") or "").strip().lower()
            if bucket == "dynamic":
                return True
            route = str(candidate.get("route") or candidate.get("source") or "").strip().lower()
            return route.startswith("dynamic") or "premarket" in route

        def _candidate_has_catalyst_backing(candidate: Mapping[str, Any]) -> bool:
            for key in ("catalyst_score", "event_score", "news_score"):
                if _candidate_float(candidate, key) > 0.0:
                    return True
            for key in ("catalyst_type", "headline"):
                if str(candidate.get(key) or "").strip():
                    return True
            return False

        def _dynamic_min_deploy_experiment_active(candidate: Mapping[str, Any]) -> bool:
            return (
                self.paper_dynamic_min_deploy_experiment_enabled
                and self.paper_dynamic_min_deploy_experiment_use_min_realloc_leg
                and self.broker_mode == "paper"
                and _candidate_is_dynamic(candidate)
            )

        def _log_size_trace(
            *,
            candidate: Mapping[str, Any],
            symbol: str,
            candidate_rank: int,
            score: float,
            raw_target_notional: float,
            target_pct: float,
            available_cash: float,
            gross_headroom: float,
            after_sleeve_cap: float,
            after_sector_cap: float,
            after_symbol_cap: float,
            after_position_cap: float,
            after_gross_headroom: float,
            final_trade_size: float,
            min_deploy: float,
            min_realloc_leg: float,
            symbol_cap_remaining: float,
            position_cap_remaining: float,
            per_trade_cap: float,
            max_trade_size: float,
            skipped_by_min_deploy: bool,
            skip_reason: str,
        ) -> None:
            route = str(candidate.get("route") or candidate.get("source") or "n/a")
            source = str(candidate.get("source") or "n/a")
            dynamic_candidate = _candidate_is_dynamic(candidate)
            strength = _candidate_float(candidate, "strength_eff")
            log.info(
                "ALLOCATOR_SIZE_TRACE symbol=%s route=%s source=%s dynamic_candidate=%s "
                "candidate_rank=%d score=%.6f strength=%.6f account_equity=%.2f "
                "available_cash=%.2f gross_headroom=%.2f raw_target_notional=%.2f "
                "target_pct=%.6f dynamic_sleeve_cap=%.2f core_sleeve_cap=%.2f "
                "sector_cap_remaining=%.2f symbol_cap_remaining=%.2f "
                "position_cap_remaining=%.2f per_trade_cap=%.2f max_trade_size=%.2f "
                "after_sleeve_cap=%.2f after_sector_cap=%.2f after_symbol_cap=%.2f "
                "after_position_cap=%.2f after_gross_headroom=%.2f final_trade_size=%.2f "
                "minimum_cash_to_deploy=%.2f min_realloc_leg=%.2f "
                "skipped_by_min_deploy=%s skip_reason=%s",
                str(symbol or "").strip().upper(),
                route,
                source,
                str(bool(dynamic_candidate)).lower(),
                int(candidate_rank),
                float(score),
                float(strength),
                float(eq),
                float(available_cash),
                float(gross_headroom),
                float(raw_target_notional),
                float(target_pct),
                _diag_float("dynamic_sleeve_cap"),
                _diag_float("core_sleeve_cap"),
                _candidate_float(candidate, "sector_cap_remaining"),
                float(symbol_cap_remaining),
                float(position_cap_remaining),
                float(per_trade_cap),
                float(max_trade_size),
                float(after_sleeve_cap),
                float(after_sector_cap),
                float(after_symbol_cap),
                float(after_position_cap),
                float(after_gross_headroom),
                float(final_trade_size),
                float(min_deploy),
                float(min_realloc_leg),
                str(bool(skipped_by_min_deploy)).lower(),
                str(skip_reason or "none"),
            )

        def _candidate_diag(
            *,
            candidate: Mapping[str, Any],
            symbol: str,
            reason: str,
            detail: str | None = None,
            requested_notional: float = 0.0,
            candidate_notional: float = 0.0,
            cap_soft_d: float = 0.0,
            cap_hard_d: float = 0.0,
            existing: Mapping[str, Any] | None = None,
            position_already_held: bool = False,
            weakest_symbol: str = "",
            weakest_score: float | None = None,
            weakest_value: float | None = None,
            tranche_min: float = 0.0,
            candidate_requested_notional: float = 0.0,
            candidate_notional_cap: float = 0.0,
            base_requested_notional: float = 0.0,
            limiting_cap: str = "",
        ) -> None:
            sym_u = str(symbol or "").strip().upper()
            if not sym_u:
                return
            if tranche_min <= 0.0:
                tranche_min = float(requested_notional)
            if candidate_requested_notional <= 0.0:
                candidate_requested_notional = _candidate_requested_notional(candidate)
            if candidate_notional_cap <= 0.0:
                candidate_notional_cap = _candidate_notional_cap(candidate)
            if base_requested_notional <= 0.0:
                base_requested_notional = float(requested_notional)
            current_line_value = 0.0
            if isinstance(existing, Mapping):
                try:
                    current_line_value = max(0.0, float(existing.get("value", 0.0) or 0.0))
                except (TypeError, ValueError, OverflowError):
                    current_line_value = 0.0
            max_single_notional = max(0.0, float(cap_hard_d) - current_line_value)
            detail_text = str(detail or "")
            payload = {
                "reason": str(reason),
                "detail": detail_text,
                "target_allocation": float(cap_hard_d),
                "available_cash": float(cash_run),
                "cash_reserve": _diag_float("cash_reserve"),
                "current_dynamic_sleeve_usage": _diag_float("current_dynamic_sleeve_usage"),
                "dynamic_sleeve_cap": _diag_float("dynamic_sleeve_cap"),
                "candidate_notional_requested": float(requested_notional),
                "candidate_notional": float(candidate_notional),
                "tranche_min": float(tranche_min),
                "candidate_requested_notional": float(candidate_requested_notional),
                "candidate_notional_cap": float(candidate_notional_cap),
                "base_requested_notional": float(base_requested_notional),
                "limiting_cap": str(limiting_cap or ""),
                "min_order_notional": float(self.min_realloc_leg),
                "max_single_dynamic_notional": float(max_single_notional),
                "position_already_held": bool(position_already_held),
                "rebalance_deploy_mode": _diag_str("allocator_mode"),
                "rebalance_fund_from_weakest": bool(self.rebalance_fund_from_weakest),
                "max_positions": int(self.max_positions),
                "weakest_symbol": str(weakest_symbol or ""),
                "weakest_score": weakest_score,
                "weakest_value": weakest_value,
                "score": candidate.get("score"),
                "source": candidate.get("source"),
            }
            self.last_no_action_details[sym_u] = payload
            log.info(
                "ALLOCATOR_NO_ACTION_DETAIL symbol=%s reason=%s detail=%s "
                "target_allocation=%.2f available_cash=%.2f cash_reserve=%.2f "
                "current_dynamic_sleeve_usage=%.2f dynamic_sleeve_cap=%.2f "
                "candidate_notional_requested=%.2f candidate_notional=%.2f "
                "tranche_min=%.2f candidate_requested_notional=%.2f "
                "candidate_notional_cap=%.2f base_requested_notional=%.2f "
                "final_trade_size=%.2f limiting_cap=%s "
                "min_order_notional=%.2f max_single_dynamic_notional=%.2f "
                "position_already_held=%s rebalance_deploy_mode=%s "
                "rebalance_fund_from_weakest=%s max_positions=%d weakest_symbol=%s "
                "weakest_score=%s weakest_value=%s source=%s score=%s",
                sym_u,
                str(reason),
                detail_text or "n/a",
                float(cap_hard_d),
                float(cash_run),
                _diag_float("cash_reserve"),
                _diag_float("current_dynamic_sleeve_usage"),
                _diag_float("dynamic_sleeve_cap"),
                float(requested_notional),
                float(candidate_notional),
                float(tranche_min),
                float(candidate_requested_notional),
                float(candidate_notional_cap),
                float(base_requested_notional),
                float(candidate_notional),
                str(limiting_cap or "none"),
                float(self.min_realloc_leg),
                float(max_single_notional),
                bool(position_already_held),
                _diag_str("allocator_mode"),
                bool(self.rebalance_fund_from_weakest),
                int(self.max_positions),
                str(weakest_symbol or "n/a"),
                "n/a" if weakest_score is None else "%.6f" % float(weakest_score),
                "n/a" if weakest_value is None else "%.2f" % float(weakest_value),
                str(candidate.get("source") or "n/a"),
                str(candidate.get("score")),
            )

        portfolio_sorted = sorted(book, key=lambda x: float(x.get("score", 0.0)))
        candidates_sorted = sorted(
            candidates,
            key=lambda x: float(x.get("score", 0.0)),
            reverse=True,
        )

        for rank_idx, candidate in enumerate(candidates_sorted):
            portfolio_sorted = sorted(book, key=lambda x: float(x.get("score", 0.0)))
            distinct_positions = len({_sym(p) for p in book})
            m = self._tranche_min_for_rank(rank_idx)

            symbol = _sym(candidate)
            if not symbol:
                log.info(
                    "ALLOCATOR_NO_ACTION_DETAIL symbol=? reason=missing_symbol detail=candidate_symbol_empty"
                )
                continue
            cap_soft_d, cap_hard_d, _dual = _caps_for(symbol)
            score = float(candidate.get("score", 0.0))
            book_sc = allocator_candidate_book_score(candidate, rank_score=score)
            replacement_ratio, high_conviction_ratio_active = _candidate_replacement_ratio(candidate)

            existing = _find(symbol)
            is_new = existing is None
            candidate_requested_notional = _candidate_requested_notional(candidate)
            candidate_notional_cap = _candidate_notional_cap(candidate)
            base_requested_notional = float(m)
            used_candidate_requested_base = False
            if (
                is_new
                and _candidate_is_dynamic(candidate)
                and _candidate_has_catalyst_backing(candidate)
                and candidate_requested_notional > 0.0
            ):
                base_requested_notional = max(float(m), candidate_requested_notional)
                used_candidate_requested_base = True
            requested_notional = float(base_requested_notional)
            limiting_cap = "none"
            soft_cap_penalty_add = False
            if is_new:
                trade_size = base_requested_notional
            else:
                v0 = float(existing.get("value", 0.0) if existing is not None else 0.0)
                p = self.cap_penalty_multiplier
                s = cap_soft_d
                h = cap_hard_d
                if not _dual:
                    if v0 >= h - 1e-6:
                        if not self.soft_cap_mode:
                            _detail = "already held value $%.0f >= hard cap $%.0f" % (v0, h)
                            _candidate_diag(
                                candidate=candidate,
                                symbol=symbol,
                                reason="cap reached",
                                detail=_detail,
                                requested_notional=requested_notional,
                                candidate_notional=0.0,
                                cap_soft_d=cap_soft_d,
                                cap_hard_d=cap_hard_d,
                                existing=existing,
                                position_already_held=True,
                            )
                            _skip(
                                symbol,
                                "cap reached",
                                detail=_detail,
                            )
                            continue
                        trade_size = m * p
                        soft_cap_penalty_add = True
                    elif v0 + m > h + 1e-6 and self.soft_cap_mode:
                        head = h - v0
                        sh = m * p
                        trade_size = min(sh, head)
                        soft_cap_penalty_add = True
                    else:
                        trade_size = m
                else:
                    if v0 >= h - 1e-6:
                        _detail = "already held value $%.0f >= hard cap $%.0f" % (v0, h)
                        _candidate_diag(
                            candidate=candidate,
                            symbol=symbol,
                            reason="cap reached",
                            detail=_detail,
                            requested_notional=requested_notional,
                            candidate_notional=0.0,
                            cap_soft_d=cap_soft_d,
                            cap_hard_d=cap_hard_d,
                            existing=existing,
                            position_already_held=True,
                        )
                        _skip(
                            symbol,
                            "cap reached",
                            detail=_detail,
                        )
                        continue
                    if v0 >= s - 1e-6:
                        trade_size = min(m * p, h - v0)
                        soft_cap_penalty_add = True
                    elif v0 + m <= s + 1e-6:
                        trade_size = m
                    else:
                        trade_size = min(m, s - v0)
                if trade_size < 1e-6:
                    _candidate_diag(
                        candidate=candidate,
                        symbol=symbol,
                        reason="size = 0",
                        detail="held add tranche clipped to zero",
                        requested_notional=requested_notional,
                        candidate_notional=trade_size,
                        cap_soft_d=cap_soft_d,
                        cap_hard_d=cap_hard_d,
                        existing=existing,
                        position_already_held=True,
                    )
                    _skip(symbol, "size = 0", detail="held add tranche clipped to zero")
                    continue
            raw_target_notional = float(base_requested_notional)
            target_pct = (raw_target_notional / eq) if eq > 0.0 else 0.0
            # Per-symbol: allocation = min(requested, max_allowed − current_exposure) at the hard line.
            # When ``soft_cap_mode`` already has the line *above* hard, the penalty tranche (still >0) is
            # intentional — do not clamp to ``hard - v0`` (negative headroom would zero the add).
            _line_value = float(existing.get("value", 0.0) if existing is not None else 0.0)
            after_sleeve_cap = float(trade_size)
            if not (self.soft_cap_mode and _line_value > cap_hard_d - 1e-9):
                line_headroom = max(0.0, cap_hard_d - _line_value)
                if line_headroom < trade_size:
                    limiting_cap = "line_hard_cap"
                trade_size = min(trade_size, line_headroom)
            else:
                line_headroom = max(0.0, cap_hard_d - _line_value)
            after_symbol_cap = float(trade_size)
            after_position_cap = float(trade_size)
            if candidate_notional_cap > 0.0:
                if candidate_notional_cap < trade_size:
                    limiting_cap = "candidate_notional_cap"
                trade_size = min(trade_size, candidate_notional_cap)
            after_sleeve_cap = min(after_sleeve_cap, float(candidate_notional_cap)) if candidate_notional_cap > 0.0 else after_sleeve_cap
            after_sector_cap = after_sleeve_cap
            after_symbol_cap = min(after_symbol_cap, after_sleeve_cap)
            after_position_cap = min(after_position_cap, after_symbol_cap)
            if trade_size < 1e-6:
                _detail = "line value $%.0f >= hard cap $%.0f" % (_line_value, cap_hard_d)
                _candidate_diag(
                    candidate=candidate,
                    symbol=symbol,
                    reason="cap reached",
                    detail=_detail,
                    requested_notional=requested_notional,
                    candidate_notional=trade_size,
                    cap_soft_d=cap_soft_d,
                    cap_hard_d=cap_hard_d,
                    existing=existing,
                    position_already_held=not is_new,
                    tranche_min=m,
                    candidate_requested_notional=candidate_requested_notional,
                    candidate_notional_cap=candidate_notional_cap,
                    base_requested_notional=base_requested_notional,
                    limiting_cap=limiting_cap,
                )
                _skip(
                    symbol,
                    "cap reached",
                    detail=_detail,
                )
                continue

            EPS = 0.01
            trade_size = round(float(trade_size), 2)
            min_leg = round(float(self.min_realloc_leg), 2)
            if 0 < trade_size + EPS < self.min_realloc_leg:
                if soft_cap_penalty_add or used_candidate_requested_base:
                    if soft_cap_penalty_add:
                        _detail = "soft-cap penalty tranche $%.2f < min_realloc_leg %.0f" % (
                            trade_size,
                            self.min_realloc_leg,
                        )
                    elif used_candidate_requested_base:
                        _detail = "candidate requested notional $%.2f < min_realloc_leg %.0f" % (
                            trade_size,
                            self.min_realloc_leg,
                        )
                    _candidate_diag(
                        candidate=candidate,
                        symbol=symbol,
                        reason="size = 0",
                        detail=_detail,
                        requested_notional=requested_notional,
                        candidate_notional=trade_size,
                        cap_soft_d=cap_soft_d,
                        cap_hard_d=cap_hard_d,
                        existing=existing,
                        position_already_held=not is_new,
                        tranche_min=m,
                        candidate_requested_notional=candidate_requested_notional,
                        candidate_notional_cap=candidate_notional_cap,
                        base_requested_notional=base_requested_notional,
                        limiting_cap=limiting_cap,
                    )
                    _skip(
                        symbol,
                        "size = 0",
                        detail=_detail,
                    )
                    continue
                trade_size = min_leg
            if (
                self.rebalance_fund_from_weakest
                and book
                and cash_run < trade_size - 1e-9
            ):
                wlist = sorted(
                    book,
                    key=lambda p: (float(p.get("score", 0.0)), _sym(p)),
                )
                _wf = self.rebalance_weakest_trim_fraction
                for wrow in wlist:
                    wu = _sym(wrow)
                    wv = max(0.0, float(wrow.get("value", 0.0)))
                    if not wu or wv < 1e-9 or _wf <= 0.0:
                        _candidate_diag(
                            candidate=candidate,
                            symbol=symbol,
                            reason="rebalance_no_funding_line",
                            detail="weakest funding line unavailable",
                            requested_notional=requested_notional,
                            candidate_notional=trade_size,
                            cap_soft_d=cap_soft_d,
                            cap_hard_d=cap_hard_d,
                            existing=existing,
                            position_already_held=not is_new,
                            weakest_symbol=wu,
                            weakest_value=wv,
                        )
                        break
                    if wu == symbol and len(wlist) > 1:
                        _candidate_diag(
                            candidate=candidate,
                            symbol=symbol,
                            reason="rebalance_skip_self_funding_line",
                            detail="candidate is funding line; trying next weakest",
                            requested_notional=requested_notional,
                            candidate_notional=trade_size,
                            cap_soft_d=cap_soft_d,
                            cap_hard_d=cap_hard_d,
                            existing=existing,
                            position_already_held=not is_new,
                            weakest_symbol=wu,
                            weakest_score=float(wrow.get("score", 0.0) or 0.0),
                            weakest_value=wv,
                        )
                        continue
                    if wu == symbol and len(wlist) <= 1:
                        _candidate_diag(
                            candidate=candidate,
                            symbol=symbol,
                            reason="rebalance_only_self_funding_line",
                            detail="candidate is only funding line",
                            requested_notional=requested_notional,
                            candidate_notional=trade_size,
                            cap_soft_d=cap_soft_d,
                            cap_hard_d=cap_hard_d,
                            existing=existing,
                            position_already_held=not is_new,
                            weakest_symbol=wu,
                            weakest_score=float(wrow.get("score", 0.0) or 0.0),
                            weakest_value=wv,
                        )
                        break
                    if self.sell_only_if_needed and not self.rotate_capital(
                        new_signal_score=book_sc,
                        weakest_position=wrow,
                        replacement_strength_ratio=replacement_ratio,
                    ):
                        if high_conviction_ratio_active:
                            log.info(
                                "HIGH_CONVICTION_ROTATION_REJECTED symbol=%s reason=rebalance_candidate_not_stronger old_ratio=%.6f new_ratio=%.6f candidate_score=%.6f weakest_symbol=%s weakest_score=%.6f",
                                symbol,
                                float(self.replacement_strength_ratio),
                                float(replacement_ratio),
                                float(book_sc),
                                wu,
                                float(wrow.get("score", 0.0) or 0.0),
                            )
                        _candidate_diag(
                            candidate=candidate,
                            symbol=symbol,
                            reason="rebalance_candidate_not_stronger",
                            detail="candidate score does not outrank funding line",
                            requested_notional=requested_notional,
                            candidate_notional=trade_size,
                            cap_soft_d=cap_soft_d,
                            cap_hard_d=cap_hard_d,
                            existing=existing,
                            position_already_held=not is_new,
                            weakest_symbol=wu,
                            weakest_score=float(wrow.get("score", 0.0) or 0.0),
                            weakest_value=wv,
                        )
                        continue
                    tr = min(wv * _wf, wv * (1.0 - 1e-9))
                    if tr < self.min_realloc_leg or tr >= wv - 1e-9:
                        _candidate_diag(
                            candidate=candidate,
                            symbol=symbol,
                            reason="rebalance_trim_invalid",
                            detail="trim $%.2f outside valid range for funding line $%.2f" % (tr, wv),
                            requested_notional=requested_notional,
                            candidate_notional=trade_size,
                            cap_soft_d=cap_soft_d,
                            cap_hard_d=cap_hard_d,
                            existing=existing,
                            position_already_held=not is_new,
                            weakest_symbol=wu,
                            weakest_score=float(wrow.get("score", 0.0) or 0.0),
                            weakest_value=wv,
                        )
                        continue
                    actions.append(self._sell(wu, tr))
                    _gross_sell(tr)
                    cash_run += tr
                    w_r = _find(wu)
                    if w_r is not None:
                        w_r["value"] = max(0.0, float(w_r.get("value", 0.0)) - tr)
                        if w_r["value"] < 1e-6:
                            book[:] = [p for p in book if _sym(p) != wu]
                    distinct_positions = len({_sym(p) for p in book})
                    break
            # Total gross (equity) headroom: available_capacity = max_gross − current; clip adds.
            before_gross_cap = trade_size
            gross_headroom_before_buy = _gross_headroom()
            trade_size = _gross_cap_add_buy(trade_size)
            if trade_size < before_gross_cap:
                limiting_cap = "gross_headroom"
            after_gross_headroom = float(trade_size)
            effective_min_deploy_dollars = float(min_deploy_dollars)
            experiment_active = _dynamic_min_deploy_experiment_active(candidate)
            if experiment_active:
                effective_min_deploy_dollars = float(self.min_realloc_leg)
                would_have_skipped_default = bool(
                    min_deploy_dollars > 0.0
                    and trade_size + MINIMUM_CASH_TO_DEPLOY_SKIP_BUFFER_USD < min_deploy_dollars
                )
                log.info(
                    "DYNAMIC_MIN_DEPLOY_EXPERIMENT symbol=%s mode=paper "
                    "dynamic_candidate=true original_floor=%.2f experiment_floor=%.2f "
                    "trade_size=%.2f would_have_skipped_default=%s",
                    symbol,
                    float(min_deploy_dollars),
                    float(effective_min_deploy_dollars),
                    float(trade_size),
                    str(bool(would_have_skipped_default)).lower(),
                )
            skipped_by_min_deploy = bool(
                effective_min_deploy_dollars > 0.0
                and trade_size + MINIMUM_CASH_TO_DEPLOY_SKIP_BUFFER_USD < effective_min_deploy_dollars
            )
            _log_size_trace(
                candidate=candidate,
                symbol=symbol,
                candidate_rank=rank_idx,
                score=score,
                raw_target_notional=raw_target_notional,
                target_pct=target_pct,
                available_cash=cash_run,
                gross_headroom=gross_headroom_before_buy,
                after_sleeve_cap=after_sleeve_cap,
                after_sector_cap=after_sector_cap,
                after_symbol_cap=after_symbol_cap,
                after_position_cap=after_position_cap,
                after_gross_headroom=after_gross_headroom,
                final_trade_size=trade_size,
                min_deploy=effective_min_deploy_dollars,
                min_realloc_leg=self.min_realloc_leg,
                symbol_cap_remaining=line_headroom,
                position_cap_remaining=line_headroom,
                per_trade_cap=candidate_notional_cap if candidate_notional_cap > 0.0 else line_headroom,
                max_trade_size=max(0.0, min(line_headroom, candidate_notional_cap if candidate_notional_cap > 0.0 else line_headroom)),
                skipped_by_min_deploy=skipped_by_min_deploy,
                skip_reason="minimum_cash_to_deploy" if skipped_by_min_deploy else "none",
            )
            if (
                effective_min_deploy_dollars > 0.0
                and trade_size + MINIMUM_CASH_TO_DEPLOY_SKIP_BUFFER_USD < effective_min_deploy_dollars
            ):
                _detail = "trade_size $%.2f + buffer $%.0f < minimum_cash_to_deploy %.0f" % (
                    trade_size,
                    MINIMUM_CASH_TO_DEPLOY_SKIP_BUFFER_USD,
                    effective_min_deploy_dollars,
                )
                _candidate_diag(
                    candidate=candidate,
                    symbol=symbol,
                    reason="size = 0",
                    detail=_detail,
                    requested_notional=requested_notional,
                    candidate_notional=trade_size,
                    cap_soft_d=cap_soft_d,
                    cap_hard_d=cap_hard_d,
                    existing=existing,
                    position_already_held=not is_new,
                    tranche_min=m,
                    candidate_requested_notional=candidate_requested_notional,
                    candidate_notional_cap=candidate_notional_cap,
                    base_requested_notional=base_requested_notional,
                    limiting_cap=limiting_cap,
                )
                _skip(
                    symbol,
                    "size = 0",
                    detail=_detail,
                )
                continue
            trade_size = round(float(trade_size), 2)
            if trade_size +EPS < self.min_realloc_leg:
                trade_size = self.min_realloc_leg
                _detail = "gross headroom leaves trade_size $%.2f < min_realloc_leg %.0f" % (
                    trade_size,
                    self.min_realloc_leg,
                )
                _candidate_diag(
                    candidate=candidate,
                    symbol=symbol,
                    reason="cap reached",
                    detail=_detail,
                    requested_notional=requested_notional,
                    candidate_notional=trade_size,
                    cap_soft_d=cap_soft_d,
                    cap_hard_d=cap_hard_d,
                    existing=existing,
                    position_already_held=not is_new,
                    tranche_min=m,
                    candidate_requested_notional=candidate_requested_notional,
                    candidate_notional_cap=candidate_notional_cap,
                    base_requested_notional=base_requested_notional,
                    limiting_cap=limiting_cap,
                )
                _skip(
                    symbol,
                    "cap reached",
                    detail=_detail,
                )
                continue
            if (
                cash_run >= trade_size
                and (not is_new or distinct_positions < self.max_positions)
            ):
                actions.append(self._buy(symbol, trade_size))
                _gross_buy(trade_size)
                cash_run -= trade_size
                if existing is not None:
                    existing["value"] = float(existing.get("value", 0.0)) + trade_size
                else:
                    book.append(
                        {
                            "symbol": symbol,
                            "value": trade_size,
                            "score": book_sc,
                        }
                    )
                continue
            if is_new and distinct_positions >= self.max_positions:
                _detail = "max_positions %d reached" % self.max_positions
                _candidate_diag(
                    candidate=candidate,
                    symbol=symbol,
                    reason="trade limit",
                    detail=_detail,
                    requested_notional=requested_notional,
                    candidate_notional=trade_size,
                    cap_soft_d=cap_soft_d,
                    cap_hard_d=cap_hard_d,
                    existing=existing,
                    position_already_held=False,
                )
                _skip(
                    symbol,
                    "trade limit",
                    detail=_detail,
                )
            elif cash_run < trade_size - 1e-9:
                _detail = "cash $%.2f < trade_size $%.2f" % (cash_run, trade_size)
                _candidate_diag(
                    candidate=candidate,
                    symbol=symbol,
                    reason="size = 0",
                    detail=_detail,
                    requested_notional=requested_notional,
                    candidate_notional=trade_size,
                    cap_soft_d=cap_soft_d,
                    cap_hard_d=cap_hard_d,
                    existing=existing,
                    position_already_held=not is_new,
                )
                _skip(
                    symbol,
                    "size = 0",
                    detail=_detail,
                )

            if not portfolio_sorted:
                _candidate_diag(
                    candidate=candidate,
                    symbol=symbol,
                    reason="no_portfolio_to_rotate",
                    detail="no weakest holding available after direct buy failed",
                    requested_notional=requested_notional,
                    candidate_notional=trade_size,
                    cap_soft_d=cap_soft_d,
                    cap_hard_d=cap_hard_d,
                    existing=existing,
                    position_already_held=not is_new,
                )
                break

            weakest = portfolio_sorted.pop(0)
            w_sym = _sym(weakest)
            w_val = max(0.0, float(weakest.get("value", 0.0)))

            if (not self.replace_weakest_with_stronger) or (
                not self.rotate_capital(
                    new_signal_score=book_sc,
                    weakest_position=weakest,
                    replacement_strength_ratio=replacement_ratio,
                )
            ):
                portfolio_sorted.insert(0, weakest)
                if high_conviction_ratio_active:
                    log.info(
                        "HIGH_CONVICTION_ROTATION_REJECTED symbol=%s reason=rotation_not_stronger old_ratio=%.6f new_ratio=%.6f candidate_score=%.6f weakest_symbol=%s weakest_score=%.6f",
                        symbol,
                        float(self.replacement_strength_ratio),
                        float(replacement_ratio),
                        float(book_sc),
                        w_sym,
                        float(weakest.get("score", 0.0) or 0.0),
                    )
                _candidate_diag(
                    candidate=candidate,
                    symbol=symbol,
                    reason="rotation_not_stronger",
                    detail="candidate book score %.6f <= weakest score %.6f * ratio %.6f"
                    % (
                        float(book_sc),
                        float(weakest.get("score", 0.0) or 0.0),
                        float(replacement_ratio),
                    ),
                    requested_notional=requested_notional,
                    candidate_notional=trade_size,
                    cap_soft_d=cap_soft_d,
                    cap_hard_d=cap_hard_d,
                    existing=existing,
                    position_already_held=not is_new,
                    weakest_symbol=w_sym,
                    weakest_score=float(weakest.get("score", 0.0) or 0.0),
                    weakest_value=w_val,
                )
                # ``break`` would end the pass; try later candidates (adds, other names) for recycled cash.
                continue

            if self.rebalance_fund_from_weakest and self.rebalance_weakest_trim_fraction > 0:
                sell_amt = min(
                    w_val * self.rebalance_weakest_trim_fraction, w_val * (1.0 - 1e-9)
                )
            else:
                sell_amt = min(
                    w_val * self.rotate_trim_fraction, m
                )
            if sell_amt < 1e-6:
                portfolio_sorted.insert(0, weakest)
                _candidate_diag(
                    candidate=candidate,
                    symbol=symbol,
                    reason="rotation_sell_size_zero",
                    detail="weakest trim clipped to zero",
                    requested_notional=requested_notional,
                    candidate_notional=trade_size,
                    cap_soft_d=cap_soft_d,
                    cap_hard_d=cap_hard_d,
                    existing=existing,
                    position_already_held=not is_new,
                    weakest_symbol=w_sym,
                    weakest_score=float(weakest.get("score", 0.0) or 0.0),
                    weakest_value=w_val,
                )
                continue
            # Replace-weakest: bump sell to at least ``min_realloc_leg`` when the line can fund it;
            # if the line is smaller than ``min_realloc_leg``, sell the full line so the swap still runs.
            mleg = self.min_realloc_leg
            if mleg > 0:
                sell_amt = min(w_val, max(sell_amt, mleg))
            if sell_amt < 1e-6:
                portfolio_sorted.insert(0, weakest)
                _candidate_diag(
                    candidate=candidate,
                    symbol=symbol,
                    reason="rotation_sell_size_zero",
                    detail="weakest trim below minimum after min leg adjustment",
                    requested_notional=requested_notional,
                    candidate_notional=trade_size,
                    cap_soft_d=cap_soft_d,
                    cap_hard_d=cap_hard_d,
                    existing=existing,
                    position_already_held=not is_new,
                    weakest_symbol=w_sym,
                    weakest_score=float(weakest.get("score", 0.0) or 0.0),
                    weakest_value=w_val,
                )
                continue
            # Equal-leg swap: buy notional is capped to total-gross headroom (after virtual sell of sell_amt).
            swap = float(sell_amt)
            if use_gross and max_g is not None:
                g_after_sell = gross_run - float(sell_amt)
                add_to = max(0.0, max_g - g_after_sell)
                swap = min(swap, add_to)
            if swap < 1e-6:
                portfolio_sorted.insert(0, weakest)
                _candidate_diag(
                    candidate=candidate,
                    symbol=symbol,
                    reason="rotation_swap_size_zero",
                    detail="gross headroom clipped rotation swap to zero",
                    requested_notional=requested_notional,
                    candidate_notional=swap,
                    cap_soft_d=cap_soft_d,
                    cap_hard_d=cap_hard_d,
                    existing=existing,
                    position_already_held=not is_new,
                    weakest_symbol=w_sym,
                    weakest_score=float(weakest.get("score", 0.0) or 0.0),
                    weakest_value=w_val,
                )
                continue

            actions.append(self._sell(w_sym, swap))
            actions.append(self._buy(symbol, swap))

            w_row = _find(w_sym)
            if w_row is not None:
                w_row["value"] = max(0.0, float(w_row.get("value", 0.0)) - swap)
                if w_row["value"] < 1e-6:
                    book[:] = [p for p in book if _sym(p) != w_sym]

            cand_row = _find(symbol)
            if cand_row is not None:
                cand_row["value"] = float(cand_row.get("value", 0.0)) + swap
                cand_row["score"] = book_sc
            else:
                book.append({"symbol": symbol, "value": swap, "score": book_sc})

        if actions:
            log.debug("CapitalAllocator produced %d actions", len(actions))
        elif self.last_no_action_details:
            for sym_u, payload in self.last_no_action_details.items():
                log.info(
                    "ALLOCATOR_NO_ACTION_DETAIL symbol=%s reason=%s detail=%s final=True "
                    "target_allocation=%.2f available_cash=%.2f cash_reserve=%.2f "
                    "current_dynamic_sleeve_usage=%.2f dynamic_sleeve_cap=%.2f "
                    "candidate_notional_requested=%.2f candidate_notional=%.2f "
                    "tranche_min=%.2f candidate_requested_notional=%.2f "
                    "candidate_notional_cap=%.2f base_requested_notional=%.2f "
                    "final_trade_size=%.2f limiting_cap=%s "
                    "min_order_notional=%.2f max_single_dynamic_notional=%.2f "
                    "position_already_held=%s rebalance_deploy_mode=%s "
                    "rebalance_fund_from_weakest=%s max_positions=%d source=%s score=%s",
                    sym_u,
                    str(payload.get("reason") or "unknown"),
                    str(payload.get("detail") or "n/a"),
                    float(payload.get("target_allocation", 0.0) or 0.0),
                    float(payload.get("available_cash", 0.0) or 0.0),
                    float(payload.get("cash_reserve", 0.0) or 0.0),
                    float(payload.get("current_dynamic_sleeve_usage", 0.0) or 0.0),
                    float(payload.get("dynamic_sleeve_cap", 0.0) or 0.0),
                    float(payload.get("candidate_notional_requested", 0.0) or 0.0),
                    float(payload.get("candidate_notional", 0.0) or 0.0),
                    float(payload.get("tranche_min", 0.0) or 0.0),
                    float(payload.get("candidate_requested_notional", 0.0) or 0.0),
                    float(payload.get("candidate_notional_cap", 0.0) or 0.0),
                    float(payload.get("base_requested_notional", 0.0) or 0.0),
                    float(payload.get("candidate_notional", 0.0) or 0.0),
                    str(payload.get("limiting_cap") or "none"),
                    float(payload.get("min_order_notional", 0.0) or 0.0),
                    float(payload.get("max_single_dynamic_notional", 0.0) or 0.0),
                    bool(payload.get("position_already_held", False)),
                    str(payload.get("rebalance_deploy_mode") or "unknown"),
                    bool(payload.get("rebalance_fund_from_weakest", False)),
                    int(payload.get("max_positions", 0) or 0),
                    str(payload.get("source") or "n/a"),
                    str(payload.get("score")),
                )
        return actions

    def _buy(self, symbol: str, amount: float) -> dict[str, Any]:
        return AllocatorAction("buy", str(symbol).strip().upper(), float(amount)).as_dict()

    def _sell(self, symbol: str, amount: float) -> dict[str, Any]:
        return AllocatorAction("sell", str(symbol).strip().upper(), float(amount)).as_dict()
