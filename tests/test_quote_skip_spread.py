"""QuoteInfo skip_spread_check and reference_mid when NBBO is one-sided."""

from __future__ import annotations

from src.brokers.alpaca_client import QuoteInfo


def test_reference_mid_uses_positive_mid() -> None:
    q = QuoteInfo(bid=99.0, ask=101.0, mid=100.0, spread_pct=2.0)
    assert q.reference_mid(50.0) == 100.0


def test_reference_mid_falls_back_when_mid_zero() -> None:
    q = QuoteInfo(
        bid=0.0,
        ask=0.0,
        mid=0.0,
        spread_pct=0.0,
        skip_spread_check=True,
    )
    assert q.reference_mid(123.4) == 123.4


def test_quote_skip_spread_check_default_false() -> None:
    q = QuoteInfo(bid=1.0, ask=1.1, mid=1.05, spread_pct=1.0)
    assert q.skip_spread_check is False
