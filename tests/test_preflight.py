from __future__ import annotations

from src.preflight import run_preflight


def _valid_config() -> dict:
    return {
        "broker": {"paper": True},
        "strategy": {"enabled": True},
        "portfolio_risk": {"max_daily_loss_pct": 2.0},
        "market_regime": {"enabled": True},
    }


def test_run_preflight_passes_with_valid_wiring() -> None:
    class Broker:
        def get_clock(self) -> object:
            return object()

    def broker_factory(config: dict) -> Broker:
        assert config["broker"]["paper"] is True
        return Broker()

    report = run_preflight(
        config=_valid_config(),
        broker_factory=broker_factory,
        critical_imports=("src.config_loader",),
        required_definitions={"src.config_loader": ("load_config",)},
        startup_callables=(lambda: None,),
    )

    assert report.ok is True
    assert report.failures == []
    assert report.as_dict()["ok"] is True


def test_run_preflight_uses_broker_market_clock_for_open_readiness() -> None:
    class Broker:
        def __init__(self) -> None:
            self.clock_checked = False

        def get_clock(self) -> object:
            self.clock_checked = True
            return type("Clock", (), {"is_open": True})()

    broker = Broker()

    report = run_preflight(
        config=_valid_config(),
        broker_factory=lambda _config: broker,
        critical_imports=(),
        required_definitions={},
        startup_callables=(),
    )

    broker_check = next(check for check in report.checks if check.name == "broker:init")
    assert report.ok is True
    assert broker.clock_checked is True
    assert broker_check.reason == "get_clock"


def test_run_preflight_catches_import_definition_config_and_startup_failures() -> None:
    def bad_startup() -> None:
        raise AttributeError("missing engine attr")

    report = run_preflight(
        config={"broker": {}},
        critical_imports=("src.config_loader", "src.does_not_exist"),
        required_definitions={"src.config_loader": ("not_a_real_name",)},
        required_config_paths=(("broker",), ("strategy",)),
        startup_callables=(bad_startup,),
    )

    assert report.ok is False
    reasons = {check.name: check.reason for check in report.failures}
    assert "import:src.does_not_exist" in reasons
    assert reasons["definitions:src.config_loader"] == "missing:not_a_real_name"
    assert reasons["config:broker"] == "empty"
    assert reasons["config:strategy"] == "missing"
    assert reasons["startup:bad_startup"].startswith("AttributeError")


def test_run_preflight_reports_broker_initialization_failure() -> None:
    def broker_factory(config: dict) -> object:
        raise NameError("bad broker symbol")

    report = run_preflight(
        config=_valid_config(),
        broker_factory=broker_factory,
        critical_imports=(),
        required_definitions={},
    )

    assert report.ok is False
    assert report.failures == [
        next(check for check in report.checks if check.name == "broker:init")
    ]
    assert report.failures[0].reason.startswith("NameError")
