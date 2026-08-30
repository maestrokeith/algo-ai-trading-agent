# Daily Trading Review

Run after each session while live entries remain disabled:

```bash
./bin/algo day-review --date YYYY-MM-DD --user live_bot
./bin/algo trading-audit --date YYYY-MM-DD --user live_bot
./bin/algo duplicate-forensics --date YYYY-MM-DD --user live_bot
./bin/algo strategy-readiness --user live_bot
```

`day-review` is read-only except for writing:

```text
reports/day_review/YYYY-MM-DD.json
reports/day_review/YYYY-MM-DD.md
```

It uses the canonical lifecycle model:

```text
scanner event -> selected candidate -> entry decision -> allocator action
-> order -> broker order -> fill -> position -> exit -> closed trade
```

Raw counts are persisted rows, loop events, or snapshots. Unique counts are
canonical lifecycle entities. Replay, mock, shadow, paper, and test rows are
not live broker evidence.

Readiness recommendations are conservative. `CONTAMINATED` and `UNRECONCILED`
days cannot support live promotion. Missing bars, missing option quotes, or
missing news links remain unavailable; the report does not fabricate
hypothetical P&L.
