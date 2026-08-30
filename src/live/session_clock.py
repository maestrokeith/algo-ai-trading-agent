"""Small ET session clock helpers shared by the live loop and exit pass."""
from __future__ import annotations

from datetime import datetime


def minutes_since_regular_session_open_et(dt_et: datetime) -> float:
    """Minutes since 09:30 America/New_York on *dt_et*'s calendar date."""
    d = dt_et.date()
    open_dt = datetime(d.year, d.month, d.day, 9, 30, 0, tzinfo=dt_et.tzinfo)
    if dt_et < open_dt:
        return 0.0
    return (dt_et - open_dt).total_seconds() / 60.0
