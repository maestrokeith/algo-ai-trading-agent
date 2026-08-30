"""Multi-user account management for the trading engine.

Loads user definitions from ``config/users.yaml``, resolves Alpaca
credentials from environment variables, and produces a merged config
per user by deep-merging user-specific overrides onto the base
``config/default.yaml``.

When no ``users.yaml`` exists the system falls back to single-user mode
using the standard ``APCA_API_KEY_ID`` / ``APCA_API_SECRET_KEY`` env
vars — fully backward-compatible with the pre-multi-user behaviour.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from src.config_loader import deep_merge, load_config
from src.risk_book_mode import apply_risk_book_mode

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UserContext:
    """Immutable context for a single trading user."""

    user_id: str
    api_key: str
    api_secret: str
    paper: bool
    config: dict[str, Any] = field(repr=False)  # full merged config


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_REQUIRED_USER_FIELDS = {"id", "paper"}


def _validate_user_entry(entry: dict, index: int) -> None:
    """Raise ``ValueError`` if a user entry is missing required fields."""
    missing = _REQUIRED_USER_FIELDS - set(entry.keys())
    if missing:
        raise ValueError(
            f"users[{index}] is missing required field(s): {', '.join(sorted(missing))}"
        )
    if not isinstance(entry["id"], str) or not entry["id"].strip():
        raise ValueError(f"users[{index}].id must be a non-empty string")
    if not isinstance(entry["paper"], bool):
        raise ValueError(
            f"users[{index}].paper must be a boolean (true/false), "
            f"got {type(entry['paper']).__name__}"
        )
    overrides = entry.get("overrides") if isinstance(entry.get("overrides"), dict) else {}
    broker_cfg = overrides.get("broker") if isinstance(overrides.get("broker"), dict) else {}
    provider = str(broker_cfg.get("provider") or broker_cfg.get("firm") or "alpaca").strip().lower()
    if provider == "alpaca":
        missing_alpaca = {"alpaca_key_env", "alpaca_secret_env"} - set(entry.keys())
        if missing_alpaca:
            raise ValueError(
                f"users[{index}] is missing required field(s): {', '.join(sorted(missing_alpaca))}"
            )


def _resolve_env(var_name: str, user_id: str, field_label: str) -> str:
    """Resolve an environment variable or raise with a helpful message."""
    value = os.environ.get(var_name)
    if not value:
        raise EnvironmentError(
            f"Environment variable '{var_name}' (for user '{user_id}' "
            f"{field_label}) is not set or is empty."
        )
    return value


# ---------------------------------------------------------------------------
# UserManager
# ---------------------------------------------------------------------------

class UserManager:
    """Load and manage multi-user trading contexts.

    Parameters
    ----------
    base_config : dict
        The base configuration loaded from ``config/default.yaml``.
    users_path : str | Path | None
        Path to ``users.yaml``.  When *None*, the manager resolves the
        default location relative to the project root
        (``config/users.yaml``).
    """

    def __init__(
        self,
        base_config: dict[str, Any],
        users_path: str | Path | None = None,
        selected_user_id: str | None = None,
    ) -> None:
        self._base_config = base_config
        self._users_path = self._resolve_users_path(users_path)
        self._selected_user_id = str(selected_user_id).strip() if selected_user_id else None
        self._users: dict[str, UserContext] = {}
        self._brokers: dict[str, Any] = {}
        self._multi_user: bool = False
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def multi_user(self) -> bool:
        """True when running in multi-user mode (users.yaml found)."""
        return self._multi_user

    def list_users(self) -> list[UserContext]:
        """Return all loaded users (order preserved from YAML)."""
        return list(self._users.values())

    def get_user(self, user_id: str) -> UserContext:
        """Return the ``UserContext`` for *user_id* or raise ``KeyError``."""
        try:
            return self._users[user_id]
        except KeyError:
            available = ", ".join(self._users) or "(none)"
            raise KeyError(
                f"Unknown user_id '{user_id}'. Available: {available}"
            ) from None

    def get_broker(self, user_id: str) -> Any:
        """Return a cached broker adapter for *user_id*.

        The broker is lazily created on first call and reused for all
        subsequent requests.  Each user gets their own isolated broker
        instance with their own credentials or authenticated transport.
        """
        if user_id in self._brokers:
            return self._brokers[user_id]

        from src.brokers.broker_factory import get_broker

        user = self.get_user(user_id)
        broker = get_broker(
            config=user.config,
            api_key=user.api_key,
            secret=user.api_secret,
            paper=user.paper,
        )
        self._brokers[user_id] = broker
        logger.info(
            "[%s] Broker initialised provider=%s paper=%s",
            user_id,
            getattr(broker, "broker_name", type(broker).__name__),
            user.paper,
        )
        return broker

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_users_path(users_path: str | Path | None) -> Path:
        if users_path is not None:
            return Path(users_path)
        return Path(__file__).resolve().parent.parent / "config" / "users.yaml"

    def _load(self) -> None:
        if not self._users_path.exists():
            logger.info(
                "No users.yaml found at %s — running in single-user mode.",
                self._users_path,
            )
            self._load_single_user_fallback()
            return

        raw = self._read_users_yaml()
        entries = raw.get("users")
        if not entries:
            logger.warning(
                "users.yaml found but 'users' list is empty — "
                "falling back to single-user mode."
            )
            self._load_single_user_fallback()
            return

        self._multi_user = True
        seen_ids: set[str] = set()
        validated_entries: list[dict[str, Any]] = []
        for idx, entry in enumerate(entries):
            _validate_user_entry(entry, idx)
            uid = entry["id"]
            if uid in seen_ids:
                raise ValueError(f"Duplicate user id '{uid}' in users.yaml")
            seen_ids.add(uid)
            validated_entries.append(entry)

        if self._selected_user_id is not None and self._selected_user_id not in seen_ids:
            available = ", ".join(sorted(seen_ids)) or "(none)"
            raise ValueError(
                f"Unknown user_id '{self._selected_user_id}' in {self._users_path}. "
                f"Available: {available}"
            )

        for entry in validated_entries:
            uid = entry["id"]
            if self._selected_user_id is not None and uid != self._selected_user_id:
                continue
            self._users[uid] = self._build_user_context(entry)

        logger.info(
            "Loaded %d user(s) from %s: %s",
            len(self._users),
            self._users_path,
            ", ".join(
                f"{u.user_id} ({'paper' if u.paper else 'LIVE'})"
                for u in self._users.values()
            ),
        )

    def _read_users_yaml(self) -> dict:
        with open(self._users_path) as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise ValueError(
                f"users.yaml must be a YAML mapping, got {type(data).__name__}"
            )
        return data

    def _build_user_context(self, entry: dict) -> UserContext:
        uid = entry["id"]
        paper = entry["paper"]

        overrides = entry.get("overrides") or {}
        merged_config = deep_merge(self._base_config, overrides)
        apply_risk_book_mode(merged_config)
        broker_cfg = merged_config.get("broker") if isinstance(merged_config.get("broker"), dict) else {}
        provider = str(broker_cfg.get("provider") or broker_cfg.get("firm") or "alpaca").strip().lower()
        if provider == "alpaca":
            api_key = _resolve_env(entry["alpaca_key_env"], uid, "API key")
            api_secret = _resolve_env(entry["alpaca_secret_env"], uid, "API secret")
        else:
            api_key = ""
            api_secret = ""

        return UserContext(
            user_id=uid,
            api_key=api_key,
            api_secret=api_secret,
            paper=paper,
            config=merged_config,
        )

    def _load_single_user_fallback(self) -> None:
        """Create a single 'default' user from standard env vars.

        In single-user mode we try to resolve credentials from the
        environment.  If the env vars are not set we still create the
        context with empty strings — the broker will raise its own error
        at connection time, preserving the existing behaviour.
        """
        self._multi_user = False

        paper = self._base_config.get("broker", {}).get("paper", True)

        if paper:
            api_key = os.environ.get("APCA_API_KEY_ID", "")
            api_secret = os.environ.get("APCA_API_SECRET_KEY", "")
        else:
            api_key = os.environ.get("ALPACA_LIVE_API_KEY_ID", "")
            api_secret = os.environ.get("ALPACA_LIVE_API_SECRET_KEY", "")

        self._users["default"] = UserContext(
            user_id="default",
            api_key=api_key,
            api_secret=api_secret,
            paper=paper,
            config=self._base_config,
        )
        logger.info(
            "Single-user mode: user_id='default', paper=%s", paper
        )


DEFAULT_SELECTED_USER_ID = "live_bot"


def load_user_ids(users_path: str | Path | None = None) -> list[str]:
    """Return user ids from ``config/users.yaml`` without resolving credentials."""

    path = UserManager._resolve_users_path(users_path)
    if not path.exists():
        return ["default"]
    with open(path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"users.yaml must be a YAML mapping, got {type(data).__name__}")
    entries = data.get("users") or []
    ids: list[str] = []
    seen: set[str] = set()
    for idx, entry in enumerate(entries):
        _validate_user_entry(entry, idx)
        uid = entry["id"]
        if uid in seen:
            raise ValueError(f"Duplicate user id '{uid}' in users.yaml")
        seen.add(uid)
        ids.append(uid)
    return ids or ["default"]


def resolve_selected_user_id(
    *,
    cli_user: str | None = None,
    env: dict[str, str] | None = None,
    users_path: str | Path | None = None,
    default_user: str = DEFAULT_SELECTED_USER_ID,
) -> str:
    """Resolve active user with CLI > ``ALGO_USER`` > ``live_bot`` order."""

    source_env = env if env is not None else os.environ
    selected = str(cli_user or "").strip()
    if not selected:
        selected = str(source_env.get("ALGO_USER") or "").strip()
    if not selected:
        selected = default_user
    available = load_user_ids(users_path)
    if selected not in available:
        raise ValueError(
            f"Unknown user_id '{selected}'. Available users in config/users.yaml: {', '.join(available)}"
        )
    return selected
