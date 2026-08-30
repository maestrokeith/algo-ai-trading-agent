"""Market-open readiness smoke tests for AlgoSphere startup."""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.config_loader import load_config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreflightCheck:
    """One readiness check result."""

    name: str
    ok: bool
    reason: str = "ok"


@dataclass(frozen=True)
class PreflightReport:
    """Aggregated preflight status."""

    ok: bool
    checks: list[PreflightCheck] = field(default_factory=list)

    @property
    def failures(self) -> list[PreflightCheck]:
        """Failed checks only."""
        return [check for check in self.checks if not check.ok]

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable report."""
        return {
            "ok": self.ok,
            "checks": [
                {"name": check.name, "ok": check.ok, "reason": check.reason}
                for check in self.checks
            ],
        }


CRITICAL_IMPORTS: tuple[str, ...] = (
    "src.config_loader",
    "src.user_manager",
    "src.brokers.alpaca_client",
    "src.position_tracker",
    "src.position_sizing",
    "src.portfolio_risk",
    "src.compliance",
    "src.execution",
    "src.universe",
    "src.market_regime",
    "src.strategy",
    "src.trading_engine",
)

REQUIRED_DEFINITIONS: Mapping[str, tuple[str, ...]] = {
    "src.config_loader": ("load_config", "deep_merge"),
    "src.user_manager": ("UserManager", "UserContext"),
    "src.brokers.alpaca_client": ("AlpacaBroker",),
    "src.position_tracker": ("load", "save", "add"),
    "src.portfolio_risk": ("PortfolioRiskManager", "PortfolioRiskState"),
    "src.market_regime": ("MarketRegimeScorer", "RegimeResult"),
    "src.trading_engine": ("TradingEngine",),
}

REQUIRED_CONFIG_PATHS: tuple[tuple[str, ...], ...] = (
    ("broker",),
    ("strategy",),
    ("portfolio_risk",),
    ("market_regime",),
)


def _check_imports(module_names: Sequence[str]) -> list[PreflightCheck]:
    checks: list[PreflightCheck] = []
    for module_name in module_names:
        try:
            importlib.import_module(module_name)
        except Exception as exc:
            checks.append(
                PreflightCheck(
                    f"import:{module_name}",
                    False,
                    f"{type(exc).__name__}: {exc}",
                )
            )
        else:
            checks.append(PreflightCheck(f"import:{module_name}", True))
    return checks


def _check_definitions(required: Mapping[str, Sequence[str]]) -> list[PreflightCheck]:
    checks: list[PreflightCheck] = []
    for module_name, names in required.items():
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            checks.append(
                PreflightCheck(
                    f"definitions:{module_name}",
                    False,
                    f"module_unavailable:{type(exc).__name__}",
                )
            )
            continue
        missing = [name for name in names if not hasattr(module, name)]
        if missing:
            checks.append(
                PreflightCheck(
                    f"definitions:{module_name}",
                    False,
                    f"missing:{','.join(missing)}",
                )
            )
        else:
            checks.append(PreflightCheck(f"definitions:{module_name}", True))
    return checks


def _get_nested(config: Mapping[str, Any], path: Sequence[str]) -> Any:
    node: Any = config
    for part in path:
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _check_config(config: Mapping[str, Any], paths: Sequence[Sequence[str]]) -> list[PreflightCheck]:
    checks: list[PreflightCheck] = []
    for path in paths:
        value = _get_nested(config, path)
        label = ".".join(path)
        if value is None:
            checks.append(PreflightCheck(f"config:{label}", False, "missing"))
        elif isinstance(value, Mapping) and not value:
            checks.append(PreflightCheck(f"config:{label}", False, "empty"))
        else:
            checks.append(PreflightCheck(f"config:{label}", True))
    return checks


def _check_broker_factory(
    broker_factory: Callable[[Mapping[str, Any]], Any] | None,
    config: Mapping[str, Any],
) -> PreflightCheck:
    if broker_factory is None:
        return PreflightCheck("broker:init", True, "skipped")
    try:
        broker = broker_factory(config)
        for method_name in ("get_account_snapshot", "get_clock", "get_equity"):
            method = getattr(broker, method_name, None)
            if callable(method):
                method()
                return PreflightCheck("broker:init", True, method_name)
    except Exception as exc:
        return PreflightCheck("broker:init", False, f"{type(exc).__name__}: {exc}")
    return PreflightCheck("broker:init", False, "no_health_read_method")


def _check_startup_callables(callables: Sequence[Callable[[], Any]]) -> list[PreflightCheck]:
    checks: list[PreflightCheck] = []
    for idx, startup_callable in enumerate(callables, start=1):
        name = getattr(startup_callable, "__name__", f"startup_{idx}")
        try:
            startup_callable()
        except Exception as exc:
            checks.append(
                PreflightCheck(
                    f"startup:{name}",
                    False,
                    f"{type(exc).__name__}: {exc}",
                )
            )
        else:
            checks.append(PreflightCheck(f"startup:{name}", True))
    return checks


def run_preflight(
    *,
    config_path: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
    broker_factory: Callable[[Mapping[str, Any]], Any] | None = None,
    startup_callables: Sequence[Callable[[], Any]] = (),
    critical_imports: Sequence[str] = CRITICAL_IMPORTS,
    required_definitions: Mapping[str, Sequence[str]] = REQUIRED_DEFINITIONS,
    required_config_paths: Sequence[Sequence[str]] = REQUIRED_CONFIG_PATHS,
) -> PreflightReport:
    """Run market-open readiness checks without placing orders."""
    checks: list[PreflightCheck] = []
    checks.extend(_check_imports(critical_imports))
    checks.extend(_check_definitions(required_definitions))

    loaded_config: Mapping[str, Any]
    if config is not None:
        loaded_config = config
        checks.append(PreflightCheck("config:load", True, "provided"))
    else:
        try:
            loaded_config = load_config(config_path)
        except Exception as exc:
            loaded_config = {}
            checks.append(
                PreflightCheck("config:load", False, f"{type(exc).__name__}: {exc}")
            )
        else:
            checks.append(PreflightCheck("config:load", True))

    checks.extend(_check_config(loaded_config, required_config_paths))
    checks.append(_check_broker_factory(broker_factory, loaded_config))
    checks.extend(_check_startup_callables(startup_callables))

    report = PreflightReport(ok=all(check.ok for check in checks), checks=checks)
    if not report.ok:
        logger.error("Preflight failed: %s", report.failures)
    return report


__all__ = ["PreflightCheck", "PreflightReport", "run_preflight"]
