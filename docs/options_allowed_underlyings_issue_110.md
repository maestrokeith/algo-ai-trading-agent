# Issue 110 - Options allowed_underlyings review

Date: 2026-06-10

Scope: local runtime evidence from `data/algo_live.db` for the 30-day window
2026-05-11 through 2026-06-10, plus current options configuration. This report
is intentionally a review artifact only. No trading logic changes were made.

## Current allowlists

Default/live `options.allowed_underlyings`:

- SPY
- QQQ
- NVDA
- AAPL
- AMZN
- SMH

The `paper_bot` override already includes MU, TSM, MRVL, ANET, AVGO, and GOOGL.
ORCL is not present in the default/live allowlist or the paper override.

## 30-day dynamic and entry evidence

`dynamic_scans` rows reviewed: 930.

| Symbol | Dynamic selected | Dynamic accepted | Dynamic candidate rows | Entry eval rows | Final entry eval rows | Current default/live status | Review result |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| ORCL | 0 | 0 | 0 | 92 | 0 | Not allowed | Do not enable yet; observed in premarket/replay artifacts, but no local option-chain liquidity evidence. |
| MU | 0 | 0 | 0 | 202 | 0 | Not allowed | Do not promote from paper/watchlist without chain evidence. |
| ANET | 0 | 0 | 70 | 91 | 0 | Not allowed | Candidate presence only; not accepted/selected and no final entries. |
| MRVL | 0 | 0 | 70 | 208 | 0 | Not allowed | Candidate presence only; blocked by entry quality such as volatility spike/no entry signal. |
| TSM | 0 | 0 | 0 | 204 | 0 | Not allowed | Do not promote from paper/watchlist without chain evidence. |
| AVGO | 25 | 0 | 70 | 223 | 1 | Not allowed | Best non-default candidate in local evidence; keep paper/watchlist until chain evidence passes. |
| GOOGL | 114 | 0 | 70 | 2 | 2 | Not allowed | Repeated dynamic selection, but still needs options liquidity/open-interest proof before live enablement. |
| AMZN | 139 | 0 | 70 | 96 | 53 | Allowed | Already enabled in default/live. |
| QQQ | 25 | 0 | 0 | 147 | 81 | Allowed | Already enabled in default/live. |

Data quality notes:

- `data/dynamic_scan_history` contains many synthetic or fixture-like symbols and
  was not used for the decision.
- `data/replay_market_session` shows ORCL core-rebuild replay activity, including
  allocator action creation, but replay evidence is not an option-chain liquidity
  report.
- `src/options_adapter.py` enforces the live block by requiring the stock
  underlying to be present in `options.allowed_underlyings`.

## Liquidity and open-interest review

No durable option-chain open-interest or option-volume snapshots for ORCL, MU,
ANET, MRVL, TSM, AVGO, or GOOGL were found in local runtime data. The existing
contract selection policy requires:

- `options.contract_selection.min_open_interest`: 500
- `options.contract_selection.min_volume`: 100
- `options.max_bid_ask_spread_pct`: 12.0
- target DTE: 7 to 21
- target delta: 0.35 to 0.60

Because the requested liquidity/open-interest evidence is absent, the default
live allowlist should not be expanded in this change.

## Recommendation

Do not add ORCL, MU, ANET, MRVL, TSM, AVGO, or GOOGL to the default/live
`options.allowed_underlyings` yet.

Before enabling any new live options underlying:

1. Capture option-chain snapshots across at least three regular sessions.
2. Confirm at least one eligible 7-21 DTE call/put candidate has open interest
   >= 500, volume >= 100, and spread <= 12.0 percent.
3. Require repeated dynamic/news-catalyst qualification in live runtime data,
   not only replay or premarket cache presence.
4. Promote first through paper/shadow options diagnostics, then update the
   default/live allowlist only after the report contains contract-level evidence.
