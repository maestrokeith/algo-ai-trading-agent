"""Portfolio exposure gates (total book cap, technology sector throttle)."""

from __future__ import annotations

import pytest

from src.exposure_gates import (
    allocator_buys_disallowed_over_max_gross,
    allocator_buys_refused_when_gross_above_threshold,
    block_new_entries_total_exposure,
    entry_size_multiplier_tech_sector_over_cap,
    hard_exposure_reject_if_projected_gross_exceeds_cap,
    is_reduce_only_overexposed,
    layer1_reject_buy_if_projected_gross_exceeds_max,
    max_additional_buy_usd_hard_stack,
    max_additional_buy_usd_layer1_stack,
    parse_equity_fraction_optional,
    parse_portfolio_exposure_gates,
    parse_portfolio_hard_gross_entry_limits,
    parse_strong_signal_cap_relief,
    portfolio_hard_gross_blocks_new_entry,
    portfolio_loop_mode,
    skip_buy_if_projected_gross_over_max,
    soft_cap_max_buy_usd,
    strong_signal_cap_relief_eligible_for_symbol,
    technology_sector_book_frac,
)


def test_parse_defaults_when_missing() -> None:
    p = parse_portfolio_exposure_gates({})
    assert p["enabled"] is False
    assert p["force_trim_weakest_when_over_max"] is True
    assert p["overexposed_reduce_only"] is True
    assert p["overexposed_reduce_only_gross_frac"] == pytest.approx(1.0)
    assert p.get("soft_cap") == {"enabled": False}


def test_parse_reads_portfolio_section() -> None:
    cfg = {
        "portfolio": {
            "exposure_gates": {
                "enabled": True,
                "max_total_exposure_frac": 0.85,
                "max_tech_sector_exposure_frac": 0.35,
                "tech_over_cap_size_multiplier": 0.6,
            }
        }
    }
    p = parse_portfolio_exposure_gates(cfg)
    assert p["enabled"] is True
    assert p["max_total_exposure_frac"] == pytest.approx(0.85)
    assert p["max_tech_sector_exposure_frac"] == pytest.approx(0.35)
    assert p["tech_over_cap_size_multiplier"] == pytest.approx(0.6)
    assert p["force_trim_weakest_when_over_max"] is True


def test_parse_includes_overexposed_reduce_only_defaults() -> None:
    p = parse_portfolio_exposure_gates(
        {
            "portfolio": {
                "exposure_gates": {
                    "overexposed_reduce_only": True,
                    "overexposed_reduce_only_gross_frac": 0.95,
                }
            }
        }
    )
    assert p["overexposed_reduce_only"] is True
    assert p["overexposed_reduce_only_gross_frac"] == pytest.approx(0.95)


def test_parse_reads_soft_cap_enabled() -> None:
    p = parse_portfolio_exposure_gates(
        {
            "portfolio": {
                "exposure_gates": {
                    "soft_cap": {"enabled": True},
                }
            }
        }
    )
    assert p.get("soft_cap") == {"enabled": True}


def test_is_reduce_only_overexposed_gross_over_threshold() -> None:
    cfg = {
        "portfolio": {
            "exposure_gates": {
                "overexposed_reduce_only": True,
                "overexposed_reduce_only_gross_frac": 1.0,
            }
        }
    }
    assert is_reduce_only_overexposed(100.0, cfg) is False
    assert is_reduce_only_overexposed(100.01, cfg) is True
    assert is_reduce_only_overexposed(101.0, cfg) is True


def test_is_reduce_only_overexposed_can_disable() -> None:
    cfg = {
        "portfolio": {
            "exposure_gates": {
                "overexposed_reduce_only": False,
                "overexposed_reduce_only_gross_frac": 1.0,
            }
        }
    }
    assert is_reduce_only_overexposed(200.0, cfg) is False


def test_portfolio_loop_mode_three_labels() -> None:
    oel = {
        "risk": {
            "over_exposure_levels": {
                "mild": 0.95,
                "high": 1.0,
                "critical": 1.05,
            }
        }
    }
    # Below mild band
    assert portfolio_loop_mode(94.0, oel) == "normal"
    # In mild band, not yet reduce-only (gross < 1.0)
    assert portfolio_loop_mode(96.0, oel) == "normalization"
    # 100% of equity: tier is ``high`` but gross fraction is not strictly above 1.0 — normalization
    assert portfolio_loop_mode(100.0, oel) == "normalization"
    # Strictly above 100% of equity -> reduce_only
    assert portfolio_loop_mode(100.01, oel) == "reduce_only"
    assert portfolio_loop_mode(106.0, oel) == "reduce_only"


def test_portfolio_loop_mode_reduce_only_falls_back_to_portfolio_threshold() -> None:
    cfg = {
        "portfolio": {
            "exposure_gates": {
                "overexposed_reduce_only": True,
                "overexposed_reduce_only_gross_frac": 0.99,
            }
        }
    }
    assert portfolio_loop_mode(98.0, cfg) == "normalization"
    assert portfolio_loop_mode(100.0, cfg) == "reduce_only"


def test_parse_force_trim_flag_false() -> None:
    p = parse_portfolio_exposure_gates(
        {
            "portfolio": {
                "exposure_gates": {
                    "enabled": True,
                    "force_trim_weakest_when_over_max": False,
                }
            }
        }
    )
    assert p["force_trim_weakest_when_over_max"] is False


def test_allocator_buys_disallowed_when_gross_over_max() -> None:
    cfg = {
        "portfolio": {
            "exposure_gates": {"enabled": True, "max_total_exposure_frac": 0.9}
        }
    }
    assert allocator_buys_disallowed_over_max_gross(95.0, cfg) is True
    assert allocator_buys_disallowed_over_max_gross(88.0, cfg) is False


def test_allocator_buys_not_disallowed_when_exposure_gates_off() -> None:
    assert allocator_buys_disallowed_over_max_gross(200.0, {}) is False


def test_parse_equity_fraction_optional_percent_points() -> None:
    assert parse_equity_fraction_optional(90) == pytest.approx(0.9)
    assert parse_equity_fraction_optional(0.9) == pytest.approx(0.9)


def test_portfolio_hard_stop_blocks_above_90() -> None:
    cfg = {
        "portfolio": {
            "hard_stop_new_entries_above_gross": 0.90,
        }
    }
    assert portfolio_hard_gross_blocks_new_entry(90.0, cfg)[0] is False
    b, r = portfolio_hard_gross_blocks_new_entry(90.01, cfg)
    assert b is True
    assert r is not None and "hard_stop" in r


def test_portfolio_hard_block_all_at_100_pct_book() -> None:
    cfg = {
        "portfolio": {
            "hard_block_all_entries_above": 1.0,
        }
    }
    assert portfolio_hard_gross_blocks_new_entry(99.99, cfg)[0] is False
    b, r = portfolio_hard_gross_blocks_new_entry(100.0, cfg)
    assert b is True
    assert r is not None and "hard_block" in r


def test_parse_portfolio_hard_gross_entry_limits() -> None:
    lim = parse_portfolio_hard_gross_entry_limits(
        {"portfolio": {"hard_stop_new_entries_above_gross": 80, "hard_block_all_entries_above": 0.99}}
    )
    assert lim["hard_stop_new_entries_above_gross"] == pytest.approx(0.8)
    assert lim["hard_block_all_entries_above"] == pytest.approx(0.99)


def test_allocator_refuse_when_gross_above_threshold() -> None:
    cfg = {
        "portfolio": {
            "capital_allocator": {"refuse_to_allocate_if_gross_above": 0.90},
        }
    }
    assert allocator_buys_refused_when_gross_above_threshold(90.0, cfg, {}) is False
    assert allocator_buys_refused_when_gross_above_threshold(91.0, cfg, {}) is True


def test_allocator_refuse_from_allocator_alias() -> None:
    cfg = {
        "portfolio": {
            "allocator": {"refuse_to_allocate_if_gross_above": 0.85},
        }
    }
    assert allocator_buys_refused_when_gross_above_threshold(86.0, cfg, {}) is True


def test_block_when_gross_pct_over_cap() -> None:
    blocked, reason = block_new_entries_total_exposure(
        91.0,
        enabled=True,
        max_total_exposure_frac=0.9,
    )
    assert blocked is True
    assert reason is not None and "90%" in reason


def test_no_block_when_under_cap() -> None:
    blocked, _ = block_new_entries_total_exposure(
        89.0,
        enabled=True,
        max_total_exposure_frac=0.9,
    )
    assert blocked is False


def test_skip_buy_projected_allows_when_under_cap_after_order() -> None:
    # 88%% + 1%% of equity = 89%% < 90%%
    skip, r = skip_buy_if_projected_gross_over_max(
        current_gross_pct=88.0,
        buy_notional=1000.0,
        account_equity=100_000.0,
        enabled=True,
        max_total_exposure_frac=0.9,
    )
    assert skip is False
    assert r is None


def test_skip_buy_projected_blocks_when_would_exceed_max() -> None:
    # 88%% + 2.1%% of equity = 90.1%% > 90%%
    skip, r = skip_buy_if_projected_gross_over_max(
        current_gross_pct=88.0,
        buy_notional=2100.0,
        account_equity=100_000.0,
        enabled=True,
        max_total_exposure_frac=0.9,
    )
    assert skip is True
    assert r is not None
    assert "projected gross" in (r or "").lower()


def test_hard_exposure_rejects_over_100_pct_equity() -> None:
    s, r = hard_exposure_reject_if_projected_gross_exceeds_cap(
        current_gross_pct=50.0,
        buy_notional=60_000.0,
        account_equity=100_000.0,
        max_gross_cap_frac=0.99,
        exposure_gates_enabled=True,
    )
    assert s is True
    assert r is not None and "100" in (r or "")


def test_hard_exposure_rejects_over_cap_no_relief() -> None:
    s, r = hard_exposure_reject_if_projected_gross_exceeds_cap(
        current_gross_pct=88.0,
        buy_notional=2_200.0,
        account_equity=100_000.0,
        max_gross_cap_frac=0.9,
        exposure_gates_enabled=True,
    )
    # 88% + 2.2% = 90.2% > 90% cap
    assert s is True
    assert r is not None and "HARD" in (r or "")


def test_hard_exposure_gates_off_only_100_backstop() -> None:
    s, _ = hard_exposure_reject_if_projected_gross_exceeds_cap(
        current_gross_pct=90.0,
        buy_notional=5.0,
        account_equity=100_000.0,
        max_gross_cap_frac=0.9,
        exposure_gates_enabled=False,
    )
    # 90.0005% — under 100%, cap ignored
    assert s is False
    s2, r2 = hard_exposure_reject_if_projected_gross_exceeds_cap(
        current_gross_pct=100.0,
        buy_notional=1.0,
        account_equity=100_000.0,
        max_gross_cap_frac=0.9,
        exposure_gates_enabled=False,
    )
    assert s2 is True
    assert r2 is not None and "100" in r2


def test_max_additional_buy_usd_hard_stack_matches_hard_rejection_threshold() -> None:
    eq = 100_000.0
    cur_pct = 88.0
    extra = max_additional_buy_usd_hard_stack(cur_pct, eq, 0.9, True)
    assert extra == pytest.approx(2000.0)
    s, _ = hard_exposure_reject_if_projected_gross_exceeds_cap(
        current_gross_pct=cur_pct,
        buy_notional=extra + 1.0,
        account_equity=eq,
        max_gross_cap_frac=0.9,
        exposure_gates_enabled=True,
    )
    assert s is True


def test_soft_cap_max_buy_usd_aligns_hard_and_layer1_when_equal_caps() -> None:
    eq = 100_000.0
    cur_pct = 88.0
    cap = 0.9
    h = max_additional_buy_usd_hard_stack(cur_pct, eq, cap, True)
    l1 = max_additional_buy_usd_layer1_stack(
        cur_pct,
        eq,
        cap,
        relief=None,
        symbol_upper=None,
        entry_strength=None,
        cap_relax_factor=1.0,
    )
    assert h == pytest.approx(2000.0)
    assert l1 == pytest.approx(2000.0)
    sc = soft_cap_max_buy_usd(
        current_gross_pct=cur_pct,
        account_equity=eq,
        max_total_exposure_frac=cap,
        exposure_gates_enabled=True,
        relief=None,
        symbol_upper=None,
        entry_strength=None,
        cap_relax_factor=1.0,
    )
    assert sc == pytest.approx(min(h, l1))


def test_soft_cap_max_buy_usd_when_gates_disabled_uses_hard_headroom_only() -> None:
    eq = 100_000.0
    cur_pct = 88.0
    sc = soft_cap_max_buy_usd(
        current_gross_pct=cur_pct,
        account_equity=eq,
        max_total_exposure_frac=0.5,
        exposure_gates_enabled=False,
        relief=None,
        symbol_upper=None,
        entry_strength=None,
        cap_relax_factor=1.0,
    )
    h_only = max_additional_buy_usd_hard_stack(cur_pct, eq, 0.5, False)
    assert sc == pytest.approx(h_only)


def test_layer1_reject_is_alias_of_skip_buy_projected() -> None:
    _kw = dict(
        current_gross_pct=88.0,
        buy_notional=2100.0,
        account_equity=100_000.0,
        enabled=True,
        max_total_exposure_frac=0.9,
    )
    assert (
        layer1_reject_buy_if_projected_gross_exceeds_max(**_kw)
        == skip_buy_if_projected_gross_over_max(**_kw)
    )


def test_skip_buy_projected_disabled_when_gates_off() -> None:
    skip, r = skip_buy_if_projected_gross_over_max(
        current_gross_pct=200.0,
        buy_notional=50_000.0,
        account_equity=100_000.0,
        enabled=False,
        max_total_exposure_frac=0.5,
    )
    assert skip is False
    assert r is None


def test_parse_strong_signal_cap_relief_defaults() -> None:
    r = parse_strong_signal_cap_relief({})
    assert r["enabled"] is False
    assert r["min_strength"] == pytest.approx(0.82)
    assert r["extra_gross_exposure_frac"] == pytest.approx(0.02)
    assert r.get("relief_symbols_upper") is None


def test_parse_strong_signal_cap_relief_reads_portfolio() -> None:
    cfg = {
        "portfolio": {
            "strong_signal_cap_relief": {
                "enabled": True,
                "min_strength": 0.88,
                "extra_gross_exposure_frac": 0.03,
            }
        }
    }
    r = parse_strong_signal_cap_relief(cfg)
    assert r["enabled"] is True
    assert r["min_strength"] == pytest.approx(0.88)
    assert r["extra_gross_exposure_frac"] == pytest.approx(0.03)
    assert r.get("relief_symbols_upper") is None


def test_parse_strong_signal_cap_relief_relief_symbols() -> None:
    cfg = {
        "portfolio": {
            "strong_signal_cap_relief": {
                "enabled": True,
                "relief_symbols": ["spy", "QQQ"],
            }
        }
    }
    r = parse_strong_signal_cap_relief(cfg)
    assert r["relief_symbols_upper"] == frozenset({"SPY", "QQQ"})


def test_block_total_exposure_relief_when_strong_signal() -> None:
    relief = parse_strong_signal_cap_relief(
        {"portfolio": {"strong_signal_cap_relief": {"enabled": True}}}
    )
    # 91.5%% book: over 0.9 base cap, but within 0.9 + 0.02 when strength ≥ min
    blocked_strong, _ = block_new_entries_total_exposure(
        91.5,
        enabled=True,
        max_total_exposure_frac=0.9,
        entry_strength=0.85,
        relief=relief,
        symbol_upper="AAPL",
    )
    assert blocked_strong is False
    blocked_weak, _ = block_new_entries_total_exposure(
        91.5,
        enabled=True,
        max_total_exposure_frac=0.9,
        entry_strength=0.5,
        relief=relief,
        symbol_upper="AAPL",
    )
    assert blocked_weak is True


def test_block_total_exposure_relief_respects_relief_symbols() -> None:
    relief = parse_strong_signal_cap_relief(
        {
            "portfolio": {
                "strong_signal_cap_relief": {
                    "enabled": True,
                    "relief_symbols": ["SPY"],
                }
            }
        }
    )
    blocked_other, _ = block_new_entries_total_exposure(
        91.5,
        enabled=True,
        max_total_exposure_frac=0.9,
        entry_strength=0.95,
        relief=relief,
        symbol_upper="XLF",
    )
    assert blocked_other is True
    blocked_spy, _ = block_new_entries_total_exposure(
        91.5,
        enabled=True,
        max_total_exposure_frac=0.9,
        entry_strength=0.95,
        relief=relief,
        symbol_upper="SPY",
    )
    assert blocked_spy is False


def test_strong_signal_cap_relief_eligible_for_symbol() -> None:
    r = parse_strong_signal_cap_relief(
        {
            "portfolio": {
                "strong_signal_cap_relief": {
                    "enabled": True,
                    "min_strength": 0.9,
                    "relief_symbols": ["NVDA"],
                }
            }
        }
    )
    assert strong_signal_cap_relief_eligible_for_symbol(r, symbol_upper="NVDA", entry_strength=0.91)
    assert not strong_signal_cap_relief_eligible_for_symbol(r, symbol_upper="QQQ", entry_strength=0.91)
    assert not strong_signal_cap_relief_eligible_for_symbol(r, symbol_upper="NVDA", entry_strength=0.5)


def test_technology_sector_book_frac() -> None:
    assert technology_sector_book_frac({"technology": 40.0}) == pytest.approx(0.4)


def test_tech_multiplier_only_for_tech_symbol_when_over_cap() -> None:
    sector_pct = {"technology": 50.0}
    m = entry_size_multiplier_tech_sector_over_cap(
        "NVDA",
        sector_pct,
        {"NVDA": "technology"},
        enabled=True,
        max_tech_sector_exposure_frac=0.4,
        tech_over_cap_size_multiplier=0.5,
    )
    assert m == pytest.approx(0.5)
    m2 = entry_size_multiplier_tech_sector_over_cap(
        "XLF",
        sector_pct,
        {"XLF": "financials"},
        enabled=True,
        max_tech_sector_exposure_frac=0.4,
        tech_over_cap_size_multiplier=0.5,
    )
    assert m2 == pytest.approx(1.0)


def test_tech_multiplier_off_when_under_cap() -> None:
    sector_pct = {"technology": 30.0}
    m = entry_size_multiplier_tech_sector_over_cap(
        "NVDA",
        sector_pct,
        {"NVDA": "technology"},
        enabled=True,
        max_tech_sector_exposure_frac=0.4,
        tech_over_cap_size_multiplier=0.5,
    )
    assert m == pytest.approx(1.0)
