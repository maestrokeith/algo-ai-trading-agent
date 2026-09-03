# Validation Checklist

Use this stack as a research harness, not as evidence that a strategy is profitable.

- Use clean 1-minute OHLCV/tick-volume data with realistic spreads for each instrument.
- Keep train and test periods chronologically separated.
- Verify that 5m/15m features are based only on completed historical bars.
- Compare results with and without spread/slippage assumptions.
- Review profit factor, expectancy, drawdown, consecutive-loss runs, and trade count together rather than optimizing one metric.
- Break results down by instrument and UTC session.
- Run walk-forward splits across multiple market regimes.
- Run Monte-Carlo resampling of realized trade P&L to estimate path sensitivity.
- Reject configurations that only work on a narrow date range or one instrument.
- Keep the `PAPER_ONLY`/`LIVE_EXECUTION` invariants unchanged.

The included workflow compiles the engine, runs focused pytest coverage, and checks that live execution remains disabled.
