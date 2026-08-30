from __future__ import annotations

import json
from pathlib import Path

from src.telegram_control import (
    TelegramLoopCommand,
    _handle_command,
    _status_text,
    allowed_chat_ids_from_env,
    parse_telegram_loop_command,
)


def test_parse_telegram_loop_command_variants() -> None:
    assert parse_telegram_loop_command("/status") == TelegramLoopCommand(action="status")
    assert parse_telegram_loop_command("/stop") == TelegramLoopCommand(action="stop")
    assert parse_telegram_loop_command("/live alice") == TelegramLoopCommand(
        action="run",
        mode="live",
        user_id="alice",
    )
    assert parse_telegram_loop_command("run paper bob") == TelegramLoopCommand(
        action="run",
        mode="paper",
        user_id="bob",
    )
    assert parse_telegram_loop_command("/bogus") is None


def test_allowed_chat_ids_falls_back_to_single_chat() -> None:
    env = {"TELEGRAM_CHAT_ID": "12345"}
    assert allowed_chat_ids_from_env(env) == {"12345"}


def test_status_text_handles_missing_and_stale_state(tmp_path: Path) -> None:
    assert "No Telegram-triggered loop" in _status_text(tmp_path)

    d = tmp_path / "data" / "telegram_control"
    d.mkdir(parents=True)
    (d / "loop_state.json").write_text(
        json.dumps({"pid": 999999, "mode": "paper", "user_id": "alice", "started_at": "now"}),
        encoding="utf-8",
    )
    out = _status_text(tmp_path)
    assert "not running anymore" in out
    assert "alice" in out


def test_handle_command_help(tmp_path: Path) -> None:
    out = _handle_command(tmp_path, "/help")
    assert "/live [user_id]" in out
