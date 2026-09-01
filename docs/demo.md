# Hackathon Demo

Run:

```bash
python -m hackathon.demo
```

or:

```bash
bin/algo hackathon demo
```

Scenarios:

- NVDA approved dry-run trade: strategy, critic, risk, and policy pass.
- TSLA critic rejection: price is too extended above VWAP.
- AAPL policy rejection: daily loss lock blocks the trade after proposal, critic, and risk pass.

The demo does not require API credentials and does not place live orders.
