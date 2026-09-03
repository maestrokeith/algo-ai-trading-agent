"""Multi-timeframe signal research for the high-precision paper scalper."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np
import pandas as pd

from .market_data import align_completed_htf, normalize_ohlcv, resample_ohlcv
from .trading_config import InstrumentSpec, StrategyConfig


@dataclass(frozen=True)
class SignalDecision:
    side: int
    reason: str


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0.0)
    down = -delta.clip(upper=0.0)
    avg_up = up.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_down = down.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_up / avg_down.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    return out.fillna(50.0)


def atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = frame["close"].shift(1)
    tr = pd.concat([frame["high"] - frame["low"], (frame["high"] - prev_close).abs(), (frame["low"] - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def _htf_features(frame: pd.DataFrame, cfg: StrategyConfig, prefix: str) -> pd.DataFrame:
    out = frame.copy()
    out[f"{prefix}_ema_fast"] = ema(out["close"], cfg.htf_fast_ema)
    out[f"{prefix}_ema_slow"] = ema(out["close"], cfg.htf_slow_ema)
    out[f"{prefix}_trend"] = np.where(out[f"{prefix}_ema_fast"] > out[f"{prefix}_ema_slow"], 1, np.where(out[f"{prefix}_ema_fast"] < out[f"{prefix}_ema_slow"], -1, 0))
    return out


def prepare_features(frame: pd.DataFrame, symbol: str, cfg: StrategyConfig | None = None) -> pd.DataFrame:
    cfg = cfg or StrategyConfig()
    spec = cfg.instrument(symbol)
    base = normalize_ohlcv(frame, spec)
    out = base.copy()
    out["ema_fast"] = ema(out["close"], cfg.ltf_fast_ema)
    out["ema_slow"] = ema(out["close"], cfg.ltf_slow_ema)
    out["rsi"] = rsi(out["close"], cfg.rsi_period)
    out["rsi_prev"] = out["rsi"].shift(1)
    out["atr"] = atr(out, cfg.atr_period)
    out["atr_pct"] = out["atr"] / out["close"]
    out["atr_median"] = out["atr"].rolling(cfg.atr_median_window, min_periods=cfg.atr_period).median()
    out["volume_ma"] = out["volume"].rolling(cfg.volume_ma_period, min_periods=cfg.volume_ma_period).mean()
    out["recent_low"] = out["low"].rolling(cfg.swing_lookback, min_periods=1).min()
    out["recent_high"] = out["high"].rolling(cfg.swing_lookback, min_periods=1).max()
    five = _htf_features(resample_ohlcv(base, "5min"), cfg, "m5")
    fifteen = _htf_features(resample_ohlcv(base, "15min"), cfg, "m15")
    aligned5 = align_completed_htf(five, out.index, ["m5_trend", "m5_ema_fast", "m5_ema_slow"])
    aligned15 = align_completed_htf(fifteen, out.index, ["m15_trend", "m15_ema_fast", "m15_ema_slow"])
    return out.join(aligned5).join(aligned15)


def decide_signal(row: Mapping[str, float], spec: InstrumentSpec, cfg: StrategyConfig) -> SignalDecision:
    required = ("close", "ema_fast", "ema_slow", "rsi", "rsi_prev", "atr", "atr_pct", "atr_median", "volume", "volume_ma", "spread", "m5_trend", "m15_trend")
    vals = [row.get(key) for key in required]
    if any(v is None or pd.isna(v) for v in vals):
        return SignalDecision(0, "warmup")
    if float(row["spread"]) > spec.default_spread * cfg.max_spread_multiple:
        return SignalDecision(0, "spread")
    if not cfg.min_atr_pct <= float(row["atr_pct"]) <= cfg.max_atr_pct:
        return SignalDecision(0, "atr_regime")
    if float(row["atr_median"]) > 0 and float(row["atr"]) > float(row["atr_median"]) * cfg.atr_spike_multiple:
        return SignalDecision(0, "atr_spike")
    if float(row["volume"]) <= float(row["volume_ma"]):
        return SignalDecision(0, "volume")
    m5 = int(row["m5_trend"])
    m15 = int(row["m15_trend"])
    if m5 == 0 or m15 == 0 or m5 != m15:
        return SignalDecision(0, "htf_disagreement")
    close, fast, slow = float(row["close"]), float(row["ema_fast"]), float(row["ema_slow"])
    rsi_now, rsi_prev = float(row["rsi"]), float(row["rsi_prev"])
    long_rsi = (cfg.long_rsi_low <= rsi_now <= cfg.long_rsi_high and rsi_now >= rsi_prev) or (rsi_prev < cfg.rsi_midline <= rsi_now)
    short_rsi = (cfg.short_rsi_low <= rsi_now <= cfg.short_rsi_high and rsi_now <= rsi_prev) or (rsi_prev > cfg.rsi_midline >= rsi_now)
    if m5 == 1 and close > fast > slow and long_rsi:
        return SignalDecision(1, "long_confluence")
    if m5 == -1 and close < fast < slow and short_rsi:
        return SignalDecision(-1, "short_confluence")
    return SignalDecision(0, "ltf_filter")


def generate_signals(frame: pd.DataFrame, symbol: str, cfg: StrategyConfig | None = None) -> pd.DataFrame:
    cfg = cfg or StrategyConfig()
    spec = cfg.instrument(symbol)
    out = prepare_features(frame, symbol, cfg)
    decisions = [decide_signal(row, spec, cfg) for row in out.to_dict(orient="records")]
    out["signal"] = [d.side for d in decisions]
    out["signal_reason"] = [d.reason for d in decisions]
    return out
