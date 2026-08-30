"""Telegram-triggered control plane for starting/stopping the trading loop."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from src.daily_report_notify import send_telegram_to_chat

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TelegramLoopCommand:
    action: str
    mode: str | None = None
    user_id: str | None = None


def parse_telegram_loop_command(text: str) -> TelegramLoopCommand | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    parts = raw.split()
    cmd = parts[0].lower()
    rest = parts[1:]

    if cmd in {"/help", "help", "/start"}:
        return TelegramLoopCommand(action="help")
    if cmd in {"/status", "status"}:
        return TelegramLoopCommand(action="status")
    if cmd in {"/stop", "stop"}:
        return TelegramLoopCommand(action="stop")
    if cmd in {"/live", "live"}:
        return TelegramLoopCommand(action="run", mode="live", user_id=rest[0] if rest else None)
    if cmd in {"/paper", "paper"}:
        return TelegramLoopCommand(action="run", mode="paper", user_id=rest[0] if rest else None)
    if cmd in {"/run", "run"}:
        if not rest:
            return None
        mode = str(rest[0]).strip().lower()
        if mode not in {"live", "paper"}:
            return None
        return TelegramLoopCommand(action="run", mode=mode, user_id=rest[1] if len(rest) > 1 else None)
    return None


def allowed_chat_ids_from_env(env: Mapping[str, str] | None = None) -> set[str]:
    e = env if env is not None else os.environ
    raw = str(e.get("TELEGRAM_ALLOWED_CHAT_IDS") or "").strip()
    vals = [x.strip() for x in raw.split(",") if x.strip()]
    if not vals:
        fallback = str(e.get("TELEGRAM_CHAT_ID") or "").strip()
        if fallback:
            vals = [fallback]
    return set(vals)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _control_dir(repo_root: Path) -> Path:
    return repo_root / "data" / "telegram_control"


def _offset_path(repo_root: Path) -> Path:
    return _control_dir(repo_root) / "update_offset.txt"


def _state_path(repo_root: Path) -> Path:
    return _control_dir(repo_root) / "loop_state.json"


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_offset(repo_root: Path) -> int | None:
    p = _offset_path(repo_root)
    try:
        raw = p.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def _save_offset(repo_root: Path, offset: int) -> None:
    p = _offset_path(repo_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(int(offset)), encoding="utf-8")


def _load_state(repo_root: Path) -> dict[str, Any] | None:
    p = _state_path(repo_root)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _save_state(repo_root: Path, state: Mapping[str, Any]) -> None:
    p = _state_path(repo_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(dict(state), indent=2, sort_keys=True), encoding="utf-8")


def _clear_state(repo_root: Path) -> None:
    try:
        _state_path(repo_root).unlink()
    except OSError:
        pass


def _status_text(repo_root: Path) -> str:
    state = _load_state(repo_root)
    if not state:
        return "No Telegram-triggered loop is currently tracked."
    try:
        pid = int(state.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    alive = _pid_alive(pid)
    mode = str(state.get("mode") or "?").upper()
    user_id = str(state.get("user_id") or "all users")
    started_at = str(state.get("started_at") or "?")
    log_path = str(state.get("log_path") or "")
    if not alive:
        return (
            f"Tracked loop is not running anymore.\n"
            f"mode={mode} user={user_id} pid={pid} started_at={started_at}"
        )
    lines = [
        "Trading loop is running.",
        f"mode={mode}",
        f"user={user_id}",
        f"pid={pid}",
        f"started_at={started_at}",
    ]
    if log_path:
        lines.append(f"log={log_path}")
    return "\n".join(lines)


def _build_loop_command(repo_root: Path, cmd: TelegramLoopCommand) -> list[str]:
    assert cmd.action == "run"
    assert cmd.mode in {"live", "paper"}
    argv = [sys.executable, str(repo_root / "scripts" / "algo_loop.py"), f"--{cmd.mode}"]
    if cmd.user_id:
        argv.extend(["--user", cmd.user_id])
    if os.environ.get("TELEGRAM_CONTROL_VERBOSE", "").strip().lower() in {"1", "true", "yes"}:
        argv.append("--verbose")
    return argv


def _start_loop(repo_root: Path, cmd: TelegramLoopCommand) -> str:
    state = _load_state(repo_root)
    if state:
        try:
            pid = int(state.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if _pid_alive(pid):
            mode = str(state.get("mode") or "?").upper()
            return f"Loop already running: mode={mode} pid={pid}"

    control_dir = _control_dir(repo_root)
    control_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = control_dir / f"algo_loop_{cmd.mode}_{stamp}.log"
    fp = open(log_path, "ab")
    proc = subprocess.Popen(
        _build_loop_command(repo_root, cmd),
        cwd=str(repo_root),
        stdout=fp,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=os.environ.copy(),
    )
    _save_state(
        repo_root,
        {
            "pid": proc.pid,
            "mode": cmd.mode,
            "user_id": cmd.user_id,
            "started_at": _now_utc_iso(),
            "log_path": str(log_path),
            "command": _build_loop_command(repo_root, cmd),
        },
    )
    return (
        f"Started {cmd.mode.upper()} loop"
        + (f" for user {cmd.user_id}" if cmd.user_id else "")
        + f". pid={proc.pid}"
    )


def _stop_loop(repo_root: Path) -> str:
    state = _load_state(repo_root)
    if not state:
        return "No Telegram-triggered loop is currently tracked."
    try:
        pid = int(state.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if not _pid_alive(pid):
        _clear_state(repo_root)
        return "Tracked loop was already stopped."
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        return f"Failed to stop pid={pid}: {exc}"
    return f"Stop signal sent to pid={pid}."


def _help_text() -> str:
    return "\n".join(
        [
            "Telegram trading loop control:",
            "/status",
            "/paper [user_id]",
            "/live [user_id]",
            "/stop",
        ]
    )


def _telegram_api_get(token: str, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"https://api.telegram.org/bot{token}/{method}"
    if qs:
        url = f"{url}?{qs}"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=90) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    payload = json.loads(raw)
    if not payload.get("ok"):
        raise RuntimeError(str(payload.get("description") or payload))
    result = payload.get("result")
    if not isinstance(result, list):
        return {"result": []}
    return {"result": result}


def _get_updates(token: str, *, offset: int | None, timeout: int) -> list[dict[str, Any]]:
    try:
        payload = _telegram_api_get(
            token,
            "getUpdates",
            {"offset": offset, "timeout": timeout},
        )
    except (OSError, ValueError, RuntimeError, urllib.error.HTTPError) as exc:
        log.warning("Telegram getUpdates failed: %s", exc)
        return []
    res = payload.get("result")
    return [x for x in res if isinstance(x, dict)] if isinstance(res, list) else []


def _extract_message(update: Mapping[str, Any]) -> dict[str, Any] | None:
    for key in ("message", "edited_message"):
        obj = update.get(key)
        if isinstance(obj, dict):
            return obj
    return None


def _chat_id_from_message(msg: Mapping[str, Any]) -> str | None:
    chat = msg.get("chat")
    if not isinstance(chat, Mapping):
        return None
    cid = chat.get("id")
    return None if cid is None else str(cid)


def _handle_command(repo_root: Path, text: str) -> str:
    cmd = parse_telegram_loop_command(text)
    if cmd is None:
        return _help_text()
    if cmd.action == "help":
        return _help_text()
    if cmd.action == "status":
        return _status_text(repo_root)
    if cmd.action == "stop":
        return _stop_loop(repo_root)
    if cmd.action == "run":
        return _start_loop(repo_root, cmd)
    return _help_text()


def poll_and_dispatch(repo_root: Path) -> None:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    allowed_chat_ids = allowed_chat_ids_from_env()
    if not allowed_chat_ids:
        raise RuntimeError("Set TELEGRAM_ALLOWED_CHAT_IDS or TELEGRAM_CHAT_ID")
    timeout = int(os.environ.get("TELEGRAM_CONTROL_TIMEOUT_SECONDS") or "30")
    sleep_s = float(os.environ.get("TELEGRAM_CONTROL_POLL_SECONDS") or "2")
    offset = _load_offset(repo_root)
    log.info("Telegram control started for chats=%s", sorted(allowed_chat_ids))
    while True:
        updates = _get_updates(token, offset=offset, timeout=timeout)
        for upd in updates:
            try:
                upd_id = int(upd.get("update_id"))
            except (TypeError, ValueError):
                continue
            offset = upd_id + 1
            _save_offset(repo_root, offset)
            msg = _extract_message(upd)
            if not msg:
                continue
            chat_id = _chat_id_from_message(msg)
            if not chat_id or chat_id not in allowed_chat_ids:
                if chat_id:
                    send_telegram_to_chat(chat_id, "Unauthorized chat for trading control.")
                continue
            text = str(msg.get("text") or "").strip()
            if not text:
                continue
            reply = _handle_command(repo_root, text)
            send_telegram_to_chat(chat_id, reply)
        if not updates:
            time.sleep(max(0.25, sleep_s))
