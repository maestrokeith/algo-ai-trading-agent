#!/usr/bin/env python3
"""Generate a paper replay conversion report from scan selection to simulated submit."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import replay_live_cycle
from src.portfolio.allocator_planner import parse_capital_allocator_cfg


def _safe_user(user_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(user_id or "default"))


def _stage(result: bool, reason: str = "", detail: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"result": bool(result), "reason": str(reason or ""), "detail": dict(detail or {})}


def _config_snapshot(config: Mapping[str, Any]) -> dict[str, Any]:
    ca_cfg = parse_capital_allocator_cfg(
        config.get("portfolio") if isinstance(config.get("portfolio"), Mapping) else {}
    )
    return {
        "capital_allocator.allow_no_trade_cycles": bool(ca_cfg.get("allow_no_trade_cycles", False)),
        "capital_allocator.require_net_sell_gte_buy": bool(ca_cfg.get("require_net_sell_gte_buy", False)),
        "capital_allocator.selected_must_execute": bool(ca_cfg.get("selected_must_execute", False)),
        "capital_allocator.min_trade_size": float(ca_cfg.get("min_trade_size", 0.0) or 0.0),
        "capital_allocator.min_realloc_leg": float(ca_cfg.get("min_realloc_leg", 0.0) or 0.0),
        "capital_allocator.minimum_cash_to_deploy_pct": float(
            ca_cfg.get("minimum_cash_to_deploy_pct", 0.0) or 0.0
        ),
    }


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _latest_saved_replay_summary(project_root: Path, *, date: str, user: str) -> dict[str, Any] | None:
    replay_dir = project_root / "data" / "replay"
    if not replay_dir.exists():
        return None
    safe_user = _safe_user(user)
    paths = sorted(replay_dir.glob(f"*_{safe_user}.json"))
    if date != "latest":
        paths = [path for path in paths if date in path.name]
    if not paths:
        return None
    payload = _read_json(paths[-1])
    if not isinstance(payload, Mapping):
        return None
    out = dict(payload)
    if str(out.get("date") or "").strip().lower() in {"", "latest"}:
        out["date"] = paths[-1].name.split("_", 1)[0]
    return out


def _parse_kv(line: str) -> dict[str, str]:
    return {
        str(match.group(1)): str(match.group(2))
        for match in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)=([^ ]+)", str(line or ""))
    }


def _trade_cycle_from_saved_logs(summary: Mapping[str, Any], symbol: str) -> dict[str, Any] | None:
    sym = str(symbol or "").strip().upper()
    for line in summary.get("log_lines") or []:
        if "TRADE_CYCLE_GATE" not in str(line):
            continue
        kv = _parse_kv(str(line))
        if str(kv.get("symbol") or "").strip().upper() != sym:
            continue
        return {
            "result": str(kv.get("trade_cycle_allowed") or "").strip().lower() in {"true", "1", "yes"},
            "reason": str(kv.get("skip_reason") or "none"),
            "line": str(line),
        }
    return None


def _no_trade_explanation(reason: str, config_values: Mapping[str, Any]) -> dict[str, Any] | None:
    if reason != "no_trade_cycle_allowed":
        return None
    return {
        "gate": "capital_allocator.allow_no_trade_cycles",
        "config_value": config_values.get("capital_allocator.allow_no_trade_cycles"),
        "explanation": (
            "Allocator had selected candidates but no action survived sizing/planner filters; "
            "the configured paper/replay idle branch allowed a no-trade cycle instead of forcing an order."
        ),
    }


def build_paper_session_conversion_report(
    *,
    project_root: Path = PROJECT_ROOT,
    date: str = "latest",
    user: str = "paper_bot",
    history_user: str | None = None,
    summary_dir: Path | None = None,
) -> dict[str, Any]:
    """Run mock replay and return candidate conversion diagnostics for a paper user."""
    try:
        summary = replay_live_cycle.run_replay(
            project_root=project_root,
            date=date,
            user=user,
            history_user=history_user,
            broker_mock=True,
            summary_dir=summary_dir,
        )
        source = "fresh_replay"
    except RuntimeError as exc:
        saved = _latest_saved_replay_summary(project_root, date=date, user=user)
        if saved is None:
            raise
        summary = saved
        source = f"saved_replay_after:{type(exc).__name__}"
    config = replay_live_cycle._load_replay_config(project_root, user)
    config_values = _config_snapshot(config)
    selected = {str(row.get("symbol") or "").upper(): row for row in summary.get("selected_candidates", [])}
    action_symbols = {str(row.get("symbol") or "").upper() for row in summary.get("allocator_actions_created", [])}
    submitted_symbols = {str(row.get("symbol") or "").upper() for row in summary.get("simulated_submitted_orders", [])}
    rows: list[dict[str, Any]] = []
    for trace in summary.get("per_symbol_trace", []):
        if not isinstance(trace, Mapping):
            continue
        sym = str(trace.get("symbol") or "").upper()
        if not sym:
            continue
        trade_cycle = trace.get("trade_cycle_allowed") if isinstance(trace.get("trade_cycle_allowed"), Mapping) else {}
        if not trade_cycle:
            trade_cycle = _trade_cycle_from_saved_logs(summary, sym) or {}
        submit_trace = trace.get("simulated_submit") if isinstance(trace.get("simulated_submit"), Mapping) else {}
        entry_eval = trace.get("entry_eval") if isinstance(trace.get("entry_eval"), Mapping) else {}
        order_skip = trace.get("order_skip") if isinstance(trace.get("order_skip"), Mapping) else {}
        submit_reason = str(submit_trace.get("reason") or "not_submitted")
        if sym not in submitted_symbols and submit_reason == "submitted":
            submit_reason = "no_submitted_order_in_replay_summary"
        tc_reason = str(trade_cycle.get("reason") or "")
        row = {
            "symbol": sym,
            "dynamic_scan": _stage(sym in selected, "selected" if sym in selected else "not_selected"),
            "selected": _stage(sym in selected, "selected" if sym in selected else "not_selected"),
            "entry_eval_pass": _stage(
                bool(entry_eval.get("result")),
                str(entry_eval.get("reason") or ("ok" if entry_eval.get("result") else "not_passed")),
            ),
            "allocator_input": trace.get("allocator_candidate") or _stage(False, "not_seen_by_allocator"),
            "allocator_action": trace.get("allocator_action") or _stage(sym in action_symbols, ""),
            "trade_cycle_allowed": trade_cycle or _stage(False, "trade_cycle_gate_not_logged"),
            "order_skip": _stage(
                bool(order_skip.get("result")),
                str(order_skip.get("reason") or "none"),
                {"source_stage": str(order_skip.get("source_stage") or "none")},
            ),
            "simulated_order_submitted": _stage(
                sym in submitted_symbols,
                "submitted" if sym in submitted_symbols else submit_reason,
            ),
            "final_stage": str(trace.get("final_stage") or ("order_submitted" if sym in submitted_symbols else "unknown")),
            "final_reason": str(trace.get("final_reason") or submit_reason),
        }
        explanation = _no_trade_explanation(tc_reason, config_values)
        if explanation is not None:
            row["no_trade_cycle_allowed_explanation"] = explanation
        rows.append(row)
    return {
        "date": summary.get("date"),
        "replay_summary_path": summary.get("summary_path"),
        "user": user,
        "history_user": summary.get("history_user"),
        "source": source,
        "config": config_values,
        "summary": {
            "selected_candidates": len(summary.get("selected_candidates", [])),
            "allocator_actions_created": len(summary.get("allocator_actions_created", [])),
            "simulated_submitted_orders": len(summary.get("simulated_submitted_orders", [])),
            "no_trade_cycle_allowed": sum(
                1
                for row in rows
                if (row.get("trade_cycle_allowed") or {}).get("reason") == "no_trade_cycle_allowed"
            ),
        },
        "candidates": rows,
    }


def render_paper_session_conversion_report(report: Mapping[str, Any]) -> str:
    lines = [
        f"# Paper Session Conversion Report - {report.get('date')}",
        "",
        "Research-only. Live thresholds and live execution are not changed.",
        "",
        f"- User: `{report.get('user')}`",
        f"- History user: `{report.get('history_user')}`",
    ]
    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    for key in ("selected_candidates", "allocator_actions_created", "simulated_submitted_orders", "no_trade_cycle_allowed"):
        lines.append(f"- {key}: {summary.get(key, 0)}")
    lines.extend(["", "## Config Gates"])
    config = report.get("config") if isinstance(report.get("config"), Mapping) else {}
    for key, value in config.items():
        lines.append(f"- `{key}` = `{value}`")
    lines.extend(["", "## Candidate Trace", ""])
    rows = list(report.get("candidates") or [])
    if not rows:
        lines.append("No candidates found.")
        return "\n".join(lines).rstrip() + "\n"
    lines.append("| Symbol | Selected | Entry Eval Pass | Allocator Input | Allocator Action | Order Skip | Sim Submit | Final Stage | Block Reason |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---|---|")
    for row in rows[:50]:
        trade_cycle = row.get("trade_cycle_allowed") if isinstance(row.get("trade_cycle_allowed"), Mapping) else {}
        submit = row.get("simulated_order_submitted") if isinstance(row.get("simulated_order_submitted"), Mapping) else {}
        entry_eval = row.get("entry_eval_pass") if isinstance(row.get("entry_eval_pass"), Mapping) else {}
        order_skip = row.get("order_skip") if isinstance(row.get("order_skip"), Mapping) else {}
        action = row.get("allocator_action") if isinstance(row.get("allocator_action"), Mapping) else {}
        alloc = row.get("allocator_input") if isinstance(row.get("allocator_input"), Mapping) else {}
        selected = row.get("selected") if isinstance(row.get("selected"), Mapping) else {}
        reason = str(
            row.get("final_reason")
            or order_skip.get("reason")
            or submit.get("reason")
            or trade_cycle.get("reason")
            or action.get("reason")
            or alloc.get("reason")
            or ""
        )
        lines.append(
            "| {symbol} | {selected} | {entry_eval} | {alloc} | {action} | {order_skip} | {submit} | {final_stage} | {reason} |".format(
                symbol=row.get("symbol"),
                selected=bool(selected.get("result")),
                entry_eval=bool(entry_eval.get("result")),
                alloc=bool(alloc.get("result")),
                action=bool(action.get("result")),
                order_skip=bool(order_skip.get("result")),
                submit=bool(submit.get("result")),
                final_stage=row.get("final_stage") or "",
                reason=reason,
            )
        )
        expl = row.get("no_trade_cycle_allowed_explanation")
        if isinstance(expl, Mapping):
            lines.append(
                f"| {row.get('symbol')} detail |  |  |  |  |  | gate `{expl.get('gate')}` = `{expl.get('config_value')}` |"
            )
    return "\n".join(lines).rstrip() + "\n"


def write_paper_session_conversion_report(
    *,
    project_root: Path = PROJECT_ROOT,
    date: str = "latest",
    user: str = "paper_bot",
    history_user: str | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    report = build_paper_session_conversion_report(
        project_root=project_root,
        date=date,
        user=user,
        history_user=history_user,
    )
    out_dir = project_root / "data" / "research" / "paper_session_conversion"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_user = _safe_user(user)
    day = str(report.get("date") or date)
    json_path = out_dir / f"{day}_{safe_user}.json"
    md_path = out_dir / f"{day}_{safe_user}.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(render_paper_session_conversion_report(report), encoding="utf-8")
    return md_path, json_path, report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default="latest", help="YYYY-MM-DD or latest.")
    parser.add_argument("--user", default="paper_bot")
    parser.add_argument("--history-user", default=None)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        md_path, json_path, report = write_paper_session_conversion_report(
            project_root=args.project_root,
            date=args.date,
            user=args.user,
            history_user=args.history_user,
        )
    except Exception as exc:
        print(f"PAPER_SESSION_CONVERSION_FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(render_paper_session_conversion_report(report))
    print(f"Markdown: {md_path}")
    print(f"JSON: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
