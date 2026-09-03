# Strategy Specification

The research strategy is a high-precision, multi-filter scalper. It is designed for evaluation, not for guaranteed profitability.

## Higher-timeframe filter

1-minute bars are aggregated into 5-minute and 15-minute bars. Each higher timeframe uses EMA 50 and EMA 200. Long research signals require both higher timeframes to be bullish; short research signals require both to be bearish. Higher-timeframe features are shifted by one full HTF bar before alignment to the 1-minute index to reduce lookahead risk when source timestamp semantics are ambiguous.

## 1-minute confirmation

The lower timeframe uses EMA 9/21, RSI 14, ATR 14, and a 20-bar volume average. Long candidates require price above EMA 9 above EMA 21 plus RSI pull/cross confirmation; shorts are symmetric. ATR percent filters remove flat and extreme regimes, ATR spikes are rejected, volume must exceed its moving average, and spread must be below the configured instrument threshold.

## Risk model

Default paper risk is 0.5% of equity per position and cannot be configured above 1%. Stops use the more conservative of a recent swing and 1.5 ATR. Targets default to 1.25R. Break-even activates after a one-ATR favorable move, and ATR trailing activates later. Grid and martingale sizing are not implemented.

## Execution simulation

Signals are generated from completed bars and entries occur at the next bar open. The paper broker applies half-spread plus configurable adverse slippage on entry and exit. If stop and target are both touched in the same OHLC bar, the conservative default assumes the stop was hit first.
