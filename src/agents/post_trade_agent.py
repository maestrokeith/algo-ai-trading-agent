"""Post-trade review and lesson generation."""

from __future__ import annotations

from src.intelligence.schemas import PostTradeReview


class PostTradeAgent:
    def review(
        self,
        *,
        strategy: str,
        regime_at_entry: str,
        entry_price: float,
        exit_price: float,
        holding_time_minutes: float,
        expected_behavior: str,
        actual_behavior: str,
        maximum_favorable_excursion: float | None = None,
        maximum_adverse_excursion: float | None = None,
    ) -> PostTradeReview:
        ret = ((exit_price - entry_price) / entry_price) * 100.0 if entry_price > 0 else 0.0
        worked: list[str] = []
        failed: list[str] = []
        if ret > 0:
            worked.append("trade closed profitably")
        else:
            failed.append("trade did not follow expected path")
        lesson = (
            f"Increase confidence for {strategy} in {regime_at_entry} after profitable follow-through."
            if ret > 0
            else f"Reduce confidence for {strategy} in {regime_at_entry} when similar entry quality weakens."
        )
        return PostTradeReview(
            strategy=strategy,
            regime_at_entry=regime_at_entry,
            entry_price=entry_price,
            exit_price=exit_price,
            return_pct=ret,
            holding_time_minutes=holding_time_minutes,
            maximum_favorable_excursion=maximum_favorable_excursion,
            maximum_adverse_excursion=maximum_adverse_excursion,
            expected_behavior=expected_behavior,
            actual_behavior=actual_behavior,
            what_worked=tuple(worked),
            what_failed=tuple(failed),
            lesson=lesson,
        )
