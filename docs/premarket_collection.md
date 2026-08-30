# Premarket News Collection

AlgoSphere uses a dedicated premarket collection job so the live loop does not
depend on manual news runs before the market opens.

## What It Writes

The collection job refreshes these session artifacts:

- `data/premarket/latest_event_feed.json`
- `data/premarket/latest_rankings.json`
- `data/premarket/latest_catalysts.json`

Each artifact includes `generated_at` and `ttl_minutes`. The default TTL is 390
minutes, so artifacts collected from 5:15 AM ET remain valid when `algo.service`
starts at 9:30 AM ET.

## Schedule

The example timer runs on weekdays from 5:15 AM ET through 9:15 AM ET every 12
minutes, with up to 3 minutes of randomized delay. The script enforces the
configured collection window and exits without side effects outside 5:15-9:25
AM ET.

Relevant config in `config/default.yaml`:

```yaml
premarket_intelligence:
  enabled: true
  allow_trading: false
  news_scan_time: "05:15"
  collection_start_time: "05:15"
  collection_end_time: "09:25"
  refresh_interval_minutes: 12
  artifact_ttl_minutes: 390
```

## Manual Run

```bash
PYTHONPATH=. python scripts/run_premarket_collection.py --force
```

Dry run without provider fetches or state mutation:

```bash
PYTHONPATH=. python scripts/run_premarket_collection.py --dry-run --force
```

## Readiness Check

Before market open:

```bash
PYTHONPATH=. python scripts/check_premarket_readiness.py
```

Expected ready output includes:

```text
PREMARKET_READINESS status=fresh present=true fresh=true catalyst_ranked_symbols=...
```

The command exits nonzero when artifacts are missing, stale, unreadable, or have
zero catalyst-ranked symbols. Use `--warn-only` to print status without failing.

## systemd Install

Copy and adjust the example files:

```bash
sudo cp deploy/systemd/algosphere-premarket.service /etc/systemd/system/
sudo cp deploy/systemd/algosphere-premarket.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now algosphere-premarket.timer
```

Edit `/etc/systemd/system/algosphere-premarket.service` if the checkout path,
Python path, user, or environment differ.

Verify:

```bash
systemctl list-timers 'algosphere-premarket*'
journalctl -u algosphere-premarket.service -n 100 --no-pager
PYTHONPATH=. python scripts/check_premarket_readiness.py
```

At 9:30 AM ET, `algo.service` startup logs include
`PREMARKET_STARTUP_ARTIFACTS` and one `PREMARKET_STARTUP_ARTIFACT` line per
artifact, reporting `fresh`, `stale`, or `missing` status and the number of
catalyst-ranked symbols loaded.
