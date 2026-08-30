"""Context surface for split live exit workflows."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from src.inverse_reentry import record_sqqq_full_exit
from src.options_premium_risk import is_option_symbol
from src.pdt_safety import block_same_day_close_for_pdt, entry_opened_same_calendar_day_et
from src.position_tracker import last_entry_within, load as load_tracked
from src.execution import parse_no_sell_within_min_of_buy
from src.strategy import ExitReason
from src.trading_engine import TradingEngine
from src.position_state_machine import exit_reason_is_stop_like, record_sell_after_exit
from src.sell_logging import log_sell
from src.trade_attribution import record_exit as record_trade_attribution_exit
from src.decision_priority import parse_decision_priority, rank_for_kind

log = logging.getLogger(__name__)

# Last :meth:`LiveExitContext.log_sell_event` time for **equity** (non-OCC) per *user_id* (``now`` as
# stored). Used for allocator soft-cap override; survives across loop iterations in-process.
_LAST_EQUITY_SELL_UTC: dict[str, Any] = {}


def reentry_block_allows_despite_flag(reason: ExitReason) -> bool:
    """Sells that still run when :func:`LiveExitContext.reentry_block_discretionary_sells` is active."""
    if exit_reason_is_stop_like(reason):
        return True
    if reason in (ExitReason.KILL_SWITCH, ExitReason.KILL_SWITCH_PARTIAL):
        return True
    return False


class LiveExitContext:
    """Per-user bindings for the exit pass (constructed once per loop iteration)."""

    def __init__(
        self,
        *,
        user_id: str,
        data_dir: Path,
        now: Any,
        verbose: bool,
        broker: Any,
        engine: TradingEngine,
        config: dict[str, Any],
        account_equity: float,
        symbols: list[str],
        news_enabled: bool,
        news_pipeline: Any,
        news_rules: Any,
        exposure_snapshot: Any | None = None,
    ) -> None:
        self.user_id = user_id
        self.data_dir = data_dir
        self.now = now
        self.verbose = verbose
        self.broker = broker
        self.engine = engine
        self.config = config
        self.account_equity = account_equity
        self.symbols = symbols
        self.exposure_snapshot = exposure_snapshot
        self.news_enabled = news_enabled
        self.news_pipeline = news_pipeline
        self.news_rules = news_rules
        self._exit_actions_by_symbol: dict[str, int] = {}
        self._bulk_trim_buy_block_until: dict[str, datetime] = {}
        self._decision_priority: dict[str, int] = parse_decision_priority(config)
        self._symbol_intent_best: dict[str, int] = {}

    def note_decision_intent(self, symbol: str, kind: str) -> None:
        """Record strongest (lowest-rank) exit intent for *symbol* this loop iteration (see ``decision_priority``)."""
        sym = str(symbol or "").strip().upper()
        if not sym:
            return
        r = rank_for_kind(self._decision_priority, kind)
        prev = self._symbol_intent_best.get(sym)
        if prev is None or r < prev:
            self._symbol_intent_best[sym] = r

    def allocator_buy_blocked_by_priority(self, symbol: str) -> tuple[bool, str | None]:
        """True when an exit intent outranks ``new_entry`` for this symbol (suppress allocator BUY)."""
        sym = str(symbol or "").strip().upper()
        if not sym:
            return False, None
        intent_rank = self._symbol_intent_best.get(sym)
        if intent_rank is None:
            return False, None
        new_entry_rank = rank_for_kind(self._decision_priority, "new_entry")
        if intent_rank < new_entry_rank:
            return True, "decision_priority exit intent rank %s beats new_entry (%s)" % (
                intent_rank,
                new_entry_rank,
            )
        return False, None

    def register_bulk_trim_sell(self, symbol: str, cooldown_minutes: float) -> None:
        """
        After a **bulk** (notional) ``rebalance_free_capital`` trim, block **buys** of the same
        symbol until *cooldown_minutes* elapses (wall time vs :attr:`now`).

        ``0`` *cooldown_minutes* = no-op. Expired entries are pruned on register and on read.
        """
        try:
            w = float(cooldown_minutes)
        except (TypeError, ValueError):
            return
        if w <= 0.0 or w != w:
            return
        su = str(symbol or "").strip().upper()
        if not su:
            return
        now = self.now
        if not isinstance(now, datetime):
            return
        for k in list(self._bulk_trim_buy_block_until.keys()):
            t0 = self._bulk_trim_buy_block_until.get(k)
            if t0 is not None and now >= t0:
                self._bulk_trim_buy_block_until.pop(k, None)
        self._bulk_trim_buy_block_until[su] = now + timedelta(minutes=float(w))

    def bulk_trim_buy_cooldown_active(self, symbol: str) -> tuple[bool, str | None]:
        """True when *symbol* is still inside a post-:meth:`register_bulk_trim_sell` **buy** block."""
        su = str(symbol or "").strip().upper()
        if not su:
            return False, None
        now = self.now
        if not isinstance(now, datetime):
            return False, None
        for k in list(self._bulk_trim_buy_block_until.keys()):
            t0 = self._bulk_trim_buy_block_until.get(k)
            if t0 is not None and now >= t0:
                self._bulk_trim_buy_block_until.pop(k, None)
        until = self._bulk_trim_buy_block_until.get(su)
        if until is None:
            return False, None
        if now >= until:
            self._bulk_trim_buy_block_until.pop(su, None)
            return False, None
        try:
            remain = max(0.0, (until - now).total_seconds() / 60.0)
        except Exception:
            remain = 0.0
        r = (self.config or {}).get("portfolio") or {}
        r2 = (r.get("rebalance_free_capital") or {}).get("bulk_trim") or {}
        try:
            win = float((r2 or {}).get("buy_cooldown_minutes", 30) or 30) if isinstance(r2, dict) else 30.0
        except (TypeError, ValueError):
            win = 30.0
        return True, "bulk trim buy cooldown (%.0f min left / %.0f min window)" % (
            remain,
            max(0.0, win),
        )

    def post_buy_sell_cooldown_active(
        self, symbol: str, pos_row: dict[str, Any] | None
    ) -> tuple[bool, str | None]:
        """
        When (True, msg), **discretionary** equity sells should be skipped: last buy/scale was within
        ``execution.no_sell_within_min_of_buy`` minutes (see :func:`last_entry_within`). ``0`` = off.
        """
        w = parse_no_sell_within_min_of_buy(self.config)
        if w <= 0.0 or not pos_row or not isinstance(pos_row, dict):
            return False, None
        sym = str(symbol or "").strip().upper()
        if not sym:
            return False, None
        now = self.now
        if not isinstance(now, datetime):
            return False, None
        tmap: dict[str, Any] = {sym: pos_row}
        if not last_entry_within(sym, w, tracked=tmap, now_dt=now):
            return False, None
        return True, "no sell within %d min of buy/scale (execution.no_sell_within_min_of_buy)" % int(w)

    def reentry_block_discretionary_sells(self) -> tuple[bool, str | None]:
        """
        When ``execution.block_exits_if_no_reentry_capacity`` is set and **effective** buying
        power for new entries (after cash reserve) is **below** ``entries.min_trade_size``,
        skip discretionary / profit-hygiene exits (balance exit vs entry pressure).
        Risk trims (cap breach, overweight) are evaluated earlier and are not gated here.
        """
        exg = self.config.get("execution")
        if not isinstance(exg, Mapping) or not bool(exg.get("block_exits_if_no_reentry_capacity", False)):
            return False, None
        from src.loop_helpers import entries_insufficient_buying_power
        from src.portfolio_allocation import effective_buying_power_for_entries

        try:
            bp = float(self.broker.get_buying_power())
        except Exception:
            return False, None
        eff = effective_buying_power_for_entries(
            buying_power=bp,
            equity=float(self.account_equity),
            config=self.config,
        )
        if entries_insufficient_buying_power(eff, self.config.get("entries")):
            return True, "reentry: effective BP < entries.min_trade_size"
        return False, None

    def max_actions_per_symbol_per_cycle(self) -> int:
        ex = self.config.get("execution")
        if not isinstance(ex, Mapping):
            return 0
        raw = ex.get("max_actions_per_symbol_per_cycle", 0)
        try:
            v = int(raw)
        except (TypeError, ValueError):
            return 0
        return max(0, v)

    def exit_action_allowed(self, symbol: str) -> bool:
        cap = self.max_actions_per_symbol_per_cycle()
        if cap <= 0:
            return True
        sym = str(symbol or "").strip().upper()
        if not sym:
            return True
        return self._exit_actions_by_symbol.get(sym, 0) < cap

    def record_exit_action(self, symbol: str) -> None:
        cap = self.max_actions_per_symbol_per_cycle()
        if cap <= 0:
            return
        sym = str(symbol or "").strip().upper()
        if not sym:
            return
        self._exit_actions_by_symbol[sym] = self._exit_actions_by_symbol.get(sym, 0) + 1

    def skip_exit_for_action_cap(self, symbol: str, reason: str) -> bool:
        """True if this symbol has already used its per-cycle exit quota (caller should skip / return)."""
        if self.exit_action_allowed(symbol):
            return False
        log.info(
            "[%s] %s — exit skipped (%s): max_actions_per_symbol_per_cycle",
            self.user_id,
            str(symbol or "").strip().upper(),
            reason,
        )
        return True

    def et_trade_date(self) -> date:
        n = self.now
        if hasattr(n, "date"):
            return n.date()  # type: ignore[no-any-return]
        return date.today()

    def note_daily_risk_order(self, symbol: str, *, side: str, full_exit: bool = False) -> None:
        from src.portfolio_risk import note_live_order_for_daily_risk

        note_live_order_for_daily_risk(
            self.engine, symbol, self.et_trade_date(), side=side, full_exit=full_exit
        )
        side_u = str(side or "").strip().lower()
        if side_u not in {"sell", "buy"}:
            return
        tracked = load_tracked(self.user_id, data_dir=self.data_dir)
        row = tracked.get(str(symbol or "").strip().upper()) if isinstance(tracked, dict) else None
        if not isinstance(row, dict):
            return
        if not entry_opened_same_calendar_day_et(
            row.get("entry_time"),
            self.now,
            entry_time_uncertain=bool(row.get("entry_time_uncertain")),
        ):
            return
        self.engine.compliance.record_day_trade_if_applicable(
            self.engine.state.pdt,
            self.et_trade_date(),
            str(symbol or "").strip().upper(),
        )

    def log_sell_event(self, symbol: str, reason: str, extra: dict[str, Any] | None = None) -> None:
        """Canonical sell log line (see :mod:`src.sell_logging`)."""
        ctx: dict[str, Any] = {"user_id": self.user_id, "channel": "live_exits"}
        if extra:
            ctx.update(extra)
        log_sell(symbol, reason, ctx)
        try:
            su_attr = str(symbol or "").strip().upper()
            tracked_attr = load_tracked(self.user_id, data_dir=self.data_dir)
            row_attr = tracked_attr.get(su_attr) if isinstance(tracked_attr, dict) else None
            row_attr = row_attr if isinstance(row_attr, dict) else {}
            entry_time = row_attr.get("entry_time")
            exit_price = None
            pnl = (extra or {}).get("pnl") if extra else None
            pnl_pct = (extra or {}).get("pnl_pct") if extra else None
            try:
                exit_raw = (extra or {}).get("exit_price") or (extra or {}).get("price") or (extra or {}).get("fill_price")
                if exit_raw is not None and str(exit_raw).strip() != "":
                    exit_price = float(exit_raw)
                entry_raw = row_attr.get("entry_price") or row_attr.get("last_entry_price") or row_attr.get("avg_price")
                qty_raw = (extra or {}).get("qty") if extra else None
                if pnl is None and exit_price is not None and entry_raw is not None and qty_raw is not None:
                    entry_price = float(entry_raw)
                    qty_float = float(qty_raw)
                    if entry_price > 0.0 and qty_float > 0.0:
                        pnl = (float(exit_price) - entry_price) * qty_float
                        pnl_pct = ((float(exit_price) - entry_price) / entry_price) * 100.0
            except Exception:
                exit_price = exit_price
            hold_minutes: float | None = None
            if isinstance(entry_time, str) and isinstance(self.now, datetime):
                try:
                    parsed_entry = datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
                    now_attr = self.now
                    if parsed_entry.tzinfo is not None and now_attr.tzinfo is None:
                        parsed_entry = parsed_entry.replace(tzinfo=None)
                    elif parsed_entry.tzinfo is None and now_attr.tzinfo is not None:
                        now_attr = now_attr.replace(tzinfo=None)
                    hold_minutes = max(0.0, (now_attr - parsed_entry).total_seconds() / 60.0)
                except Exception:
                    hold_minutes = None
            mfe_pct = None
            mae_pct = None
            try:
                entry_raw_mfe = row_attr.get("entry_price") or row_attr.get("last_entry_price") or row_attr.get("avg_price")
                entry_float = float(entry_raw_mfe)
                high_float = float(row_attr.get("max_price_since_entry") or row_attr.get("trail_high") or entry_float)
                low_float = float(row_attr.get("min_price_since_entry") or entry_float)
                if entry_float > 0.0:
                    mfe_pct = (high_float - entry_float) / entry_float * 100.0
                    mae_pct = (low_float - entry_float) / entry_float * 100.0
            except Exception:
                mfe_pct = None
                mae_pct = None
            record_trade_attribution_exit(
                data_dir=self.data_dir,
                user_id=str(self.user_id),
                timestamp=self.now,
                symbol=su_attr,
                qty=(extra or {}).get("qty") if extra else None,
                exit_reason=str((extra or {}).get("engine_reason") or (extra or {}).get("variant") or reason),
                pnl=pnl,
                pnl_pct=pnl_pct,
                hold_minutes=hold_minutes,
                entry_route=str(row_attr.get("route") or row_attr.get("source") or "") or None,
                entry_source=str(row_attr.get("source") or "") or None,
                mfe_pct=mfe_pct,
                mae_pct=mae_pct,
                entry_time=entry_time,
                exit_time=self.now.isoformat() if isinstance(self.now, datetime) else str(self.now),
                market_regime_score=row_attr.get("market_regime_score") or row_attr.get("regime_score"),
                market_regime_label=row_attr.get("market_regime_label") or row_attr.get("regime_condition"),
                spy_above_vwap=row_attr.get("spy_above_vwap"),
                qqq_above_vwap=row_attr.get("qqq_above_vwap"),
                symbol_above_vwap=row_attr.get("symbol_above_vwap") or row_attr.get("vwap_above"),
                sector_etf=row_attr.get("sector_etf"),
                sector_above_vwap=row_attr.get("sector_above_vwap"),
                relative_volume=row_attr.get("relative_volume"),
                spread_pct=row_attr.get("spread_pct"),
                day_gain_pct=row_attr.get("day_gain_pct") or row_attr.get("gain_pct"),
                atr_expansion=row_attr.get("atr_expansion") or row_attr.get("atr_expansion_ratio"),
                vwap_distance_pct=row_attr.get("vwap_distance_pct"),
                alignment_1m=row_attr.get("alignment_1m"),
                alignment_5m=row_attr.get("alignment_5m"),
                trend_15m=row_attr.get("trend_15m"),
                catalyst_score=row_attr.get("catalyst_score"),
                news_score=row_attr.get("news_score"),
                event_score=row_attr.get("event_score"),
                article_count=row_attr.get("article_count"),
                premarket_injected=row_attr.get("premarket_injected"),
                trend_long_quality_score=row_attr.get("trend_long_quality_score"),
                entry_quality_reason=row_attr.get("entry_quality_reason"),
            )
            log.info(
                "TRADE_ATTRIBUTION_EXIT_RECORDED user_id=%s symbol=%s reason=%s qty=%s exit_price=%s pnl=%s route=%s",
                self.user_id,
                su_attr,
                str((extra or {}).get("engine_reason") or (extra or {}).get("variant") or reason),
                (extra or {}).get("qty") if extra else None,
                exit_price,
                pnl,
                str(row_attr.get("route") or row_attr.get("source") or "") or "n/a",
            )
        except Exception:
            log.warning("[%s] trade attribution exit write failed for %s", self.user_id, symbol, exc_info=True)
        try:
            su = str(symbol or "").strip().upper()
            if su and not is_option_symbol(su):
                self._equity_sell_events = int(getattr(self, "_equity_sell_events", 0) or 0) + 1
                _uid = str(self.user_id or "").strip()
                if _uid and isinstance(self.now, datetime):
                    _LAST_EQUITY_SELL_UTC[_uid] = self.now
        except Exception:
            pass

    def had_equity_sell_this_pass(self) -> bool:
        """True when at least one **equity** (non-OCC) sell was logged this exit pass (see :meth:`log_sell_event`)."""
        return int(getattr(self, "_equity_sell_events", 0) or 0) > 0

    def recent_sell_within(self, minutes: float) -> bool:
        """
        True when an equity (non-OCC) sell was logged at :meth:`log_sell_event` within the last
        *minutes* vs :attr:`now` (per-process timestamp; used by capital allocator
        ``ignore_soft_caps_after_sell_minutes``).
        """
        try:
            w = float(minutes)
        except (TypeError, ValueError):
            return False
        if w <= 0.0 or w != w:
            return False
        _uid = str(self.user_id or "").strip()
        if not _uid:
            return False
        last = _LAST_EQUITY_SELL_UTC.get(_uid)
        if last is None or not isinstance(last, datetime):
            return False
        now = self.now
        if not isinstance(now, datetime):
            return False
        try:
            delta_s = (now - last).total_seconds()
        except Exception:
            return False
        if delta_s < 0.0:
            return True
        return (delta_s / 60.0) <= w + 1e-9

    def same_day_close_blocked(self, sym_u: str, pos_row: dict) -> bool:
        blocked, reason = block_same_day_close_for_pdt(
            config=self.config,
            account_equity=self.account_equity,
            entry_time_iso=pos_row.get("entry_time"),
            now_et=self.now,
            entry_time_uncertain=bool(pos_row.get("entry_time_uncertain")),
        )
        if blocked and reason:
            print(self.now.strftime("%H:%M ET"), sym_u, reason, flush=True)
        return blocked

    def record_engine_after_sell(
        self,
        sym: str,
        exit_reason: ExitReason,
        exit_price: float,
        *,
        entry_price_for_stop: float | None = None,
        remaining_qty_after: int | None = None,
    ) -> None:
        r = exit_reason.value
        if r in ("stop_loss", "option_stop_loss"):
            ep = entry_price_for_stop if (entry_price_for_stop is not None and entry_price_for_stop > 0) else None
            self.engine.record_stop_loss(sym, self.now, entry_price=ep)
        elif r in (
            "tp",
            "take_profit",
            "partial_take_profit",
            "trail",
            "trailing_stop",
            "option_profit_take",
            "option_profit_take_partial",
            "option_pnl_trail",
            "option_max_hold_days",
            "option_underlying_break_signal",
            "news_sentiment",
            "time_bars",
            "signal_exit",
            "kill_switch",
            "kill_switch_partial",
            "risk_cap_rebalance",
            "overweight_trim",
        ):
            after_partial = r in (
                "partial_take_profit",
                "kill_switch_partial",
                "option_profit_take_partial",
                "risk_cap_rebalance",
                "overweight_trim",
            )
            self.engine.record_profit_exit(sym, self.now, float(exit_price), after_partial=after_partial)
            if r in ("kill_switch", "kill_switch_partial"):
                self.engine.record_kill_switch_exit(sym, self.now)
        if remaining_qty_after is not None:
            try:
                record_sell_after_exit(
                    sym,
                    self.user_id,
                    self.data_dir,
                    self.now,
                    exit_reason,
                    int(remaining_qty_after),
                    self.config,
                )
            except Exception:
                log.exception("[%s] position_state record_sell_after_exit failed", self.user_id)

    def notify_sqqq_tracker_removed(self, sym: str) -> None:
        if str(sym).upper() != "SQQQ":
            return
        bear_cfg = (self.config.get("universe") or {}).get("bear_etfs") or {}
        if not (bear_cfg.get("sqqq_reentry") or {}).get("enabled", False):
            return
        qqq_close = None
        try:
            qdf = self.broker.get_bars("QQQ", timeframe="1Day", limit=1)
            if not qdf.empty:
                qqq_close = float(qdf["close"].iloc[-1])
        except Exception:
            pass
        record_sqqq_full_exit(self.user_id, self.data_dir, self.now, qqq_close)

    @staticmethod
    def synthetic_option_tracker_pos(bp: dict[str, Any]) -> dict[str, Any]:
        qty = abs(int(float(bp.get("qty") or 0)))
        return {
            "qty": qty,
            "side": "long",
            "entry_time": "",
            "entry_time_uncertain": True,
        }


__all__ = [
    "LiveExitContext",
    "reentry_block_allows_despite_flag",
]
