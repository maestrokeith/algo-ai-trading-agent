"""Options adapter: bullish/bearish → call vs put (spread aliases)."""

from __future__ import annotations

import pytest

from src.options_adapter import (
    OptionIntent,
    adapt_stock_signal_to_option_intent,
    use_call_spread,
    use_put_spread,
)


def _opts_base() -> dict:
    return {
        "options": {
            "enabled": True,
            "mode": "long_premium_only",
            "allowed_underlyings": ["QQQ"],
            "entry_mapping": {
                "bullish_signal": "call_spread",
                "bearish_signal": "put_spread",
            },
        }
    }


def test_use_call_spread_and_put_spread_return_rights() -> None:
    assert use_call_spread() == "call"
    assert use_put_spread() == "put"


def test_adapt_bullish_call_spread_maps_to_call_intent() -> None:
    intent, err = adapt_stock_signal_to_option_intent(
        _opts_base(),
        underlying="QQQ",
        direction="bullish",
        source="trend",
        stock_symbol="QQQ",
    )
    assert err is None
    assert isinstance(intent, OptionIntent)
    assert intent.right == "call"
    assert intent.underlying == "QQQ"


def test_adapt_bearish_put_spread_maps_to_put_intent() -> None:
    cfg = _opts_base()
    intent, err = adapt_stock_signal_to_option_intent(
        cfg,
        underlying="QQQ",
        direction="bearish",
        source="trend",
        stock_symbol="QQQ",
    )
    assert err is None
    assert intent is not None
    assert intent.right == "put"


def test_legacy_call_put_still_work() -> None:
    cfg = {
        "options": {
            "enabled": True,
            "mode": "long_premium_only",
            "allowed_underlyings": ["SPY"],
            "entry_mapping": {"bullish_signal": "call", "bearish_signal": "put"},
        }
    }
    a, _ = adapt_stock_signal_to_option_intent(cfg, underlying="SPY", direction="bullish", source="x")
    b, _ = adapt_stock_signal_to_option_intent(cfg, underlying="SPY", direction="bearish", source="x")
    assert a is not None and a.right == "call"
    assert b is not None and b.right == "put"
