"""Tests for UserManager — multi-user loading, validation, and fallback."""

import os
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.config_loader import load_config
from src.user_manager import UserContext, UserManager, resolve_selected_user_id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def base_config():
    """Load the real default.yaml as the base config."""
    return load_config()


@pytest.fixture()
def users_yaml(tmp_path):
    """Helper that writes a users.yaml to a temp dir and returns the path."""
    def _write(content: str) -> Path:
        p = tmp_path / "users.yaml"
        p.write_text(textwrap.dedent(content))
        return p
    return _write


# ---------------------------------------------------------------------------
# Multi-user mode
# ---------------------------------------------------------------------------

class TestMultiUserLoading:

    def test_loads_two_users(self, base_config, users_yaml, monkeypatch):
        monkeypatch.setenv("U1_KEY", "key1")
        monkeypatch.setenv("U1_SECRET", "secret1")
        monkeypatch.setenv("U2_KEY", "key2")
        monkeypatch.setenv("U2_SECRET", "secret2")

        path = users_yaml("""\
            users:
              - id: alice
                alpaca_key_env: U1_KEY
                alpaca_secret_env: U1_SECRET
                paper: true
              - id: bob
                alpaca_key_env: U2_KEY
                alpaca_secret_env: U2_SECRET
                paper: false
        """)

        mgr = UserManager(base_config, users_path=path)

        assert mgr.multi_user is True
        assert len(mgr.list_users()) == 2
        alice = mgr.get_user("alice")
        assert alice.api_key == "key1"
        assert alice.paper is True
        bob = mgr.get_user("bob")
        assert bob.api_key == "key2"
        assert bob.paper is False

    def test_overrides_merged(self, base_config, users_yaml, monkeypatch):
        monkeypatch.setenv("U1_KEY", "key1")
        monkeypatch.setenv("U1_SECRET", "secret1")

        path = users_yaml("""\
            users:
              - id: alice
                alpaca_key_env: U1_KEY
                alpaca_secret_env: U1_SECRET
                paper: true
                overrides:
                  position_sizing:
                    max_position_dollar_cap: 999
        """)

        mgr = UserManager(base_config, users_path=path)
        alice = mgr.get_user("alice")

        # Overridden value
        assert alice.config["position_sizing"]["max_position_dollar_cap"] == 999
        # Non-overridden value preserved from base
        assert alice.config["position_sizing"]["risk_per_trade_pct"] == base_config["position_sizing"]["risk_per_trade_pct"]

    def test_overrides_do_not_mutate_base(self, base_config, users_yaml, monkeypatch):
        monkeypatch.setenv("U1_KEY", "k")
        monkeypatch.setenv("U1_SECRET", "s")
        original_cap = base_config["position_sizing"]["max_position_dollar_cap"]

        path = users_yaml("""\
            users:
              - id: alice
                alpaca_key_env: U1_KEY
                alpaca_secret_env: U1_SECRET
                paper: true
                overrides:
                  position_sizing:
                    max_position_dollar_cap: 1
        """)

        UserManager(base_config, users_path=path)
        assert base_config["position_sizing"]["max_position_dollar_cap"] == original_cap

    def test_user_order_preserved(self, base_config, users_yaml, monkeypatch):
        for i in range(1, 4):
            monkeypatch.setenv(f"K{i}", f"key{i}")
            monkeypatch.setenv(f"S{i}", f"sec{i}")

        path = users_yaml("""\
            users:
              - id: charlie
                alpaca_key_env: K1
                alpaca_secret_env: S1
                paper: true
              - id: alice
                alpaca_key_env: K2
                alpaca_secret_env: S2
                paper: true
              - id: bob
                alpaca_key_env: K3
                alpaca_secret_env: S3
                paper: true
        """)

        mgr = UserManager(base_config, users_path=path)
        ids = [u.user_id for u in mgr.list_users()]
        assert ids == ["charlie", "alice", "bob"]

    def test_selected_user_loads_only_selected_credentials(self, base_config, users_yaml, monkeypatch):
        monkeypatch.setenv("LIVE_KEY", "live_key")
        monkeypatch.setenv("LIVE_SECRET", "live_secret")
        monkeypatch.delenv("PAPER_KEY", raising=False)
        monkeypatch.delenv("PAPER_SECRET", raising=False)

        path = users_yaml("""\
            users:
              - id: live_bot
                alpaca_key_env: LIVE_KEY
                alpaca_secret_env: LIVE_SECRET
                paper: false
              - id: paper_bot
                alpaca_key_env: PAPER_KEY
                alpaca_secret_env: PAPER_SECRET
                paper: true
        """)

        mgr = UserManager(base_config, users_path=path, selected_user_id="live_bot")

        assert [u.user_id for u in mgr.list_users()] == ["live_bot"]
        assert mgr.get_user("live_bot").api_key == "live_key"
        with pytest.raises(KeyError, match="Unknown user_id 'paper_bot'"):
            mgr.get_user("paper_bot")


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------

class TestValidation:

    def test_missing_required_field(self, base_config, users_yaml):
        path = users_yaml("""\
            users:
              - id: alice
                alpaca_key_env: X
                paper: true
        """)
        with pytest.raises(ValueError, match="missing required field.*alpaca_secret_env"):
            UserManager(base_config, users_path=path)

    def test_duplicate_user_id(self, base_config, users_yaml, monkeypatch):
        monkeypatch.setenv("K", "k")
        monkeypatch.setenv("S", "s")

        path = users_yaml("""\
            users:
              - id: alice
                alpaca_key_env: K
                alpaca_secret_env: S
                paper: true
              - id: alice
                alpaca_key_env: K
                alpaca_secret_env: S
                paper: false
        """)
        with pytest.raises(ValueError, match="Duplicate user id 'alice'"):
            UserManager(base_config, users_path=path)

    def test_empty_user_id(self, base_config, users_yaml):
        path = users_yaml("""\
            users:
              - id: ""
                alpaca_key_env: X
                alpaca_secret_env: Y
                paper: true
        """)
        with pytest.raises(ValueError, match="non-empty string"):
            UserManager(base_config, users_path=path)

    def test_paper_not_bool(self, base_config, users_yaml):
        path = users_yaml("""\
            users:
              - id: alice
                alpaca_key_env: X
                alpaca_secret_env: Y
                paper: "yes"
        """)
        with pytest.raises(ValueError, match="must be a boolean"):
            UserManager(base_config, users_path=path)

    def test_missing_env_var(self, base_config, users_yaml, monkeypatch):
        monkeypatch.delenv("NONEXISTENT_VAR", raising=False)
        path = users_yaml("""\
            users:
              - id: alice
                alpaca_key_env: NONEXISTENT_VAR
                alpaca_secret_env: ALSO_MISSING
                paper: true
        """)
        with pytest.raises(EnvironmentError, match="NONEXISTENT_VAR"):
            UserManager(base_config, users_path=path)

    def test_selected_invalid_user_fails_with_available_users(self, base_config, users_yaml):
        path = users_yaml("""\
            users:
              - id: live_bot
                alpaca_key_env: LIVE_KEY
                alpaca_secret_env: LIVE_SECRET
                paper: false
        """)
        with pytest.raises(ValueError, match="Unknown user_id 'paper_bot'.*Available: live_bot"):
            UserManager(base_config, users_path=path, selected_user_id="paper_bot")


class TestUserSelection:

    def test_default_live_bot(self, users_yaml):
        path = users_yaml("""\
            users:
              - id: live_bot
                alpaca_key_env: LIVE_KEY
                alpaca_secret_env: LIVE_SECRET
                paper: false
              - id: paper_bot
                alpaca_key_env: PAPER_KEY
                alpaca_secret_env: PAPER_SECRET
                paper: true
        """)

        assert resolve_selected_user_id(users_path=path, env={}) == "live_bot"

    def test_cli_override_wins_over_env(self, users_yaml):
        path = users_yaml("""\
            users:
              - id: live_bot
                alpaca_key_env: LIVE_KEY
                alpaca_secret_env: LIVE_SECRET
                paper: false
              - id: paper_bot
                alpaca_key_env: PAPER_KEY
                alpaca_secret_env: PAPER_SECRET
                paper: true
        """)

        assert (
            resolve_selected_user_id(
                cli_user="paper_bot",
                env={"ALGO_USER": "live_bot"},
                users_path=path,
            )
            == "paper_bot"
        )

    def test_environment_override(self, users_yaml):
        path = users_yaml("""\
            users:
              - id: live_bot
                alpaca_key_env: LIVE_KEY
                alpaca_secret_env: LIVE_SECRET
                paper: false
              - id: paper_bot
                alpaca_key_env: PAPER_KEY
                alpaca_secret_env: PAPER_SECRET
                paper: true
        """)

        assert resolve_selected_user_id(env={"ALGO_USER": "paper_bot"}, users_path=path) == "paper_bot"

    def test_invalid_user(self, users_yaml):
        path = users_yaml("""\
            users:
              - id: live_bot
                alpaca_key_env: LIVE_KEY
                alpaca_secret_env: LIVE_SECRET
                paper: false
              - id: paper_bot
                alpaca_key_env: PAPER_KEY
                alpaca_secret_env: PAPER_SECRET
                paper: true
        """)

        with pytest.raises(ValueError, match="Unknown user_id 'bad_user'.*live_bot, paper_bot"):
            resolve_selected_user_id(cli_user="bad_user", env={}, users_path=path)

    def test_real_users_yaml_options_allowlist_expanded_for_paper_and_live_pilot_is_inactive(
        self,
        base_config,
        monkeypatch,
    ):
        users_path = Path("config/users.yaml")
        monkeypatch.setenv("ALPACA_LIVE_API_KEY_ID", "live_key")
        monkeypatch.setenv("ALPACA_LIVE_API_SECRET_KEY", "live_secret")
        monkeypatch.setenv("APCA_API_KEY_ID", "paper_key")
        monkeypatch.setenv("APCA_API_SECRET_KEY", "paper_secret")

        live_mgr = UserManager(base_config, users_path=users_path, selected_user_id="live_bot")
        paper_mgr = UserManager(base_config, users_path=users_path, selected_user_id="paper_bot")

        live_options = live_mgr.get_user("live_bot").config["options"]
        paper_options = paper_mgr.get_user("paper_bot").config["options"]
        paper_allowed = paper_options["allowed_underlyings"]
        paper_symbols = paper_options["allowed_symbols"]
        assert live_options["enabled"] is False
        assert live_options["mode"] == "live_long_premium"
        assert live_options["live_pilot"]["enabled"] is False
        assert live_options["live_pilot_enabled"] is False
        assert live_options["total_exposure_limit"] == pytest.approx(0.01)
        assert live_options["max_positions"] == 1
        assert live_options["max_option_positions"] == 1
        assert live_options["max_contracts_per_trade"] == 1
        assert live_options["only_buy_options"] is True
        assert live_options["require_top_signal"] is True
        assert live_options["never_bypass_stock_risk_caps"] is True
        assert live_options["bypass_when_full"]["allow_when_full"] is False
        assert paper_options["enabled"] is False
        assert paper_allowed == [
            "SPY",
            "QQQ",
            "NVDA",
            "AAPL",
            "AMZN",
            "SMH",
            "TSLA",
            "META",
            "MSFT",
            "GOOGL",
            "AMD",
            "AVGO",
            "NFLX",
            "PLTR",
            "CRWD",
            "DDOG",
            "SNOW",
            "ARM",
            "MU",
            "TSM",
            "ADBE",
            "RDDT",
            "COIN",
            "MSTR",
            "HOOD",
            "SOFI",
            "UBER",
            "SHOP",
            "CRM",
            "NOW",
            "PANW",
            "NET",
            "MDB",
            "MRVL",
            "SMCI",
            "ANET",
            "INTC",
            "QCOM",
            "TXN",
            "BA",
            "JPM",
            "GS",
            "LLY",
            "UNH",
            "XOM",
            "CVX",
            "IWM",
            "XBI",
            "ORCL",
            "BABA",
            "INDA",
            "GOOG",
        ]
        required = {"ORCL", "BABA", "INDA", "GOOG", "AMZN", "MU", "TSM", "ANET", "MRVL", "INTC"}
        assert required.issubset(set(paper_allowed))
        assert required.issubset(set(paper_symbols))
        assert paper_symbols == paper_allowed
        assert len(paper_allowed) == len(set(paper_allowed))
        assert len(paper_symbols) == len(set(paper_symbols))


# ---------------------------------------------------------------------------
# Single-user fallback
# ---------------------------------------------------------------------------

class TestSingleUserFallback:

    def test_no_users_yaml(self, base_config, tmp_path):
        """When users.yaml doesn't exist, fall back to single default user."""
        nonexistent = tmp_path / "users.yaml"
        mgr = UserManager(base_config, users_path=nonexistent)

        assert mgr.multi_user is False
        users = mgr.list_users()
        assert len(users) == 1
        assert users[0].user_id == "default"
        assert users[0].paper == base_config.get("broker", {}).get("paper", True)

    def test_empty_users_list(self, base_config, users_yaml):
        """Empty users list in YAML also falls back to single-user."""
        path = users_yaml("""\
            users: []
        """)
        mgr = UserManager(base_config, users_path=path)
        assert mgr.multi_user is False
        assert len(mgr.list_users()) == 1
        assert mgr.list_users()[0].user_id == "default"

    def test_fallback_config_is_base(self, base_config, tmp_path):
        """In single-user mode, config should be the unmodified base config."""
        mgr = UserManager(base_config, users_path=tmp_path / "nope.yaml")
        default = mgr.get_user("default")
        assert default.config is base_config

    def test_get_unknown_user_raises(self, base_config, tmp_path):
        mgr = UserManager(base_config, users_path=tmp_path / "nope.yaml")
        with pytest.raises(KeyError, match="Unknown user_id 'bob'"):
            mgr.get_user("bob")


# ---------------------------------------------------------------------------
# Broker caching (get_broker)
# ---------------------------------------------------------------------------

class TestGetBroker:

    def _patch_broker(self):
        """Context manager to mock AlpacaBroker where get_broker imports it."""
        return patch.dict(
            "sys.modules",
            {"src.brokers.alpaca_client": MagicMock()},
        )

    def test_get_broker_creates_and_caches(self, base_config, users_yaml, monkeypatch):
        monkeypatch.setenv("K1", "key1")
        monkeypatch.setenv("S1", "secret1")

        path = users_yaml("""\
            users:
              - id: alice
                alpaca_key_env: K1
                alpaca_secret_env: S1
                paper: true
        """)

        mgr = UserManager(base_config, users_path=path)

        mock_cls = MagicMock()
        mock_instance = MagicMock(name="broker_alice")
        mock_cls.return_value = mock_instance

        # Inject a fake broker directly into the cache to test caching
        mgr._brokers["alice"] = mock_instance
        broker1 = mgr.get_broker("alice")
        broker2 = mgr.get_broker("alice")

        assert broker1 is broker2
        assert broker1 is mock_instance

    def test_get_broker_passes_user_credentials(self, base_config, users_yaml, monkeypatch):
        monkeypatch.setenv("K1", "alice_key")
        monkeypatch.setenv("S1", "alice_secret")

        path = users_yaml("""\
            users:
              - id: alice
                alpaca_key_env: K1
                alpaca_secret_env: S1
                paper: true
        """)

        mgr = UserManager(base_config, users_path=path)
        mock_cls = MagicMock(name="AlpacaBroker")

        # Patch the import that happens inside get_broker
        import src.brokers.alpaca_client as broker_mod
        original_cls = getattr(broker_mod, "AlpacaBroker", None)
        broker_mod.AlpacaBroker = mock_cls
        try:
            mgr.get_broker("alice")
            mock_cls.assert_called_once_with(
                config=mgr.get_user("alice").config,
                api_key="alice_key",
                secret="alice_secret",
                paper=True,
            )
        finally:
            if original_cls is not None:
                broker_mod.AlpacaBroker = original_cls

    def test_get_broker_separate_instances_per_user(self, base_config, users_yaml, monkeypatch):
        monkeypatch.setenv("K1", "k1")
        monkeypatch.setenv("S1", "s1")
        monkeypatch.setenv("K2", "k2")
        monkeypatch.setenv("S2", "s2")

        path = users_yaml("""\
            users:
              - id: alice
                alpaca_key_env: K1
                alpaca_secret_env: S1
                paper: true
              - id: bob
                alpaca_key_env: K2
                alpaca_secret_env: S2
                paper: false
        """)

        mgr = UserManager(base_config, users_path=path)

        import src.brokers.alpaca_client as broker_mod
        mock_cls = MagicMock(name="AlpacaBroker")
        mock_cls.side_effect = [MagicMock(name="broker_alice"), MagicMock(name="broker_bob")]
        original_cls = getattr(broker_mod, "AlpacaBroker", None)
        broker_mod.AlpacaBroker = mock_cls
        try:
            broker_alice = mgr.get_broker("alice")
            broker_bob = mgr.get_broker("bob")
            assert broker_alice is not broker_bob
            assert mock_cls.call_count == 2
        finally:
            if original_cls is not None:
                broker_mod.AlpacaBroker = original_cls

    def test_get_broker_unknown_user_raises(self, base_config, tmp_path):
        mgr = UserManager(base_config, users_path=tmp_path / "nope.yaml")
        with pytest.raises(KeyError, match="Unknown user_id 'bob'"):
            mgr.get_broker("bob")
