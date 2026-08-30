"""Load and validate app configuration from YAML.

Top-level keys merged at load time: ``exits`` / ``exit`` / ``cooldown`` → ``strategy.exits``;
``reentry`` → ``strategy.exits`` + ``entries``; ``risk.max_total_exposure_pct`` → portfolio gross gate;
``cooldowns`` → ``entries.symbol_cooldown_minutes`` + ``entries.leader_cooldown_overrides``;
``execution.allow_add_to_position`` / ``add_position_cooldown_minutes`` / ``min_trade_notional`` /
``max_trades_per_symbol_per_day`` → entries + portfolio + risk;
(see ``load_config``).
"""
import copy
from pathlib import Path
from typing import Any

import yaml

from src.risk_book_mode import apply_risk_book_mode
from src.risk_limits import parse_allocation_fraction


def _as_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on", "y"}:
            return True
        if text in {"0", "false", "no", "off", "n", ""}:
            return False
    return bool(value)


def deep_merge(base: dict, overrides: dict) -> dict:
    """Recursively merge *overrides* into a deep copy of *base*.

    - Dict values are merged recursively (nested keys update in place).
    - All other types (lists, scalars, None) in *overrides* replace the
      corresponding key in *base* outright.
    - Keys present in *overrides* but not in *base* are added.
    - The original *base* dict is never mutated.
    """
    result = copy.deepcopy(base)
    for key, override_val in overrides.items():
        base_val = result.get(key)
        if isinstance(base_val, dict) and isinstance(override_val, dict):
            result[key] = deep_merge(base_val, override_val)
        else:
            result[key] = copy.deepcopy(override_val)
    return result


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    if path is None:
        path = Path(__file__).resolve().parent.parent / "config" / "default.yaml"
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    # Operator shortcut: top-level ``exits:`` merges into ``strategy.exits`` (same keys as YAML under strategy).
    top_exits = cfg.get("exits")
    if isinstance(top_exits, dict) and isinstance(cfg.get("strategy"), dict):
        strat = cfg["strategy"]
        strat["exits"] = deep_merge(strat.get("exits") or {}, top_exits)
    # Same merge for ``exit:`` (singular) — e.g. take_profit_pct + partial_trim_trigger_pct.
    top_exit = cfg.get("exit")
    if isinstance(top_exit, dict) and isinstance(cfg.get("strategy"), dict):
        strat = cfg["strategy"]
        strat["exits"] = deep_merge(strat.get("exits") or {}, top_exit)
    # Operator shortcut: bare root key for the partial P/L trim gross-exposure guard.
    if (
        "disable_partial_trim_below_gross_pct" in cfg
        and isinstance(cfg.get("strategy"), dict)
    ):
        strat = cfg["strategy"]
        strat["exits"] = deep_merge(
            strat.get("exits") or {},
            {
                "disable_partial_trim_below_gross_pct": cfg.get(
                    "disable_partial_trim_below_gross_pct"
                )
            },
        )
    # Operator shortcut: ``cooldown:`` → ``strategy.exits`` (aliases for stop re-entry + strong-trend bypass).
    cd = cfg.get("cooldown")
    if isinstance(cd, dict) and isinstance(cfg.get("strategy"), dict):
        strat = cfg["strategy"]
        overlay: dict[str, Any] = {}
        if (
            "after_stop_loss_minutes" in cd
            and cd.get("after_stop_loss_minutes") is not None
            and str(cd.get("after_stop_loss_minutes")).strip() != ""
        ):
            overlay["cooldown_after_stop_minutes"] = cd["after_stop_loss_minutes"]
        if "allow_reentry_if_strong_trend" in cd:
            overlay["strong_trend_reconfirm_bypass_cooldown"] = bool(
                cd["allow_reentry_if_strong_trend"]
            )
        if overlay:
            strat["exits"] = deep_merge(strat.get("exits") or {}, overlay)
    # Operator shortcut: ``reentry:`` → stop breakout gate + profit pullback re-entry (entries).
    ren = cfg.get("reentry")
    if isinstance(ren, dict):
        ox_re: dict[str, Any] = {}
        if "require_new_high" in ren:
            ox_re["require_new_breakout_after_stop"] = bool(ren["require_new_high"])
        if ox_re and isinstance(cfg.get("strategy"), dict):
            strat = cfg["strategy"]
            strat["exits"] = deep_merge(strat.get("exits") or {}, ox_re)
        oe_re: dict[str, Any] = {}
        if "allow_pullback_reentry" in ren:
            oe_re["allow_reentry_on_pullback"] = bool(ren["allow_pullback_reentry"])
        if oe_re:
            cfg["entries"] = deep_merge(cfg.get("entries") or {}, oe_re)
    # Operator shortcuts: ``entries`` aliases for post-exit symbol cooldown tiers feed the
    # execution-layer structured exit cooldowns used by the live loop.
    ent = cfg.get("entries")
    if isinstance(ent, dict):
        ex_overlay: dict[str, Any] = {}
        port_overlay: dict[str, Any] = {}
        raw_stock_cd = ent.get("symbol_cooldown_stock_minutes")
        if raw_stock_cd is not None and str(raw_stock_cd).strip() != "":
            ex_overlay["symbol_cooldown_minutes"] = raw_stock_cd
        raw_etf_cd = ent.get("symbol_cooldown_etf_minutes")
        if raw_etf_cd is not None and str(raw_etf_cd).strip() != "":
            ex_overlay["symbol_cooldown_etf_minutes"] = raw_etf_cd
        if "allow_add_to_existing_positions" in ent:
            allow_existing = bool(ent.get("allow_add_to_existing_positions"))
            port_overlay["allow_add"] = allow_existing
            if allow_existing:
                port_overlay["allow_add_on_strong_momentum"] = True
                port_overlay["pyramid_into_winners"] = {
                    "enabled": True,
                }
        if ex_overlay:
            cfg["execution"] = deep_merge(cfg.get("execution") or {}, ex_overlay)
        if port_overlay:
            cfg["portfolio"] = deep_merge(cfg.get("portfolio") or {}, port_overlay)
    # Operator block: ``cooldowns.default_minutes`` → ``entries.symbol_cooldown_minutes``;
    # ``cooldowns.leader_overrides`` → ``entries.leader_cooldown_overrides`` (merged with existing).
    cds = cfg.get("cooldowns")
    if isinstance(cds, dict):
        dm = cds.get("default_minutes")
        if dm is not None and str(dm).strip() != "":
            cfg["entries"] = deep_merge(
                cfg.get("entries") or {},
                {"symbol_cooldown_minutes": dm},
            )
        loc = cds.get("leader_overrides")
        if isinstance(loc, dict) and loc:
            cfg["entries"] = deep_merge(
                cfg.get("entries") or {},
                {"leader_cooldown_overrides": loc},
            )
    # Operator shortcuts: ``execution.cooldown_minutes`` / ``add_position_cooldown_minutes``
    # overlay ``entries.symbol_cooldown_minutes`` (same live-loop gate as ``entries.per_symbol_buy_cooldown_min``).
    ex = cfg.get("execution")
    if isinstance(ex, dict):
        raw_cd = ex.get("cooldown_minutes")
        if raw_cd is not None and str(raw_cd).strip() != "":
            cfg["entries"] = deep_merge(
                cfg.get("entries") or {},
                {"symbol_cooldown_minutes": raw_cd},
            )
        raw_ap_cd = ex.get("add_position_cooldown_minutes")
        if raw_ap_cd is not None and str(raw_ap_cd).strip() != "":
            cfg["entries"] = deep_merge(
                cfg.get("entries") or {},
                {"symbol_cooldown_minutes": raw_ap_cd},
            )
        raw_mtps = ex.get("max_trades_per_symbol_per_day")
        if raw_mtps is not None and str(raw_mtps).strip() != "":
            try:
                mtps = int(raw_mtps)
            except (TypeError, ValueError):
                mtps = None
            if mtps is not None:
                cfg["risk"] = deep_merge(
                    cfg.get("risk") or {},
                    {"max_trades_per_symbol_per_day": mtps},
                )
                cfg["portfolio_risk"] = deep_merge(
                    cfg.get("portfolio_risk") or {},
                    {"max_trades_per_symbol_per_day": mtps},
                )
        if "allow_add_to_position" in ex:
            allow_ap = bool(ex.get("allow_add_to_position"))
            cfg["entries"] = deep_merge(
                cfg.get("entries") or {},
                {"allow_add_to_existing_positions": allow_ap},
            )
            if allow_ap:
                cfg["portfolio"] = deep_merge(
                    cfg.get("portfolio") or {},
                    {
                        "allow_add": True,
                        "allow_add_on_strong_momentum": True,
                        "pyramid_into_winners": {"enabled": True},
                    },
                )
        raw_mtn = ex.get("min_trade_notional")
        if raw_mtn is not None and str(raw_mtn).strip() != "":
            try:
                mtn = float(raw_mtn)
            except (TypeError, ValueError):
                mtn = 0.0
            if mtn > 0:
                cfg["execution"] = deep_merge(
                    cfg.get("execution") or {},
                    {"min_order_notional": mtn, "min_trade_dollars": mtn},
                )
                cfg["entries"] = deep_merge(
                    cfg.get("entries") or {},
                    {"min_trade_size": mtn},
                )
        raw_add_on_thr = ex.get("add_on_signal_threshold")
        if raw_add_on_thr is not None and str(raw_add_on_thr).strip() != "":
            cfg["portfolio"] = deep_merge(
                cfg.get("portfolio") or {},
                {"add_on": {"min_signal_strength": raw_add_on_thr}},
            )
    # Operator shortcut: ``strategy.reinforcement`` maps to the existing strong-trend
    # add-on / winner-pyramiding controls used by the live loop.
    strat_cfg = cfg.get("strategy")
    if isinstance(strat_cfg, dict):
        reinf = strat_cfg.get("reinforcement")
        if isinstance(reinf, dict) and bool(reinf.get("enabled")):
            port_overlay: dict[str, Any] = {
                "allow_add": True,
                "allow_add_on_strong_momentum": True,
                "pyramid_into_winners": {
                    "enabled": True,
                },
            }
            raw_profit = reinf.get("add_if_profit_pct")
            if raw_profit is not None and str(raw_profit).strip() != "":
                port_overlay["pyramid_into_winners"]["min_unrealized_profit_pct"] = raw_profit
            cfg["portfolio"] = deep_merge(cfg.get("portfolio") or {}, port_overlay)
            raw_max_adds = reinf.get("max_adds")
            if raw_max_adds is not None and str(raw_max_adds).strip() != "":
                try:
                    max_adds = max(0, int(raw_max_adds))
                except (TypeError, ValueError):
                    max_adds = 0
                risk_cfg = cfg.get("risk") if isinstance(cfg.get("risk"), dict) else {}
                if (
                    "max_adds_per_symbol_per_day" not in risk_cfg
                    and "max_addons_per_day" not in risk_cfg
                ):
                    cfg["risk"] = deep_merge(
                        cfg.get("risk") or {},
                        {"max_adds_per_symbol_per_day": max_adds},
                    )
    # Operator shortcut: ``position_sizing.allow_add_to_winners`` / ``max_positions_per_symbol``
    # map to the live loop's existing add-on controls.
    ps = cfg.get("position_sizing")
    if isinstance(ps, dict):
        port_overlay: dict[str, Any] = {}
        if "allow_add_to_winners" in ps:
            _allow_add_to_winners = bool(ps.get("allow_add_to_winners"))
            port_overlay["allow_add"] = _allow_add_to_winners
            if _allow_add_to_winners:
                # Keep the full "winner add" path enabled so held names do not get
                # short-circuited by downstream strong-momentum / pyramiding gates.
                port_overlay["allow_add_on_strong_momentum"] = True
                port_overlay["pyramid_into_winners"] = {
                    "enabled": True,
                }
        if "add_on_strength" in ps:
            port_overlay["add_on"] = {
                "enabled": bool(ps.get("add_on_strength"))
            }
            if bool(ps.get("add_on_strength")):
                port_overlay.setdefault("allow_add_on_strong_momentum", True)
        raw_add_on_threshold = ps.get("add_on_signal_threshold")
        if raw_add_on_threshold is not None and str(raw_add_on_threshold).strip() != "":
            add_on_overlay = port_overlay.get("add_on")
            if not isinstance(add_on_overlay, dict):
                add_on_overlay = {}
            add_on_overlay["min_signal_strength"] = raw_add_on_threshold
            port_overlay["add_on"] = add_on_overlay
        raw_incremental_add = ps.get("incremental_add_pct")
        if raw_incremental_add is None or str(raw_incremental_add).strip() == "":
            raw_incremental_add = ps.get("allow_add_small_pct")
        if raw_incremental_add is not None and str(raw_incremental_add).strip() != "":
            add_on_overlay = port_overlay.get("add_on")
            if not isinstance(add_on_overlay, dict):
                add_on_overlay = {}
            add_on_overlay["incremental_add_pct"] = raw_incremental_add
            port_overlay["add_on"] = add_on_overlay
        if port_overlay:
            cfg["portfolio"] = deep_merge(cfg.get("portfolio") or {}, port_overlay)
        raw_mps = ps.get("max_positions_per_symbol")
        if raw_mps is not None and str(raw_mps).strip() != "":
            try:
                max_positions_per_symbol = max(1, int(raw_mps))
            except (TypeError, ValueError):
                max_positions_per_symbol = 1
            risk_cfg = cfg.get("risk") if isinstance(cfg.get("risk"), dict) else {}
            if (
                "max_adds_per_symbol_per_day" not in risk_cfg
                and "max_addons_per_day" not in risk_cfg
            ):
                cfg["risk"] = deep_merge(
                    cfg.get("risk") or {},
                    {
                        # One base position counts as the first slot, so add-ons are the remainder.
                        "max_adds_per_symbol_per_day": max(0, max_positions_per_symbol - 1),
                    },
                )
    # Operator shortcut: top-level ``portfolio.min_deploy_pct`` / ``force_deploy_if_signals``
    # feed the existing capital allocator deployment logic when that nested key is unset.
    port = cfg.get("portfolio")
    if isinstance(port, dict):
        cap_alloc = port.get("capital_allocator") if isinstance(port.get("capital_allocator"), dict) else {}
        alloc_cfg = port.get("allocator") if isinstance(port.get("allocator"), dict) else {}
        cap_alloc_overlay: dict[str, Any] = {}
        port_overlay: dict[str, Any] = {}
        if (
            "min_deploy_pct" in port
            and "min_gross_deployment_pct" not in cap_alloc
        ):
            cap_alloc_overlay["min_gross_deployment_pct"] = port.get("min_deploy_pct")
        if (
            "force_deploy_if_signals" in port
            and "bullish_force_minimum_deploy" not in cap_alloc
        ):
            cap_alloc_overlay["bullish_force_minimum_deploy"] = bool(
                port.get("force_deploy_if_signals")
            )
        if "allow_add_ons" in cap_alloc:
            allow_add_ons = bool(cap_alloc.get("allow_add_ons"))
            port_overlay["allow_add"] = allow_add_ons
            if allow_add_ons:
                port_overlay["allow_add_on_strong_momentum"] = True
                port_overlay["pyramid_into_winners"] = {
                    "enabled": True,
                }
                add_on_overlay = port_overlay.get("add_on")
                if not isinstance(add_on_overlay, dict):
                    add_on_overlay = {}
                add_on_overlay["enabled"] = True
                port_overlay["add_on"] = add_on_overlay
        raw_ca_add_on_threshold = cap_alloc.get("add_on_signal_threshold")
        if raw_ca_add_on_threshold is not None and str(raw_ca_add_on_threshold).strip() != "":
            add_on_overlay = port_overlay.get("add_on")
            if not isinstance(add_on_overlay, dict):
                add_on_overlay = {}
            add_on_overlay["min_signal_strength"] = raw_ca_add_on_threshold
            port_overlay["add_on"] = add_on_overlay
        raw_ca_incremental_add = cap_alloc.get("incremental_add_pct")
        if raw_ca_incremental_add is None or str(raw_ca_incremental_add).strip() == "":
            raw_ca_incremental_add = cap_alloc.get("allow_add_small_pct")
        if raw_ca_incremental_add is not None and str(raw_ca_incremental_add).strip() != "":
            add_on_overlay = port_overlay.get("add_on")
            if not isinstance(add_on_overlay, dict):
                add_on_overlay = {}
            add_on_overlay["incremental_add_pct"] = raw_ca_incremental_add
            port_overlay["add_on"] = add_on_overlay
        if cap_alloc_overlay:
            cfg["portfolio"] = deep_merge(
                cfg.get("portfolio") or {},
                {"capital_allocator": cap_alloc_overlay},
            )
        if port_overlay:
            cfg["portfolio"] = deep_merge(
                cfg.get("portfolio") or {},
                port_overlay,
            )
        raw_topn = alloc_cfg.get("top_n_signals")
        if raw_topn is not None and str(raw_topn).strip() != "":
            try:
                top_n_signals = max(1, int(raw_topn))
            except (TypeError, ValueError):
                top_n_signals = 5
            allocation_cfg = cfg.get("allocation") if isinstance(cfg.get("allocation"), dict) else {}
            if "allocate_top_n" not in allocation_cfg:
                cfg["allocation"] = deep_merge(
                    cfg.get("allocation") or {},
                    {"allocate_top_n": top_n_signals},
                )
            if "deploy_top_n_signals" not in cap_alloc:
                cfg["portfolio"] = deep_merge(
                    cfg.get("portfolio") or {},
                    {"capital_allocator": {"deploy_top_n_signals": top_n_signals}},
                )
    # Operator shortcut: top-level ``allocator`` aliases current per-cycle ranked-entry caps.
    alloc = cfg.get("allocator")
    if isinstance(alloc, dict):
        allocation_overlay: dict[str, Any] = {}
        execution_overlay: dict[str, Any] = {}
        regime_overlay: dict[str, Any] = {}
        portfolio_cap_alloc_overlay: dict[str, Any] = {}
        raw_max_new = alloc.get("max_new_positions_per_cycle")
        if raw_max_new is not None and str(raw_max_new).strip() != "":
            try:
                max_new_positions = max(0, int(raw_max_new))
            except (TypeError, ValueError):
                max_new_positions = 0
            if "alpha" not in cfg or not isinstance(cfg.get("alpha"), dict):
                cfg["alpha"] = {}
            if "risk" not in cfg or not isinstance(cfg.get("risk"), dict):
                cfg["risk"] = {}
            cfg["alpha"] = deep_merge(
                cfg.get("alpha") or {},
                {"max_new_positions_per_cycle": max_new_positions},
            )
            cfg["risk"] = deep_merge(
                cfg.get("risk") or {},
                {"max_new_positions_per_cycle": max_new_positions},
            )
        raw_max_new_neutral = alloc.get("max_new_positions_neutral")
        if raw_max_new_neutral is not None and str(raw_max_new_neutral).strip() != "":
            try:
                max_new_neutral = max(0, int(raw_max_new_neutral))
            except (TypeError, ValueError):
                max_new_neutral = 0
            regime_overlay["score_3"] = {"max_new_positions": max_new_neutral}
        raw_pick_top_n = alloc.get("pick_top_n_signals")
        if raw_pick_top_n is not None and str(raw_pick_top_n).strip() != "":
            try:
                pick_top_n = max(1, int(raw_pick_top_n))
            except (TypeError, ValueError):
                pick_top_n = 5
            allocation_overlay["allocate_top_n"] = pick_top_n
            cfg["portfolio"] = deep_merge(
                cfg.get("portfolio") or {},
                {"capital_allocator": {"deploy_top_n_signals": pick_top_n}},
            )
        raw_sort_by = alloc.get("sort_by")
        if raw_sort_by is not None and str(raw_sort_by).strip() != "":
            sort_key = str(raw_sort_by).strip().lower()
            if sort_key in {"momentum", "momentum_rs_volume", "mrv"}:
                allocation_overlay["rank_by_signal_strength"] = True
                allocation_overlay["rank_top_k_by"] = "momentum_rs_volume"
            elif sort_key in {"momentum_volume_ema", "mve"}:
                allocation_overlay["rank_by_signal_strength"] = True
                allocation_overlay["rank_top_k_by"] = "momentum_volume_ema"
            elif sort_key in {"strength", "strength_eff"}:
                allocation_overlay["rank_by_signal_strength"] = True
                allocation_overlay["rank_top_k_by"] = "strength_eff"
        raw_rank_by = alloc.get("rank_by")
        if isinstance(raw_rank_by, (list, tuple)) and raw_rank_by:
            rank_parts = {str(x).strip().lower() for x in raw_rank_by if str(x).strip()}
            if {"momentum", "volume_spike", "distance_from_ema"}.issubset(rank_parts):
                allocation_overlay["rank_by_signal_strength"] = True
                allocation_overlay["rank_top_k_by"] = "momentum_volume_ema"
        raw_min_trade_notional = alloc.get("min_trade_notional")
        if (
            raw_min_trade_notional is not None
            and str(raw_min_trade_notional).strip() != ""
            and "min_trade_notional" not in (
                cfg.get("portfolio", {}).get("capital_allocator", {})
                if isinstance(cfg.get("portfolio", {}).get("capital_allocator", {}), dict)
                else {}
            )
        ):
            try:
                portfolio_cap_alloc_overlay["min_trade_notional"] = float(raw_min_trade_notional)
            except (TypeError, ValueError):
                pass
        raw_min_cash_deploy = alloc.get("minimum_cash_to_deploy_pct")
        if (
            raw_min_cash_deploy is not None
            and str(raw_min_cash_deploy).strip() != ""
            and "minimum_cash_to_deploy_pct" not in (
                cfg.get("portfolio", {}).get("capital_allocator", {})
                if isinstance(cfg.get("portfolio", {}).get("capital_allocator", {}), dict)
                else {}
            )
        ):
            portfolio_cap_alloc_overlay["minimum_cash_to_deploy_pct"] = 0.004
        raw_no_actions_cycles = alloc.get("if_no_actions_cycles")
        if raw_no_actions_cycles is not None and str(raw_no_actions_cycles).strip() != "":
            try:
                portfolio_cap_alloc_overlay["if_no_actions_cycles"] = max(0, int(raw_no_actions_cycles))
            except (TypeError, ValueError):
                pass
        if "allow_no_trade_cycles" in alloc:
            portfolio_cap_alloc_overlay["allow_no_trade_cycles"] = _as_bool(
                alloc.get("allow_no_trade_cycles")
            )
        if "selected_must_execute" in alloc:
            portfolio_cap_alloc_overlay["selected_must_execute"] = _as_bool(
                alloc.get("selected_must_execute")
            )
        raw_fallback = alloc.get("fallback")
        if isinstance(raw_fallback, dict):
            raw_pick_top_n = raw_fallback.get("pick_top_n")
            if raw_pick_top_n is not None and str(raw_pick_top_n).strip() != "":
                try:
                    portfolio_cap_alloc_overlay["fallback_pick_top_n"] = max(1, int(raw_pick_top_n))
                except (TypeError, ValueError):
                    pass
            raw_size_pct = raw_fallback.get("size_pct")
            if raw_size_pct is not None and str(raw_size_pct).strip() != "":
                portfolio_cap_alloc_overlay["fallback_size_pct"] = raw_size_pct
            if "enforce_diversity" in raw_fallback:
                portfolio_cap_alloc_overlay["fallback_enforce_diversity"] = bool(
                    raw_fallback.get("enforce_diversity")
                )
        raw_idle_fallback = alloc.get("idle_fallback")
        if isinstance(raw_idle_fallback, dict):
            idle_overlay: dict[str, Any] = {}
            for key in ("enabled", "max_gross_pct", "prefer_dynamic_symbols"):
                if key in raw_idle_fallback:
                    idle_overlay[key] = raw_idle_fallback.get(key)
            if idle_overlay:
                portfolio_cap_alloc_overlay["idle_fallback"] = idle_overlay
        raw_corr = cfg.get("correlation")
        if isinstance(raw_corr, dict):
            raw_max_per_group = raw_corr.get("max_per_group")
            if raw_max_per_group is not None and str(raw_max_per_group).strip() != "":
                try:
                    portfolio_cap_alloc_overlay["correlation_max_per_group"] = max(0, int(raw_max_per_group))
                except (TypeError, ValueError):
                    pass
            raw_groups = raw_corr.get("groups")
            if isinstance(raw_groups, dict) and raw_groups:
                portfolio_cap_alloc_overlay["correlation_groups"] = raw_groups
        raw_groups_top = cfg.get("groups")
        if isinstance(raw_groups_top, dict) and raw_groups_top:
            portfolio_cap_alloc_overlay["correlation_groups"] = raw_groups_top
        raw_corr_groups = cfg.get("correlation_groups")
        if isinstance(raw_corr_groups, dict) and raw_corr_groups:
            portfolio_cap_alloc_overlay["correlation_groups"] = raw_corr_groups
        raw_reentry_cooldown = alloc.get("reentry_cooldown_after_exit_minutes")
        if raw_reentry_cooldown is not None and str(raw_reentry_cooldown).strip() != "":
            try:
                execution_overlay["min_recent_exit_reentry_minutes"] = max(0, int(raw_reentry_cooldown))
            except (TypeError, ValueError):
                pass
        if allocation_overlay:
            cfg["allocation"] = deep_merge(
                cfg.get("allocation") or {},
                allocation_overlay,
            )
        if portfolio_cap_alloc_overlay:
            cfg["portfolio"] = deep_merge(
                cfg.get("portfolio") or {},
                {"capital_allocator": portfolio_cap_alloc_overlay},
            )
        if execution_overlay:
            cfg["execution"] = deep_merge(
                cfg.get("execution") or {},
                execution_overlay,
            )
        if regime_overlay:
            cfg["regime"] = deep_merge(
                cfg.get("regime") or {},
                regime_overlay,
            )
    sig_cfg = cfg.get("signals")
    if isinstance(sig_cfg, dict):
        signal_allocation_overlay: dict[str, Any] = {}
        raw_rank_by = sig_cfg.get("rank_by")
        if isinstance(raw_rank_by, (list, tuple)) and raw_rank_by:
            rank_parts = {str(x).strip().lower() for x in raw_rank_by if str(x).strip()}
            if {
                "momentum_5m",
                "volume_spike",
                "distance_from_20ema",
            }.issubset(rank_parts):
                signal_allocation_overlay["rank_by_signal_strength"] = True
                signal_allocation_overlay["rank_top_k_by"] = "momentum_volume_ema"
        raw_max_new_signals = sig_cfg.get("max_new_positions")
        if raw_max_new_signals is not None and str(raw_max_new_signals).strip() != "":
            try:
                max_new_signals = max(0, int(raw_max_new_signals))
            except (TypeError, ValueError):
                max_new_signals = 0
            signal_allocation_overlay["allocate_top_n"] = max_new_signals
            if "alpha" not in cfg or not isinstance(cfg.get("alpha"), dict):
                cfg["alpha"] = {}
            if "risk" not in cfg or not isinstance(cfg.get("risk"), dict):
                cfg["risk"] = {}
            cfg["alpha"] = deep_merge(
                cfg.get("alpha") or {},
                {"max_new_positions_per_cycle": max_new_signals},
            )
            cfg["risk"] = deep_merge(
                cfg.get("risk") or {},
                {"max_new_positions_per_cycle": max_new_signals},
            )
            cfg["portfolio"] = deep_merge(
                cfg.get("portfolio") or {},
                {"capital_allocator": {"deploy_top_n_signals": max_new_signals}},
            )
        if signal_allocation_overlay:
            cfg["allocation"] = deep_merge(
                cfg.get("allocation") or {},
                signal_allocation_overlay,
            )
    # ``risk.max_total_exposure_pct`` (fraction or %% points) → portfolio gross + exposure gate cap.
    rsk = cfg.get("risk")
    if isinstance(rsk, dict) and "max_total_exposure_pct" in rsk:
        frac = parse_allocation_fraction(rsk.get("max_total_exposure_pct"))
        if frac > 0:
            portfolio_cfg = cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {}
            gates_cfg = (
                portfolio_cfg.get("exposure_gates")
                if isinstance(portfolio_cfg.get("exposure_gates"), dict)
                else {}
            )

            def _max_existing_fraction(raw: Any) -> float:
                existing = parse_allocation_fraction(raw)
                return max(frac, existing) if existing > 0 else frac

            cfg["portfolio"] = deep_merge(
                portfolio_cfg,
                {
                    "exposure_gates": {
                        "max_total_exposure_frac": _max_existing_fraction(
                            gates_cfg.get("max_total_exposure_frac")
                        )
                    },
                    "max_gross_exposure": _max_existing_fraction(
                        portfolio_cfg.get("max_gross_exposure")
                    ),
                    "target_gross_exposure_pct": _max_existing_fraction(
                        portfolio_cfg.get("target_gross_exposure_pct")
                    ),
                },
            )
    apply_risk_book_mode(cfg)
    return cfg


def load_app_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load main config and merge optional ``config/strategy_v2.yaml`` into ``strategy_v2``."""
    base = load_config(path)
    base_p = Path(path) if path is not None else Path(__file__).resolve().parent.parent / "config" / "default.yaml"
    v2_path = base_p.parent / "strategy_v2.yaml"
    if v2_path.exists():
        with open(v2_path) as f:
            overlay = yaml.safe_load(f) or {}
        if overlay:
            base["strategy_v2"] = deep_merge(base.get("strategy_v2") or {}, overlay)
    ps = base.get("position_sizing")
    if isinstance(ps, dict):
        raw_base_pct = ps.get("base_position_pct")
        if raw_base_pct is not None and str(raw_base_pct).strip() != "":
            base["strategy_v2"] = deep_merge(
                base.get("strategy_v2") or {},
                {"portfolio": {"base_position_pct": raw_base_pct}},
            )
    apply_risk_book_mode(base)
    return base
