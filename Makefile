# Shortcuts from repo root: `make loop-paper`, `make help`
PYTHON ?= python3

.PHONY: help loop loop-paper loop-live loop-v once example backtest \
	equity prices positions charts sells summary reset download-data scheduled \
	algo-debug-report

help:
	@echo "AlgoSphere — quick commands (use PYTHON=python3.12 to pick interpreter):"
	@echo ""
	@echo "  make loop-paper   Alpaca loop, paper (default)"
	@echo "  make loop-live    Alpaca loop, LIVE — real money"
	@echo "  make loop-v       loop + --verbose"
	@echo "  make once         single Alpaca engine pass"
	@echo "  make example      synthetic run_example"
	@echo "  make backtest     run_backtest"
	@echo "  make equity|prices|positions|charts|sells|summary"
	@echo "  make reset        reset paper + tracked JSON"
	@echo "  make download-data"
	@echo "  make algo-debug-report   collect recent algo logs and request OpenAI analysis"
	@echo ""
	@echo "Lean CLI:  python lean backtest|live   (→ scripts/lean_cli.py)"
	@echo "Same via:  ./bin/algo <command>   (see ./bin/algo help)"

loop loop-paper:
	$(PYTHON) scripts/run_alpaca_loop.py --paper

loop-live:
	$(PYTHON) scripts/run_alpaca_loop.py --live

loop-v:
	$(PYTHON) scripts/run_alpaca_loop.py --paper --verbose

once:
	$(PYTHON) scripts/run_alpaca.py

example:
	$(PYTHON) scripts/run_example.py

backtest:
	$(PYTHON) scripts/run_backtest.py

equity:
	$(PYTHON) scripts/check_equity.py

prices:
	$(PYTHON) scripts/check_prices.py

positions:
	$(PYTHON) scripts/check_positions.py

charts:
	$(PYTHON) scripts/show_position_charts.py

sells:
	$(PYTHON) scripts/show_sell_strategy.py

summary:
	$(PYTHON) scripts/show_daily_summary.py

reset:
	$(PYTHON) scripts/reset_paper.py

download-data:
	$(PYTHON) scripts/download_backtest_data.py

scheduled:
	$(PYTHON) scripts/run_scheduled_alpaca.py

algo-debug-report:
	PYTHONPATH=. $(PYTHON) scripts/send_debug_to_openai.py --since "30 minutes ago"
