"""
Alpaca broker integration: account, bars, quotes (spread), order submission.

Uses alpaca-py (TradingClient + StockHistoricalDataClient).

Credentials can be supplied in two ways (checked in order):
  1. Explicit ``api_key`` / ``secret`` / ``paper`` arguments (multi-user mode).
  2. Environment variables: ``APCA_API_KEY_ID`` / ``APCA_API_SECRET_KEY`` (paper only) or
     ``ALPACA_LIVE_API_KEY_ID`` / ``ALPACA_LIVE_API_SECRET_KEY`` (live only — paper keys are not used as fallback).

Retries on connection errors (RemoteDisconnected, ConnectionError) so the loop
doesn't crash.
"""
import logging
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, is_dataclass
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence, TypeVar

import pandas as pd
from alpaca.common.exceptions import APIError

T = TypeVar("T")

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import (
        GetOrdersRequest,
        GetPortfolioHistoryRequest,
        LimitOrderRequest,
        MarketOrderRequest,
    )
    from alpaca.trading.enums import OrderSide, OrderType as AlpacaOrderType, TimeInForce
    from alpaca.data.historical import NewsClient, StockHistoricalDataClient
    from alpaca.data.requests import NewsRequest, StockBarsRequest, StockLatestQuoteRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed
    ALPACA_AVAILABLE = True
except ImportError:
    ALPACA_AVAILABLE = False
    DataFeed = None
    NewsClient = None
    NewsRequest = None

ALPACA_OPTIONS_CHAIN = False
OptionHistoricalDataClient = None
OptionChainRequest = None
OptionLatestQuoteRequest = None
OptionsFeed = None
if ALPACA_AVAILABLE:
    try:
        from alpaca.data.historical.option import OptionHistoricalDataClient
        from alpaca.data.requests import OptionChainRequest
        from alpaca.data.enums import OptionsFeed

        ALPACA_OPTIONS_CHAIN = True
    except ImportError:
        pass
    if ALPACA_OPTIONS_CHAIN:
        try:
            from alpaca.data.requests import OptionLatestQuoteRequest as _OptLatestQuoteReq

            OptionLatestQuoteRequest = _OptLatestQuoteReq
        except ImportError:
            OptionLatestQuoteRequest = None

from ..execution import OrderRequest, OrderType
from ..options_premium_risk import is_option_symbol
from ..options_selector import OptionContractCandidate, parse_occ_equity_option_symbol
from .base import BrokerAccount, BrokerCapabilities, BrokerPosition, BrokerQuote

log = logging.getLogger(__name__)

StockLatestTradeRequest = None
GetCorporateAnnouncementsRequest = None
CorporateActionType = None
TradingStream = None
if ALPACA_AVAILABLE:
    try:
        from alpaca.data.requests import StockLatestTradeRequest as _StockLatestTradeRequest

        StockLatestTradeRequest = _StockLatestTradeRequest
    except ImportError:
        pass
    try:
        from alpaca.trading.requests import GetCorporateAnnouncementsRequest as _GetCorporateAnnouncementsRequest
        from alpaca.trading.enums import CorporateActionType as _CorporateActionType

        GetCorporateAnnouncementsRequest = _GetCorporateAnnouncementsRequest
        CorporateActionType = _CorporateActionType
    except ImportError:
        pass
    try:
        from alpaca.trading.stream import TradingStream as _TradingStream

        TradingStream = _TradingStream
    except ImportError:
        pass


@dataclass
class QuoteInfo:
    """Bid/ask quote. ``mid = (ask + bid) / 2``; fractional spread ``= abs(ask - bid) / mid``;
    ``spread_pct`` stores that fraction × 100 (percent points) for caps in config and gates."""
    bid: float
    ask: float
    mid: float
    spread_pct: float
    timestamp: datetime | None = None  # quote time (UTC); None = unknown
    last: float | None = None  # latest trade price when available (separate MD request)
    skip_spread_check: bool = False  # True if bid or ask missing / invalid — do not gate on spread

    def reference_mid(self, fallback: float) -> float:
        """Executable mid when NBBO is usable; else *fallback* (e.g. last daily close)."""
        if self.mid > 0:
            return float(self.mid)
        return float(fallback)

    def is_stale(self, max_age_seconds: float) -> bool:
        """True if quote is older than max_age_seconds (use for spread gate)."""
        if self.timestamp is None:
            return False  # unknown age: treat as fresh
        from datetime import timezone
        now = datetime.now(timezone.utc)
        ts = self.timestamp
        if getattr(ts, "tzinfo", None) is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (now - ts).total_seconds() > max_age_seconds


def filled_avg_price_from_order(order: Any) -> float | None:
    """Return Alpaca order ``filled_avg_price`` (or legacy alias) when present and positive."""
    if order is None:
        return None
    for attr in ("filled_avg_price", "filled_average_price"):
        raw = getattr(order, attr, None)
        if raw is None or raw == "":
            continue
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        if v > 0:
            return v
    return None


def _order_id(order: Any) -> str:
    return str(getattr(order, "id", "") or "")


def _order_status(order: Any) -> str:
    return str(getattr(order, "status", "") or "")


def _order_filled_qty(order: Any) -> str | None:
    raw = getattr(order, "filled_qty", None)
    if raw in (None, ""):
        raw = getattr(order, "qty", None)
    if raw in (None, ""):
        return None
    return str(raw)


def _log_order_intent(symbol: str, side: str, *, qty: Any = None, notional: Any = None, source: str = "broker") -> None:
    qty_text = "n/a" if qty in (None, "") else str(qty)
    try:
        notional_text = "n/a" if notional in (None, "") else "%.2f" % float(notional)
    except (TypeError, ValueError):
        notional_text = str(notional)
    log.info(
        "ORDER_INTENT symbol=%s side=%s qty=%s notional=%s source=%s",
        str(symbol or "").strip().upper() or "?",
        str(side or "").strip().lower() or "?",
        qty_text,
        notional_text,
        str(source or "broker"),
    )


def _log_order_submitted(
    symbol: str,
    side: str,
    order: Any,
    *,
    qty: Any = None,
    notional: Any = None,
    source: str = "broker",
    metadata: Mapping[str, Any] | None = None,
) -> None:
    qty_text = "n/a" if qty in (None, "") else str(qty)
    try:
        notional_text = "n/a" if notional in (None, "") else "%.2f" % float(notional)
    except (TypeError, ValueError):
        notional_text = str(notional)
    meta = metadata or {}
    if meta:
        log.info(
            "ORDER_SUBMITTED symbol=%s side=%s qty=%s notional=%s source=%s order_id=%s status=%s allocator_requested_notional=%s allocator_requested_qty=%s bounded_pilot_applied=%s final_submitted_qty=%s final_reference_price=%s final_estimated_notional=%s broker_request_type=%s broker_returned_qty=%s broker_returned_notional=%s",
            str(symbol or "").strip().upper() or "?",
            str(side or "").strip().lower() or "?",
            qty_text,
            notional_text,
            str(source or "broker"),
            _order_id(order),
            _order_status(order),
            meta.get("allocator_requested_notional", "n/a"),
            meta.get("allocator_requested_qty", "n/a"),
            str(bool(meta.get("bounded_pilot_applied"))).lower(),
            meta.get("final_submitted_qty", "n/a"),
            meta.get("final_reference_price", "n/a"),
            meta.get("final_estimated_notional", "n/a"),
            meta.get("broker_request_type", "n/a"),
            getattr(order, "qty", "n/a"),
            getattr(order, "notional", "n/a"),
        )
        return
    log.info(
        "ORDER_SUBMITTED symbol=%s side=%s qty=%s notional=%s source=%s order_id=%s status=%s",
        str(symbol or "").strip().upper() or "?",
        str(side or "").strip().lower() or "?",
        qty_text,
        notional_text,
        str(source or "broker"),
        _order_id(order),
        _order_status(order),
    )


def _log_order_filled_if_present(symbol: str, side: str, order: Any) -> None:
    status = _order_status(order).lower()
    filled_qty = _order_filled_qty(order)
    filled_avg = filled_avg_price_from_order(order)
    if status not in {"filled", "partially_filled"} and filled_avg is None:
        return
    if filled_qty is None:
        return
    log.info(
        "ORDER_FILLED symbol=%s side=%s filled_qty=%s filled_avg_price=%s order_id=%s",
        str(symbol or "").strip().upper() or "?",
        str(side or "").strip().lower() or "?",
        filled_qty,
        "n/a" if filled_avg is None else "%.6g" % float(filled_avg),
        _order_id(order),
    )


def _log_order_skip(symbol: str, reason: str, *, source: str = "broker") -> None:
    log.info(
        "ORDER_SKIP symbol=%s reason=%s source=%s",
        str(symbol or "").strip().upper() or "?",
        str(reason or "unknown"),
        str(source or "broker"),
    )


class AlpacaBroker:
    """Alpaca broker: account, historical bars, latest quote, order submission."""
    broker_name = "alpaca"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        api_key: str | None = None,
        secret: str | None = None,
        paper: bool | None = None,
    ):
        if not ALPACA_AVAILABLE:
            raise RuntimeError("alpaca-py is required for Alpaca broker. Install: pip install alpaca-py")
        self.config = config or {}
        broker_cfg = self.config.get("broker", {})

        # --- Resolve paper vs live -----------------------------------------
        if paper is not None:
            # Explicit argument takes priority (multi-user mode).
            self.paper = paper
        else:
            # Legacy: config → env overrides
            paper_cfg = broker_cfg.get("paper", True)
            apca_paper = _env("APCA_PAPER")
            alpaca_live = _env("ALPACA_LIVE")
            if apca_paper is not None:
                self.paper = str(apca_paper).strip().lower() in ("true", "1", "yes")
            elif alpaca_live is not None:
                self.paper = not (str(alpaca_live).strip().lower() in ("true", "1", "yes"))
            else:
                self.paper = paper_cfg

        # --- Resolve credentials -------------------------------------------
        if api_key is not None and secret is not None:
            # Explicit credentials (multi-user mode) — use directly.
            resolved_key = api_key
            resolved_secret = secret
        else:
            # Legacy: read from environment / config.
            if self.paper:
                resolved_key = _env("APCA_API_KEY_ID") or broker_cfg.get("api_key")
                resolved_secret = _env("APCA_API_SECRET_KEY") or broker_cfg.get("secret_key")
            else:
                # Live API rejects paper keys — do not fall back to APCA_* (avoids opaque 401/unauthorized).
                resolved_key = _env("ALPACA_LIVE_API_KEY_ID") or broker_cfg.get("api_key")
                resolved_secret = _env("ALPACA_LIVE_API_SECRET_KEY") or broker_cfg.get("secret_key")
        if not resolved_key or not resolved_secret:
            if self.paper:
                raise ValueError(
                    "Alpaca paper credentials required. Set APCA_API_KEY_ID and APCA_API_SECRET_KEY "
                    "(Alpaca dashboard → Paper Trading → API Keys)."
                )
            raise ValueError(
                "Alpaca LIVE credentials required. Set ALPACA_LIVE_API_KEY_ID and "
                "ALPACA_LIVE_API_SECRET_KEY (Alpaca dashboard → Live → API Keys). "
                "Paper keys (APCA_*) are not accepted on the live trading API."
            )

        self._trading = TradingClient(resolved_key, resolved_secret, paper=self.paper)
        self._api_key = resolved_key
        self._secret = resolved_secret
        self._fractionable_cache: dict[str, bool] = {}
        self._tradable_cache: dict[str, bool] = {}
        self._order_state_by_id: dict[str, dict[str, Any]] = {}
        stream_cfg = broker_cfg.get("trade_update_stream", {}) or {}
        self._trade_stream_enabled = bool(stream_cfg.get("enabled", False))
        self._trade_stream: Any = None
        self._trade_stream_thread: threading.Thread | None = None
        self._trade_stream_started = False
        self._data = StockHistoricalDataClient(resolved_key, resolved_secret)
        self._news: Any = None
        if NewsClient is not None:
            try:
                self._news = NewsClient(resolved_key, resolved_secret)
            except Exception as e:
                log.warning("Alpaca NewsClient unavailable (%s); news catalyst disabled", e)
        self._screener: Any = None
        try:
            from alpaca.data.historical.screener import ScreenerClient

            self._screener = ScreenerClient(resolved_key, resolved_secret)
        except Exception as e:
            log.warning("Alpaca ScreenerClient unavailable (%s); dynamic universe uses fallback movers", e)
        # IEX is free; SIP requires paid subscription ("subscription does not permit querying recent SIP data")
        feed_name = (broker_cfg.get("data_feed") or "iex").strip().upper()
        self._feed_name = feed_name
        self._feed_enum = getattr(DataFeed, feed_name, DataFeed.IEX) if ALPACA_AVAILABLE else None
        self._retry_times = int(broker_cfg.get("api_retry_times", 3))
        self._retry_delay_sec = float(broker_cfg.get("api_retry_delay_sec", 3.0))
        self._option_data: Any = None
        self._options_feed: Any = None
        if ALPACA_OPTIONS_CHAIN and OptionHistoricalDataClient is not None and OptionsFeed is not None:
            self._option_data = OptionHistoricalDataClient(resolved_key, resolved_secret)
            opt_feed_name = (broker_cfg.get("options_feed") or "indicative").strip().lower()
            self._options_feed = getattr(OptionsFeed, opt_feed_name.upper(), OptionsFeed.INDICATIVE)

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            supports_fractional_equities=True,
            supports_options=self._option_data is not None,
            supports_crypto=False,
            supports_shorting=True,
            supports_extended_hours=True,
            supports_market_data=True,
        )

    @staticmethod
    def _stream_value(obj: Any, key: str, default: Any = None) -> Any:
        if isinstance(obj, Mapping):
            return obj.get(key, default)
        return getattr(obj, key, default)

    @classmethod
    def _stream_order_state(cls, *, event: str, order: Any) -> dict[str, Any]:
        status_by_event = {
            "new": "new",
            "accepted": "accepted",
            "partial_fill": "partially_filled",
            "partially_filled": "partially_filled",
            "fill": "filled",
            "filled": "filled",
            "canceled": "canceled",
            "cancelled": "canceled",
            "expired": "expired",
            "rejected": "rejected",
        }
        event_s = str(event or "").strip().lower()
        status = status_by_event.get(event_s) or str(cls._stream_value(order, "status", "") or "").lower()
        timestamp = (
            cls._stream_value(order, "updated_at")
            or cls._stream_value(order, "filled_at")
            or cls._stream_value(order, "submitted_at")
            or cls._stream_value(order, "created_at")
            or datetime.now(timezone.utc)
        )
        return {
            "symbol": str(cls._stream_value(order, "symbol", "") or "").strip().upper(),
            "side": str(cls._stream_value(order, "side", "") or "").strip().lower(),
            "order_id": str(cls._stream_value(order, "id", "") or ""),
            "status": status,
            "qty": cls._stream_value(order, "qty"),
            "filled_qty": cls._stream_value(order, "filled_qty"),
            "avg_fill_price": (
                cls._stream_value(order, "filled_avg_price")
                or cls._stream_value(order, "filled_average_price")
            ),
            "timestamp": timestamp,
            "event": event_s,
        }

    def _ensure_order_state_store(self) -> dict[str, dict[str, Any]]:
        """Return the local order-state store, creating it for test doubles that bypass __init__."""
        store = getattr(self, "_order_state_by_id", None)
        if not isinstance(store, dict):
            store = {}
            self._order_state_by_id = store
        return store

    def _record_order_state(self, *, event: str, order: Any) -> dict[str, Any] | None:
        state = self._stream_order_state(event=event, order=order)
        order_id = str(state.get("order_id") or "")
        if not order_id:
            return None
        self._ensure_order_state_store()[order_id] = state
        return state

    def get_order_state(self, order_id: str) -> dict[str, Any] | None:
        """Return the latest locally observed order state from stream or polling."""
        return self._ensure_order_state_store().get(str(order_id or ""))

    def handle_trade_update(self, update: Any) -> dict[str, Any] | None:
        """Record one Alpaca trade update event. Public for deterministic tests."""
        event = self._stream_value(update, "event", "")
        order = self._stream_value(update, "order", None)
        if order is None:
            log.info("ALPACA_TRADE_STREAM_EVENT event=%s order_id=n/a status=n/a", event or "unknown")
            return None
        state = self._record_order_state(event=str(event), order=order)
        if state is None:
            return None
        log.info(
            "ALPACA_TRADE_STREAM_EVENT symbol=%s side=%s order_id=%s event=%s status=%s qty=%s filled_qty=%s avg_fill_price=%s timestamp=%s",
            state.get("symbol") or "?",
            state.get("side") or "?",
            state.get("order_id") or "",
            state.get("event") or "",
            state.get("status") or "",
            state.get("qty"),
            state.get("filled_qty"),
            state.get("avg_fill_price"),
            state.get("timestamp"),
        )
        if str(state.get("status") or "").lower() in {"partially_filled", "filled"}:
            log.info(
                "ALPACA_TRADE_STREAM_FILL symbol=%s side=%s order_id=%s status=%s filled_qty=%s avg_fill_price=%s timestamp=%s",
                state.get("symbol") or "?",
                state.get("side") or "?",
                state.get("order_id") or "",
                state.get("status") or "",
                state.get("filled_qty"),
                state.get("avg_fill_price"),
                state.get("timestamp"),
            )
        if str(state.get("status") or "").lower() == "rejected":
            log.info(
                "ALPACA_TRADE_STREAM_REJECTED symbol=%s side=%s order_id=%s qty=%s timestamp=%s",
                state.get("symbol") or "?",
                state.get("side") or "?",
                state.get("order_id") or "",
                state.get("qty"),
                state.get("timestamp"),
            )
        return state

    async def _handle_trade_update_async(self, update: Any) -> None:
        self.handle_trade_update(update)

    def start_trade_update_stream(self, stream_factory: Callable[..., Any] | None = None) -> bool:
        """Start optional Alpaca trade update streaming in a daemon thread.

        Returns False when disabled/unavailable/setup fails. REST polling remains the source of truth.
        """
        if self._trade_stream_started:
            return True
        if not self._trade_stream_enabled:
            log.info("ALPACA_TRADE_STREAM_FALLBACK_POLLING reason=disabled")
            return False
        factory = stream_factory or TradingStream
        if factory is None:
            log.info("ALPACA_TRADE_STREAM_FALLBACK_POLLING reason=stream_unavailable")
            return False
        try:
            stream = factory(self._api_key, self._secret, paper=self.paper)
            stream.subscribe_trade_updates(self._handle_trade_update_async)
        except Exception as exc:
            log.warning("ALPACA_TRADE_STREAM_FALLBACK_POLLING reason=setup_failed error=%s", exc)
            return False

        self._trade_stream = stream
        self._trade_stream_started = True
        log.info("ALPACA_TRADE_STREAM_START paper=%s", self.paper)

        def _run() -> None:
            try:
                stream.run()
            except Exception as exc:
                self._trade_stream_started = False
                log.warning("ALPACA_TRADE_STREAM_FALLBACK_POLLING reason=stream_disconnected error=%s", exc)

        self._trade_stream_thread = threading.Thread(
            target=_run,
            name="alpaca-trade-updates",
            daemon=True,
        )
        self._trade_stream_thread.start()
        return True

    def _with_retry(self, fn: Callable[[], T]) -> T:
        """Retry on connection errors (e.g. Remote end closed connection without response)."""
        last: BaseException | None = None
        for attempt in range(self._retry_times):
            try:
                return fn()
            except Exception as e:
                last = e
                name = type(e).__name__
                if "RemoteDisconnected" in name or "ConnectionError" in name or "Connection aborted" in str(e) or "ProtocolError" in name:
                    if attempt < self._retry_times - 1:
                        time.sleep(self._retry_delay_sec)
                        continue
                raise
        if last:
            raise last
        raise RuntimeError("retry failed")

    def get_equity(self) -> float:
        def _get() -> float:
            acc = self._trading.get_account()
            return float(acc.equity or 0)
        return self._with_retry(_get)

    def get_buying_power(self) -> float:
        """Cash available to open new positions (avoids Alpaca 403 insufficient buying power)."""
        def _get() -> float:
            acc = self._trading.get_account()
            return float(getattr(acc, "buying_power", 0) or getattr(acc, "cash", 0) or 0)
        return self._with_retry(_get)

    def get_account(self) -> BrokerAccount:
        """Canonical account view for broker-neutral diagnostics."""

        def _get() -> BrokerAccount:
            acc = self._trading.get_account()

            def fnum(name: str) -> float | None:
                raw = getattr(acc, name, None)
                if raw in (None, ""):
                    return None
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    return None

            return BrokerAccount(
                broker=self.broker_name,
                account_id=str(getattr(acc, "id", "") or getattr(acc, "account_number", "") or "") or None,
                account_type="paper" if self.paper else "live",
                status=str(getattr(acc, "status", "") or "") or None,
                buying_power=fnum("buying_power"),
                equity=fnum("equity"),
                cash=fnum("cash"),
                raw={},
            )

        return self._with_retry(_get)

    def get_account_snapshot(self) -> dict[str, Any]:
        """
        Current equity, last_equity (prior regular session close), cash.
        Session P&L (mark-to-market since prior close) ≈ equity - last_equity when last_equity is set.
        """
        def _get() -> dict[str, Any]:
            acc = self._trading.get_account()

            def fnum(x: Any) -> float | None:
                if x is None or x == "":
                    return None
                try:
                    return float(x)
                except (TypeError, ValueError):
                    return None

            return {
                "equity": fnum(getattr(acc, "equity", None)) or 0.0,
                "last_equity": fnum(getattr(acc, "last_equity", None)),
                "cash": fnum(getattr(acc, "cash", None)),
            }

        return self._with_retry(_get)

    def get_recent_news(
        self,
        symbols: list[str] | tuple[str, ...],
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 50,
        exclude_contentless: bool = False,
    ) -> list[Any]:
        """Fetch recent Alpaca news articles for symbols. Best-effort caller handles failures."""
        if self._news is None or NewsRequest is None:
            return []
        syms = [str(s or "").strip().upper() for s in symbols if str(s or "").strip()]
        if not syms:
            return []
        req = NewsRequest(
            symbols=",".join(list(dict.fromkeys(syms))),
            start=start,
            end=end,
            limit=max(1, int(limit)),
            include_content=False,
            exclude_contentless=bool(exclude_contentless),
        )
        resp = self._with_retry(lambda: self._news.get_news(req))
        if resp is None:
            return []
        if isinstance(resp, list):
            return resp
        news = getattr(resp, "news", None)
        if news is not None:
            return list(news)
        try:
            return list(resp)
        except TypeError:
            return []

    def get_corporate_actions(
        self,
        symbols: Sequence[str],
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[Any]:
        """Fetch Alpaca corporate announcements for symbols, best-effort.

        Returns an empty list when the SDK/API path is unavailable so scanner callers can continue safely.
        """
        syms = [str(s or "").strip().upper() for s in symbols if str(s or "").strip()]
        if not syms:
            return []
        if GetCorporateAnnouncementsRequest is None or not hasattr(self._trading, "get_corporate_announcements"):
            log.info("ALPACA_CORP_ACTION_FALLBACK reason=api_unavailable")
            return []
        start_d = start or (date.today() - timedelta(days=7))
        end_d = end or date.today()
        ca_types = []
        if CorporateActionType is not None:
            for name in ("SPLIT", "MERGER", "DIVIDEND", "SPINOFF"):
                val = getattr(CorporateActionType, name, None)
                if val is not None:
                    ca_types.append(val)
        out: list[Any] = []
        for sym in syms:
            try:
                req = GetCorporateAnnouncementsRequest(
                    ca_types=ca_types,
                    since=start_d,
                    until=end_d,
                    symbol=sym,
                )
                rows = self._with_retry(lambda req=req: self._trading.get_corporate_announcements(req))
                out.extend(list(rows or []))
            except Exception as exc:
                log.info("ALPACA_CORP_ACTION_FALLBACK symbol=%s reason=fetch_failed error=%s", sym, str(exc)[:180])
        return out

    def get_portfolio_daily_pnl_for_date(self, d: date) -> dict[str, Any] | None:
        """
        Alpaca portfolio history (1D bars): profit_loss / profit_loss_pct / equity for calendar date d in US/Eastern.
        Returns None if the API has no bar for that day (e.g. weekend) or on error.
        """
        try:
            import pytz
            from alpaca.trading.requests import GetPortfolioHistoryRequest

            et = pytz.timezone("America/New_York")
        except Exception:
            return None

        def _fetch() -> dict[str, Any] | None:
            req = GetPortfolioHistoryRequest(period="6M", timeframe="1D", date_end=d)
            ph = self._trading.get_portfolio_history(req)
            ts_list = list(ph.timestamp or [])
            pl_list = list(ph.profit_loss or [])
            plp_list = list(ph.profit_loss_pct or [])
            eq_list = list(ph.equity or [])
            if not ts_list:
                return None
            target_idx: int | None = None
            for i, ts in enumerate(ts_list):
                dt_et = datetime.fromtimestamp(int(ts), tz=pytz.UTC).astimezone(et)
                if dt_et.date() == d:
                    target_idx = i
            if target_idx is None:
                return None
            out: dict[str, Any] = {
                "profit_loss": float(pl_list[target_idx]) if target_idx < len(pl_list) else 0.0,
                "equity": float(eq_list[target_idx]) if target_idx < len(eq_list) else 0.0,
            }
            if target_idx < len(plp_list) and plp_list[target_idx] is not None:
                out["profit_loss_pct"] = float(plp_list[target_idx])
            else:
                out["profit_loss_pct"] = None
            return out

        try:
            return self._with_retry(_fetch)
        except Exception:
            return None

    def get_portfolio_equity_series(
        self,
        period: str = "1A",
        timeframe: str = "1D",
        date_end: date | None = None,
    ) -> dict[str, Any] | None:
        """
        Portfolio history bars for dashboard charts: aligned ``dates`` (ISO, ET),
        ``equity``, and ``daily_pnl`` (``profit_loss`` from Alpaca per bar, else 0).
        """
        try:
            import pytz
            from alpaca.trading.requests import GetPortfolioHistoryRequest

            et = pytz.timezone("America/New_York")
        except Exception:
            return None

        def _fetch() -> dict[str, Any] | None:
            end = date_end or date.today()
            req = GetPortfolioHistoryRequest(period=period, timeframe=timeframe, date_end=end)
            ph = self._trading.get_portfolio_history(req)
            ts_list = list(ph.timestamp or [])
            eq_list = list(ph.equity or [])
            pl_list = list(ph.profit_loss or [])
            if not ts_list or not eq_list:
                return None
            dates_out: list[str] = []
            equity_out: list[float] = []
            pnl_out: list[float] = []
            for i, ts in enumerate(ts_list):
                dt_et = datetime.fromtimestamp(int(ts), tz=pytz.UTC).astimezone(et)
                dates_out.append(dt_et.date().isoformat())
                equity_out.append(float(eq_list[i]) if i < len(eq_list) else 0.0)
                if i < len(pl_list) and pl_list[i] is not None and str(pl_list[i]).strip() != "":
                    try:
                        pnl_out.append(float(pl_list[i]))
                    except (TypeError, ValueError):
                        pnl_out.append(0.0)
                else:
                    pnl_out.append(0.0)
            # Fallback daily $ PnL from equity deltas when API pnl is effectively missing
            if len(equity_out) >= 2 and sum(abs(x) for x in pnl_out) < 1e-6:
                pnl_out = [0.0]
                for j in range(1, len(equity_out)):
                    pnl_out.append(equity_out[j] - equity_out[j - 1])
            return {"dates": dates_out, "equity": equity_out, "daily_pnl": pnl_out}

        try:
            return self._with_retry(_fetch)
        except Exception:
            return None

    def get_positions(self) -> list[dict[str, Any]]:
        def _get() -> list[dict[str, Any]]:
            positions = self._trading.get_all_positions()
            out = []
            for p in positions:
                _avg = float(getattr(p, "avg_entry_price", 0) or 0)
                row: dict[str, Any] = {
                    "symbol": p.symbol,
                    "qty": float(p.qty),
                    "side": str(p.side),
                    "market_value": float(p.market_value or 0),
                    "cost_basis": float(p.cost_basis or 0),
                    "unrealized_pl": float(p.unrealized_pl or 0),
                    "avg_entry_price": _avg,
                    "avg_price": _avg,
                    "current_price": float(getattr(p, "current_price", 0) or 0),
                }
                for _extra in ("qty_held_for_orders", "qty_available"):
                    _raw = getattr(p, _extra, None)
                    if _raw is not None and str(_raw).strip() != "":
                        try:
                            row[_extra] = float(_raw)
                        except (TypeError, ValueError):
                            pass
                out.append(row)
            return out
        return self._with_retry(_get)

    def list_positions(self) -> list[BrokerPosition]:
        """Canonical positions without changing legacy ``get_positions`` rows."""

        out: list[BrokerPosition] = []
        for row in self.get_positions():
            out.append(
                BrokerPosition(
                    broker=self.broker_name,
                    symbol=str(row.get("symbol") or "").upper(),
                    qty=float(row.get("qty") or 0.0),
                    side=str(row.get("side") or "long").lower(),
                    market_value=float(row["market_value"]) if row.get("market_value") is not None else None,
                    cost_basis=float(row["cost_basis"]) if row.get("cost_basis") is not None else None,
                    avg_entry_price=float(row["avg_entry_price"]) if row.get("avg_entry_price") is not None else None,
                    current_price=float(row["current_price"]) if row.get("current_price") is not None else None,
                    unrealized_pl=float(row["unrealized_pl"]) if row.get("unrealized_pl") is not None else None,
                    raw=row,
                )
            )
        return out

    def get_position(self, symbol: str) -> dict[str, Any] | None:
        """Single open position row (same shape as items from :meth:`get_positions`) or ``None`` if flat."""

        su = str(symbol or "").strip()
        if not su:
            return None

        def _get() -> dict[str, Any] | None:
            try:
                p = self._trading.get_open_position(su)
            except Exception:
                return None
            _avg = float(getattr(p, "avg_entry_price", 0) or 0)
            row: dict[str, Any] = {
                "symbol": getattr(p, "symbol", su),
                "qty": float(getattr(p, "qty", 0) or 0),
                "side": str(getattr(p, "side", "long")),
                "market_value": float(getattr(p, "market_value", 0) or 0),
                "cost_basis": float(getattr(p, "cost_basis", 0) or 0),
                "unrealized_pl": float(getattr(p, "unrealized_pl", 0) or 0),
                "avg_entry_price": _avg,
                "avg_price": _avg,
                "current_price": float(getattr(p, "current_price", 0) or 0),
            }
            for _extra in ("qty_held_for_orders", "qty_available"):
                _raw = getattr(p, _extra, None)
                if _raw is not None and str(_raw).strip() != "":
                    try:
                        row[_extra] = float(_raw)
                    except (TypeError, ValueError):
                        pass
            return row

        return self._with_retry(_get)

    def submit_market_sell(self, symbol: str, qty: float | int) -> Any | None:
        """Market DAY sell by quantity (inventory-free path for emergency trims)."""
        sym = str(symbol or "").strip().upper()
        try:
            q = float(qty)
        except (TypeError, ValueError):
            return None
        if not sym or q <= 0:
            return None
        from src.execution import OrderRequest, OrderType
        from src.trading_control import EntryBlocked, ShadowOrder, authorize_order_submission, log_order_submission_block
        auth_order = OrderRequest(sym, "sell", q, OrderType.MARKET)
        allowed, reason = authorize_order_submission(self.config, auth_order, paper=getattr(self, "paper", None))
        if not allowed:
            log_order_submission_block(
                self.config,
                auth_order,
                reason=reason or "ORDER_BLOCKED_TRADING_MODE",
                paper=getattr(self, "paper", None),
                route="submit_market_sell",
            )
            if reason in {"ENTRY_BLOCKED_SHADOW_MODE", "ORDER_BLOCKED_SHADOW_MODE"}:
                return ShadowOrder(auth_order)
            raise EntryBlocked(reason or "ORDER_BLOCKED_TRADING_MODE")

        def _sub() -> Any:
            return self._trading.submit_order(
                order_data=MarketOrderRequest(
                    symbol=sym,
                    qty=q,
                    side=OrderSide.SELL,
                    type=AlpacaOrderType.MARKET,
                    time_in_force=TimeInForce.DAY,
                )
            )

        return self._with_retry(_sub)

    def close_position(self, symbol: str) -> Any | None:
        """Close one open equity position through Alpaca's position-close API."""
        sym = str(symbol or "").strip().upper()
        if not sym:
            return None
        from src.execution import OrderRequest, OrderType
        from src.trading_control import EntryBlocked, ShadowOrder, authorize_order_submission, log_order_submission_block

        auth_order = OrderRequest(sym, "sell", 1, OrderType.MARKET)
        allowed, reason = authorize_order_submission(self.config, auth_order, paper=getattr(self, "paper", None))
        if not allowed:
            log_order_submission_block(
                self.config,
                auth_order,
                reason=reason or "ORDER_BLOCKED_TRADING_MODE",
                paper=getattr(self, "paper", None),
                route="close_position",
            )
            if reason in {"ENTRY_BLOCKED_SHADOW_MODE", "ORDER_BLOCKED_SHADOW_MODE"}:
                return ShadowOrder(auth_order)
            raise EntryBlocked(reason or "ORDER_BLOCKED_TRADING_MODE")

        def _close() -> Any:
            return self._trading.close_position(sym)

        return self._with_retry(_close)

    def get_bars(
        self,
        symbol: str,
        timeframe: str = "1Day",
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 300,
    ) -> pd.DataFrame:
        """Return OHLCV DataFrame with columns open, high, low, close, volume."""
        if end is None:
            end = datetime.utcnow()
        if start is None:
            if timeframe == "1Day":
                start = end - timedelta(days=400)
            else:
                start = end - timedelta(days=5)

        if timeframe == "1Day" or timeframe == TimeFrame.Day:
            tf = TimeFrame.Day
        elif timeframe == "1Min" or timeframe == TimeFrame.Minute:
            tf = TimeFrame.Minute
        else:
            tf = timeframe
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=tf,
            start=start,
            end=end,
            feed=self._feed_enum,
        )
        bars = self._with_retry(lambda: self._data.get_stock_bars(req))
        if bars is None or getattr(bars, "df", None) is None:
            return pd.DataFrame()
        df = bars.df
        # BarSet.df can be multi-index (symbol, column) or (timestamp, column)
        if isinstance(df.index, pd.MultiIndex) and "symbol" in list(df.index.names):
            try:
                df = df.xs(symbol, level="symbol").copy()
            except (KeyError, ValueError):
                return pd.DataFrame()
        if isinstance(df.columns, pd.MultiIndex):
            if symbol in df.columns.get_level_values(0):
                df = df[symbol].copy()
            else:
                return pd.DataFrame()
        need = {"open", "high", "low", "close", "volume"}
        renames = {
            "open_price": "open", "high_price": "high", "low_price": "low",
            "close_price": "close",
        }
        df = df.rename(columns=renames)
        cols = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
        if len(cols) < 5:
            return pd.DataFrame()
        df = df[cols].astype(float)
        return df.tail(limit)

    def get_bars_batch(
        self,
        symbols: list[str] | tuple[str, ...],
        timeframe: str = "1Day",
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 300,
    ) -> dict[str, pd.DataFrame]:
        """Return OHLCV frames keyed by symbol using one Alpaca multi-symbol bars request."""
        syms = [str(s or "").strip().upper() for s in symbols if str(s or "").strip()]
        if not syms:
            return {}
        if end is None:
            end = datetime.utcnow()
        if start is None:
            if timeframe == "1Day" or timeframe == TimeFrame.Day:
                start = end - timedelta(days=400)
            else:
                start = end - timedelta(days=5)

        if timeframe == "1Day" or timeframe == TimeFrame.Day:
            tf = TimeFrame.Day
        elif timeframe == "1Min" or timeframe == TimeFrame.Minute:
            tf = TimeFrame.Minute
        else:
            tf = timeframe
        req = StockBarsRequest(
            symbol_or_symbols=syms,
            timeframe=tf,
            start=start,
            end=end,
            feed=self._feed_enum,
        )
        bars = self._with_retry(lambda: self._data.get_stock_bars(req))
        if bars is None or getattr(bars, "df", None) is None:
            return {s: pd.DataFrame() for s in syms}
        df = bars.df
        renames = {
            "open_price": "open",
            "high_price": "high",
            "low_price": "low",
            "close_price": "close",
        }
        out: dict[str, pd.DataFrame] = {}
        if isinstance(df.index, pd.MultiIndex) and "symbol" in list(df.index.names):
            for sym in syms:
                try:
                    sub = df.xs(sym, level="symbol").copy()
                except (KeyError, ValueError):
                    out[sym] = pd.DataFrame()
                    continue
                sub = sub.rename(columns=renames)
                cols = [c for c in ["open", "high", "low", "close", "volume"] if c in sub.columns]
                out[sym] = sub[cols].astype(float).tail(limit) if len(cols) >= 5 else pd.DataFrame()
            return out
        if isinstance(df.columns, pd.MultiIndex):
            for sym in syms:
                if sym not in df.columns.get_level_values(0):
                    out[sym] = pd.DataFrame()
                    continue
                sub = df[sym].copy().rename(columns=renames)
                cols = [c for c in ["open", "high", "low", "close", "volume"] if c in sub.columns]
                out[sym] = sub[cols].astype(float).tail(limit) if len(cols) >= 5 else pd.DataFrame()
            return out
        if len(syms) == 1:
            sub = df.rename(columns=renames)
            cols = [c for c in ["open", "high", "low", "close", "volume"] if c in sub.columns]
            out[syms[0]] = sub[cols].astype(float).tail(limit) if len(cols) >= 5 else pd.DataFrame()
            return out
        return {s: pd.DataFrame() for s in syms}

    def _stock_latest_trade_price(self, symbol: str) -> float | None:
        if StockLatestTradeRequest is None:
            return None
        try:
            treq = StockLatestTradeRequest(symbol_or_symbols=symbol, feed=self._feed_enum)
            trades = self._with_retry(lambda: self._data.get_stock_latest_trade(treq))
            if trades and symbol in trades:
                lp = getattr(trades[symbol], "price", None)
                if lp is not None:
                    return float(lp)
        except Exception as e:
            log.debug("get_stock_latest_trade %s: %s", symbol, e)
        return None

    def get_latest_quote(self, symbol: str) -> QuoteInfo | None:
        sym = str(symbol or "").strip().upper()
        if is_option_symbol(sym):
            return None
        req = StockLatestQuoteRequest(symbol_or_symbols=sym, feed=self._feed_enum)
        quotes = self._with_retry(lambda: self._data.get_stock_latest_quote(req))
        if not quotes or sym not in quotes:
            return None
        q = quotes[sym]
        ts = None
        if hasattr(q, "timestamp") and q.timestamp is not None:
            ts = q.timestamp if isinstance(q.timestamp, datetime) else datetime.fromisoformat(str(q.timestamp).replace("Z", "+00:00"))
        bid = float(q.bid_price or 0)
        ask = float(q.ask_price or 0)

        if bid <= 0 or ask <= 0:
            last_px = self._stock_latest_trade_price(sym)
            mid = float(last_px) if last_px is not None and last_px > 0 else 0.0
            if log.isEnabledFor(logging.DEBUG):
                log.debug(f"{sym} bid={bid} ask={ask} last={last_px} skip_spread_check=True")
            return QuoteInfo(
                bid=bid,
                ask=ask,
                mid=mid,
                spread_pct=0.0,
                timestamp=ts,
                last=last_px,
                skip_spread_check=True,
            )

        mid = (bid + ask) / 2.0
        if mid <= 0:
            if log.isEnabledFor(logging.DEBUG):
                log.debug(f"{sym} invalid_quote bid={bid} ask={ask} mid={mid} skip_spread_check=True")
            return QuoteInfo(
                bid=bid,
                ask=ask,
                mid=0.0,
                spread_pct=0.0,
                timestamp=ts,
                last=None,
                skip_spread_check=True,
            )
        spread_pct = abs(ask - bid) / mid * 100.0
        if spread_pct > 15.0:
            log.warning("Unstable quote %s", sym)
            return None
        last: float | None = None
        if log.isEnabledFor(logging.DEBUG):
            last = self._stock_latest_trade_price(sym)
            log.debug(f"{sym} bid={bid} ask={ask} last={last}")
        return QuoteInfo(bid=bid, ask=ask, mid=mid, spread_pct=spread_pct, timestamp=ts, last=last)

    def get_quote(self, symbol: str) -> BrokerQuote | None:
        """Canonical quote view. Existing strategy code still uses ``get_latest_quote``."""

        q = self.get_latest_quote(symbol)
        if q is None:
            return None
        sym = str(symbol or "").strip().upper()
        return BrokerQuote(
            broker=self.broker_name,
            symbol=sym,
            bid=q.bid,
            ask=q.ask,
            last=q.last,
            timestamp=q.timestamp,
            source="alpaca",
            quote_age=None,
            raw={"skip_spread_check": q.skip_spread_check},
        )

    def get_quotes(self, symbols: Sequence[str]) -> dict[str, BrokerQuote]:
        return {
            str(sym or "").strip().upper(): quote
            for sym in symbols
            if (quote := self.get_quote(str(sym or ""))) is not None
        }

    def get_latest_trade(self, symbol: str) -> dict[str, Any] | None:
        price = self._stock_latest_trade_price(str(symbol or "").strip().upper())
        if price is None:
            return None
        return {"symbol": str(symbol or "").strip().upper(), "price": price, "source": "alpaca"}

    def get_option_latest_quote(self, symbol: str) -> QuoteInfo | None:
        """Latest NBBO-style quote for one OCC option symbol (options market data feed)."""
        if not self.paper:
            return None
        if self._option_data is None or OptionLatestQuoteRequest is None:
            return None
        sym = str(symbol or "").strip().upper()
        if not sym:
            return None

        def _fetch() -> Any:
            req = OptionLatestQuoteRequest(symbol_or_symbols=sym, feed=self._options_feed)
            return self._option_data.get_option_latest_quote(req)

        try:
            quotes = self._with_retry(_fetch)
        except Exception as e:
            print(
                datetime.now().strftime("%H:%M"),
                "option quote",
                sym,
                type(e).__name__,
                str(e)[:100],
                flush=True,
            )
            return None
        if not quotes or sym not in quotes:
            return None
        q = quotes[sym]
        bid = float(getattr(q, "bid_price", 0) or 0)
        ask = float(getattr(q, "ask_price", 0) or 0)
        ts = None
        if hasattr(q, "timestamp") and q.timestamp is not None:
            ts = q.timestamp if isinstance(q.timestamp, datetime) else datetime.fromisoformat(
                str(q.timestamp).replace("Z", "+00:00")
            )
        if bid <= 0 or ask <= 0:
            if log.isEnabledFor(logging.DEBUG):
                log.debug(f"{sym} bid={bid} ask={ask} last={None} skip_spread_check=True")
            return QuoteInfo(
                bid=bid,
                ask=ask,
                mid=0.0,
                spread_pct=0.0,
                timestamp=ts,
                last=None,
                skip_spread_check=True,
            )
        mid = (bid + ask) / 2.0
        if mid <= 0:
            if log.isEnabledFor(logging.DEBUG):
                log.debug(f"{sym} invalid_quote bid={bid} ask={ask} mid={mid} skip_spread_check=True")
            return QuoteInfo(
                bid=bid,
                ask=ask,
                mid=0.0,
                spread_pct=0.0,
                timestamp=ts,
                last=None,
                skip_spread_check=True,
            )
        spread_pct = abs(ask - bid) / mid * 100.0
        if spread_pct > 15.0:
            log.warning("Unstable quote %s", sym)
            return None
        if log.isEnabledFor(logging.DEBUG):
            log.debug(f"{sym} bid={bid} ask={ask} last={None}")
        return QuoteInfo(bid=bid, ask=ask, mid=mid, spread_pct=spread_pct, timestamp=ts, last=None)

    def get_option_chain_candidates(
        self,
        underlying: str,
        *,
        expiration_date_gte: date,
        expiration_date_lte: date,
    ) -> list[OptionContractCandidate]:
        """
        Option chain snapshots from Alpaca Market Data, mapped for `select_option_contract`.

        Requires options market data access (see broker.options_feed: indicative vs opra).
        Open interest is not in the snapshot model; set to 0 (use min_open_interest: 0 or rely on volume).
        """
        opts = (self.config or {}).get("options") or {}
        if not bool(opts.get("enabled")) or not self.paper:
            return []
        if self._option_data is None or OptionChainRequest is None:
            return []
        und = str(underlying or "").strip().upper()
        if not und:
            return []

        def _fetch() -> dict[str, Any]:
            req = OptionChainRequest(
                underlying_symbol=und,
                feed=self._options_feed,
                expiration_date_gte=expiration_date_gte,
                expiration_date_lte=expiration_date_lte,
            )
            return self._option_data.get_option_chain(req)

        try:
            chain = self._with_retry(_fetch)
        except Exception as e:
            print(
                datetime.now().strftime("%H:%M"),
                "option chain",
                und,
                type(e).__name__,
                str(e)[:120],
                flush=True,
            )
            return []

        if not chain:
            return []

        out: list[OptionContractCandidate] = []
        for sym, snap in chain.items():
            parsed = parse_occ_equity_option_symbol(sym)
            if parsed is None:
                continue
            root, exp, right, strike = parsed
            if root != und:
                continue
            lq = getattr(snap, "latest_quote", None)
            if lq is None:
                continue
            bid = float(getattr(lq, "bid_price", 0) or 0)
            ask = float(getattr(lq, "ask_price", 0) or 0)
            if bid <= 0 or ask <= 0:
                continue
            lt = getattr(snap, "latest_trade", None)
            vol = int(float(getattr(lt, "size", 0) or 0)) if lt is not None else 0
            delta: float | None = None
            gr = getattr(snap, "greeks", None)
            if gr is not None:
                raw_d = getattr(gr, "delta", None)
                if raw_d is not None:
                    try:
                        delta = float(raw_d)
                    except (TypeError, ValueError):
                        delta = None
                raw_iv = getattr(gr, "implied_volatility", None)
                if raw_iv is None:
                    raw_iv = getattr(gr, "iv", None)
                try:
                    iv = float(raw_iv) if raw_iv is not None else None
                except (TypeError, ValueError):
                    iv = None
            else:
                iv = None
            out.append(
                OptionContractCandidate(
                    symbol=str(sym).strip().upper(),
                    strike=strike,
                    expiration=exp,
                    right=right,
                    open_interest=0,
                    volume=vol,
                    bid=bid,
                    ask=ask,
                    delta=delta,
                    iv=iv,
                )
            )
        return out

    def is_asset_fractionable(self, symbol: str) -> bool:
        """Alpaca asset flag; defaults to ``True`` when lookup fails (preserve notional path)."""
        sym = str(symbol or "").strip().upper()
        if not sym:
            return True
        cached = self._fractionable_cache.get(sym)
        if cached is not None:
            return cached
        try:
            asset = self._trading.get_asset(sym)
            frac = bool(getattr(asset, "fractionable", True))
        except Exception as e:
            log.debug("get_asset fractionable lookup failed for %s: %s", sym, e)
            frac = True
        self._fractionable_cache[sym] = frac
        return frac

    def is_asset_tradable(self, symbol: str) -> bool:
        """Alpaca asset tradable flag; defaults to ``True`` when lookup fails."""
        sym = str(symbol or "").strip().upper()
        if not sym:
            return True
        tradable_cache = getattr(self, "_tradable_cache", {})
        cached = tradable_cache.get(sym)
        if cached is not None:
            return cached
        try:
            asset = self._trading.get_asset(sym)
            tradable = bool(getattr(asset, "tradable", True))
        except Exception as e:
            log.debug("get_asset tradable lookup failed for %s: %s", sym, e)
            tradable = True
        tradable_cache[sym] = tradable
        self._tradable_cache = tradable_cache
        return tradable

    def _whole_share_qty_from_notional(self, symbol: str, notional: float) -> tuple[int | None, float | None]:
        """Return fallback whole-share qty and the price used to compute it."""
        sym = str(symbol or "").strip().upper()
        mid: float | None = None
        q = self.get_latest_quote(sym)
        if q is not None and getattr(q, "mid", None):
            try:
                mid = float(q.mid)
            except (TypeError, ValueError):
                mid = None
        if mid is None or mid <= 0:
            mid = self._stock_latest_trade_price(sym)
        if mid is None or mid <= 0:
            return None, None
        qty = int(float(notional) / float(mid))
        if qty < 1:
            return 0, float(mid)
        return qty, float(mid)

    def submit_notional_market_day(self, action: Mapping[str, Any]) -> Any | None:
        """
        Notional **market** **DAY** equity order — same intent as Alpaca REST::

            api.submit_order(
                symbol=action["symbol"],
                notional=action["notional"],
                side=action["action"],  # or key ``side`` — ``buy`` / ``sell``
                type="market",
                time_in_force="day",
            )

        Implemented as ``MarketOrderRequest`` + ``TradingClient.submit_order``.
        Returns ``None`` if *action* lacks a non-empty symbol, positive notional, or a ``buy``/``sell`` side.
        """
        sym = str(action.get("symbol") or "").strip().upper()
        raw_side = action.get("action", action.get("side"))
        side_str = str(raw_side or "").strip().lower()
        if side_str not in ("buy", "sell"):
            _log_order_skip(sym or "?", "invalid_side")
            return None
        try:
            n = float(action.get("notional", 0) or 0)
        except (TypeError, ValueError):
            _log_order_skip(sym or "?", "invalid_notional")
            return None
        if not sym or n <= 0:
            _log_order_skip(sym or "?", "notional_nonpositive")
            return None
        from src.execution import OrderRequest, OrderType
        from src.controlled_live_equity import bounded_live_pilot_active, controlled_live_equity_active, controlled_live_exit_health
        from src.limited_live_pilot import finalize_pilot_submission_reservation, reserve_pilot_submission
        from src.limited_live_pilot import trading_day_et
        from src.trading_control import EntryBlocked, ShadowOrder, authorize_order_submission, log_order_submission_block

        auth_order = OrderRequest(
            symbol=sym,
            side=side_str,
            quantity=1,
            order_type=OrderType.MARKET,
            notional=n,
        )
        setattr(auth_order, "route", action.get("route"))
        setattr(auth_order, "source", action.get("source"))
        setattr(auth_order, "strategy", action.get("strategy") or action.get("route") or action.get("source"))
        user_id = str(getattr(self, "_sqlite_user_id", None) or "live_bot")
        if bool(action.get("_limited_live_reserved")):
            setattr(auth_order, "_limited_live_reserved", True)
            setattr(auth_order, "_limited_live_reservation_id", action.get("_limited_live_reservation_id"))
        setattr(auth_order, "user_id", user_id)
        setattr(auth_order, "instrument_type", "equity")
        setattr(auth_order, "limited_live_pilot", bool(bounded_live_pilot_active(self.config)))
        allowed, reason = authorize_order_submission(
            self.config,
            auth_order,
            paper=getattr(self, "paper", None),
            data_dir=Path(__file__).resolve().parents[2] / "data",
            user_id=user_id,
            reserve_live_pilot=True,
        )
        if not allowed:
            log_order_submission_block(
                self.config,
                auth_order,
                reason=reason or "ORDER_BLOCKED_TRADING_MODE",
                paper=getattr(self, "paper", None),
                user_id=user_id,
                route=str(action.get("route") or action.get("source") or "") or None,
            )
            if reason in {"ENTRY_BLOCKED_SHADOW_MODE", "ORDER_BLOCKED_SHADOW_MODE"}:
                return ShadowOrder(auth_order)
            raise EntryBlocked(reason or "ORDER_BLOCKED_TRADING_MODE")
        sdk_side = OrderSide.BUY if side_str == "buy" else OrderSide.SELL
        data_dir = Path(__file__).resolve().parents[2] / "data"
        if controlled_live_equity_active(self.config) and side_str == "buy":
            healthy, exit_reason = controlled_live_exit_health(
                config=self.config,
                broker=self,
                data_dir=data_dir,
                user_id=user_id,
                day=trading_day_et(),
            )
            if not healthy:
                log_order_submission_block(
                    self.config,
                    auth_order,
                    reason=exit_reason,
                    paper=getattr(self, "paper", None),
                    user_id=user_id,
                    route=str(action.get("route") or action.get("source") or "") or None,
                )
                raise EntryBlocked(exit_reason)
        pilot_active = not bool(getattr(self, "paper", None)) and bool(bounded_live_pilot_active(self.config))
        pilot_meta = {
            "allocator_requested_notional": action.get("allocator_requested_notional"),
            "allocator_requested_qty": action.get("allocator_requested_qty"),
            "bounded_pilot_applied": bool(action.get("bounded_pilot_applied")),
            "final_submitted_qty": action.get("final_submitted_qty"),
            "final_reference_price": action.get("final_reference_price"),
            "final_estimated_notional": action.get("final_estimated_notional") or n,
            "broker_request_type": "notional",
        }
        if not self.is_asset_fractionable(sym):
            qty, mid = self._whole_share_qty_from_notional(sym, n)
            if qty is None or mid is None:
                _log_order_skip(sym, "no_price_for_qty")
                log.warning(
                    "NON_FRACTIONABLE_ORDER_SKIP symbol=%s notional=%.2f reason=no_price_for_qty",
                    sym,
                    n,
                )
                return None
            if qty < 1:
                _log_order_skip(sym, "qty_below_1")
                log.warning(
                    "NON_FRACTIONABLE_ORDER_SKIP symbol=%s notional=%.2f mid=%.4f reason=qty_below_1",
                    sym,
                    n,
                    float(mid),
                )
                return None
            log.info(
                "ORDER_NON_FRACTIONABLE_QTY symbol=%s notional=%.2f price=%.3f qty=%d",
                sym,
                n,
                float(mid),
                qty,
            )
            log.info(
                "ORDER_SUBMIT_ATTEMPT symbol=%s side=%s qty=%d notional=n/a order_type=market",
                sym,
                side_str,
                qty,
            )
            _log_order_intent(sym, side_str, qty=qty, notional=None)
            reserved = False
            if pilot_active and side_str == "buy" and not bool(getattr(auth_order, "_limited_live_reserved", False)):
                reservation = reserve_pilot_submission(self.config, auth_order, data_dir=data_dir, user_id=user_id)
                if not reservation.allowed:
                    log_order_submission_block(
                        self.config,
                        auth_order,
                        reason=reservation.reason or "limited_live_reservation_blocked",
                        paper=getattr(self, "paper", None),
                        user_id=user_id,
                        route=str(action.get("route") or action.get("source") or "") or None,
                    )
                    raise EntryBlocked(reservation.reason or "limited_live_reservation_blocked")
                reserved = bool(reservation.reserved)
            log.info(
                "LIMITED_LIVE_BROKER_DISPATCH_ATTEMPT user_id=%s symbol=%s route=%s source=%s strategy=%s reservation_id=%s broker_dispatch_attempted=true execution_allowed=true",
                user_id,
                sym,
                getattr(auth_order, "route", None) or "n/a",
                getattr(auth_order, "source", None) or "n/a",
                getattr(auth_order, "strategy", None) or "n/a",
                getattr(auth_order, "_limited_live_reservation_id", None) or "n/a",
            )
            try:
                result = self._trading.submit_order(
                    order_data=MarketOrderRequest(
                        symbol=sym,
                        qty=qty,
                        side=sdk_side,
                        type=AlpacaOrderType.MARKET,
                        time_in_force=TimeInForce.DAY,
                    )
                )
            except Exception as exc:
                log.exception(
                    "ORDER_SUBMIT_FAILED symbol=%s side=%s qty=%d notional=%.2f error=%s",
                    sym,
                    side_str,
                    qty,
                    n,
                    exc,
                )
                raise
            finally:
                if reserved:
                    finalize_pilot_submission_reservation(data_dir, user_id, order=auth_order, reason="broker_dispatch_completed")
            log.info(
                "ORDER_SUBMITTED symbol=%s side=%s qty=%d notional=n/a order_id=%s status=%s",
                sym,
                side_str,
                qty,
                str(getattr(result, "id", "") or ""),
                str(getattr(result, "status", "") or ""),
            )
            pilot_meta["broker_request_type"] = "qty"
            for key, value in pilot_meta.items():
                if value is not None:
                    setattr(result, f"_{key}", value)
            _log_order_submitted(sym, side_str, result, qty=qty, notional=None, metadata=pilot_meta if pilot_meta.get("bounded_pilot_applied") else None)
            _log_order_filled_if_present(sym, side_str, result)
            self._record_order_state(event="submitted", order=result)
            self._record_sqlite_trade_event(
                symbol=sym,
                side=side_str,
                qty=qty,
                notional=n,
                result=result,
                payload={
                    "order_type": "market",
                    "time_in_force": "day",
                    "input": dict(action),
                    "fractionable": False,
                    "qty_from_notional": True,
                },
            )
            return result
        reserved = False
        retry_whole_shares = False
        retry_qty: int | None = None
        retry_mid: float | None = None
        try:
            log.info(
                "ORDER_SUBMIT_ATTEMPT symbol=%s side=%s qty=n/a notional=%.2f order_type=market",
                sym,
                side_str,
                n,
            )
            _log_order_intent(sym, side_str, qty=None, notional=n)
            if pilot_active and side_str == "buy" and not bool(getattr(auth_order, "_limited_live_reserved", False)):
                reservation = reserve_pilot_submission(self.config, auth_order, data_dir=data_dir, user_id=user_id)
                if not reservation.allowed:
                    log_order_submission_block(
                        self.config,
                        auth_order,
                        reason=reservation.reason or "limited_live_reservation_blocked",
                        paper=getattr(self, "paper", None),
                        user_id=user_id,
                        route=str(action.get("route") or action.get("source") or "") or None,
                    )
                    raise EntryBlocked(reservation.reason or "limited_live_reservation_blocked")
                reserved = bool(reservation.reserved)
            log.info(
                "LIMITED_LIVE_BROKER_DISPATCH_ATTEMPT user_id=%s symbol=%s route=%s source=%s strategy=%s reservation_id=%s broker_dispatch_attempted=true execution_allowed=true",
                user_id,
                sym,
                getattr(auth_order, "route", None) or "n/a",
                getattr(auth_order, "source", None) or "n/a",
                getattr(auth_order, "strategy", None) or "n/a",
                getattr(auth_order, "_limited_live_reservation_id", None) or "n/a",
            )
            result = self._trading.submit_order(
                order_data=MarketOrderRequest(
                    symbol=sym,
                    notional=n,
                    side=sdk_side,
                    type=AlpacaOrderType.MARKET,
                    time_in_force=TimeInForce.DAY,
                )
            )
        except APIError as exc:
            if side_str != "buy" or "fractionable" not in str(exc).lower():
                log.exception(
                    "ORDER_SUBMIT_FAILED symbol=%s side=%s qty=n/a notional=%.2f error=%s",
                    sym,
                    side_str,
                    n,
                    exc,
                )
                raise
            self._fractionable_cache[sym] = False
            if not self.is_asset_tradable(sym):
                _log_order_skip(sym, "asset_not_tradable")
                log.warning(
                    "NON_FRACTIONABLE_ORDER_SKIP symbol=%s notional=%.2f reason=asset_not_tradable",
                    sym,
                    n,
                )
                return None
            qty, mid = self._whole_share_qty_from_notional(sym, n)
            if qty is None or mid is None:
                _log_order_skip(sym, "no_price_for_qty")
                log.warning(
                    "NON_FRACTIONABLE_ORDER_SKIP symbol=%s notional=%.2f reason=no_price_for_qty",
                    sym,
                    n,
                )
                return None
            if qty < 1:
                _log_order_skip(sym, "qty_below_1")
                log.warning(
                    "NON_FRACTIONABLE_ORDER_SKIP symbol=%s notional=%.2f mid=%.4f reason=qty_below_1",
                    sym,
                    n,
                    float(mid),
                )
                return None
            log.info(
                "ORDER_NON_FRACTIONABLE_QTY symbol=%s notional=%.2f price=%.3f qty=%d",
                sym,
                n,
                float(mid),
                qty,
            )
            log.info(
                "ORDER_SUBMIT_ATTEMPT symbol=%s side=%s qty=%d notional=n/a order_type=market",
                sym,
                side_str,
                qty,
            )
            _log_order_intent(sym, side_str, qty=qty, notional=None)
            result = self._trading.submit_order(
                order_data=MarketOrderRequest(
                    symbol=sym,
                    qty=qty,
                    side=sdk_side,
                    type=AlpacaOrderType.MARKET,
                    time_in_force=TimeInForce.DAY,
                )
            )
            retry_whole_shares = True
            retry_qty = qty
            retry_mid = float(mid)
        finally:
            if reserved:
                finalize_pilot_submission_reservation(data_dir, user_id, order=auth_order, reason="broker_dispatch_completed")
        if retry_whole_shares:
            log.info(
                "ORDER_SUBMITTED symbol=%s side=%s qty=%d notional=n/a order_id=%s status=%s",
                sym,
                side_str,
                int(retry_qty or 0),
                str(getattr(result, "id", "") or ""),
                str(getattr(result, "status", "") or ""),
            )
            pilot_meta["broker_request_type"] = "qty"
            for key, value in pilot_meta.items():
                if value is not None:
                    setattr(result, f"_{key}", value)
            _log_order_submitted(sym, side_str, result, qty=retry_qty, notional=None, metadata=pilot_meta if pilot_meta.get("bounded_pilot_applied") else None)
            _log_order_filled_if_present(sym, side_str, result)
            self._record_order_state(event="submitted", order=result)
            log.info(
                "STOCK_RETRY_WHOLE_SHARES symbol=%s notional=%.2f price=%.3f qty=%d",
                sym,
                n,
                float(retry_mid or 0.0),
                int(retry_qty or 0),
            )
            self._record_sqlite_trade_event(
                symbol=sym,
                side=side_str,
                qty=retry_qty,
                notional=n,
                result=result,
                payload={
                    "order_type": "market",
                    "time_in_force": "day",
                    "input": dict(action),
                    "fractionable": False,
                    "qty_from_notional": True,
                    "retry_whole_shares": True,
                },
            )
            return result
        log.info(
            "ORDER_SUBMITTED symbol=%s side=%s qty=n/a notional=%.2f order_id=%s status=%s",
            sym,
            side_str,
            n,
            str(getattr(result, "id", "") or ""),
            str(getattr(result, "status", "") or ""),
        )
        for key, value in pilot_meta.items():
            if value is not None:
                setattr(result, f"_{key}", value)
        _log_order_submitted(sym, side_str, result, qty=None, notional=n, metadata=pilot_meta if pilot_meta.get("bounded_pilot_applied") else None)
        _log_order_filled_if_present(sym, side_str, result)
        self._record_order_state(event="submitted", order=result)
        self._record_sqlite_trade_event(
            symbol=sym,
            side=side_str,
            qty=None,
            notional=n,
            result=result,
            payload={"order_type": "market", "time_in_force": "day", "input": dict(action)},
        )
        return result

    def _record_sqlite_trade_event(
        self,
        *,
        symbol: str,
        side: str,
        qty: Any,
        notional: Any,
        result: Any,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        """Best-effort live DB event hook installed by the live loop."""
        store = getattr(self, "_sqlite_event_store", None)
        if store is None or not hasattr(store, "record_trade"):
            return
        try:
            order_id = getattr(result, "id", None)
            status = getattr(result, "status", None)
            filled_qty = getattr(result, "filled_qty", None)
            filled_qty_float: float | None = None
            try:
                if filled_qty not in (None, ""):
                    filled_qty_float = float(filled_qty)
            except (TypeError, ValueError):
                filled_qty_float = None
            status_s = str(status).lower() if status is not None else ""
            if filled_qty_float is not None and filled_qty_float <= 0:
                return
            if filled_qty_float is None and status_s not in {"filled", "partially_filled"}:
                return
            price = getattr(result, "filled_avg_price", None)
            if price is None:
                price = getattr(result, "limit_price", None)
            stored_qty = filled_qty if filled_qty not in (None, "") else qty
            event_payload = dict(payload or {})
            if filled_qty not in (None, ""):
                event_payload["filled_qty"] = filled_qty
            store.record_trade(
                user_id=getattr(self, "_sqlite_user_id", None),
                symbol=symbol,
                side=side,
                qty=stored_qty,
                notional=notional,
                price=price,
                order_id=str(order_id) if order_id is not None else None,
                status=str(status) if status is not None else None,
                payload=event_payload,
            )
        except Exception:
            log.debug("SQLite trade event hook failed", exc_info=True)

    def submit_order(self, order: OrderRequest) -> Any:
        """
        Submit to Alpaca (alpaca-py ``TradingClient``).

        Stock **notional** orders delegate to :meth:`submit_notional_market_day` (REST-shaped).

        ``side`` / ``type`` / ``time_in_force`` use SDK enums (``OrderSide``, ``OrderType.MARKET``, ``TimeInForce.DAY``).
        """
        from src.controlled_live_equity import bounded_live_pilot_active, controlled_live_equity_active, controlled_live_exit_health
        from src.limited_live_pilot import finalize_pilot_submission_reservation, reserve_pilot_submission
        from src.limited_live_pilot import trading_day_et
        from src.trading_control import EntryBlocked, ShadowOrder, authorize_order_submission, log_order_submission_block

        user_id = str(getattr(self, "_sqlite_user_id", None) or "live_bot")
        route = getattr(order, "route", None) or getattr(order, "strategy", None) or getattr(order, "source", None)
        if route and not getattr(order, "route", None):
            setattr(order, "route", str(route).strip())
        if route and not getattr(order, "strategy", None):
            setattr(order, "strategy", str(route).strip())
        if route and not getattr(order, "source", None):
            setattr(order, "source", str(route).strip())
        setattr(order, "user_id", user_id)
        setattr(order, "instrument_type", "equity")

        allowed, reason = authorize_order_submission(
            self.config,
            order,
            paper=getattr(self, "paper", None),
            data_dir=Path(__file__).resolve().parents[2] / "data",
            user_id=user_id,
            reserve_live_pilot=True,
        )
        if not allowed:
            log_order_submission_block(
                self.config,
                order,
                reason=reason or "ORDER_BLOCKED_TRADING_MODE",
                paper=getattr(self, "paper", None),
                user_id=user_id,
                route=str(route or "") or None,
            )
            if reason in {"ENTRY_BLOCKED_SHADOW_MODE", "ORDER_BLOCKED_SHADOW_MODE"}:
                return ShadowOrder(order)
            raise EntryBlocked(reason or "ORDER_BLOCKED_TRADING_MODE")
        if controlled_live_equity_active(self.config) and order.side.lower() == "buy":
            healthy, exit_reason = controlled_live_exit_health(
                config=self.config,
                broker=self,
                data_dir=Path(__file__).resolve().parents[2] / "data",
                user_id=user_id,
                day=trading_day_et(),
            )
            if not healthy:
                log_order_submission_block(
                    self.config,
                    order,
                    reason=exit_reason,
                    paper=getattr(self, "paper", None),
                    user_id=user_id,
                    route=str(route or "") or None,
                )
                raise EntryBlocked(exit_reason)
        sym = str(order.symbol).strip().upper()
        side = OrderSide.BUY if order.side.lower() == "buy" else OrderSide.SELL
        tif = TimeInForce.DAY
        nf = getattr(order, "notional", None)
        if nf is not None:
            try:
                n = float(nf)
            except (TypeError, ValueError):
                n = 0.0
            if n > 0:
                return self.submit_notional_market_day(
                    {
                        "symbol": sym,
                        "notional": n,
                        "action": order.side.lower(),
                        "route": getattr(order, "route", None),
                        "source": getattr(order, "source", None),
                        "strategy": getattr(order, "strategy", None),
                        "allocator_requested_notional": getattr(order, "_allocator_requested_notional", None),
                        "allocator_requested_qty": getattr(order, "_allocator_requested_qty", None),
                        "bounded_pilot_applied": bool(getattr(order, "_limited_live_sized", False) or getattr(order, "_bounded_pilot_applied", False)),
                        "final_submitted_qty": getattr(order, "_limited_live_final_quantity", getattr(order, "quantity", None)),
                        "final_reference_price": getattr(order, "_limited_live_reference_price", getattr(order, "expected_price", None)),
                        "final_estimated_notional": getattr(order, "_limited_live_final_notional", n),
                        "_limited_live_reserved": bool(getattr(order, "_limited_live_reserved", False)),
                        "_limited_live_reservation_id": getattr(order, "_limited_live_reservation_id", None),
                    }
                )
        if order.order_type == OrderType.LIMIT and order.limit_price is not None:
            limit_price = float(order.limit_price)
            req = LimitOrderRequest(
                symbol=sym,
                qty=order.quantity,
                side=side,
                time_in_force=tif,
                limit_price=limit_price,
            )
        else:
            req = MarketOrderRequest(
                symbol=sym,
                qty=order.quantity,
                side=side,
                type=AlpacaOrderType.MARKET,
                time_in_force=TimeInForce.DAY,
            )
        _log_order_intent(sym, order.side.lower(), qty=order.quantity, notional=getattr(order, "notional", None))
        pilot_meta = {
            "allocator_requested_notional": getattr(order, "_allocator_requested_notional", None),
            "allocator_requested_qty": getattr(order, "_allocator_requested_qty", None),
            "bounded_pilot_applied": bool(getattr(order, "_limited_live_sized", False) or getattr(order, "_bounded_pilot_applied", False)),
            "final_submitted_qty": getattr(order, "_limited_live_final_quantity", getattr(order, "quantity", None)),
            "final_reference_price": getattr(order, "_limited_live_reference_price", getattr(order, "expected_price", None)),
            "final_estimated_notional": getattr(order, "_limited_live_final_notional", getattr(order, "notional", None)),
            "broker_request_type": "notional" if getattr(order, "notional", None) is not None else "qty",
        }
        data_dir = Path(__file__).resolve().parents[2] / "data"
        pilot_active = not bool(getattr(self, "paper", None)) and bool(bounded_live_pilot_active(self.config))
        reserved = False
        if pilot_active and order.side.lower() == "buy" and not bool(getattr(order, "_limited_live_reserved", False)):
            reservation = reserve_pilot_submission(self.config, order, data_dir=data_dir, user_id=user_id)
            if not reservation.allowed:
                log_order_submission_block(
                    self.config,
                    order,
                    reason=reservation.reason or "limited_live_reservation_blocked",
                    paper=getattr(self, "paper", None),
                    user_id=user_id,
                    route=str(route or "") or None,
                )
                raise EntryBlocked(reservation.reason or "limited_live_reservation_blocked")
            reserved = bool(reservation.reserved)
        log.info(
            "LIMITED_LIVE_BROKER_DISPATCH_ATTEMPT user_id=%s symbol=%s route=%s source=%s strategy=%s reservation_id=%s broker_dispatch_attempted=true execution_allowed=true",
            user_id,
            sym,
            getattr(order, "route", None) or "n/a",
            getattr(order, "source", None) or "n/a",
            getattr(order, "strategy", None) or "n/a",
            getattr(order, "_limited_live_reservation_id", None) or "n/a",
        )
        try:
            result = self._trading.submit_order(order_data=req)
        except Exception:
            log.exception(
                "LIMITED_LIVE_BROKER_SUBMISSION_FAILED user_id=%s symbol=%s route=%s source=%s strategy=%s reservation_id=%s broker_dispatch_attempted=true execution_allowed=true",
                user_id,
                sym,
                getattr(order, "route", None) or "n/a",
                getattr(order, "source", None) or "n/a",
                getattr(order, "strategy", None) or "n/a",
                getattr(order, "_limited_live_reservation_id", None) or "n/a",
            )
            raise
        finally:
            if reserved:
                finalize_pilot_submission_reservation(data_dir, user_id, order=order, reason="broker_dispatch_completed")
        log.info(
            "LIMITED_LIVE_BROKER_SUBMISSION_SUCCEEDED user_id=%s symbol=%s route=%s source=%s strategy=%s reservation_id=%s broker_dispatch_attempted=true execution_allowed=true",
            user_id,
            sym,
            getattr(order, "route", None) or "n/a",
            getattr(order, "source", None) or "n/a",
            getattr(order, "strategy", None) or "n/a",
            getattr(order, "_limited_live_reservation_id", None) or "n/a",
        )
        for key, value in pilot_meta.items():
            if value is not None:
                setattr(result, f"_{key}", value)
        _log_order_submitted(
            sym,
            order.side.lower(),
            result,
            qty=order.quantity,
            notional=getattr(order, "notional", None),
            metadata=pilot_meta if pilot_meta.get("bounded_pilot_applied") else None,
        )
        _log_order_filled_if_present(sym, order.side.lower(), result)
        self._record_order_state(event="submitted", order=result)
        self._record_sqlite_trade_event(
            symbol=sym,
            side=order.side.lower(),
            qty=order.quantity,
            notional=getattr(order, "notional", None),
            result=result,
            payload={
                "order_type": str(getattr(order.order_type, "value", order.order_type)),
                "limit_price": order.limit_price,
                "time_in_force": "day",
            },
        )
        return result

    def get_order(self, order_id: str) -> Any:
        order = self._trading.get_order_by_id(order_id)
        self._record_order_state(event="poll", order=order)
        return order

    def get_order_activities(self, order_id: str) -> list[dict[str, Any]]:
        """Return read-only Alpaca fill activities for one broker order id."""

        oid = str(order_id or "").strip()
        if not oid:
            return []
        base = "https://paper-api.alpaca.markets" if self.paper else "https://api.alpaca.markets"
        query = urllib.parse.urlencode({"order_id": oid, "direction": "asc", "page_size": "100"})
        request = urllib.request.Request(
            f"{base}/v2/account/activities/FILL?{query}",
            headers={
                "APCA-API-KEY-ID": self._api_key,
                "APCA-API-SECRET-KEY": self._secret,
                "Accept": "application/json",
            },
            method="GET",
        )

        def _fetch() -> list[dict[str, Any]]:
            with urllib.request.urlopen(request, timeout=20) as response:
                import json

                payload = json.loads(response.read().decode("utf-8"))
            if isinstance(payload, list):
                return [dict(row) for row in payload if isinstance(row, Mapping)]
            return []

        return self._with_retry(_fetch)

    def resolve_entry_price_from_fill(self, order: Any, fallback: float) -> float:
        """Prefer Alpaca ``filled_avg_price`` after submit; refetch by order id; else *fallback*."""
        p = filled_avg_price_from_order(order)
        if p is not None:
            return p
        oid = getattr(order, "id", None) if order is not None else None
        if oid:
            try:
                o2 = self.get_order(str(oid))
                p2 = filled_avg_price_from_order(o2)
                if p2 is not None:
                    return p2
            except Exception:
                pass
        return float(fallback)

    def close_all_positions(self, cancel_orders: bool = True) -> list[Any]:
        """Liquidate all open positions. If cancel_orders is True, cancel open orders first.
        Returns list of close-position responses. Only use on paper accounts for reset."""
        def _close() -> list[Any]:
            return self._trading.close_all_positions(cancel_orders=cancel_orders) or []
        return self._with_retry(_close)

    def get_open_orders(self) -> list[dict[str, Any]]:
        """Return all open (pending) orders (id, symbol, side, qty, submitted_at).

        ``submitted_at`` is the Alpaca order timestamp used by :func:`src.loop_helpers.cancel_orders_older_than`.
        """
        req = GetOrdersRequest(status="open", limit=500)
        orders = self._with_retry(lambda: self._trading.get_orders(req))
        out = []
        for o in orders or []:
            submitted = getattr(o, "submitted_at", None) or getattr(o, "created_at", None)
            out.append({
                "id": str(getattr(o, "id", "")),
                "symbol": getattr(o, "symbol", ""),
                "side": str(getattr(o, "side", "")),
                "qty": int(float(getattr(o, "qty", 0) or 0)),
                "submitted_at": submitted,
            })
        return out

    def qty_reserved_by_open_orders(self, symbol: str) -> float:
        """Sum of open **sell** order quantity reserved against *symbol*."""
        su = str(symbol or "").strip().upper()
        if not su:
            return 0.0
        held = 0.0
        for row in self.get_open_orders():
            sym = str(row.get("symbol") or "").strip().upper()
            if sym != su:
                continue
            if str(row.get("side") or "").strip().lower() != "sell":
                continue
            try:
                held += max(0.0, float(row.get("qty") or 0))
            except (TypeError, ValueError):
                continue
        return held

    def available_position_qty(self, symbol: str) -> tuple[float, float, float]:
        """
        ``(position_qty, qty_reserved_by_open_orders, available_qty)`` for *symbol*.

        Prefers Alpaca ``qty_available`` / ``qty_held_for_orders`` on the position when present;
        otherwise derives reservation from open sell orders.
        """
        su = str(symbol or "").strip().upper()
        pos = self.get_position(su) if su else None
        pos_qty = 0.0
        if isinstance(pos, dict):
            try:
                pos_qty = float(pos.get("qty") or 0)
            except (TypeError, ValueError):
                pos_qty = 0.0

        if isinstance(pos, dict) and pos.get("qty_available") is not None:
            try:
                available = max(0.0, float(pos["qty_available"]))
                reserved = max(0.0, pos_qty - available)
                return pos_qty, reserved, available
            except (TypeError, ValueError):
                pass

        reserved = 0.0
        if isinstance(pos, dict) and pos.get("qty_held_for_orders") is not None:
            try:
                reserved = max(0.0, float(pos["qty_held_for_orders"]))
            except (TypeError, ValueError):
                reserved = 0.0
        if reserved <= 0.0:
            reserved = self.qty_reserved_by_open_orders(su)
        available = max(0.0, pos_qty - reserved)
        return pos_qty, reserved, available

    def list_orders(self, status: str = "open") -> list[Any]:
        """Return raw Alpaca orders for compatibility with live coordination guards."""
        req = GetOrdersRequest(status=status, limit=500)
        return list(self._with_retry(lambda: self._trading.get_orders(req)) or [])

    def cancel_order_by_id(self, order_id: str) -> None:
        """Cancel a single open order by Alpaca order id (used for stale-order cleanup)."""
        oid = str(order_id or "").strip()
        if not oid:
            return

        def _cancel() -> None:
            self._trading.cancel_order_by_id(oid)

        self._with_retry(_cancel)
        log.info("ORDER_CANCELLED symbol=n/a reason=cancel_order_by_id order_id=%s", oid)

    def cancel_order(self, order_id: str) -> None:
        self.cancel_order_by_id(order_id)

    def cancel_all_orders(self) -> Any:
        """Cancel **all** open orders for this account (Alpaca ``TradingClient.cancel_orders``)."""
        def _run() -> Any:
            return self._trading.cancel_orders()

        result = self._with_retry(_run)
        log.info("ORDER_CANCELLED symbol=n/a reason=cancel_all_orders order_id=all")
        return result

    def get_orders_for_date(self, trade_date: "datetime | date") -> list[dict[str, Any]]:
        """Return orders (filled or closed) that were submitted on the given date (ET)."""
        from datetime import date as date_type
        if hasattr(trade_date, "date"):
            d = trade_date.date()
        else:
            d = trade_date
        # Alpaca expects UTC; use ET day boundaries
        try:
            import pytz
            et = pytz.timezone("America/New_York")
            after = et.localize(datetime(d.year, d.month, d.day, 0, 0, 0))
            until = et.localize(datetime(d.year, d.month, d.day, 23, 59, 59)) + timedelta(seconds=1)
            after_utc = after.astimezone(pytz.UTC)
            until_utc = until.astimezone(pytz.UTC)
        except Exception:
            after_utc = datetime(d.year, d.month, d.day, 0, 0, 0)
            until_utc = datetime(d.year, d.month, d.day, 23, 59, 59)
        req = GetOrdersRequest(status="closed", after=after_utc, until=until_utc, limit=500)
        orders = self._trading.get_orders(req)
        out = []
        for o in orders or []:
            filled = getattr(o, "filled_avg_price", None) or getattr(o, "filled_average_price", None)
            out.append({
                "id": str(getattr(o, "id", "")),
                "symbol": getattr(o, "symbol", ""),
                "side": str(getattr(o, "side", "")),
                "qty": int(float(getattr(o, "filled_qty", 0) or getattr(o, "qty", 0) or 0)),
                "filled_avg_price": float(filled) if filled is not None else None,
                "submitted_at": getattr(o, "submitted_at", None),
                "filled_at": getattr(o, "filled_at", None),
            })
        return out

    # --- Dynamic universe (screener + intraday snapshot helpers) ---

    def get_top_movers(self) -> list[dict[str, Any]]:
        """
        Combine Alpaca **market movers** (gainers) and **most actives** (by volume).

        Sizes come from ``dynamic_universe.screener_top_gainers`` /
        ``dynamic_universe.screener_most_actives`` in the merged app config.
        Falls back to a tiny hard-coded list if the screener API is unavailable.
        """
        du = (self.config or {}).get("dynamic_universe") or {}
        try:
            top_g = int(float(du.get("screener_top_gainers", 40) or 40))
        except (TypeError, ValueError):
            top_g = 40
        try:
            top_ma = int(float(du.get("screener_most_actives", 25) or 25))
        except (TypeError, ValueError):
            top_ma = 25
        top_g = max(1, min(100, top_g))
        top_ma = max(0, min(100, top_ma))

        out: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(sym: str) -> None:
            su = str(sym or "").strip().upper()
            if su and su not in seen:
                seen.add(su)
                out.append({"symbol": su})

        if self._screener is None:
            for s in ("PLTR", "COIN", "SMCI"):
                add(s)
            return out

        try:
            from alpaca.data.enums import MarketType, MostActivesBy
            from alpaca.data.requests import MarketMoversRequest, MostActivesRequest

            movers = self._with_retry(
                lambda: self._screener.get_market_movers(
                    MarketMoversRequest(market_type=MarketType.STOCKS, top=top_g)
                )
            )
            for m in getattr(movers, "gainers", []) or []:
                add(str(getattr(m, "symbol", "") or ""))
            if top_ma > 0:
                actives = self._with_retry(
                    lambda: self._screener.get_most_actives(
                        MostActivesRequest(by=MostActivesBy.VOLUME, top=top_ma)
                    )
                )
                for row in getattr(actives, "most_actives", []) or []:
                    add(str(getattr(row, "symbol", "") or ""))
        except Exception as e:
            log.warning("Alpaca screener movers/actives failed (%s); using fallback symbols", e)
            for s in ("PLTR", "COIN", "SMCI"):
                add(s)
        return out

    def get_snapshot(self, symbol: str) -> dict[str, Any]:
        """Latest NBBO mid, session volume and **approximate** day %% change from daily bars."""
        sym = str(symbol or "").strip().upper()
        if not sym or is_option_symbol(sym):
            return {}
        q = self.get_latest_quote(sym)
        if not q:
            return {}
        bid = float(getattr(q, "bid", 0) or 0)
        ask = float(getattr(q, "ask", 0) or 0)
        mid_attr = getattr(q, "mid", None)
        try:
            mid_q = float(mid_attr) if mid_attr is not None else 0.0
        except (TypeError, ValueError):
            mid_q = 0.0
        if bid > 0 and ask > 0:
            price = (bid + ask) / 2.0
        elif mid_q > 0:
            price = mid_q
        else:
            price = max(bid, ask)

        day_gain_pct = 0.0
        volume = 0.0
        try:
            df = self.get_bars(sym, timeframe="1Day", limit=8)
            if df is not None and not getattr(df, "empty", True) and len(df) >= 2:
                prev_c = float(df["close"].iloc[-2])
                last_c = float(df["close"].iloc[-1])
                volume = float(df["volume"].iloc[-1])
                if prev_c > 0:
                    day_gain_pct = (last_c / prev_c - 1.0) * 100.0
        except Exception:
            pass

        return {
            "price": float(price),
            "day_gain_pct": float(day_gain_pct),
            "volume": float(volume),
            "bid": bid,
            "ask": ask,
        }

    def get_snapshots_batch(self, symbols: list[str] | tuple[str, ...]) -> dict[str, dict[str, Any]]:
        """Batch latest quotes plus daily-bar day gain/volume for dynamic universe scans."""
        syms = [str(s or "").strip().upper() for s in symbols if str(s or "").strip()]
        if not syms:
            return {}
        quotes: Mapping[str, Any] = {}
        try:
            req = StockLatestQuoteRequest(symbol_or_symbols=syms, feed=self._feed_enum)
            quotes = self._with_retry(lambda: self._data.get_stock_latest_quote(req)) or {}
        except Exception:
            quotes = {}
        daily = self.get_bars_batch(syms, timeframe="1Day", limit=8)
        out: dict[str, dict[str, Any]] = {}
        for sym in syms:
            q = quotes.get(sym) if isinstance(quotes, Mapping) else None
            if q is None:
                out[sym] = self.get_snapshot(sym)
                continue
            bid = float(getattr(q, "bid_price", 0) or 0)
            ask = float(getattr(q, "ask_price", 0) or 0)
            mid_attr = getattr(q, "mid", None)
            try:
                mid_q = float(mid_attr) if mid_attr is not None else 0.0
            except (TypeError, ValueError):
                mid_q = 0.0
            if bid > 0 and ask > 0:
                price = (bid + ask) / 2.0
            elif mid_q > 0:
                price = mid_q
            else:
                price = max(bid, ask)

            day_gain_pct = 0.0
            volume = 0.0
            df = daily.get(sym)
            try:
                if df is not None and not getattr(df, "empty", True) and len(df) >= 2:
                    prev_c = float(df["close"].iloc[-2])
                    last_c = float(df["close"].iloc[-1])
                    volume = float(df["volume"].iloc[-1])
                    if prev_c > 0:
                        day_gain_pct = (last_c / prev_c - 1.0) * 100.0
            except Exception:
                pass
            out[sym] = {
                "symbol": sym,
                "price": float(price),
                "day_gain_pct": float(day_gain_pct),
                "volume": float(volume),
                "bid": bid,
                "ask": ask,
            }
        return out

    def get_avg_volume(self, symbol: str) -> float:
        """20-trading-day average daily volume (prior sessions; excludes latest bar as incomplete proxy)."""
        sym = str(symbol or "").strip().upper()
        if not sym:
            return 1.0
        try:
            df = self.get_bars(sym, timeframe="1Day", limit=40)
            if df is None or getattr(df, "empty", True) or len(df) < 2:
                return 1.0
            vols = df["volume"].astype(float).iloc[:-1]
            if vols.empty:
                return 1.0
            tail = vols.tail(20)
            m = float(tail.mean())
            return max(1.0, m)
        except Exception:
            return 1.0

    def get_avg_volumes(self, symbols: list[str] | tuple[str, ...]) -> dict[str, float]:
        """Batch 20-trading-day average daily volume keyed by symbol."""
        syms = [str(s or "").strip().upper() for s in symbols if str(s or "").strip()]
        if not syms:
            return {}
        bars = self.get_bars_batch(syms, timeframe="1Day", limit=40)
        out: dict[str, float] = {}
        for sym in syms:
            try:
                df = bars.get(sym)
                if df is None or getattr(df, "empty", True) or len(df) < 2:
                    out[sym] = 1.0
                    continue
                vols = df["volume"].astype(float).iloc[:-1]
                if vols.empty:
                    out[sym] = 1.0
                    continue
                out[sym] = max(1.0, float(vols.tail(20).mean()))
            except Exception:
                out[sym] = 1.0
        return out


@dataclass(frozen=True)
class AlpacaCredentialResolution:
    """Resolved Alpaca API credentials for diagnostics and optional ad-hoc clients."""

    mode: str
    selected: str
    live_key_present: bool
    paper_key_present: bool
    api_key: str
    secret: str

    @property
    def credentials_present(self) -> bool:
        return bool(self.api_key and self.secret)


def resolve_alpaca_paper_flag(
    config: Mapping[str, Any] | None,
    *,
    paper: bool | None = None,
) -> bool:
    """Match :class:`AlpacaBroker` paper/live resolution."""
    if paper is not None:
        return bool(paper)
    broker_cfg = (config or {}).get("broker") if isinstance(config, Mapping) else {}
    broker_cfg = broker_cfg if isinstance(broker_cfg, Mapping) else {}
    paper_cfg = broker_cfg.get("paper", True)
    apca_paper = _env("APCA_PAPER")
    alpaca_live = _env("ALPACA_LIVE")
    if apca_paper is not None:
        return str(apca_paper).strip().lower() in ("true", "1", "yes")
    if alpaca_live is not None:
        return not (str(alpaca_live).strip().lower() in ("true", "1", "yes"))
    return bool(paper_cfg)


def resolve_alpaca_credentials(
    config: Mapping[str, Any] | None,
    *,
    paper: bool | None = None,
    paper_fallback_on_live: bool = True,
) -> AlpacaCredentialResolution:
    """
    Resolve Alpaca credentials with the same priority as the live broker.

    Live mode:
      1. ``broker.api_key`` / ``broker.secret_key`` in config
      2. ``ALPACA_LIVE_API_KEY_ID`` / ``ALPACA_LIVE_API_SECRET_KEY``
      3. ``APCA_API_KEY_ID`` / ``APCA_API_SECRET_KEY`` (when *paper_fallback_on_live*)

    Paper mode:
      1. broker config keys
      2. ``APCA_*`` env vars
    """
    broker_cfg = (config or {}).get("broker") if isinstance(config, Mapping) else {}
    broker_cfg = dict(broker_cfg) if isinstance(broker_cfg, Mapping) else {}
    is_paper = resolve_alpaca_paper_flag(config, paper=paper)

    config_key = str(broker_cfg.get("api_key") or "").strip()
    config_secret = str(broker_cfg.get("secret_key") or "").strip()
    live_key = str(_env("ALPACA_LIVE_API_KEY_ID") or "").strip()
    live_secret = str(_env("ALPACA_LIVE_API_SECRET_KEY") or "").strip()
    paper_key = str(_env("APCA_API_KEY_ID") or "").strip()
    paper_secret = str(_env("APCA_API_SECRET_KEY") or "").strip()

    live_present = bool(live_key and live_secret)
    paper_present = bool(paper_key and paper_secret)
    mode = "paper" if is_paper else "live"

    if config_key and config_secret:
        return AlpacaCredentialResolution(
            mode=mode,
            selected="config",
            live_key_present=live_present,
            paper_key_present=paper_present,
            api_key=config_key,
            secret=config_secret,
        )

    if is_paper:
        if paper_present:
            return AlpacaCredentialResolution(
                mode=mode,
                selected="paper",
                live_key_present=live_present,
                paper_key_present=paper_present,
                api_key=paper_key,
                secret=paper_secret,
            )
    else:
        if live_present:
            return AlpacaCredentialResolution(
                mode=mode,
                selected="live",
                live_key_present=live_present,
                paper_key_present=paper_present,
                api_key=live_key,
                secret=live_secret,
            )
        if paper_fallback_on_live and paper_present:
            return AlpacaCredentialResolution(
                mode=mode,
                selected="paper",
                live_key_present=live_present,
                paper_key_present=paper_present,
                api_key=paper_key,
                secret=paper_secret,
            )

    return AlpacaCredentialResolution(
        mode=mode,
        selected="none",
        live_key_present=live_present,
        paper_key_present=paper_present,
        api_key="",
        secret="",
    )


def fetch_alpaca_news_with_credentials(
    api_key: str,
    secret: str,
    symbols: Sequence[str],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 50,
    exclude_contentless: bool = False,
) -> list[Any]:
    """Fetch Alpaca news using explicit credentials (premarket diagnostics / probes)."""
    if not ALPACA_AVAILABLE or NewsClient is None or NewsRequest is None:
        return []
    key = str(api_key or "").strip()
    sec = str(secret or "").strip()
    if not key or not sec:
        return []
    syms = [str(s or "").strip().upper() for s in symbols if str(s or "").strip()]
    if not syms:
        return []
    client = NewsClient(key, sec)
    req = NewsRequest(
        symbols=",".join(list(dict.fromkeys(syms))),
        start=start,
        end=end,
        limit=max(1, int(limit)),
        include_content=False,
        exclude_contentless=bool(exclude_contentless),
    )
    resp = client.get_news(req)

    def _response_keys(obj: Any) -> list[str]:
        keys: list[str] = []
        if isinstance(obj, Mapping):
            keys.extend(str(key) for key in obj.keys())
        elif isinstance(obj, tuple) and len(obj) == 2 and isinstance(obj[0], str):
            keys.append(str(obj[0]))
            if isinstance(obj[1], Mapping):
                keys.extend(str(key) for key in obj[1].keys())
        elif hasattr(obj, "__dict__"):
            try:
                keys.extend(str(key) for key in vars(obj).keys())
            except Exception:
                pass
        elif isinstance(obj, (tuple, list)):
            keys.extend(str(idx) for idx in range(len(obj)))
        return keys

    def _article_dict(item: Any) -> dict[str, Any]:
        if isinstance(item, Mapping):
            return dict(item)
        if hasattr(item, "model_dump") and callable(getattr(item, "model_dump")):
            try:
                dumped = item.model_dump()
                if isinstance(dumped, Mapping):
                    return dict(dumped)
            except Exception:
                pass
        if hasattr(item, "dict") and callable(getattr(item, "dict")):
            try:
                dumped = item.dict()
                if isinstance(dumped, Mapping):
                    return dict(dumped)
            except Exception:
                pass
        if is_dataclass(item):
            try:
                dumped = asdict(item)
                if isinstance(dumped, Mapping):
                    return dict(dumped)
            except Exception:
                pass
        if hasattr(item, "__dict__"):
            try:
                return dict(vars(item))
            except Exception:
                pass
        return {}

    def _news_payload(obj: Any, *, depth: int = 0) -> list[Any]:
        if obj is None or depth > 4:
            return []
        if isinstance(obj, list):
            if obj and all(isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str) for item in obj):
                for key, val in obj:
                    if key in {"news", "data", "results"}:
                        nested = _news_payload(val, depth=depth + 1)
                        if nested:
                            return nested
                for _, val in obj:
                    nested = _news_payload(val, depth=depth + 1)
                    if nested:
                        return nested
                return []
            return list(obj)
        if isinstance(obj, tuple):
            if len(obj) == 2 and isinstance(obj[1], Mapping):
                return _news_payload(obj[1], depth=depth + 1)
            if len(obj) == 1:
                return _news_payload(obj[0], depth=depth + 1)
            for item in obj:
                payload = _news_payload(item, depth=depth + 1)
                if payload:
                    return payload
            return list(obj)
        if isinstance(obj, Mapping):
            for key in ("news", "data", "results"):
                if key not in obj:
                    continue
                payload = obj.get(key)
                if key == "data" and payload is not None and not isinstance(payload, (list, tuple, Mapping)):
                    continue
                if key == "news" and payload is not None:
                    return _news_payload(payload, depth=depth + 1)
                if key in {"data", "results"} and payload is not None:
                    nested = _news_payload(payload, depth=depth + 1)
                if nested:
                    return nested
        return []
        for attr in ("news", "data", "results"):
            if not hasattr(obj, attr):
                continue
            payload = getattr(obj, attr)
            nested = _news_payload(payload, depth=depth + 1)
            if nested:
                return nested
        return []

    log.info("ALPACA_RESPONSE_TYPE type=%s", type(resp).__name__ if resp is not None else "NoneType")
    log.info("ALPACA_RESPONSE_KEYS keys=%s", ",".join(_response_keys(resp)) or "none")
    news = [_article_dict(item) for item in _news_payload(resp)]
    news = [item for item in news if item]
    log.info("ALPACA_NEWS_NORMALIZED_COUNT count=%d", len(news))
    log.info("ALPACA_NEWS_COUNT count=%d", len(news))
    return list(news)


def _env(key: str) -> str | None:
    import os
    return os.environ.get(key)
