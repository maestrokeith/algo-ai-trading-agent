# Codex Nightly Report

Date: 2026-06-05
Branch: local `main`

## Issues completed

| Issue | Commit | Summary |
| --- | --- | --- |
| #72 | `8b764cf0` | Implemented market-open preflight smoke tests and CLI. |
| #73 | `1ad77c76` | Implemented global risk guardrails and persisted kill switch. |
| #78 | `local commit` | Completed FastMCP ChatGPT monitoring server, full tool surface, approval-gated actions, and integration docs. |
| #79 | `6f40bc8d` | Implemented autonomous incident detection, packaging, diagnostics, remediation workflow, and controlled restart handling. |
| #74 | `03d44777` | Implemented generic no-capital strategy shadow mode. |
| #75 | `cfe984f6` | Implemented strategy attribution dashboard data aggregation. |
| #76 | `70329f4d` | Added explicit bull, bear, sideways, high-volatility, and low-volatility regime detection. |
| #77 | `bb7777a5` | Implemented self-tuning recommendations for stops, targets, hold time, and ranking weights. |
| #81 | `274d3f1e` | Fixed MCP recent-error filtering, validated read-only server startup, and documented ChatGPT/MCP commands. |
| #83 | `this commit` | Aligned dynamic scanner selection with live dynamic-entry spread, relative-volume, VWAP, and confirmation gates. |

## Files changed

Production files:

- `mcp_server.py`
- `scripts/run_mcp_supervisor.py`
- `requirements.txt`
- `scripts/run_preflight.py`
- `src/global_risk.py`
- `src/incident_response.py`
- `src/market_regime.py`
- `src/preflight.py`
- `src/shadow_mode.py`
- `src/strategy_attribution.py`
- `src/supervisor.py`
- `src/tuning_recommendations.py`
- `src/dynamic_universe.py`
- `src/app/live_cycle.py`
- `scripts/preflight_live_safety.py`

Docs added:

- `docs/MCP_SETUP.md`
- `docs/CHATGPT_INTEGRATION.md`

Tests added:

- `tests/test_global_risk.py`
- `tests/test_incident_response.py`
- `tests/test_market_regime_detection.py`
- `tests/test_mcp_server.py`
- `tests/test_preflight.py`
- `tests/test_shadow_mode.py`
- `tests/test_strategy_attribution.py`
- `tests/test_supervisor.py`
- `tests/test_tuning_recommendations.py`
- `tests/test_dynamic_universe.py`

## Tests executed

Focused tests were run for each new feature area before the full suite:

- `PYTHONPATH=. pytest tests/test_preflight.py -v`
- `PYTHONPATH=. pytest tests/test_global_risk.py -v`
- `PYTHONPATH=. pytest tests/test_supervisor.py -v`
- `PYTHONPATH=. pytest tests/test_supervisor.py tests/test_mcp_server.py -v`
- `PYTHONPATH=. pytest tests/test_incident_response.py -v`
- `PYTHONPATH=. pytest tests/test_shadow_mode.py -v`
- `PYTHONPATH=. pytest tests/test_strategy_attribution.py -v`
- `PYTHONPATH=. pytest tests/test_market_regime_detection.py tests/test_market_regime.py -v`
- `PYTHONPATH=. pytest tests/test_tuning_recommendations.py -v`
- `PYTHONPATH=. pytest tests/test_supervisor.py tests/test_mcp_server.py -v`
- `PYTHONPATH=. pytest tests/test_dynamic_universe.py tests/test_live_cycle.py tests/test_preflight_live_safety.py -v`
- `PYTHONPATH=. python scripts/run_mcp_supervisor.py --config config/default.yaml get_health_status`
- `PYTHONPATH=. python scripts/run_mcp_supervisor.py --config config/default.yaml get_recent_logs`
- `PYTHONPATH=. python scripts/run_mcp_supervisor.py --config config/default.yaml get_recent_errors`
- `PYTHONPATH=. python scripts/run_preflight.py --config config/default.yaml`

Full suite was run after each issue:

- #72: `2035 passed, 28 warnings`
- #73: `2039 passed, 28 warnings`
- #78: `2045 passed, 28 warnings`
- #79: `2051 passed, 28 warnings`
- #74: `2054 passed, 28 warnings`
- #75: `2057 passed, 28 warnings`
- #76: `2061 passed, 28 warnings`
- #77: `2066 passed, 28 warnings`
- #78 completion follow-up: `2069 passed, 28 warnings`
- #81/#83 focused gates: `17 passed, 1 warning`; `133 passed, 1 warning`

Final full-suite gate after #81/#83: `2092 passed, 1 failed, 28 warnings`.

## Blockers

- Full pytest is blocked by pre-existing dirty config drift in `config/default.yaml`: `dynamic_universe.catalyst_boost.min_relative_volume_with_catalyst` is currently `0.75`, while `tests/test_config_loader.py::test_default_config_uses_concentrated_stock_book` expects `1.0`. This file was already modified before #81/#83 work and was not changed for these issues.

## Recommendations

- Use `PYTHONPATH=. python mcp_server.py` for local stdio MCP monitoring and put any remote MCP transport behind authenticated TLS.
- Keep restart callbacks disabled or paper-only until an operator explicitly approves deployment wiring.
- Keep `ALGOSPHERE_MCP_ALLOW_APPROVED_ACTIONS` unset for normal ChatGPT monitoring.
- Review global risk limits in `config/default.yaml` before enabling guardrail enforcement against real accounts.
- Use shadow-mode and attribution outputs for paper validation before promoting any strategy changes.
