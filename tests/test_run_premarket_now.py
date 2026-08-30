from __future__ import annotations

from datetime import datetime, timezone

import scripts.run_premarket_now as run_premarket_now


def test_run_premarket_now_uses_scheduler_tick_dry_run(monkeypatch) -> None:
    captured = {}

    def _load_app_config(_path):
        return {"premarket_intelligence": {"allow_trading": True}}

    def _startup(config):
        captured["allow_trading"] = config["premarket_intelligence"]["allow_trading"]

        class _PM:
            enabled = True
            keep_alive_overnight = True
            allow_trading = False
            news_scan_time = "05:00"

        return _PM()

    def _tick(config, now, **kwargs):
        captured["config"] = config
        captured["now"] = now
        captured["kwargs"] = kwargs
        return []

    monkeypatch.setattr(run_premarket_now, "load_app_config", _load_app_config)
    monkeypatch.setattr(run_premarket_now, "log_premarket_startup_config", _startup)
    monkeypatch.setattr(run_premarket_now, "run_premarket_scheduler_tick", _tick)

    run_premarket_now.main(["--dry-run", "--now", "2026-06-01T06:03:00-04:00"])

    assert captured["allow_trading"] is False
    assert captured["kwargs"]["dry_run"] is True
    assert captured["kwargs"]["force_jobs"] == ["news_5am"]
    assert captured["kwargs"]["reason"] == "manual_debug"
    assert captured["now"].isoformat() == "2026-06-01T06:03:00-04:00"
