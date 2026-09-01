# Trade Memory

Trade memory is stored in SQLite, defaulting to:

```text
data/algo_memory.db
```

Tables:

- `trade_proposals`
- `critic_reviews`
- `risk_decisions`
- `executions`
- `closed_trades`
- `post_trade_reviews`
- `strategy_statistics`

Memory stores structured decisions and lessons. It does not store API secrets or full brokerage account numbers. Strategy learning currently adjusts confidence and ranking inputs through aggregate statistics, not source code.
