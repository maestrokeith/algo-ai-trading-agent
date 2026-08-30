"""Tests for portfolio_risk — single-user and multi-user wrappers."""

import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from src.portfolio_risk import (
    MultiUserPortfolioRiskManager,
    PortfolioRiskManager,
    PortfolioRiskState,
    note_live_order_for_daily_risk,
)


# ---------------------------------------------------------------------------
# PortfolioRiskState defaults
# ---------------------------------------------------------------------------

class TestPortfolioRiskState:

    def test_defaults(self):
        s = PortfolioRiskState()
        assert s.equity_curve == []
        assert s.peak_equity == 0.0
        assert s.daily_pnl_pct == 0.0
        assert s.daily_trade_count == 0
        assert s.daily_trades_per_symbol == {}
        assert s.daily_round_trips_per_symbol == {}
        assert s.last_trade_date is None
        assert s.safe_mode is False
        assert s.trading_stopped_for_day is False
        assert s.disable_trading is False

    def test_disable_trading_follows_trading_stopped_for_day(self):
        s = PortfolioRiskState(trading_stopped_for_day=True)
        assert s.disable_trading is True


# ---------------------------------------------------------------------------
# PortfolioRiskManager — core logic
# ---------------------------------------------------------------------------

class TestUpdateEquity:

    def test_appends_to_curve(self):
        mgr = PortfolioRiskManager({})
        state = PortfolioRiskState()
        dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
        mgr.update_equity(state, dt, 10_000.0)
        assert len(state.equity_curve) == 1
        assert state.equity_curve[0] == (dt, 10_000.0)

    def test_updates_peak(self):
        mgr = PortfolioRiskManager({})
        state = PortfolioRiskState()
        dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
        mgr.update_equity(state, dt, 10_000.0)
        assert state.peak_equity == 10_000.0
        mgr.update_equity(state, dt, 9_000.0)
        assert state.peak_equity == 10_000.0  # peak not lowered
        mgr.update_equity(state, dt, 11_000.0)
        assert state.peak_equity == 11_000.0


class TestCurrentDrawdownPct:

    def test_zero_peak(self):
        mgr = PortfolioRiskManager({})
        state = PortfolioRiskState()
        assert mgr.current_drawdown_pct(state, 5_000.0) == 0.0

    def test_no_drawdown(self):
        mgr = PortfolioRiskManager({})
        state = PortfolioRiskState(peak_equity=10_000.0)
        assert mgr.current_drawdown_pct(state, 10_000.0) == 0.0

    def test_ten_percent_drawdown(self):
        mgr = PortfolioRiskManager({})
        state = PortfolioRiskState(peak_equity=10_000.0)
        dd = mgr.current_drawdown_pct(state, 9_000.0)
        assert dd == pytest.approx(-10.0)


class TestDrawdownSizeMultiplier:

    def test_zero_peak_returns_full_size(self):
        mgr = PortfolioRiskManager({})
        state = PortfolioRiskState()
        assert mgr.drawdown_size_multiplier(state, 5_000.0) == 1.0

    def test_no_drawdown_full_size(self):
        mgr = PortfolioRiskManager({})
        state = PortfolioRiskState(peak_equity=100_000.0)
        assert mgr.drawdown_size_multiplier(state, 100_000.0) == 1.0

    def test_tiers(self):
        mgr = PortfolioRiskManager({})
        peak = 100_000.0
        state = PortfolioRiskState(peak_equity=peak)
        # -8% and worse → 0.50
        assert mgr.drawdown_size_multiplier(state, 91_999.0) == pytest.approx(0.50)
        # (-7.9%, -5%] → 0.70
        assert mgr.drawdown_size_multiplier(state, 92_100.0) == pytest.approx(0.70)
        assert mgr.drawdown_size_multiplier(state, 95_000.0) == pytest.approx(0.70)
        # (-4.9%, -3%] → 0.85
        assert mgr.drawdown_size_multiplier(state, 95_100.0) == pytest.approx(0.85)
        assert mgr.drawdown_size_multiplier(state, 97_000.0) == pytest.approx(0.85)
        # above -3% → 1.0
        assert mgr.drawdown_size_multiplier(state, 97_100.0) == pytest.approx(1.0)


class TestCheckDailyReset:

    def test_resets_on_new_day(self):
        mgr = PortfolioRiskManager({})
        state = PortfolioRiskState(
            daily_pnl_pct=-1.5,
            daily_trade_count=5,
            daily_trades_per_symbol={"AAPL": 2},
            trading_stopped_for_day=True,
            last_trade_date=date(2026, 1, 1),
        )
        mgr.check_daily_reset(state, date(2026, 1, 2))
        assert state.daily_pnl_pct == 0.0
        assert state.daily_trade_count == 0
        assert state.daily_trades_per_symbol == {}
        assert state.trading_stopped_for_day is False
        assert state.last_trade_date == date(2026, 1, 2)

    def test_no_reset_same_day(self):
        mgr = PortfolioRiskManager({})
        state = PortfolioRiskState(
            daily_pnl_pct=-1.0,
            daily_trade_count=3,
            last_trade_date=date(2026, 1, 1),
        )
        mgr.check_daily_reset(state, date(2026, 1, 1))
        assert state.daily_pnl_pct == -1.0
        assert state.daily_trade_count == 3


class TestCanTrade:

    def _mgr(self, **overrides):
        cfg = {"portfolio_risk": overrides}
        return PortfolioRiskManager(cfg)

    def test_allowed(self):
        mgr = self._mgr()
        state = PortfolioRiskState(peak_equity=10_000.0, last_trade_date=date(2026, 1, 1))
        ok, reason = mgr.can_trade(state, 10_000.0, None, today=date(2026, 1, 1))
        assert ok is True
        assert reason == "ok"

    def test_safe_mode_blocks(self):
        mgr = self._mgr(recovery_criteria_pct=-8.0)
        state = PortfolioRiskState(peak_equity=10_000.0, safe_mode=True)
        ok, reason = mgr.can_trade(state, 8_500.0, None, today=date(2026, 1, 1))
        assert ok is False
        assert "safe_mode" in reason

    def test_safe_mode_allows_when_recovered(self):
        mgr = self._mgr(recovery_criteria_pct=-8.0)
        state = PortfolioRiskState(peak_equity=10_000.0, safe_mode=True,
                                   last_trade_date=date(2026, 1, 1))
        # -5% drawdown is above -8% threshold → allowed
        ok, reason = mgr.can_trade(state, 9_500.0, None, today=date(2026, 1, 1))
        assert ok is True

    def test_daily_loss_limit_stops_trading(self):
        mgr = self._mgr(daily_loss_limit_pct=-2.0)
        state = PortfolioRiskState(
            peak_equity=10_000.0,
            daily_pnl_pct=-2.5,
            last_trade_date=date(2026, 1, 1),
        )
        ok, reason = mgr.can_trade(state, 10_000.0, None, today=date(2026, 1, 1))
        assert ok is False
        assert "daily loss limit" in reason
        assert state.trading_stopped_for_day is True
        assert state.disable_trading is True

    def test_max_daily_loss_pct_positive_semantics(self):
        """max_daily_loss_pct: 3.0 → block when daily_pnl_pct <= -3.0."""
        mgr = self._mgr(max_daily_loss_pct=3.0)
        assert mgr.daily_loss_limit_pct == pytest.approx(-3.0)
        state = PortfolioRiskState(
            peak_equity=10_000.0,
            daily_pnl_pct=-2.5,
            last_trade_date=date(2026, 1, 1),
        )
        ok, _ = mgr.can_trade(state, 10_000.0, None, today=date(2026, 1, 1))
        assert ok is True
        state.daily_pnl_pct = -3.1
        ok2, _ = mgr.can_trade(state, 10_000.0, None, today=date(2026, 1, 1))
        assert ok2 is False
        assert state.disable_trading is True

    def test_max_daily_loss_pct_overrides_daily_loss_limit(self):
        mgr = self._mgr(max_daily_loss_pct=3.0, daily_loss_limit_pct=-1.0)
        assert mgr.daily_loss_limit_pct == pytest.approx(-3.0)

    def test_trading_stopped_for_day_blocks(self):
        mgr = self._mgr()
        state = PortfolioRiskState(
            peak_equity=10_000.0,
            trading_stopped_for_day=True,
            last_trade_date=date(2026, 1, 1),
        )
        ok, reason = mgr.can_trade(state, 10_000.0, None, today=date(2026, 1, 1))
        assert ok is False
        assert "trading stopped" in reason

    def test_max_drawdown_triggers_safe_mode(self):
        mgr = self._mgr(max_drawdown_pct=-10.0)
        state = PortfolioRiskState(peak_equity=10_000.0, last_trade_date=date(2026, 1, 1))
        ok, reason = mgr.can_trade(state, 8_900.0, None, today=date(2026, 1, 1))
        assert ok is False
        assert "max drawdown" in reason
        assert state.safe_mode is True

    def test_max_trades_per_day(self):
        mgr = self._mgr(max_trades_per_day=2)
        state = PortfolioRiskState(
            peak_equity=10_000.0,
            daily_trade_count=2,
            last_trade_date=date(2026, 1, 1),
        )
        ok, reason = mgr.can_trade(state, 10_000.0, None, today=date(2026, 1, 1))
        assert ok is False
        assert "max trades per day" in reason

    def test_max_trades_per_symbol_per_day(self):
        mgr = self._mgr(max_trades_per_symbol_per_day=1)
        state = PortfolioRiskState(
            peak_equity=10_000.0,
            daily_trades_per_symbol={"AAPL": 1},
            last_trade_date=date(2026, 1, 1),
        )
        ok, reason = mgr.can_enter_symbol_activity(state, "AAPL", today=date(2026, 1, 1))
        assert ok is False
        assert "max trades per symbol" in reason

    def test_today_defaults_to_now(self):
        mgr = self._mgr()
        state = PortfolioRiskState(peak_equity=10_000.0)
        ok, _ = mgr.can_trade(state, 10_000.0, None)
        assert ok is True
        assert state.last_trade_date == date.today()

    def test_safe_mode_not_triggered_when_disabled(self):
        mgr = self._mgr(max_drawdown_pct=-10.0, safe_mode_after_max_dd=False)
        state = PortfolioRiskState(peak_equity=10_000.0, last_trade_date=date(2026, 1, 1))
        ok, _ = mgr.can_trade(state, 8_900.0, None, today=date(2026, 1, 1))
        assert ok is True
        assert state.safe_mode is False


class TestPerSymbolEntryCaps:

    def test_max_round_trips_blocks_entry(self):
        mgr = PortfolioRiskManager({"portfolio_risk": {"max_round_trips_per_symbol": 1}})
        state = PortfolioRiskState(
            last_trade_date=date(2026, 4, 20),
            daily_round_trips_per_symbol={"XLP": 1},
        )
        ok, reason = mgr.can_enter_symbol_activity(state, "XLP", today=date(2026, 4, 20))
        assert ok is False
        assert "round trip" in reason.lower()

    def test_risk_yaml_fallback_when_portfolio_risk_omits(self):
        mgr = PortfolioRiskManager(
            {"risk": {"max_trades_per_symbol_per_day": 5, "max_round_trips_per_symbol": 3}}
        )
        assert mgr.max_trades_per_symbol_per_day == 5
        assert mgr.max_round_trips_per_symbol == 3

    def test_portfolio_risk_overrides_risk_block(self):
        mgr = PortfolioRiskManager(
            {
                "portfolio_risk": {"max_trades_per_symbol_per_day": 9},
                "risk": {"max_trades_per_symbol_per_day": 5},
            }
        )
        assert mgr.max_trades_per_symbol_per_day == 9


def test_note_live_order_persists_and_roundtrip(tmp_path) -> None:
    cfg = {
        "portfolio_risk": {
            "live_daily_pnl_from_equity_snapshot": True,
            "max_round_trips_per_symbol": 2,
        }
    }
    mgr = PortfolioRiskManager(cfg)
    state = PortfolioRiskState()
    state.risk_counters_user_id = "u1"
    state.risk_counters_data_dir = tmp_path
    engine = SimpleNamespace(portfolio_risk=mgr, state=SimpleNamespace(portfolio_risk=state))
    d = date(2026, 4, 20)
    mgr.check_daily_reset(state, d)
    note_live_order_for_daily_risk(engine, "xlp", d, side="buy", full_exit=False)
    assert state.daily_trades_per_symbol["XLP"] == 1
    note_live_order_for_daily_risk(engine, "xlp", d, side="sell", full_exit=True)
    assert state.daily_trades_per_symbol["XLP"] == 2
    assert state.daily_round_trips_per_symbol["XLP"] == 1
    f = tmp_path / "portfolio_risk_daily_u1_2026-04-20.json"
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data["daily_trades_per_symbol"]["XLP"] == 2
    assert data["daily_round_trips_per_symbol"]["XLP"] == 1


def test_counters_merge_from_disk(tmp_path) -> None:
    d = date(2026, 4, 21)
    f = tmp_path / f"portfolio_risk_daily_u9_{d.isoformat()}.json"
    f.write_text(
        json.dumps(
            {
                "et_date": d.isoformat(),
                "daily_trade_count": 10,
                "daily_trades_per_symbol": {"QQQ": 4},
                "daily_round_trips_per_symbol": {"QQQ": 1},
            }
        ),
        encoding="utf-8",
    )
    mgr = PortfolioRiskManager({})
    state = PortfolioRiskState()
    state.risk_counters_user_id = "u9"
    state.risk_counters_data_dir = tmp_path
    mgr.check_daily_reset(state, d)
    assert state.daily_trades_per_symbol.get("QQQ") == 4
    assert state.daily_trade_count == 10


class TestRecordTrade:

    def test_increments_counts(self):
        mgr = PortfolioRiskManager({})
        state = PortfolioRiskState()
        mgr.record_trade(state, "AAPL", -0.5)
        assert state.daily_trade_count == 1
        assert state.daily_trades_per_symbol == {"AAPL": 1}
        assert state.daily_pnl_pct == pytest.approx(-0.5)

    def test_accumulates(self):
        mgr = PortfolioRiskManager({})
        state = PortfolioRiskState()
        mgr.record_trade(state, "AAPL", -0.5)
        mgr.record_trade(state, "AAPL", 0.3)
        mgr.record_trade(state, "TSLA", 1.0)
        assert state.daily_trade_count == 3
        assert state.daily_trades_per_symbol == {"AAPL": 2, "TSLA": 1}
        assert state.daily_pnl_pct == pytest.approx(0.8)


class TestLiveDailyPnlEquitySnapshot:
    """Broker-anchored session P&L must not stack with per-exit realized %%."""

    @staticmethod
    def _mgr_snapshot() -> PortfolioRiskManager:
        return PortfolioRiskManager(
            {"portfolio_risk": {"live_daily_pnl_from_equity_snapshot": True, "max_daily_loss_pct": 3.0}}
        )

    def test_refresh_daily_pnl_from_snapshot_sets_session_return(self):
        mgr = self._mgr_snapshot()
        state = PortfolioRiskState()
        today = date(2026, 1, 15)
        mgr.refresh_daily_pnl_from_snapshot(state, equity=99_000.0, last_equity=100_000.0, today=today)
        assert state.daily_pnl_pct == pytest.approx(-1.0)
        assert state.last_trade_date == today

    def test_can_trade_applies_session_last_equity(self):
        mgr = self._mgr_snapshot()
        state = PortfolioRiskState(peak_equity=100_000.0, last_trade_date=date(2026, 1, 20))
        ok, _ = mgr.can_trade(
            state,
            current_equity=98_000.0,
            symbol=None,
            today=date(2026, 1, 20),
            session_last_equity=100_000.0,
        )
        assert ok is True
        assert state.daily_pnl_pct == pytest.approx(-2.0)

    def test_record_trade_does_not_add_pnl_when_snapshot_mode(self):
        mgr = self._mgr_snapshot()
        state = PortfolioRiskState(daily_pnl_pct=-1.5, last_trade_date=date(2026, 1, 20))
        mgr.record_trade(state, "QQQ", -2.0)
        assert state.daily_trade_count == 1
        assert state.daily_pnl_pct == pytest.approx(-1.5)

    def test_record_order_activity_matches_record_trade_in_snapshot_mode(self):
        mgr = self._mgr_snapshot()
        state = PortfolioRiskState(daily_pnl_pct=-0.5, last_trade_date=date(2026, 1, 20))
        mgr.record_order_activity(state, "NVDA", -3.0)
        assert state.daily_trade_count == 1
        assert state.daily_pnl_pct == pytest.approx(-0.5)


# ---------------------------------------------------------------------------
# MultiUserPortfolioRiskManager
# ---------------------------------------------------------------------------

class TestMultiUserPortfolioRiskManager:

    @staticmethod
    def _configs():
        return {
            "alice": {"portfolio_risk": {"daily_loss_limit_pct": -1.0, "max_trades_per_day": 5}},
            "bob": {"portfolio_risk": {"daily_loss_limit_pct": -3.0, "max_trades_per_day": 20}},
        }

    def test_register_and_get_state(self):
        mu = MultiUserPortfolioRiskManager(self._configs())
        assert isinstance(mu.get_state("alice"), PortfolioRiskState)
        assert isinstance(mu.get_state("bob"), PortfolioRiskState)

    def test_unregistered_user_raises(self):
        mu = MultiUserPortfolioRiskManager()
        with pytest.raises(KeyError, match="charlie"):
            mu.get_state("charlie")

    def test_register_idempotent(self):
        mu = MultiUserPortfolioRiskManager()
        mu.register_user("alice", {})
        state1 = mu.get_state("alice")
        mu.register_user("alice", {"portfolio_risk": {"daily_loss_limit_pct": -99}})
        state2 = mu.get_state("alice")
        assert state1 is state2  # not replaced

    def test_users_isolated(self):
        mu = MultiUserPortfolioRiskManager(self._configs())
        dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
        mu.update_equity("alice", dt, 10_000.0)
        mu.update_equity("bob", dt, 50_000.0)
        assert mu.get_state("alice").peak_equity == 10_000.0
        assert mu.get_state("bob").peak_equity == 50_000.0

    def test_can_trade_per_user_config(self):
        mu = MultiUserPortfolioRiskManager(self._configs())
        dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
        today = date(2026, 1, 1)
        mu.update_equity("alice", dt, 10_000.0)
        mu.update_equity("bob", dt, 10_000.0)

        # Alice: daily_loss_limit=-1%, set pnl to -1.5%
        mu.get_state("alice").daily_pnl_pct = -1.5
        mu.get_state("alice").last_trade_date = today
        ok_a, _ = mu.can_trade("alice", 10_000.0, "AAPL", today)
        assert ok_a is False

        # Bob: daily_loss_limit=-3%, same pnl → still allowed
        mu.get_state("bob").daily_pnl_pct = -1.5
        mu.get_state("bob").last_trade_date = today
        ok_b, _ = mu.can_trade("bob", 10_000.0, "AAPL", today)
        assert ok_b is True

    def test_record_trade(self):
        mu = MultiUserPortfolioRiskManager(self._configs())
        mu.record_trade("alice", "AAPL", -0.5)
        assert mu.get_state("alice").daily_trade_count == 1
        assert mu.get_state("bob").daily_trade_count == 0

    def test_check_daily_reset(self):
        mu = MultiUserPortfolioRiskManager(self._configs())
        mu.get_state("alice").daily_trade_count = 5
        mu.get_state("alice").last_trade_date = date(2026, 1, 1)
        mu.check_daily_reset("alice", date(2026, 1, 2))
        assert mu.get_state("alice").daily_trade_count == 0

    def test_update_equity_unknown_user(self):
        mu = MultiUserPortfolioRiskManager()
        with pytest.raises(KeyError, match="unknown"):
            mu.update_equity("unknown", datetime.now(timezone.utc), 1000.0)

    def test_can_trade_unknown_user(self):
        mu = MultiUserPortfolioRiskManager()
        with pytest.raises(KeyError):
            mu.can_trade("unknown", 1000.0, "AAPL")

    def test_record_trade_unknown_user(self):
        mu = MultiUserPortfolioRiskManager()
        with pytest.raises(KeyError):
            mu.record_trade("unknown", "AAPL", 0.1)

    def test_check_daily_reset_unknown_user(self):
        mu = MultiUserPortfolioRiskManager()
        with pytest.raises(KeyError):
            mu.check_daily_reset("unknown", date.today())
