"""Tests for loop_helpers — multi-user loop context, init, error isolation."""

import logging
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.loop_helpers import (
    UserLoopContext,
    alpaca_pdt_exit_hint_line,
    entry_scan_allowed_et,
    init_user_contexts,
    is_alpaca_pdt_trade_denial,
    log_startup_summary,
    minutes_since_last_recorded_exit,
    parse_cli_args,
    effective_per_symbol_buy_cooldown_min,
    parse_per_symbol_buy_cooldown_min,
    parse_per_symbol_sell_cooldown_min,
    reduce_only_mode_exit_interval_minutes,
    resolve_dynamic_momentum_intervals,
    resolve_live_loop_intervals,
    run_user_pass,
)
from src.trading_engine import TradingEngine
from src.user_manager import UserContext


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_user_manager():
    """Return a mock UserManager with two users."""
    mgr = MagicMock()
    mgr.multi_user = True

    alice_ctx = UserContext(
        user_id="alice",
        api_key="k1",
        api_secret="s1",
        paper=True,
        config={"broker": {"firm": "alpaca"}},
    )
    bob_ctx = UserContext(
        user_id="bob",
        api_key="k2",
        api_secret="s2",
        paper=False,
        config={"broker": {"firm": "alpaca"}},
    )

    mgr.list_users.return_value = [alice_ctx, bob_ctx]
    mgr.get_user.side_effect = lambda uid: {"alice": alice_ctx, "bob": bob_ctx}[uid]
    mgr.get_broker.side_effect = lambda uid: MagicMock(name=f"broker_{uid}")

    return mgr


@pytest.fixture()
def single_user_manager():
    """Return a mock UserManager with single default user."""
    mgr = MagicMock()
    mgr.multi_user = False

    default_ctx = UserContext(
        user_id="default",
        api_key="k",
        api_secret="s",
        paper=True,
        config={"broker": {"firm": "alpaca"}},
    )

    mgr.list_users.return_value = [default_ctx]
    mgr.get_user.side_effect = lambda uid: default_ctx if uid == "default" else (_ for _ in ()).throw(KeyError(uid))
    mgr.get_broker.return_value = MagicMock(name="broker_default")

    return mgr


# ---------------------------------------------------------------------------
# UserLoopContext
# ---------------------------------------------------------------------------

class TestUserLoopContext:

    def test_fields(self):
        ctx = UserLoopContext(
            user_id="alice",
            user_ctx=MagicMock(),
            broker=MagicMock(),
            engine=MagicMock(),
            config={"broker": {}},
            paper=True,
            data_dir=Path("/tmp/data"),
        )
        assert ctx.user_id == "alice"
        assert ctx.paper is True
        assert ctx.data_dir == Path("/tmp/data")

    def test_data_dir_defaults_none(self):
        ctx = UserLoopContext(
            user_id="bob",
            user_ctx=MagicMock(),
            broker=MagicMock(),
            engine=MagicMock(),
            config={},
            paper=False,
        )
        assert ctx.data_dir is None


# ---------------------------------------------------------------------------
# init_user_contexts
# ---------------------------------------------------------------------------

class TestInitUserContexts:

    @patch("src.loop_helpers.TradingEngine")
    def test_creates_contexts_for_all_users(self, MockEngine, mock_user_manager):
        MockEngine.return_value = MagicMock(name="engine")
        contexts = init_user_contexts(mock_user_manager, project_root=Path("/tmp/proj"))
        assert len(contexts) == 2
        assert contexts[0].user_id == "alice"
        assert contexts[1].user_id == "bob"
        assert contexts[0].paper is True
        assert contexts[1].paper is False
        assert contexts[0].data_dir == Path("/tmp/proj/data")

    @patch("src.loop_helpers.TradingEngine")
    def test_user_filter(self, MockEngine, mock_user_manager):
        MockEngine.return_value = MagicMock(name="engine")
        contexts = init_user_contexts(
            mock_user_manager,
            project_root=Path("/tmp/proj"),
            user_filter="alice",
        )
        assert len(contexts) == 1
        assert contexts[0].user_id == "alice"

    @patch("src.loop_helpers.TradingEngine")
    def test_user_filter_unknown_raises(self, MockEngine, mock_user_manager):
        mock_user_manager.get_user.side_effect = KeyError("Unknown user_id 'charlie'")
        with pytest.raises(KeyError, match="charlie"):
            init_user_contexts(
                mock_user_manager,
                project_root=Path("/tmp/proj"),
                user_filter="charlie",
            )

    @patch("src.loop_helpers.TradingEngine")
    def test_single_user_fallback(self, MockEngine, single_user_manager):
        MockEngine.return_value = MagicMock(name="engine")
        contexts = init_user_contexts(single_user_manager, project_root=Path("/tmp/proj"))
        assert len(contexts) == 1
        assert contexts[0].user_id == "default"

    @patch("src.loop_helpers.TradingEngine")
    def test_broker_failure_skips_user(self, MockEngine, mock_user_manager, caplog):
        MockEngine.return_value = MagicMock(name="engine")
        # Alice's broker raises, Bob's works
        mock_user_manager.get_broker.side_effect = [
            RuntimeError("auth failed"),
            MagicMock(name="broker_bob"),
        ]
        with caplog.at_level(logging.ERROR):
            contexts = init_user_contexts(mock_user_manager, project_root=Path("/tmp/proj"))
        assert len(contexts) == 1
        assert contexts[0].user_id == "bob"

    @patch("src.loop_helpers.TradingEngine")
    def test_engine_failure_skips_user(self, MockEngine, mock_user_manager, caplog):
        call_count = [0]
        def side_effect(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ValueError("bad config")
            return MagicMock(name="engine")

        MockEngine.side_effect = side_effect
        with caplog.at_level(logging.ERROR):
            contexts = init_user_contexts(mock_user_manager, project_root=Path("/tmp/proj"))
        assert len(contexts) == 1
        assert contexts[0].user_id == "bob"


# ---------------------------------------------------------------------------
# log_startup_summary
# ---------------------------------------------------------------------------

class TestLogStartupSummary:

    def test_logs_users(self, caplog):
        ctx1 = UserLoopContext(
            user_id="alice", user_ctx=MagicMock(), broker=MagicMock(),
            engine=MagicMock(), config={}, paper=True,
        )
        ctx2 = UserLoopContext(
            user_id="bob", user_ctx=MagicMock(), broker=MagicMock(),
            engine=MagicMock(), config={}, paper=False,
        )
        with caplog.at_level(logging.INFO):
            log_startup_summary([ctx1, ctx2])
        assert "2 user(s)" in caplog.text
        assert "alice" in caplog.text
        assert "PAPER" in caplog.text
        assert "bob" in caplog.text
        assert "LIVE" in caplog.text

    def test_logs_warning_empty(self, caplog):
        with caplog.at_level(logging.WARNING):
            log_startup_summary([])
        assert "No user contexts" in caplog.text


# ---------------------------------------------------------------------------
# run_user_pass — error isolation
# ---------------------------------------------------------------------------

class TestRunUserPass:

    def _ctx(self, uid="alice"):
        return UserLoopContext(
            user_id=uid, user_ctx=MagicMock(), broker=MagicMock(),
            engine=MagicMock(), config={}, paper=True,
        )

    def test_success_returns_true(self):
        ctx = self._ctx()
        callback = MagicMock()
        result = run_user_pass(ctx, callback)
        assert result is True
        callback.assert_called_once_with(ctx)

    def test_passes_kwargs(self):
        ctx = self._ctx()
        callback = MagicMock()
        run_user_pass(ctx, callback, dt="now", verbose=True)
        callback.assert_called_once_with(ctx, dt="now", verbose=True)

    def test_exception_returns_false(self, caplog):
        ctx = self._ctx()
        callback = MagicMock(side_effect=RuntimeError("broker down"))
        with caplog.at_level(logging.ERROR):
            result = run_user_pass(ctx, callback)
        assert result is False
        assert "alice" in caplog.text
        assert "Error during trading pass" in caplog.text

    def test_exception_does_not_propagate(self):
        ctx = self._ctx()
        callback = MagicMock(side_effect=Exception("fatal"))
        # Should NOT raise
        result = run_user_pass(ctx, callback)
        assert result is False

    def test_multiple_users_one_fails(self):
        """Simulate iterating over users: one fails, other succeeds."""
        ctxs = [self._ctx("alice"), self._ctx("bob")]
        results = []
        for ctx in ctxs:
            if ctx.user_id == "alice":
                cb = MagicMock(side_effect=RuntimeError("alice broker down"))
            else:
                cb = MagicMock()
            results.append(run_user_pass(ctx, cb))
        assert results == [False, True]


# ---------------------------------------------------------------------------
# Alpaca PDT exit helpers
# ---------------------------------------------------------------------------

class TestAlpacaPDTExitHelpers:

    def test_is_pdt_by_code_in_string(self):
        exc = RuntimeError('APIError {"code":40310100,"message":"trade denied due to pattern day')
        assert is_alpaca_pdt_trade_denial(exc) is True

    def test_is_pdt_by_message(self):
        assert is_alpaca_pdt_trade_denial(ValueError("trade denied — pattern day restrictions")) is True

    def test_not_pdt_generic(self):
        assert is_alpaca_pdt_trade_denial(RuntimeError("network timeout")) is False

    def test_hint_non_empty(self):
        assert "PDT" in alpaca_pdt_exit_hint_line()


# ---------------------------------------------------------------------------
# parse_cli_args
# ---------------------------------------------------------------------------

class TestParseCLIArgs:

    def test_defaults(self):
        args = parse_cli_args([])
        assert args.live is False
        assert args.paper is False
        assert args.user is None
        assert args.verbose is False

    def test_live_flag(self):
        args = parse_cli_args(["--live"])
        assert args.live is True

    def test_paper_flag(self):
        args = parse_cli_args(["--paper"])
        assert args.paper is True

    def test_user_flag(self):
        args = parse_cli_args(["--user", "alice"])
        assert args.user == "alice"

    def test_verbose_short(self):
        args = parse_cli_args(["-v"])
        assert args.verbose is True

    def test_combined_flags(self):
        args = parse_cli_args(["--paper", "--user", "bob", "-v"])
        assert args.paper is True
        assert args.user == "bob"
        assert args.verbose is True


# ---------------------------------------------------------------------------
# Per-symbol entry cooldown config (live loop)
# ---------------------------------------------------------------------------


class TestPerSymbolCooldownParsers:
    def test_buy_prefers_new_key(self) -> None:
        assert (
            parse_per_symbol_buy_cooldown_min(
                {"per_symbol_buy_cooldown_min": 10, "min_minutes_since_last_entry_for_symbol": 99}
            )
            == 10.0
        )

    def test_buy_legacy_alias(self) -> None:
        assert (
            parse_per_symbol_buy_cooldown_min({"min_minutes_since_last_entry_for_symbol": 20})
            == 20.0
        )

    def test_buy_symbol_cooldown_minutes_fallback(self) -> None:
        assert parse_per_symbol_buy_cooldown_min({"symbol_cooldown_minutes": 30}) == 30.0
        assert (
            parse_per_symbol_buy_cooldown_min(
                {"symbol_cooldown_minutes": 30, "min_minutes_since_last_entry_for_symbol": 99}
            )
            == 30.0
        )

    def test_buy_per_symbol_wins_over_symbol_cooldown_minutes(self) -> None:
        assert (
            parse_per_symbol_buy_cooldown_min(
                {"per_symbol_buy_cooldown_min": 10, "symbol_cooldown_minutes": 30}
            )
            == 10.0
        )

    def test_leader_cooldown_overrides(self) -> None:
        ec = {
            "symbol_cooldown_minutes": 30,
            "leader_cooldown_overrides": {"NVDA": 15, "amzn": 20},
        }
        assert effective_per_symbol_buy_cooldown_min(ec, "SPY") == 30.0
        assert effective_per_symbol_buy_cooldown_min(ec, "NVDA") == 15.0
        assert effective_per_symbol_buy_cooldown_min(ec, "AMZN") == 20.0

    def test_sell_default_zero(self) -> None:
        assert parse_per_symbol_sell_cooldown_min({}) == 0.0

    def test_sell_parsed(self) -> None:
        assert parse_per_symbol_sell_cooldown_min({"per_symbol_sell_cooldown_min": 20}) == 20.0


class TestEntryScanAllowedEt:
    noon = datetime(2026, 4, 28, 12, 0, 0)
    pre_open = datetime(2026, 4, 28, 9, 30, 0)
    late = datetime(2026, 4, 28, 15, 30, 0)

    def test_disabled_entries(self) -> None:
        assert entry_scan_allowed_et(self.noon, {"enable_new_entries": False}) is False

    def test_before_avoid_before_blocked(self) -> None:
        ec = {"avoid_new_entries_before": "09:40"}
        assert entry_scan_allowed_et(self.pre_open, ec) is False

    def test_after_avoid_before_allowed(self) -> None:
        ec = {"avoid_new_entries_before": "09:40"}
        assert entry_scan_allowed_et(self.noon, ec) is True

    def test_last_hour_blocked(self) -> None:
        ec = {"avoid_new_entries_after": "15:00"}
        assert entry_scan_allowed_et(self.late, ec) is False

    def test_last_hour_still_ok_before_cutoff(self) -> None:
        ec = {"avoid_new_entries_after": "15:00"}
        assert entry_scan_allowed_et(self.noon, ec) is True

    def test_after_alias_et(self) -> None:
        ec = {"avoid_new_entries_after_et": "15:00"}
        assert entry_scan_allowed_et(self.late, ec) is False


class TestMinutesSinceLastRecordedExit:
    def test_none_when_no_exit(self) -> None:
        eng = TradingEngine({"strategy": {"exits": {}}})
        assert minutes_since_last_recorded_exit(eng, "SPY", datetime(2026, 1, 1, 12, 0, 0)) is None

    def test_uses_latest_of_stop_and_profit(self) -> None:
        eng = TradingEngine({"strategy": {"exits": {}}})
        t_stop = datetime(2026, 1, 1, 10, 0, 0)
        t_profit = datetime(2026, 1, 1, 11, 0, 0)
        eng.state.last_stop_loss_at["SPY"] = t_stop
        eng.state.last_profit_exit_at["SPY"] = t_profit
        now = datetime(2026, 1, 1, 11, 30, 0)
        m = minutes_since_last_recorded_exit(eng, "SPY", now)
        assert m is not None and 29.0 < m < 31.0

    def test_case_insensitive_symbol(self) -> None:
        eng = TradingEngine({"strategy": {"exits": {}}})
        eng.state.last_profit_exit_at["qqq"] = datetime(2026, 1, 1, 10, 0, 0)
        m = minutes_since_last_recorded_exit(eng, "QQQ", datetime(2026, 1, 1, 10, 15, 0))
        assert m is not None and 14.0 < m < 16.0


def test_resolve_dynamic_momentum_intervals() -> None:
    assert resolve_dynamic_momentum_intervals({}) == (None, None)
    assert resolve_dynamic_momentum_intervals(
        {"dynamic_universe": {"enabled": False, "entry_check_interval_minutes": 1}}
    ) == (None, None)
    assert resolve_dynamic_momentum_intervals(
        {
            "dynamic_universe": {
                "enabled": True,
                "entry_check_interval_minutes": 3,
                "exit_check_interval_minutes": 5,
            }
        }
    ) == (3, 5)
    assert resolve_dynamic_momentum_intervals(
        {
            "dynamic_universe": {
                "enabled": True,
                "entry_check_interval_minutes": "",
            }
        }
    ) == (None, None)


class TestResolveLiveLoopIntervals:
    def test_defaults(self) -> None:
        assert resolve_live_loop_intervals({}) == (12, 10)
        assert resolve_live_loop_intervals(None) == (12, 10)

    def test_broker_only(self) -> None:
        cfg = {"broker": {"exit_check_interval_minutes": 15, "entry_check_interval_minutes": 20}}
        assert resolve_live_loop_intervals(cfg) == (15, 20)

    def test_legacy_check_interval_minutes(self) -> None:
        cfg = {"broker": {"check_interval_minutes": 10}}
        assert resolve_live_loop_intervals(cfg) == (10, 10)

    def test_timing_overrides_broker(self) -> None:
        cfg = {
            "timing": {"exit_interval_min": 15, "entry_interval_min": 10},
            "broker": {"exit_check_interval_minutes": 5, "entry_check_interval_minutes": 99},
        }
        assert resolve_live_loop_intervals(cfg) == (15, 10)

    def test_timing_partial_uses_broker_for_missing(self) -> None:
        cfg = {"timing": {"exit_interval_min": 14}, "broker": {"entry_check_interval_minutes": 11}}
        assert resolve_live_loop_intervals(cfg) == (14, 11)

    def test_minimum_one_minute(self) -> None:
        assert resolve_live_loop_intervals({"timing": {"exit_interval_min": 0, "entry_interval_min": 0}}) == (1, 1)


def test_reduce_only_mode_exit_interval_defaults() -> None:
    assert reduce_only_mode_exit_interval_minutes({}) == 5
    assert reduce_only_mode_exit_interval_minutes(None) == 5


def test_reduce_only_mode_exit_interval_from_timing() -> None:
    cfg = {
        "timing": {
            "reduce_only_mode": {"exit_interval_minutes": 7},
        }
    }
    assert reduce_only_mode_exit_interval_minutes(cfg) == 7


def test_reduce_only_mode_exit_interval_top_level_fallback() -> None:
    cfg = {"reduce_only_mode": {"exit_interval_minutes": 3}}
    assert reduce_only_mode_exit_interval_minutes(cfg) == 3
