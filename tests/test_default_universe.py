"""Default config universe.symbols — canonical list for live/backtest scans."""

from __future__ import annotations

from src.config_loader import load_config

# Keep in sync with config/default.yaml universe.symbols (35 names).
_EXPECTED_UNIVERSE = frozenset(
    {
        "SPY",
        "QQQ",
        "IWM",
        "XLK",
        "XLF",
        "XLE",
        "XLY",
        "SMH",
        "AAPL",
        "MSFT",
        "NVDA",
        "AMZN",
        "META",
        "GOOGL",
        "AVGO",
        "AMD",
        "ORCL",
        "NFLX",
        "PLTR",
        "CRWD",
        "DDOG",
        "NOW",
        "SNOW",
        "UBER",
        "SHOP",
        "SQ",
        "ARM",
        "MU",
        "ANET",
        "MRVL",
        "SMCI",
        "TSM",
        "JPM",
        "GS",
        "LLY",
    }
)


def test_default_yaml_universe_matches_expected() -> None:
    cfg = load_config()
    symbols = cfg["universe"]["symbols"]
    assert len(symbols) == len(set(symbols)), "universe.symbols contains duplicates"
    assert frozenset(symbols) == _EXPECTED_UNIVERSE
