# Paper Options Route Observability

When `options.enabled=true` and `options.mode=paper_only`, the normal live loop emits
`OPTION_ROUTE_CHECK` for stock candidates whose `ENTRY_EVAL` is final true or whose
news-catalyst or momentum fields are strong enough to make options routing relevant.

If that observed paper-options path is not used, the loop emits `OPTION_ROUTE_SKIPPED`
with one normalized reason:

- `entry_eval_false`
- `underlying_not_allowed`
- `require_top_signal_failed`
- `environment_blocked`
- `daily_cap`
- `cooldown`
- `gross_exposure`
- `selector_no_contract`
- `fallback_to_stock`
- `stock_route_selected`

These logs are diagnostic only. They do not change sizing, spreads, DTE windows, risk
controls, exposure limits, conviction thresholds, or trading logic.
