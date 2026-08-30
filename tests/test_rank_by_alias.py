from __future__ import annotations

from src.config_loader import load_config
from src.signal_ranking import row_momentum_volume_ema_score


def test_rank_by_alias_maps_to_momentum_volume_ema(tmp_path) -> None:
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(
        """
allocator:
  rank_by: [momentum, volume_spike, distance_from_ema]
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert cfg["allocation"]["rank_by_signal_strength"] is True
    assert cfg["allocation"]["rank_top_k_by"] == "momentum_volume_ema"


def test_signals_rank_by_alias_maps_to_momentum_volume_ema(tmp_path) -> None:
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(
        """
signals:
  rank_by:
    - momentum_5m
    - volume_spike
    - distance_from_20ema
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert cfg["allocation"]["rank_by_signal_strength"] is True
    assert cfg["allocation"]["rank_top_k_by"] == "momentum_volume_ema"


def test_signals_max_new_positions_alias_caps_ranked_batch(tmp_path) -> None:
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(
        """
signals:
  max_new_positions: 3
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert cfg["allocation"]["allocate_top_n"] == 3
    assert cfg["alpha"]["max_new_positions_per_cycle"] == 3
    assert cfg["risk"]["max_new_positions_per_cycle"] == 3
    assert cfg["portfolio"]["capital_allocator"]["deploy_top_n_signals"] == 3


def test_row_momentum_volume_ema_score_uses_three_components() -> None:
    row = {
        "rank_breakdown": {
            "momentum": 0.7,
            "trend_strength": 0.8,
            "relative_strength": 0.6,
        }
    }
    assert row_momentum_volume_ema_score(row) == 2.1
