"""Tests for AI-style news ranking service."""

from __future__ import annotations

from datetime import datetime, timezone

from src.ai_news_ranking import (
    finance_news_source_weight,
    infer_catalyst_type,
    is_software_package_spam,
    rank_news_item,
    score_adjustment,
    score_catalyst_strength,
    score_news_quality,
)


def test_score_news_quality_rewards_specific_fresh_trusted_headline() -> None:
    now = datetime(2026, 6, 5, 12, tzinfo=timezone.utc)

    strong = score_news_quality(
        symbol="CRWD",
        headline="CRWD raises full-year revenue guidance by 12% after earnings beat",
        source="alpaca",
        published_at="2026-06-05T11:00:00Z",
        now=now,
    )
    vague = score_news_quality(
        symbol="CRWD",
        headline="Why is this stock moving today?",
        source="blog",
        published_at="2026-06-03T11:00:00Z",
        now=now,
    )

    assert strong > vague
    assert strong >= 0.80
    assert vague < 0.45


def test_catalyst_strength_infers_guidance_and_ai() -> None:
    ctype, strength = score_catalyst_strength(
        headline="Company raises guidance on AI demand",
        catalyst_type="",
    )

    assert ctype == "guidance"
    assert strength >= 0.90
    assert infer_catalyst_type("New OpenAI partnership announced") == "ai"


def test_rank_news_item_combines_quality_strength_and_confidence() -> None:
    rank = rank_news_item(
        symbol="NVDA",
        headline="NVDA signs AI infrastructure partnership worth $2 billion",
        source="benzinga",
        catalyst_type="deal",
        sentiment=0.6,
        published_at="2026-06-05T11:00:00Z",
        now=datetime(2026, 6, 5, 12, tzinfo=timezone.utc),
    )

    assert rank.symbol == "NVDA"
    assert rank.catalyst_type == "deal"
    assert rank.llm_confidence > 0.70
    assert rank.combined_score > 0.70
    assert score_adjustment(rank, weight=2.0) > 0


def test_finance_source_quality_prioritizes_reuters_and_the_information() -> None:
    now = datetime(2026, 6, 12, 12, tzinfo=timezone.utc)

    reuters = rank_news_item(
        symbol="NVDA",
        headline="Nvidia Vera CPU roadmap points to faster AI server chips",
        source="newsapi",
        publisher="Reuters",
        url="https://www.reuters.com/technology/",
        published_at="2026-06-12T11:30:00Z",
        now=now,
    )
    generic = rank_news_item(
        symbol="NVDA",
        headline="Nvidia Vera CPU roadmap points to faster AI server chips",
        source="newsapi",
        publisher="Generic Tech Blog",
        published_at="2026-06-12T11:30:00Z",
        now=now,
    )

    assert finance_news_source_weight("newsapi", "The Information") > 0.20
    assert reuters.catalyst_type == "product"
    assert reuters.news_quality > generic.news_quality
    assert reuters.combined_score > generic.combined_score


def test_software_package_release_spam_gets_low_quality() -> None:
    now = datetime(2026, 6, 12, 12, tzinfo=timezone.utc)

    spam = rank_news_item(
        symbol="NVDA",
        headline="NVDA 0.4.1 package released on PyPI",
        source="newsapi",
        publisher="PyPI",
        url="https://pypi.org/project/nvda/",
        published_at="2026-06-12T11:30:00Z",
        now=now,
    )

    assert is_software_package_spam(
        "NVDA 0.4.1 package released on PyPI",
        publisher="PyPI",
        url="https://pypi.org/project/nvda/",
    )
    assert spam.news_quality < 0.35
