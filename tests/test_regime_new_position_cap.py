from __future__ import annotations

from src.risk_limits import risk_max_new_positions_per_cycle


def test_regime_score_3_new_position_cap_overrides_default() -> None:
    cfg = {
        "regime": {"score_3": {"max_new_positions": 3}},
        "alpha": {"max_new_positions_per_cycle": 5},
        "risk": {"max_new_positions_per_cycle": 6},
    }
    assert risk_max_new_positions_per_cycle(cfg, regime_score=3) == 3
    assert risk_max_new_positions_per_cycle(cfg, regime_score=4) == 5
