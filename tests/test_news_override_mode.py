"""news.override_mode normalization."""

from __future__ import annotations

import pytest

from src.news_sentiment.rules import evaluate_high_conviction_news_override, normalize_news_override_mode


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("light", "light"),
        ("LIGHT", "light"),
        ("full", "full"),
        ("off", "off"),
        (None, "full"),
        ("", "full"),
        ("bogus", "full"),
    ],
)
def test_normalize_news_override_mode(raw: object, expected: str) -> None:
    assert normalize_news_override_mode(raw) == expected


def test_high_conviction_override_uses_catalyst_specific_thresholds() -> None:
    allowed, reason, score, thresholds = evaluate_high_conviction_news_override(
        {
            "trading": {
                "dynamic": {
                    "high_conviction_news_override": {
                        "enabled": True,
                        "min_news_score": 9.0,
                        "min_event_score": 9.0,
                        "min_catalyst_score": 9.0,
                        "min_relative_volume": 1.5,
                        "require_positive_sentiment": True,
                        "thresholds": {
                            "fda_approval": {
                                "min_news_score": 6.0,
                                "min_event_score": 6.0,
                                "min_catalyst_score": 6.0,
                            }
                        },
                    }
                }
            }
        },
        catalyst_type="FDA approval",
        news_score=6.5,
        event_score=0,
        catalyst_score=0,
        relative_volume=2.0,
        sentiment=0.2,
    )

    assert allowed is True
    assert reason == "high_conviction_fda_approval"
    assert score == pytest.approx(6.5)
    assert thresholds["min_news_score"] == pytest.approx(6.0)


def test_high_conviction_override_blocks_unsupported_and_safety_inputs() -> None:
    base = {
        "trading": {
            "dynamic": {
                "high_conviction_news_override": {
                    "enabled": True,
                    "min_news_score": 7.0,
                    "min_relative_volume": 1.5,
                    "require_positive_sentiment": True,
                }
            }
        }
    }
    unsupported, unsupported_reason, _, _ = evaluate_high_conviction_news_override(
        base,
        catalyst_type="rumor",
        news_score=10,
        relative_volume=3.0,
        sentiment=0.5,
    )
    low_volume, low_volume_reason, _, _ = evaluate_high_conviction_news_override(
        base,
        catalyst_type="earnings",
        news_score=10,
        relative_volume=0.9,
        sentiment=0.5,
    )

    assert unsupported is False
    assert unsupported_reason == "unsupported_catalyst_type"
    assert low_volume is False
    assert low_volume_reason == "relative_volume_below_threshold"
