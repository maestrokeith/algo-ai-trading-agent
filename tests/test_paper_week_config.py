from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.options_config import max_premium_frac_of_equity, trend_long_options_top_signals_only_passes
from src.portfolio_allocation import effective_options_total_cap_frac


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _default_config() -> dict:
    return yaml.safe_load((PROJECT_ROOT / "config" / "default.yaml").read_text(encoding="utf-8"))


def test_paper_week_dynamic_history_and_quote_retry_enabled() -> None:
    cfg = _default_config()

    history = cfg["dynamic_universe"]["paper_min_history_bars_experiment"]
    retry = cfg["market_data"]["dynamic_quote_retry"]

    assert history == {"enabled": True, "min_bars": 50}
    assert retry["enabled"] is True
    assert retry["live_enabled"] is True
    assert retry["paper_enabled"] is True
    assert retry["attempts"] == 2


def test_paper_week_options_top_one_day_two_percent_sleeve_inactive() -> None:
    cfg = _default_config()
    options = cfg["options"]

    assert options["enabled"] is False
    assert options["mode"] == "paper_only"
    assert options["require_top_signal"] is True
    assert options["max_option_trades_per_day"] == 1
    assert options["max_option_contracts_per_day"] == 1
    assert options["max_positions"] == 1
    assert effective_options_total_cap_frac(cfg) == pytest.approx(0.02)
    assert max_premium_frac_of_equity(cfg) == pytest.approx(0.02)
    assert trend_long_options_top_signals_only_passes(cfg, {"in_top_signals": False}) is False
    assert trend_long_options_top_signals_only_passes(cfg, {"in_top_signals": True}) is True
