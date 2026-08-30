"""Tests for compliance — PDT rules, single-user and multi-user wrappers."""

from datetime import date, timedelta

import pytest

from src.compliance import (
    ComplianceManager,
    MultiUserComplianceManager,
    PDTState,
)


# ---------------------------------------------------------------------------
# PDTState defaults
# ---------------------------------------------------------------------------

class TestPDTState:

    def test_fields(self):
        s = PDTState(equity=25_000.0, day_trades_count_rolling=0, day_trade_dates=[])
        assert s.equity == 25_000.0
        assert s.day_trades_count_rolling == 0
        assert s.day_trade_dates == []


# ---------------------------------------------------------------------------
# ComplianceManager — core PDT logic
# ---------------------------------------------------------------------------

class TestCanDayTrade:

    def _mgr(self, **overrides):
        cfg = {"compliance": overrides}
        return ComplianceManager(cfg)

    def test_allowed_above_threshold(self):
        mgr = self._mgr()
        state = PDTState(equity=30_000.0, day_trades_count_rolling=0, day_trade_dates=[])
        ok, reason = mgr.can_day_trade(state, date(2026, 1, 10))
        assert ok is True
        assert "above PDT threshold" in reason

    def test_allowed_below_threshold_few_trades(self):
        mgr = self._mgr()
        state = PDTState(
            equity=20_000.0,
            day_trades_count_rolling=0,
            day_trade_dates=[date(2026, 1, 8), date(2026, 1, 9)],
        )
        ok, reason = mgr.can_day_trade(state, date(2026, 1, 10))
        assert ok is True
        assert reason == "ok"

    def test_blocked_below_threshold_too_many_trades(self):
        mgr = self._mgr()
        state = PDTState(
            equity=20_000.0,
            day_trades_count_rolling=0,
            day_trade_dates=[date(2026, 1, 7), date(2026, 1, 8), date(2026, 1, 9)],
        )
        ok, reason = mgr.can_day_trade(state, date(2026, 1, 10))
        assert ok is False
        assert "PDT" in reason
        assert "day trade limit" in reason

    def test_pdt_disabled(self):
        mgr = self._mgr(pdt_enabled=False)
        state = PDTState(equity=1_000.0, day_trades_count_rolling=0,
                         day_trade_dates=[date(2026, 1, i) for i in range(1, 11)])
        ok, reason = mgr.can_day_trade(state, date(2026, 1, 10))
        assert ok is True
        assert "not applicable" in reason

    def test_cash_account_not_applicable(self):
        mgr = self._mgr(margin_account=False)
        state = PDTState(equity=1_000.0, day_trades_count_rolling=0,
                         day_trade_dates=[date(2026, 1, i) for i in range(1, 11)])
        ok, reason = mgr.can_day_trade(state, date(2026, 1, 10))
        assert ok is True
        assert "not applicable" in reason

    def test_old_trades_pruned_from_window(self):
        """Trades older than 7 days shouldn't count."""
        mgr = self._mgr()
        old = date(2026, 1, 1)  # well outside 7-day window
        state = PDTState(
            equity=20_000.0,
            day_trades_count_rolling=0,
            day_trade_dates=[old, old, old],
        )
        ok, _ = mgr.can_day_trade(state, date(2026, 1, 15))
        assert ok is True

    def test_custom_pdt_min_equity(self):
        mgr = self._mgr(pdt_min_equity=50_000)
        state = PDTState(
            equity=30_000.0,
            day_trades_count_rolling=0,
            day_trade_dates=[date(2026, 1, 8), date(2026, 1, 9), date(2026, 1, 10)],
        )
        ok, reason = mgr.can_day_trade(state, date(2026, 1, 10))
        assert ok is False
        assert "$50,000" in reason


class TestRecordDayTrade:

    def test_appends_date(self):
        mgr = ComplianceManager({})
        state = PDTState(equity=25_000.0, day_trades_count_rolling=0, day_trade_dates=[])
        mgr.record_day_trade(state, date(2026, 1, 10))
        assert state.day_trade_dates == [date(2026, 1, 10)]

    def test_prunes_when_over_20(self):
        mgr = ComplianceManager({})
        dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(21)]
        state = PDTState(equity=25_000.0, day_trades_count_rolling=0, day_trade_dates=dates)
        mgr.record_day_trade(state, date(2026, 2, 1))
        assert len(state.day_trade_dates) == 20


class TestRecordDayTradeSymbolDedupe:
    """Same-day partial exits on one symbol must not multiply PDT rolling counts."""

    def test_multiple_partials_same_symbol_same_day_one_slot(self):
        mgr = ComplianceManager({})
        state = PDTState(equity=20_000.0, day_trades_count_rolling=0, day_trade_dates=[])
        d = date(2026, 3, 2)
        mgr.record_day_trade(state, d, "AAPL")
        mgr.record_day_trade(state, d, "AAPL")
        mgr.record_day_trade(state, d, "AAPL")
        assert state.day_trade_log == [(d, "AAPL")]
        assert state.day_trade_dates == [d]
        assert mgr._recent_day_trade_count(state, d - timedelta(days=7)) == 1

    def test_two_symbols_same_day_two_slots(self):
        mgr = ComplianceManager({})
        state = PDTState(equity=20_000.0, day_trades_count_rolling=0, day_trade_dates=[])
        d = date(2026, 3, 2)
        mgr.record_day_trade(state, d, "AAPL")
        mgr.record_day_trade(state, d, "MSFT")
        assert mgr._recent_day_trade_count(state, d - timedelta(days=7)) == 2

    def test_record_day_trade_if_applicable_returns_false_on_repeat(self):
        mgr = ComplianceManager({})
        state = PDTState(equity=20_000.0, day_trades_count_rolling=0, day_trade_dates=[])
        d = date(2026, 3, 2)
        assert mgr.record_day_trade_if_applicable(state, d, "AAPL") is True
        assert mgr.record_day_trade_if_applicable(state, d, "AAPL") is False
        assert len(state.day_trade_log) == 1


class TestUpdateEquity:

    def test_updates_equity(self):
        mgr = ComplianceManager({})
        state = PDTState(equity=10_000.0, day_trades_count_rolling=0, day_trade_dates=[])
        mgr.update_equity(state, 20_000.0)
        assert state.equity == 20_000.0


class TestComplianceManagerDefaults:

    def test_defaults(self):
        mgr = ComplianceManager({})
        assert mgr.pdt_min_equity == 25_000.0
        assert mgr.pdt_enabled is True
        assert mgr.margin_account is True
        assert mgr.best_execution_note == ""

    def test_custom_config(self):
        cfg = {
            "compliance": {
                "pdt_min_equity": 50_000,
                "pdt_enabled": False,
                "margin_account": False,
                "best_execution_note": "test note",
            }
        }
        mgr = ComplianceManager(cfg)
        assert mgr.pdt_min_equity == 50_000.0
        assert mgr.pdt_enabled is False
        assert mgr.margin_account is False
        assert mgr.best_execution_note == "test note"


# ---------------------------------------------------------------------------
# MultiUserComplianceManager
# ---------------------------------------------------------------------------

class TestMultiUserComplianceManager:

    @staticmethod
    def _configs():
        return {
            "alice": {"compliance": {"pdt_min_equity": 25_000, "margin_account": True}},
            "bob": {"compliance": {"pdt_min_equity": 25_000, "margin_account": False}},
        }

    def test_register_and_get_state(self):
        mu = MultiUserComplianceManager(self._configs())
        assert isinstance(mu.get_state("alice"), PDTState)
        assert isinstance(mu.get_state("bob"), PDTState)

    def test_unregistered_user_raises(self):
        mu = MultiUserComplianceManager()
        with pytest.raises(KeyError, match="charlie"):
            mu.get_state("charlie")

    def test_register_idempotent(self):
        mu = MultiUserComplianceManager()
        mu.register_user("alice", {})
        state1 = mu.get_state("alice")
        mu.register_user("alice", {"compliance": {"pdt_min_equity": 99}})
        state2 = mu.get_state("alice")
        assert state1 is state2

    def test_users_isolated(self):
        mu = MultiUserComplianceManager(self._configs())
        mu.update_equity("alice", 30_000.0)
        mu.update_equity("bob", 10_000.0)
        assert mu.get_state("alice").equity == 30_000.0
        assert mu.get_state("bob").equity == 10_000.0

    def test_can_day_trade_per_user_config(self):
        mu = MultiUserComplianceManager(self._configs())
        mu.update_equity("alice", 20_000.0)
        mu.update_equity("bob", 20_000.0)

        # Both have 3 recent trades
        for uid in ("alice", "bob"):
            for d in (date(2026, 1, 8), date(2026, 1, 9), date(2026, 1, 10)):
                mu.record_day_trade(uid, d)

        # Alice: margin_account=True, equity < 25k → blocked
        ok_a, _ = mu.can_day_trade("alice", date(2026, 1, 11))
        assert ok_a is False

        # Bob: margin_account=False → PDT not applicable
        ok_b, _ = mu.can_day_trade("bob", date(2026, 1, 11))
        assert ok_b is True

    def test_record_day_trade_isolated(self):
        mu = MultiUserComplianceManager(self._configs())
        mu.record_day_trade("alice", date(2026, 1, 10))
        assert len(mu.get_state("alice").day_trade_dates) == 1
        assert len(mu.get_state("bob").day_trade_dates) == 0

    def test_record_day_trade_if_applicable_isolated(self):
        mu = MultiUserComplianceManager(self._configs())
        d = date(2026, 3, 3)
        assert mu.record_day_trade_if_applicable("alice", d, "SPY") is True
        assert mu.record_day_trade_if_applicable("alice", d, "SPY") is False
        assert len(mu.get_state("alice").day_trade_log) == 1
        assert len(mu.get_state("bob").day_trade_log) == 0

    def test_update_equity_unknown_user(self):
        mu = MultiUserComplianceManager()
        with pytest.raises(KeyError, match="unknown"):
            mu.update_equity("unknown", 1000.0)

    def test_can_day_trade_unknown_user(self):
        mu = MultiUserComplianceManager()
        with pytest.raises(KeyError):
            mu.can_day_trade("unknown", date.today())

    def test_record_day_trade_unknown_user(self):
        mu = MultiUserComplianceManager()
        with pytest.raises(KeyError):
            mu.record_day_trade("unknown", date.today())

    def test_record_day_trade_if_applicable_unknown_user(self):
        mu = MultiUserComplianceManager()
        with pytest.raises(KeyError):
            mu.record_day_trade_if_applicable("unknown", date.today(), "SPY")

    def test_empty_init(self):
        mu = MultiUserComplianceManager()
        mu.register_user("alice", {})
        state = mu.get_state("alice")
        assert state.equity == 0.0
        assert state.day_trade_dates == []
