"""Legacy compatibility wrapper for the trend-long dispatch split.

The implementation now lives under :mod:`src.strategies.entries`, but this
module remains as the stable import path for tests and older callers.
"""

from __future__ import annotations
from src.sell_logging import log_sell
import src.portfolio.cap_pressure as _impl
_REAL_EXECUTE_CAP_PRESSURE_PARTIAL_TRIM = _impl.execute_cap_pressure_partial_trim
_REAL_CONSIDER_REPLACEMENT_FOR_SIZING_REJECT = _impl.consider_replacement_for_sizing_reject

from src.portfolio.replacement_preflight import (
    evaluate_portfolio_replacement_for_dispatch,
)
from src.entry_router import route_to_options_executor, route_to_stock_executor
from src.portfolio_brain import portfolio_brain_enabled
from src.position_tracker import (
    add as add_tracked,
    load as load_tracked,
    remove as remove_tracked,
    update as update_tracked,
)
from src.risk_limits import bucket_allocation_allows
from src.strategies.entries import trend_long_dispatch as _impl
from src.strategies.entries.trend_long_dispatch import _log_options_stock_fallback_state
from src.strategies.entries.trend_long_dispatch import (
    evaluate_strength_based_portfolio_swap,
)


from src.sell_logging import log_sell
def _sync_impl_globals():
    _impl.log_sell = log_sell

    if "evaluate_portfolio_replacement_for_dispatch" in globals():
        _impl.evaluate_portfolio_replacement_for_dispatch = (
            evaluate_portfolio_replacement_for_dispatch
        )
def execute_cap_pressure_partial_trim(*args, **kwargs):
    _sync_impl_globals()
    return _impl.execute_cap_pressure_partial_trim(*args, **kwargs)


def consider_replacement_for_sizing_reject(*args, **kwargs):
    _sync_impl_globals()
    return _impl.consider_replacement_for_sizing_reject(*args, **kwargs)

def _sync_impl_globals():
    _impl.load_tracked = load_tracked
    _impl.add_tracked = add_tracked
    _impl.remove_tracked = remove_tracked
    _impl.update_tracked = update_tracked
    _impl.log_sell = log_sell
    _impl.evaluate_portfolio_replacement_for_dispatch = evaluate_portfolio_replacement_for_dispatch

def _sync_impl_globals(sync_execute: bool = False):
    _impl.log_sell = log_sell
    _impl.load_tracked = load_tracked
    _impl.add_tracked = add_tracked
    _impl.remove_tracked = remove_tracked
    _impl.update_tracked = update_tracked

    if "evaluate_portfolio_replacement_for_dispatch" in globals():
        _impl.evaluate_portfolio_replacement_for_dispatch = (
            evaluate_portfolio_replacement_for_dispatch
        )

    if sync_execute:
        patched_execute = globals().get("execute_cap_pressure_partial_trim")
        if patched_execute is _LEGACY_EXECUTE_CAP_PRESSURE_PARTIAL_TRIM:
            _impl.execute_cap_pressure_partial_trim = _REAL_EXECUTE_CAP_PRESSURE_PARTIAL_TRIM
        else:
            _impl.execute_cap_pressure_partial_trim = patched_execute


def execute_cap_pressure_partial_trim(*args, **kwargs):
    _sync_impl_globals(sync_execute=False)
    return _REAL_EXECUTE_CAP_PRESSURE_PARTIAL_TRIM(*args, **kwargs)


def consider_replacement_for_sizing_reject(*args, **kwargs):
    _sync_impl_globals(sync_execute=True)
    return _REAL_CONSIDER_REPLACEMENT_FOR_SIZING_REJECT(*args, **kwargs)


_LEGACY_EXECUTE_CAP_PRESSURE_PARTIAL_TRIM = execute_cap_pressure_partial_trim

def dispatch_trend_long_after_buying_power(*args, **kwargs):
    """Compatibility wrapper that preserves monkeypatchable legacy module globals."""
    _impl.load_tracked = load_tracked
    _impl.add_tracked = add_tracked
    _impl.remove_tracked = remove_tracked
    _impl.update_tracked = update_tracked
    _impl.portfolio_brain_enabled = portfolio_brain_enabled
    _impl.bucket_allocation_allows = bucket_allocation_allows
    _impl.route_to_options_executor = route_to_options_executor
    _impl.route_to_stock_executor = route_to_stock_executor
    _impl.evaluate_strength_based_portfolio_swap = evaluate_strength_based_portfolio_swap
    _impl.evaluate_portfolio_replacement_for_dispatch = (
        evaluate_portfolio_replacement_for_dispatch
    )
    return _impl.dispatch_trend_long_after_buying_power(*args, **kwargs)

__all__ = [
    "_log_options_stock_fallback_state",
    "add_tracked",
    "bucket_allocation_allows",
    "consider_replacement_for_sizing_reject",
    "dispatch_trend_long_after_buying_power",
    "evaluate_portfolio_replacement_for_dispatch",
    "evaluate_strength_based_portfolio_swap",
    "execute_cap_pressure_partial_trim",
    "load_tracked",
    "portfolio_brain_enabled",
    "remove_tracked",
    "route_to_options_executor",
    "route_to_stock_executor",
    "update_tracked",
    "log_sell",
]
