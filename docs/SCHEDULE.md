# Scheduling the trading loop at market open

**Nothing runs or schedules by default.** The app does not start the loop or run at market open unless you explicitly:

- run a script yourself, or  
- set up cron (or another scheduler) yourself.

Use the options below **only when you want** to schedule the loop at market open.

---

## Option 1: Cron (run at 9:30 AM ET every weekday)

1. Make the script executable (once):
   ```bash
   chmod +x /Users/psuriset/cursor/algo/start_trading_at_market_open.sh
   ```

2. Set your API keys so cron can see them. Either:
   - Add the keys to the top of `start_trading_at_market_open.sh`:
     ```bash
     export APCA_API_KEY_ID=your_key
     export APCA_API_SECRET_KEY=your_secret
     ```
   - Or use a `.env` file and in the script add: `set -a; source /path/to/algo/.env; set +a`

3. Open crontab:
   ```bash
   crontab -e
   ```

4. Add one line (use the **full path** to the script and adjust time if you're not on Eastern):
   - **System clock is Eastern (e.g. New York):**
     ```
     30 9 * * 1-5 /Users/psuriset/cursor/algo/start_trading_at_market_open.sh >> /Users/psuriset/cursor/algo/logs/cron.log 2>&1
     ```
   - **System clock is Pacific:** 9:30 AM ET = 6:30 AM PT:
     ```
     30 6 * * 1-5 /Users/psuriset/cursor/algo/start_trading_at_market_open.sh >> /Users/psuriset/cursor/algo/logs/cron.log 2>&1
     ```
   - **System clock is UTC:** 9:30 AM ET ≈ 14:30 UTC (13:30 in winter):
     ```
     30 14 * * 1-5 /Users/psuriset/cursor/algo/start_trading_at_market_open.sh >> /Users/psuriset/cursor/algo/logs/cron.log 2>&1
     ```

5. Create the log directory so cron doesn’t fail:
   ```bash
   mkdir -p /Users/psuriset/cursor/algo/logs
   ```

Cron will start the loop at 9:30; the loop runs until market close (or daily limit), then exits. The next day cron starts it again.

---

## Option 2: Python scheduler (wait until 9:30, then run; repeats daily)

Run once and leave it running (e.g. in `screen` or `tmux` or as a service). It sleeps until 9:30 AM ET (Mon–Fri), runs the trading loop, and when the loop exits, waits for the next market open.

```bash
cd /Users/psuriset/cursor/algo
python3 run_scheduled_alpaca.py
```

To run in the background and keep it after you log out:
```bash
nohup python3 run_scheduled_alpaca.py >> logs/scheduled.log 2>&1 &
```

No cron needed; ensure the process (or your machine) stays running.

---

## Summary

| Method | When it runs | Best if |
|--------|----------------|--------|
| **Cron** | Fires at 9:30 AM (your system time); starts the loop once per day | You want the OS to start the loop at a fixed time |
| **run_scheduled_alpaca.py** | Waits until 9:30 AM ET, runs loop, then waits for next open | You run one long-lived process and don’t want to configure cron |

Both assume API keys are in the environment (or set in the shell script for cron).

---

## Default behavior

- **No automatic scheduling.** The trading loop does not run at market open (or at any time) unless you start it or configure a schedule.
- To run the loop once: `python scripts/run_alpaca.py` or `python scripts/run_alpaca_loop.py`.
- To schedule at market open: use Option 1 (cron) or Option 2 (run_scheduled_alpaca.py) only when you decide you want it.
