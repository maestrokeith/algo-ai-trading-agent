from __future__ import annotations

from pathlib import Path

import yaml

from src.options_pilot_status import build_options_pilot_status, format_options_pilot_status


def _write_config(root: Path, *, enabled: bool, live_pilot: bool, mode: str = "live_long_premium") -> None:
    cfg = root / "config"
    cfg.mkdir()
    (cfg / "default.yaml").write_text(
        yaml.safe_dump(
            {
                "options": {
                    "enabled": False,
                    "mode": "paper_only",
                    "live_pilot_enabled": False,
                    "total_exposure_limit": 0.01,
                    "max_positions": 1,
                    "max_contracts_per_trade": 1,
                    "allowed_underlyings": ["SPY", "QQQ"],
                }
            }
        ),
        encoding="utf-8",
    )
    (cfg / "users.yaml").write_text(
        yaml.safe_dump(
            {
                "users": [
                    {
                        "id": "live_bot",
                        "alpaca_key_env": "ALPACA_LIVE_API_KEY_ID",
                        "alpaca_secret_env": "ALPACA_LIVE_API_SECRET_KEY",
                        "paper": False,
                        "overrides": {
                            "options": {
                                "enabled": enabled,
                                "mode": mode,
                                "live_pilot_enabled": live_pilot,
                                "live_pilot": {"enabled": live_pilot},
                            }
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def test_enabled_live_pilot_emits_active_status(tmp_path: Path) -> None:
    _write_config(tmp_path, enabled=True, live_pilot=True)

    status = build_options_pilot_status(
        root=tmp_path,
        env_name="live",
        log_text="OPTIONS_ENTRY_LANE symbol=QQQ lane=trend action=attempt reason=live_pilot_active\n",
    )
    lines = format_options_pilot_status(status)

    assert "OPTIONS_CONFIG enabled=true" in lines
    assert "OPTIONS_LIVE_PILOT enabled=true" in lines
    assert "mode=live_long_premium" in lines
    assert any(line.startswith("latest_entry_lane_logs=OPTIONS_ENTRY_LANE") for line in lines)
    assert "reason_if_no_orders=lane_active_no_order" in lines


def test_disabled_pilot_reports_disabled_reason(tmp_path: Path) -> None:
    _write_config(tmp_path, enabled=False, live_pilot=False, mode="paper_only")

    status = build_options_pilot_status(root=tmp_path, env_name="live", log_text="")
    lines = format_options_pilot_status(status)

    assert "OPTIONS_CONFIG enabled=false" in lines
    assert "OPTIONS_LIVE_PILOT enabled=false" in lines
    assert "reason_if_no_orders=no_options_lane_logs" in lines


def test_no_option_orders_but_lane_active_reports_filter_reason(tmp_path: Path) -> None:
    _write_config(tmp_path, enabled=True, live_pilot=True)

    status = build_options_pilot_status(
        root=tmp_path,
        env_name="live",
        log_text=(
            "OPTIONS_SCAN_RESULT symbol=QQQ right=call selected=none "
            "chain_rows=12 reason_codes=spread_failed reason=no_contract_selected\n"
        ),
    )

    assert status.latest_options_order_logs == []
    assert status.reason_if_no_orders == "no_contract_selected"
