from __future__ import annotations

from pathlib import Path
from typing import Sequence

import pytest

from scripts import run_end_day_review as edr


class FakeRunner(edr.CommandRunner):
    def __init__(self, responses: dict[str, edr.CommandResult] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: Sequence[str], *, cwd: Path = edr.PROJECT_ROOT) -> edr.CommandResult:
        call = tuple(str(part) for part in argv)
        self.calls.append(call)
        for key, result in self.responses.items():
            if key in " ".join(call):
                return result
        return edr.CommandResult(0, "")


class MissingJournalRunner(FakeRunner):
    def run(self, argv: Sequence[str], *, cwd: Path = edr.PROJECT_ROOT) -> edr.CommandResult:
        call = tuple(str(part) for part in argv)
        self.calls.append(call)
        if call and call[0] == "journalctl":
            raise FileNotFoundError("journalctl")
        for key, result in self.responses.items():
            if key in " ".join(call):
                return result
        return edr.CommandResult(0, "")


def test_build_review_commands_routes_live_read_only_self_heal() -> None:
    root = Path("/repo")

    commands = edr.build_review_commands(root=root, env="live", day="2026-06-24")
    argv = [command.argv for command in commands]

    assert argv[0] == ("/repo/bin/algo", "capture-metrics", "--end-day", "--live", "--date", "2026-06-24", "--user", "live_bot")
    assert argv[1] == ("/repo/bin/algo", "summary", "2026-06-24", "--user", "live_bot")
    assert argv[2] == ("/repo/bin/algo", "research-feedback", "2026-06-24", "--user", "live_bot")
    assert argv[3] == ("/repo/bin/algo", "strategy-quality-report", "--date", "2026-06-24", "--user", "live_bot")
    assert argv[4][-1] == "--live"
    assert argv[5] == ("/repo/bin/algo", "self-heal", "--live", "--dry-run")
    assert argv[6] == ("/repo/bin/algo", "autoops", "report")
    assert argv[7] == ("/repo/bin/algo", "dynamic-entry-alignment-report", "--date", "2026-06-24", "--user", "live_bot")
    assert argv[8] == ("/repo/bin/algo", "dynamic-entry-adaptive-report", "--date", "2026-06-24", "--user", "live_bot")
    assert argv[9] == ("/repo/bin/algo", "dynamic-funnel-report", "--date", "2026-06-24", "--user", "live_bot")


def test_build_review_commands_routes_paper_user_and_flags() -> None:
    root = Path("/repo")

    commands = edr.build_review_commands(root=root, env="paper", day="2026-06-24")
    argv = [command.argv for command in commands]

    assert argv[0] == ("/repo/bin/algo", "capture-metrics", "--end-day", "--paper", "--date", "2026-06-24", "--user", "paper_bot")
    assert argv[1] == ("/repo/bin/algo", "summary", "2026-06-24", "--user", "paper_bot")
    assert argv[2] == ("/repo/bin/algo", "research-feedback", "2026-06-24", "--user", "paper_bot")
    assert argv[3] == ("/repo/bin/algo", "strategy-quality-report", "--date", "2026-06-24", "--user", "paper_bot")
    assert argv[4][-1] == "--paper"
    assert argv[5] == ("/repo/bin/algo", "self-heal", "--paper", "--dry-run")
    paper_log = "/repo/data/review/2026-06-24/paper_full.log"
    assert argv[9] == (
        "/repo/bin/algo",
        "dynamic-funnel-report",
        "--date",
        "2026-06-24",
        "--user",
        "paper_bot",
        "--log-file",
        paper_log,
    )
    assert argv[10] == (
        "/repo/bin/algo",
        "dynamic-rvol-forward-returns",
        "--date",
        "2026-06-24",
        "--user",
        "paper_bot",
        "--log-path",
        paper_log,
    )


def test_bin_algo_exposes_end_day_route() -> None:
    text = (edr.PROJECT_ROOT / "bin" / "algo").read_text(encoding="utf-8")

    assert "end-day)" in text
    assert 'scripts/run_end_day_review.py "$@"' in text
    assert "end-day      consolidated read-only end-of-day review --live|--paper" in text


def test_collect_logs_paper_mac_uses_review_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ALGO_AUTOOPS_PLATFORM", "Darwin")
    monkeypatch.delenv("ALGO_END_DAY_LOG_FILE", raising=False)
    monkeypatch.delenv("ALGO_PAPER_LOG_FILE", raising=False)
    log_path = tmp_path / "data" / "review" / "2026-06-24" / "paper_full.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("ORDER_SUBMITTED symbol=AMD\n", encoding="utf-8")
    runner = MissingJournalRunner()

    logs = edr._collect_logs_since_market_open(
        runner=runner,
        root=tmp_path,
        env="paper",
        day="2026-06-24",
        log_file=None,
    )

    assert logs == "ORDER_SUBMITTED symbol=AMD\n"
    assert runner.calls == []
    out = capsys.readouterr().out
    assert f"END_DAY_LOG_SOURCE source=file path={log_path}" in out


def test_collect_logs_paper_mac_does_not_use_older_generic_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ALGO_AUTOOPS_PLATFORM", "Darwin")
    monkeypatch.delenv("ALGO_END_DAY_LOG_FILE", raising=False)
    monkeypatch.delenv("ALGO_PAPER_LOG_FILE", raising=False)
    stale_dir = tmp_path / "data" / "logs"
    stale_dir.mkdir(parents=True)
    stale_log = stale_dir / "paper_full.log"
    stale_log.write_text("ORDER_SUBMITTED symbol=STALE\n", encoding="utf-8")
    runner = MissingJournalRunner()

    logs = edr._collect_logs_since_market_open(
        runner=runner,
        root=tmp_path,
        env="paper",
        day="2026-06-24",
        log_file=None,
    )

    assert logs == ""
    assert runner.calls == []
    out = capsys.readouterr().out
    assert "END_DAY_LOG_SOURCE source=none" in out
    assert str(stale_log) not in out


def test_collect_logs_missing_journalctl_is_graceful(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ALGO_AUTOOPS_PLATFORM", "Linux")
    monkeypatch.delenv("ALGO_END_DAY_LOG_FILE", raising=False)
    monkeypatch.delenv("ALGO_PAPER_LOG_FILE", raising=False)
    runner = MissingJournalRunner()

    logs = edr._collect_logs_since_market_open(
        runner=runner,
        root=tmp_path,
        env="paper",
        day="2026-06-24",
        log_file=None,
    )

    assert logs == ""
    assert ("journalctl", "-u", "paper.service", "--since", "2026-06-24 09:30:00", "--no-pager") in runner.calls
    assert "END_DAY_LOG_SOURCE source=none reason=journalctl_unavailable" in capsys.readouterr().out


def test_collect_logs_live_linux_keeps_journalctl_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ALGO_AUTOOPS_PLATFORM", "Linux")
    monkeypatch.delenv("ALGO_END_DAY_LOG_FILE", raising=False)
    runner = FakeRunner(
        {
            "journalctl -u algo.service": edr.CommandResult(0, "ORDER_FILLED symbol=SPY\n", ""),
        }
    )

    logs = edr._collect_logs_since_market_open(
        runner=runner,
        root=tmp_path,
        env="live",
        day="2026-06-24",
        log_file=None,
    )

    assert logs == "ORDER_FILLED symbol=SPY\n"
    assert ("journalctl", "-u", "algo.service", "--since", "2026-06-24 09:30:00", "--no-pager") in runner.calls
    assert "END_DAY_LOG_SOURCE source=journalctl" in capsys.readouterr().out


def test_end_day_review_prints_final_status(tmp_path: Path, capsys) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "default.yaml").write_text(
        "\n".join(
            [
                "options:",
                "  enabled: false",
                "  mode: paper_only",
            ]
        ),
        encoding="utf-8",
    )
    (config_dir / "users.yaml").write_text(
        "\n".join(
            [
                "users:",
                "  - id: live_bot",
                "    paper: false",
                "    overrides:",
                "      options:",
                "        enabled: true",
                "        mode: live_long_premium",
                "        live_pilot:",
                "          enabled: true",
            ]
        ),
        encoding="utf-8",
    )
    log_file = tmp_path / "algo.log"
    log_file.write_text(
        "\n".join(
            [
                "2026-06-24T10:00:00 OPTIONS_ALLOCATOR_ACCEPT symbol=QQQ",
                "2026-06-24T10:01:00 OPTIONS_ORDER_INTENT symbol=QQQ",
                "2026-06-24T10:02:00 ORDER_SUBMITTED symbol=QQQ",
                "2026-06-24T10:03:00 ORDER_FILLED symbol=QQQ",
                "2026-06-24T10:04:00 DYNAMIC_WEAK_CATALYST_CLASSIFIED symbol=ABCD",
                "2026-06-24T10:05:00 DYNAMIC_WEAK_CATALYST_REJECT symbol=ABCD reason=weak_catalyst",
            ]
        ),
        encoding="utf-8",
    )
    runner = FakeRunner(
        {
            "summary": edr.CommandResult(
                0,
                "Daily Summary 2026-06-24 [live_bot]\n"
                "Activity: submitted_orders=6 buys=2 sells=16 exits=16 pnl_missing_exits=15 symbols=MU\n",
            ),
            "self-heal": edr.CommandResult(0, "SELF_HEAL status=healthy env=live\n"),
            "autoops report": edr.CommandResult(0, "AutoOps report\n- success %: 75.0\n"),
        }
    )
    args = edr.build_parser().parse_args(
        [
            "--live",
            "--date",
            "2026-06-24",
            "--project-root",
            str(tmp_path),
            "--log-file",
            str(log_file),
        ]
    )

    assert edr.run_end_day_review(args, runner=runner) == 0

    out = capsys.readouterr().out
    assert "END_DAY_REVIEW_STATUS" in out
    assert "- metrics captured: yes" in out
    assert "- summary generated: yes" in out
    assert "- self-heal: healthy" in out
    assert "- autoops: 75.0%" in out
    assert "- options pilot: enabled yes, orders count 1, kill switches count 0" in out
    assert (
        "ORDER_COUNT_RECONCILIATION journal_submitted=1 journal_filled=1 journal_rejected=0 "
        "daily_summary_submitted=6 daily_summary_sells=16 daily_summary_exits=16 "
        "daily_summary_pnl_missing_exits=15 sources=journalctl,daily_summary"
    ) in out
    assert "DYNAMIC_WEAK_CATALYST_REVIEW" in out
    assert "- classified: 1" in out
    assert "- rejected: 1" in out
    assert "- recommendation: leave config unchanged" in out


def test_end_day_review_reconciles_broker_open_unrealized_after_positions(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "default.yaml").write_text("options: {}\n", encoding="utf-8")
    log_file = tmp_path / "algo.log"
    log_file.write_text("", encoding="utf-8")
    positions_output = "\n".join(
        [
            "[LIVE] Equity: $100,000.00",
            "Open positions: 3",
            "",
            "Symbol       Qty  Side       Market $      Unreal $      P/L %",
            "--------------------------------------------------------------",
            "IWM           18  LONG     $   5,400.00  $     +18.25    +0.34%",
            "XLF           38  LONG     $   2,000.00  $     +18.61    +0.93%",
            "JPM            1  LONG     $     340.00  $      +0.00    +0.00%",
            "--------------------------------------------------------------",
            "TOTAL                         $   7,740.00  $     +36.86    +0.48%",
        ]
    )
    runner = FakeRunner(
        {
            "summary": edr.CommandResult(
                0,
                "Daily Summary 2026-06-26 [live_bot]\n"
                "PnL: realized=$-59.82 unrealized=$0.00 total=$-59.82\n"
                "Activity: submitted_orders=6 buys=2 sells=16 exits=16 pnl_missing_exits=15 symbols=MU\n"
                "Unrealized source: trade_attribution_only broker_open_unrealized=$0.00 positions=0\n",
            ),
            "check_positions.py": edr.CommandResult(0, positions_output),
            "self-heal": edr.CommandResult(0, "SELF_HEAL status=healthy env=live\n"),
        }
    )
    args = edr.build_parser().parse_args(
        [
            "--live",
            "--date",
            "2026-06-26",
            "--project-root",
            str(tmp_path),
            "--log-file",
            str(log_file),
        ]
    )

    assert edr.run_end_day_review(args, runner=runner) == 0

    out = capsys.readouterr().out
    assert (
        "ORDER_COUNT_RECONCILIATION journal_submitted=0 journal_filled=0 journal_rejected=0 "
        "daily_summary_submitted=6 daily_summary_sells=16 daily_summary_exits=16 "
        "daily_summary_pnl_missing_exits=15 sources=journalctl,daily_summary"
    ) in out
    assert "Unrealized source: broker_open_positions broker_open_unrealized=$36.86 positions=3" in out
    assert (
        "END_DAY_UNREALIZED_RECONCILIATION trade_attribution_unrealized=$0.00 "
        "broker_open_unrealized=$36.86 positions=3 sources=daily_summary,positions_command"
    ) in out


def test_end_day_review_reconciliation_reports_attribution_fill_source(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "default.yaml").write_text("options: {}\n", encoding="utf-8")
    log_file = tmp_path / "algo.log"
    log_file.write_text("2026-07-06T10:00:00 ORDER_SUBMITTED symbol=OPEN\n", encoding="utf-8")
    runner = FakeRunner(
        {
            "summary": edr.CommandResult(
                0,
                "Daily Summary 2026-07-06 [live_bot]\n"
                "Activity: submitted_orders=8 buys=0 sells=12 exits=12 pnl_missing_exits=10 symbols=OPEN\n",
            ),
            "self-heal": edr.CommandResult(0, "SELF_HEAL status=healthy env=live\n"),
        }
    )
    args = edr.build_parser().parse_args(
        [
            "--live",
            "--date",
            "2026-07-06",
            "--project-root",
            str(tmp_path),
            "--log-file",
            str(log_file),
        ]
    )

    assert edr.run_end_day_review(args, runner=runner) == 0

    out = capsys.readouterr().out
    assert "journal_submitted=1 journal_filled=0" in out
    assert "alternate_fill_source=daily_summary_attribution_exits" in out
    assert "alternate_fill_count=12" in out
    assert "journalctl_order_fill_events_absent_but_daily_summary_has_exit_activity" in out


def test_end_day_review_recommends_review_for_blocked_self_heal(tmp_path: Path, capsys) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "default.yaml").write_text("options: {}\n", encoding="utf-8")
    log_file = tmp_path / "algo.log"
    log_file.write_text("OPTIONS_KILL_SWITCH triggered\n", encoding="utf-8")
    runner = FakeRunner(
        {
            "self-heal": edr.CommandResult(0, "SELF_HEAL status=blocked reason=entry_eval_pending\n"),
            "autoops report": edr.CommandResult(0, "AutoOps report\n- success %: 50.0\n"),
        }
    )
    args = edr.build_parser().parse_args(
        [
            "--paper",
            "--date",
            "2026-06-24",
            "--project-root",
            str(tmp_path),
            "--log-file",
            str(log_file),
        ]
    )

    assert edr.run_end_day_review(args, runner=runner) == 1

    out = capsys.readouterr().out
    assert "- self-heal: blocked" in out
    assert "- options pilot: enabled no, orders count 0, kill switches count 1" in out
    assert "- recommendation: review issues" in out


def test_end_day_review_paper_mac_missing_logs_creates_empty_review_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ALGO_AUTOOPS_PLATFORM", "Darwin")
    monkeypatch.delenv("ALGO_END_DAY_LOG_FILE", raising=False)
    monkeypatch.delenv("ALGO_PAPER_LOG_FILE", raising=False)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "default.yaml").write_text("options: {}\n", encoding="utf-8")
    runner = MissingJournalRunner(
        {
            "self-heal": edr.CommandResult(0, "SELF_HEAL status=healthy env=paper\n"),
            "autoops report": edr.CommandResult(0, "AutoOps report\n- success %: 100.0\n"),
        }
    )
    args = edr.build_parser().parse_args(
        [
            "--paper",
            "--date",
            "2026-06-24",
            "--project-root",
            str(tmp_path),
        ]
    )

    assert edr.run_end_day_review(args, runner=runner) == 0

    out = capsys.readouterr().out
    assert "END_DAY_LOG_SOURCE source=none" in out
    paper_log = tmp_path / "data" / "review" / "2026-06-24" / "paper_full.log"
    assert (tmp_path / "data" / "review" / "2026-06-24").is_dir()
    assert paper_log.is_file()
    assert paper_log.read_text(encoding="utf-8") == ""
    assert "[capture_metrics] exit_code=0" in out
    assert "[daily_summary] exit_code=0" in out
    assert "[research_feedback] exit_code=0" in out
    assert "[positions] exit_code=0" in out
    assert "[self_heal] exit_code=0" in out
    assert "[autoops_report] exit_code=0" in out
    assert "OPTIONS_PILOT_LOG_SUMMARY since_market_open" in out
    assert "ORDER_LOG_SUMMARY since_market_open" in out
    assert "- paper review log: present" in out
    assert "- recommendation: leave config unchanged" in out
    assert not any(call and call[0] == "journalctl" for call in runner.calls)


def test_end_day_review_paper_writes_full_log_and_passes_to_dynamic_reports(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ALGO_AUTOOPS_PLATFORM", "Darwin")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "default.yaml").write_text("options: {}\n", encoding="utf-8")
    source_log = tmp_path / "paper-source.log"
    source_log.write_text("DYNAMIC_FUNNEL symbol=AMD stage=entry\n", encoding="utf-8")
    runner = FakeRunner(
        {
            "self-heal": edr.CommandResult(0, "SELF_HEAL status=healthy env=paper\n"),
            "autoops report": edr.CommandResult(0, "AutoOps report\n- success %: 100.0\n"),
        }
    )
    args = edr.build_parser().parse_args(
        [
            "--paper",
            "--date",
            "2026-06-24",
            "--project-root",
            str(tmp_path),
            "--log-file",
            str(source_log),
        ]
    )

    assert edr.run_end_day_review(args, runner=runner) == 0

    paper_log = tmp_path / "data" / "review" / "2026-06-24" / "paper_full.log"
    assert paper_log.read_text(encoding="utf-8") == "DYNAMIC_FUNNEL symbol=AMD stage=entry\n"
    assert any(
        call
        == (
            str(tmp_path / "bin" / "algo"),
            "dynamic-funnel-report",
            "--date",
            "2026-06-24",
            "--user",
            "paper_bot",
            "--log-file",
            str(paper_log),
        )
        for call in runner.calls
    )
    assert any(
        call
        == (
            str(tmp_path / "bin" / "algo"),
            "dynamic-rvol-forward-returns",
            "--date",
            "2026-06-24",
            "--user",
            "paper_bot",
            "--log-path",
            str(paper_log),
        )
        for call in runner.calls
    )
