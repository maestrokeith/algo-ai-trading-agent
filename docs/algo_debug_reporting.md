# Algo Debug Reporting

`scripts/send_debug_to_openai.py` collects recent `algo` service logs, filters the lines most useful for trading diagnostics, writes local debug artifacts, and optionally sends the filtered log bundle to the OpenAI Responses API for an operator report.

The script is read-only with respect to trading. It does not place orders, cancel orders, change config, or modify broker state.

## Dry Run

Use dry-run mode to verify log collection and local artifact writing without an OpenAI API key:

```bash
PYTHONPATH=. python scripts/send_debug_to_openai.py --since "30 minutes ago" --dry-run
```

Dry-run writes the filtered log artifacts and a Markdown file showing the prompt that would be sent.

## Real Run

Set `OPENAI_API_KEY` and run:

```bash
OPENAI_API_KEY=... PYTHONPATH=. python scripts/send_debug_to_openai.py --since "30 minutes ago"
```

The default model is `gpt-4.1-mini`. Override it with:

```bash
OPENAI_API_KEY=... PYTHONPATH=. python scripts/send_debug_to_openai.py --model gpt-4.1-mini --since "30 minutes ago"
```

You can also run the Makefile target:

```bash
make algo-debug-report
```

## Cron

Run every 30 minutes during regular market hours:

```cron
*/30 9-16 * * 1-5 cd /opt/algosphere/algo-ai-trading-agent && PYTHONPATH=. python scripts/send_debug_to_openai.py --since "30 minutes ago" --retention-days 5 >> reports/debug/openai_debug.log 2>&1
```

## Local Files

Filtered logs:

```text
reports/debug/algo_debug_<timestamp>.log
reports/debug/algo_debug_<timestamp>.log.gz
reports/debug/algo_debug_latest.log
reports/debug/algo_debug_latest.log.gz
```

Manual upload file:

```text
reports/debug/algo_debug_latest.log.gz
```

AI analysis:

```text
reports/debug/chatgpt_analysis_<timestamp>.md
reports/debug/chatgpt_analysis_latest.md
```

## Retention

By default, old debug files older than 5 days are deleted from `reports/debug` only:

```text
algo_debug_*.log
algo_debug_*.log.gz
chatgpt_analysis_*.md
```

Latest files are always preserved. Disable cleanup with:

```bash
PYTHONPATH=. python scripts/send_debug_to_openai.py --no-cleanup
```

Change retention:

```bash
PYTHONPATH=. python scripts/send_debug_to_openai.py --retention-days 10
```

Cleanup output includes:

```text
CLEANUP_DELETED path=...
CLEANUP_SKIPPED reason=...
```
