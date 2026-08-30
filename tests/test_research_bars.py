from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from src.research_bars import (
    backfill_research_bars,
    backfill_forward_bars,
    build_research_bars_status,
    clear_bar_loader_cache,
    capture_runtime_forward_bars,
    discover_bar_paths,
    expected_bar_dirs,
    inspect_forward_bar_cache,
    inspect_bar_file,
    load_canonical_bars,
    render_forward_bars_backfill,
    render_research_bars_backfill,
    render_research_bars_status,
    write_forward_bars_backfill,
    write_research_bars_status,
    discover_forward_bar_symbols,
)
from src.signal_expectancy_report import build_signal_expectancy_report

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_research_bars_status_works_when_no_bar_dirs_exist(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    report = build_research_bars_status(
        data_dir=data_dir,
        day="2026-06-12",
        user_id="live_bot",
        symbols=["AAPL", "MSFT"],
    )

    assert report["expected_directories"] == [str(path) for path in expected_bar_dirs(data_dir)]
    assert report["existing_directories"] == []
    assert report["symbols_checked"] == ["AAPL", "MSFT"]
    assert report["symbols_with_bars"] == []
    assert report["symbols_missing_bars"] == ["AAPL", "MSFT"]
    assert "Symbols Missing Bars" in render_research_bars_status(report)


def test_canonical_diagnostic_commands_registered() -> None:
    text = (PROJECT_ROOT / "bin" / "algo").read_text(encoding="utf-8")
    for command in (
        "day-review",
        "duplicate-forensics",
        "strategy-readiness",
        "backfill-forward-bars",
        "research-bars-backfill",
        "research-bars-status",
        "signal-expectancy-report",
        "research-bars-consistency",
    ):
        assert f"{command})" in text
        assert command in text.split("help|--help|-h)", 1)[1]


def test_canonical_loader_sees_same_process_file_repair(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    path = data_dir / "historical_bars" / "SPY_2026-07-23_1Min.csv"
    path.parent.mkdir(parents=True)
    clear_bar_loader_cache()

    bars, meta = load_canonical_bars(data_dir, symbol="SPY", day="2026-07-23")
    assert bars is None
    assert meta["reason"] == "no_historical_source"

    path.write_text("timestamp,open,high,low,close,volume\nbad,1,2,1,1,10\n", encoding="utf-8")
    bars, meta = load_canonical_bars(data_dir, symbol="SPY", day="2026-07-23")
    assert bars is None
    assert meta["reason"] == "timestamp_parse_error"

    path.write_text(
        "\n".join(
            [
                "timestamp,open,high,low,close,volume",
                "2026-07-23T13:30:00+00:00,100,101,99,100.5,1000",
                "2026-07-23T13:31:00+00:00,101,102,100,101.5,1100",
            ]
        ),
        encoding="utf-8",
    )
    bars, meta = load_canonical_bars(data_dir, symbol="SPY", day="2026-07-23")
    assert bars is not None
    assert len(bars) == 2
    assert meta["reason"] is None


def test_discover_bar_paths_does_not_match_symbol_prefix_collision(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    bars_dir = data_dir / "historical_bars"
    bars_dir.mkdir(parents=True)
    (bars_dir / "MUZ_2026-07-28_1Min.csv").write_text("timestamp,open,high,low,close,volume\n", encoding="utf-8")
    exact = bars_dir / "MU_2026-07-28_1Min.csv"
    exact.write_text("timestamp,open,high,low,close,volume\n", encoding="utf-8")

    paths = discover_bar_paths(data_dir, "MU", "2026-07-28")

    assert paths == [exact]


def test_research_bars_status_detects_csv_json_and_latest_timestamp(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    bars_dir = data_dir / "historical_bars"
    bars_dir.mkdir(parents=True)
    (bars_dir / "AAPL_2026-06-12_1Min.csv").write_text(
        "\n".join(
            [
                "timestamp,open,high,low,close,volume",
                "2026-06-12T13:30:00+00:00,100,101,99,100.5,1000",
                "2026-06-12T19:59:00+00:00,102,103,101,102.5,2000",
            ]
        ),
        encoding="utf-8",
    )
    (data_dir / "bars").mkdir()
    (data_dir / "bars" / "MSFT_20260612.json").write_text(
        json.dumps(
            {
                "bars": [
                    {
                        "timestamp": "2026-06-12T14:00:00+00:00",
                        "open": 1,
                        "high": 2,
                        "low": 1,
                        "close": 1.5,
                        "volume": 10,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = build_research_bars_status(
        data_dir=data_dir,
        day="2026-06-12",
        user_id="live_bot",
        symbols=["AAPL", "MSFT", "QQQ"],
    )
    by_symbol = {row["symbol"]: row for row in report["symbols"]}

    assert report["symbols_with_bars"] == ["AAPL", "MSFT"]
    assert report["symbols_missing_bars"] == ["QQQ"]
    assert by_symbol["AAPL"]["formats"] == ["csv"]
    assert by_symbol["AAPL"]["latest_timestamp"] == "2026-06-12T15:59:00-04:00"
    assert by_symbol["MSFT"]["formats"] == ["json"]
    assert "AAPL latest=2026-06-12T15:59:00-04:00" in render_research_bars_status(report)


def test_research_bars_status_infers_symbols_and_cli_writes_artifacts(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    history = data_dir / "dynamic_scan_history"
    history.mkdir(parents=True)
    (history / "20260612T143000000000Z_default.json").write_text(
        json.dumps(
            {
                "user_id": "default",
                "generated_at": "2026-06-12T14:30:00+00:00",
                "candidates": [{"symbol": "AAPL"}, {"symbol": "MSFT"}],
            }
        ),
        encoding="utf-8",
    )

    json_path, text_path, report = write_research_bars_status(
        data_dir=data_dir,
        day="2026-06-12",
        user_id="paper_bot",
    )
    assert report["symbol_source"] == "inferred"
    assert report["symbols_checked"] == ["AAPL", "MSFT"]
    assert json_path.exists()
    assert text_path.exists()

    proc = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "research_bars_status.py"),
            "--date",
            "2026-06-12",
            "--user",
            "paper_bot",
            "--data-dir",
            str(data_dir),
        ],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "Research Bars Status - 2026-06-12 user=paper_bot" in proc.stdout
    assert "JSON:" in proc.stdout


def test_research_bars_backfill_writes_expected_csv_with_fake_broker(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"

    class FakeBroker:
        def get_bars(self, symbol: str, **kwargs):
            assert kwargs["timeframe"] == "1Min"
            assert kwargs["start"].isoformat() == "2026-06-12T08:00:00+00:00"
            assert kwargs["end"].isoformat() == "2026-06-13T00:00:00+00:00"
            if symbol == "MISS":
                return pd.DataFrame()
            return pd.DataFrame(
                {
                    "open": [100.0],
                    "high": [101.0],
                    "low": [99.0],
                    "close": [100.5],
                    "volume": [1000],
                },
                index=pd.DatetimeIndex([datetime.fromisoformat("2026-06-12T13:30:00+00:00")]),
            )

    report = backfill_research_bars(
        data_dir=data_dir,
        day="2026-06-12",
        user_id="paper_bot",
        symbols=["AAPL", "MISS"],
        broker_factory=FakeBroker,
    )

    assert report["summary"] == {"requested": 2, "written": 1, "missing": 1}
    path = data_dir / "historical_bars" / "AAPL_2026-06-12_1Min.csv"
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "timestamp,open,high,low,close,volume" in text
    assert "100.5" in text
    assert "MISS" in render_research_bars_backfill(report)


def _forward_frame(day: str = "2026-07-23", *, start_minute: int = 0, end_minute: int = 960) -> pd.DataFrame:
    start = pd.Timestamp(f"{day} 04:00:00", tz="America/New_York") + pd.Timedelta(minutes=start_minute)
    periods = max(1, end_minute - start_minute + 1)
    idx = pd.date_range(start=start, periods=periods, freq="min").tz_convert("UTC")
    prices = [100.0 + (i * 0.01) for i in range(periods)]
    return pd.DataFrame(
        {
            "open": prices,
            "high": [p + 0.05 for p in prices],
            "low": [p - 0.05 for p in prices],
            "close": prices,
            "volume": [1000] * periods,
        },
        index=idx,
    )


class ForwardFakeBroker:
    provider_name = "fake_alpaca"

    def __init__(self, frames: dict[str, pd.DataFrame] | None = None, failures: dict[str, list[Exception]] | None = None):
        self.frames = frames or {}
        self.failures = failures or {}
        self.calls: list[tuple[str, dict]] = []
        self.orders_submitted = 0

    def get_bars(self, symbol: str, **kwargs):
        self.calls.append((symbol, kwargs))
        failures = self.failures.get(symbol) or []
        if failures:
            raise failures.pop(0)
        return self.frames.get(symbol, pd.DataFrame())

    def submit_order(self, *_args, **_kwargs):  # pragma: no cover - defensive contract
        self.orders_submitted += 1
        raise AssertionError("backfill must not submit orders")


def test_forward_bars_backfill_fetches_persists_and_signal_expectancy_consumes(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    attr = data_dir / "trade_attribution" / "daily" / "2026-07-23_live_bot.json"
    attr.parent.mkdir(parents=True)
    attr.write_text(
        json.dumps(
            {
                "date": "2026-07-23",
                "user_id": "live_bot",
                "candidates": [{"timestamp": "2026-07-23T10:00:00-04:00", "symbol": "SPY", "route": "trend_long", "price": 103.6}],
                "allocator_candidates": [{"timestamp": "2026-07-23T10:01:00-04:00", "symbol": "QQQ", "route": "core_rebuild"}],
                "orders": [],
                "exits": [],
            }
        ),
        encoding="utf-8",
    )
    broker = ForwardFakeBroker({"SPY": _forward_frame(), "QQQ": _forward_frame()})

    report = backfill_forward_bars(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-07-23",
        user_id="live_bot",
        broker_factory=lambda: broker,
        retry_sleep_seconds=0,
    )

    assert report["provider_selected"] == "fake_alpaca"
    assert report["summary"]["successful"] == 2
    assert report["summary"]["failed"] == 0
    assert (data_dir / "historical_bars" / "SPY_2026-07-23_1Min.csv").exists()
    assert not list((data_dir / "historical_bars").glob("*.tmp"))
    signal = build_signal_expectancy_report(project_root=tmp_path, data_dir=data_dir, day="2026-07-23", user_id="live_bot", log_text="")
    assert signal["data_quality"]["signals_with_valid_forward_bars"] > 0
    assert signal["data_quality"]["lookup_failure_breakdown"] == {}
    assert signal["signals"][0]["return_5m_pct"] is not None
    assert signal["signals"][0]["max_favorable_excursion_pct"] is not None
    assert signal["signals"][0]["max_adverse_excursion_pct"] is not None


def test_forward_bar_symbol_discovery_prefers_live_signal_expectancy_scope(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    report_dir = data_dir / "research_metrics" / "2026-07-23"
    report_dir.mkdir(parents=True)
    (report_dir / "signal_expectancy_report.json").write_text(
        json.dumps(
            {
                "data_quality": {"symbols_missing_bars": ["SPY", "QQQ"]},
                "signals": [
                    {"symbol": "SPY", "route": "trend_long"},
                    {"symbol": "META", "route": "premarket_catalyst_replay"},
                ],
            }
        ),
        encoding="utf-8",
    )
    history = data_dir / "dynamic_scan_history"
    history.mkdir(parents=True)
    (history / "20260723_default.json").write_text(
        json.dumps({"date": "2026-07-23", "candidates": [{"symbol": "RAWX"}]}),
        encoding="utf-8",
    )

    discovery = discover_forward_bar_symbols(project_root=tmp_path, data_dir=data_dir, day="2026-07-23", user_id="live_bot")

    assert discovery["source"] == "signal_expectancy_report"
    assert discovery["symbols"] == ["QQQ", "SPY"]


def test_forward_bars_backfill_skips_existing_complete_cache(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    path = data_dir / "historical_bars" / "SPY_2026-07-23_1Min.csv"
    normalized = pd.DataFrame(
        {
            "timestamp": [ts.isoformat() for ts in _forward_frame().index],
            "open": [100.0] * len(_forward_frame()),
            "high": [101.0] * len(_forward_frame()),
            "low": [99.0] * len(_forward_frame()),
            "close": [100.5] * len(_forward_frame()),
            "volume": [1000] * len(_forward_frame()),
        }
    )
    path.parent.mkdir(parents=True)
    normalized.to_csv(path, index=False)
    broker = ForwardFakeBroker({"SPY": _forward_frame()})

    report = backfill_forward_bars(data_dir=data_dir, day="2026-07-23", user_id="live_bot", symbols=["SPY"], broker_factory=lambda: broker)

    assert report["summary"]["skipped"] == 1
    assert broker.calls == []
    assert inspect_forward_bar_cache(data_dir=data_dir, symbol="SPY", day="2026-07-23")["complete"] is True


def test_forward_bars_backfill_repairs_partial_file(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    partial = _forward_frame(end_minute=30)
    path = data_dir / "historical_bars" / "SPY_2026-07-23_1Min.csv"
    path.parent.mkdir(parents=True)
    pd.DataFrame({"timestamp": [ts.isoformat() for ts in partial.index], "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}).to_csv(path, index=False)
    broker = ForwardFakeBroker({"SPY": _forward_frame()})

    report = backfill_forward_bars(data_dir=data_dir, day="2026-07-23", user_id="live_bot", symbols=["SPY"], broker_factory=lambda: broker)

    assert report["summary"]["successful"] == 1
    assert broker.calls[0][0] == "SPY"
    assert inspect_forward_bar_cache(data_dir=data_dir, symbol="SPY", day="2026-07-23")["complete"] is True


def test_forward_bars_backfill_classifies_calendar_and_symbol_failures(tmp_path: Path) -> None:
    weekend = backfill_forward_bars(data_dir=tmp_path / "data", day="2026-07-25", user_id="live_bot", symbols=["SPY"], broker_factory=lambda: ForwardFakeBroker())
    holiday = backfill_forward_bars(data_dir=tmp_path / "data", day="2026-07-03", user_id="live_bot", symbols=["SPY"], broker_factory=lambda: ForwardFakeBroker())
    invalid = backfill_forward_bars(data_dir=tmp_path / "data", day="2026-07-23", user_id="live_bot", symbols=["BAD SYMBOL"], broker_factory=lambda: ForwardFakeBroker())

    assert weekend["symbols"][0]["reason"] == "weekend"
    assert holiday["symbols"][0]["reason"] == "market_holiday"
    assert invalid["summary"]["requested"] == 0
    assert invalid["summary"]["invalid_symbols"] == 1


def test_forward_bars_backfill_retries_rate_limit_and_classifies_provider_errors(tmp_path: Path) -> None:
    broker = ForwardFakeBroker(
        {"SPY": _forward_frame(), "QQQ": pd.DataFrame()},
        failures={"SPY": [RuntimeError("429 too many requests")], "AMD": [RuntimeError("401 unauthorized")]},
    )

    report = backfill_forward_bars(
        data_dir=tmp_path / "data",
        day="2026-07-23",
        user_id="live_bot",
        symbols=["SPY", "AMD", "QQQ"],
        broker_factory=lambda: broker,
        retry_sleep_seconds=0,
    )
    by_symbol = {row["symbol"]: row for row in report["symbols"]}

    assert by_symbol["SPY"]["status"] == "successful"
    assert len(by_symbol["SPY"]["fetch_attempts"]) == 2
    assert by_symbol["SPY"]["fetch_attempts"][0]["reason"] == "rate_limited"
    assert by_symbol["AMD"]["reason"] == "authorization_error"
    assert by_symbol["QQQ"]["reason"] == "empty_provider_response"


def test_forward_bars_backfill_classifies_feed_entitlement_before_authorization(tmp_path: Path) -> None:
    broker = ForwardFakeBroker(failures={"SPY": [RuntimeError("403 SIP subscription entitlement does not permit this query")]})

    report = backfill_forward_bars(
        data_dir=tmp_path / "data",
        day="2026-07-23",
        user_id="live_bot",
        symbols=["SPY"],
        broker_factory=lambda: broker,
        retry_sleep_seconds=0,
    )

    assert report["symbols"][0]["reason"] == "feed_entitlement_error"


def test_forward_bars_backfill_classifies_provider_construction_auth_failure(tmp_path: Path) -> None:
    def factory():
        raise ValueError("Alpaca LIVE credentials required")

    report = backfill_forward_bars(
        data_dir=tmp_path / "data",
        day="2026-07-23",
        user_id="live_bot",
        symbols=["SPY"],
        broker_factory=factory,
    )

    assert report["summary"]["failed"] == 1
    assert report["symbols"][0]["reason"] == "authorization_error"
    assert "Alpaca LIVE credentials required" in report["provider_error"]


def test_forward_bars_backfill_marks_incomplete_returned_bars_partial(tmp_path: Path) -> None:
    broker = ForwardFakeBroker({"SPY": _forward_frame(end_minute=60)})

    report = backfill_forward_bars(data_dir=tmp_path / "data", day="2026-07-23", user_id="live_bot", symbols=["SPY"], broker_factory=lambda: broker)

    assert report["summary"]["partial"] == 1
    assert report["symbols"][0]["reason"] == "incomplete_session"
    assert "status=partial" in render_forward_bars_backfill(report)


def test_forward_bars_backfill_normalizes_abbreviated_json_payload(tmp_path: Path) -> None:
    frame = _forward_frame()
    rows = [
        {"t": ts.isoformat(), "o": row.open, "h": row.high, "l": row.low, "c": row.close, "v": row.volume, "n": 10, "vw": row.close}
        for ts, row in frame.iterrows()
    ]
    broker = ForwardFakeBroker({"SPY": {"bars": {"SPY": rows}}})

    report = backfill_forward_bars(data_dir=tmp_path / "data", day="2026-07-23", user_id="live_bot", symbols=["SPY"], broker_factory=lambda: broker)

    assert report["summary"]["successful"] == 1
    saved = pd.read_csv(tmp_path / "data" / "historical_bars" / "SPY_2026-07-23_1Min.csv")
    assert {"timestamp", "open", "high", "low", "close", "volume", "trade_count", "vwap"}.issubset(saved.columns)


def test_forward_bars_backfill_normalizes_full_json_payload(tmp_path: Path) -> None:
    frame = _forward_frame()
    rows = [
        {
            "timestamp": ts.isoformat(),
            "open": row.open,
            "high": row.high,
            "low": row.low,
            "close": row.close,
            "volume": row.volume,
        }
        for ts, row in frame.iterrows()
    ]
    broker = ForwardFakeBroker({"SPY": {"bars": rows}})

    report = backfill_forward_bars(data_dir=tmp_path / "data", day="2026-07-23", user_id="live_bot", symbols=["SPY"], broker_factory=lambda: broker)

    assert report["summary"]["successful"] == 1
    saved = pd.read_csv(tmp_path / "data" / "historical_bars" / "SPY_2026-07-23_1Min.csv")
    assert list(saved.columns[:6]) == ["timestamp", "open", "high", "low", "close", "volume"]


def test_forward_bars_backfill_normalizes_multiindex_dataframe_payload(tmp_path: Path) -> None:
    base = _forward_frame()
    multi = base.copy()
    multi.index = pd.MultiIndex.from_arrays([["SPY"] * len(base), base.index], names=["symbol", "timestamp"])
    broker = ForwardFakeBroker({"SPY": multi})

    report = backfill_forward_bars(data_dir=tmp_path / "data", day="2026-07-23", user_id="live_bot", symbols=["SPY"], broker_factory=lambda: broker)

    assert report["summary"]["successful"] == 1
    assert report["symbols"][0]["provider_result"]["dataframe_index_type"] == "MultiIndex"


def test_forward_bars_backfill_extracts_timestamp_first_multiindex_payload(tmp_path: Path) -> None:
    base = _forward_frame()
    multi = base.copy()
    multi.index = pd.MultiIndex.from_arrays([base.index, ["SPY"] * len(base)], names=["timestamp", "symbol"])
    broker = ForwardFakeBroker({"SPY": multi})

    report = backfill_forward_bars(data_dir=tmp_path / "data", day="2026-07-23", user_id="live_bot", symbols=["SPY"], broker_factory=lambda: broker)

    assert report["summary"]["successful"] == 1
    saved = pd.read_csv(tmp_path / "data" / "historical_bars" / "SPY_2026-07-23_1Min.csv")
    assert list(saved.columns[:6]) == ["timestamp", "open", "high", "low", "close", "volume"]


def test_forward_bars_backfill_extracts_unnamed_multiindex_payload(tmp_path: Path) -> None:
    base = _forward_frame()
    multi = base.copy()
    multi.index = pd.MultiIndex.from_arrays([["SPY"] * len(base), base.index], names=[None, None])
    broker = ForwardFakeBroker({"SPY": multi})

    report = backfill_forward_bars(data_dir=tmp_path / "data", day="2026-07-23", user_id="live_bot", symbols=["SPY"], broker_factory=lambda: broker)

    assert report["summary"]["successful"] == 1
    assert report["symbols"][0]["provider_result"]["multiindex_symbol_level"] == 0
    assert report["symbols"][0]["provider_result"]["multiindex_timestamp_level"] == 1


def test_forward_bars_backfill_reports_symbol_missing_from_multi_symbol_response(tmp_path: Path) -> None:
    base = _forward_frame()
    multi = base.copy()
    multi.index = pd.MultiIndex.from_arrays([["QQQ"] * len(base), base.index], names=["symbol", "timestamp"])
    broker = ForwardFakeBroker({"SPY": multi})

    report = backfill_forward_bars(data_dir=tmp_path / "data", day="2026-07-23", user_id="live_bot", symbols=["SPY"], broker_factory=lambda: broker)

    assert report["summary"]["failed"] == 1
    assert report["symbols"][0]["reason"] == "symbol_missing_from_response"
    assert not (tmp_path / "data" / "historical_bars" / "SPY_2026-07-23_1Min.csv").exists()


def test_forward_bars_backfill_filters_symbol_column_payload(tmp_path: Path) -> None:
    base = _forward_frame()
    frame = base.reset_index(names="timestamp")
    frame["symbol"] = "SPY"
    other = frame.copy()
    other["symbol"] = "QQQ"
    payload = pd.concat([frame, other], ignore_index=True)
    broker = ForwardFakeBroker({"SPY": payload})

    report = backfill_forward_bars(data_dir=tmp_path / "data", day="2026-07-23", user_id="live_bot", symbols=["SPY"], broker_factory=lambda: broker)

    assert report["summary"]["successful"] == 1
    saved = pd.read_csv(tmp_path / "data" / "historical_bars" / "SPY_2026-07-23_1Min.csv")
    assert set(saved["symbol"]) == {"SPY"}


def test_forward_bars_backfill_normalizes_barset_and_object_list_payloads(tmp_path: Path) -> None:
    frame = _forward_frame()
    barset = SimpleNamespace(df=frame)
    objects = [
        SimpleNamespace(timestamp=ts.isoformat(), open=row.open, high=row.high, low=row.low, close=row.close, volume=row.volume)
        for ts, row in frame.iterrows()
    ]
    broker = ForwardFakeBroker({"SPY": barset, "QQQ": objects})

    report = backfill_forward_bars(data_dir=tmp_path / "data", day="2026-07-23", user_id="live_bot", symbols=["SPY", "QQQ"], broker_factory=lambda: broker)

    assert report["summary"]["successful"] == 2
    by_symbol = {row["symbol"]: row for row in report["symbols"]}
    assert by_symbol["SPY"]["provider_result"]["coerce_status"] == "barset_dataframe"
    assert by_symbol["QQQ"]["provider_result"]["coerce_status"] == "sequence_rows"


def test_forward_bars_backfill_distinguishes_empty_malformed_and_missing_columns(tmp_path: Path) -> None:
    broker = ForwardFakeBroker(
        {
            "SPY": pd.DataFrame(),
            "QQQ": {"unexpected": "payload"},
            "AMD": pd.DataFrame({"timestamp": ["2026-07-23T13:30:00+00:00"], "foo": [1]}),
        }
    )

    report = backfill_forward_bars(data_dir=tmp_path / "data", day="2026-07-23", user_id="live_bot", symbols=["SPY", "QQQ", "AMD"], broker_factory=lambda: broker)
    by_symbol = {row["symbol"]: row for row in report["symbols"]}

    assert by_symbol["SPY"]["reason"] == "empty_provider_response"
    assert by_symbol["QQQ"]["reason"] == "malformed_provider_response"
    assert by_symbol["AMD"]["reason"] == "missing_required_columns"
    assert by_symbol["AMD"]["validation"]["rows"] == 1


def test_inspect_bar_file_classifies_zero_header_wrong_symbol_wrong_date_and_bad_rows(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    root = data_dir / "historical_bars"
    root.mkdir(parents=True)
    zero = root / "SPY_2026-07-23_1Min.csv"
    zero.write_text("", encoding="utf-8")
    header = root / "QQQ_2026-07-23_1Min.csv"
    header.write_text("timestamp,open,high,low,close,volume\n", encoding="utf-8")
    wrong_symbol = root / "AMD_2026-07-23_1Min.csv"
    wrong_symbol.write_text("timestamp,open,high,low,close,volume,symbol\n2026-07-23T13:30:00+00:00,1,1,1,1,1,QQQ\n", encoding="utf-8")
    wrong_date = root / "JPM_2026-07-23_1Min.csv"
    wrong_date.write_text("timestamp,open,high,low,close,volume\n2026-07-24T13:30:00+00:00,1,1,1,1,1\n", encoding="utf-8")
    bad = root / "XLE_2026-07-23_1Min.csv"
    bad.write_text("timestamp,open,high,low,close,volume\n2026-07-23T13:30:00+00:00,notnum,1,1,1,1\n", encoding="utf-8")

    assert inspect_bar_file(zero, symbol="SPY", day="2026-07-23")["classification"] == "zero-byte"
    assert inspect_bar_file(header, symbol="QQQ", day="2026-07-23")["classification"] == "header-only"
    assert inspect_bar_file(wrong_symbol, symbol="AMD", day="2026-07-23")["reason"] == "wrong_symbol"
    assert inspect_bar_file(wrong_date, symbol="JPM", day="2026-07-23")["reason"] == "wrong_date"
    assert inspect_bar_file(bad, symbol="XLE", day="2026-07-23")["reason"] == "invalid_ohlcv"


def test_forward_bars_backfill_repairs_corrupted_cache_and_preserves_valid_cache(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    root = data_dir / "historical_bars"
    root.mkdir(parents=True)
    (root / "SPY_2026-07-23_1Min.csv").write_text("timestamp,open,high,low,close,volume\n", encoding="utf-8")
    valid = _forward_frame()
    pd.DataFrame(
        {
            "timestamp": [ts.isoformat() for ts in valid.index],
            "open": valid["open"].tolist(),
            "high": valid["high"].tolist(),
            "low": valid["low"].tolist(),
            "close": valid["close"].tolist(),
            "volume": valid["volume"].tolist(),
        }
    ).to_csv(root / "QQQ_2026-07-23_1Min.csv", index=False)
    broker = ForwardFakeBroker({"SPY": _forward_frame(), "QQQ": _forward_frame()})

    report = backfill_forward_bars(data_dir=data_dir, day="2026-07-23", user_id="live_bot", symbols=["SPY", "QQQ"], broker_factory=lambda: broker)

    assert report["summary"]["repaired_files"] == 1
    assert report["summary"]["skipped_valid_files"] == 1
    assert [call[0] for call in broker.calls] == ["SPY"]
    assert inspect_bar_file(root / "SPY_2026-07-23_1Min.csv", symbol="SPY", day="2026-07-23")["usable"] is True


def test_signal_expectancy_loader_accepts_valid_partial_backfill_csv(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    attr = data_dir / "trade_attribution" / "daily" / "2026-07-23_live_bot.json"
    attr.parent.mkdir(parents=True)
    attr.write_text(
        json.dumps(
            {
                "date": "2026-07-23",
                "user_id": "live_bot",
                "candidates": [{"timestamp": "2026-07-23T09:35:00-04:00", "symbol": "SPY", "route": "trend_long", "price": 100.0}],
                "allocator_candidates": [],
                "orders": [],
                "exits": [],
            }
        ),
        encoding="utf-8",
    )
    partial = _forward_frame(start_minute=330, end_minute=420)
    path = data_dir / "historical_bars" / "SPY_2026-07-23_1Min.csv"
    path.parent.mkdir(parents=True)
    pd.DataFrame(
        {
            "volume": partial["volume"].tolist(),
            "close": partial["close"].tolist(),
            "timestamp": [ts.isoformat() for ts in partial.index],
            "open": partial["open"].tolist(),
            "high": partial["high"].tolist(),
            "low": partial["low"].tolist(),
            "vwap": partial["close"].tolist(),
        }
    ).to_csv(path, index=False)

    report = build_signal_expectancy_report(project_root=tmp_path, data_dir=data_dir, day="2026-07-23", user_id="live_bot", log_text="")

    assert report["data_quality"]["signals_with_valid_forward_bars"] == 1
    assert report["data_quality"]["missing_bars"] == 0
    assert report["data_quality"]["persistence_status"] == {"loaded": 1}


def test_forward_bars_backfill_diagnostic_mode_reports_provider_schema(tmp_path: Path) -> None:
    broker = ForwardFakeBroker({"SPY": _forward_frame()})
    broker._feed_name = "IEX"

    report = backfill_forward_bars(
        data_dir=tmp_path / "data",
        day="2026-07-23",
        user_id="live_bot",
        symbols=["SPY"],
        broker_factory=lambda: broker,
        diagnostic=True,
    )
    rendered = render_forward_bars_backfill(report)

    assert report["diagnostic"] is True
    assert report["symbols"][0]["provider_result"]["sdk_method"] == "broker.get_bars"
    assert report["symbols"][0]["provider_result"]["selected_feed"] == "IEX"
    assert "provider_result: method=broker.get_bars feed=IEX" in rendered


def test_runtime_forward_bar_capture_entries_disabled_sidecar_does_not_trade(tmp_path: Path) -> None:
    broker = ForwardFakeBroker({"SPY": _forward_frame()})

    report = capture_runtime_forward_bars(
        broker=broker,
        data_dir=tmp_path / "data",
        user_id="live_bot",
        timestamp="2026-07-23T10:00:00-04:00",
        symbols=["SPY"],
        config={"trading_control": {"mode": "entries-disabled"}},
    )

    assert report["summary"]["successful"] == 1
    assert broker.orders_submitted == 0


def test_forward_bars_backfill_cli_writes_artifacts_with_existing_cache(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    path = data_dir / "historical_bars" / "SPY_2026-07-23_1Min.csv"
    path.parent.mkdir(parents=True)
    frame = _forward_frame()
    pd.DataFrame(
        {
            "timestamp": [ts.isoformat() for ts in frame.index],
            "open": frame["open"].tolist(),
            "high": frame["high"].tolist(),
            "low": frame["low"].tolist(),
            "close": frame["close"].tolist(),
            "volume": frame["volume"].tolist(),
        }
    ).to_csv(path, index=False)

    json_path, text_path, report = write_forward_bars_backfill(data_dir=data_dir, day="2026-07-23", user_id="live_bot", symbols=["SPY"], broker_factory=lambda: ForwardFakeBroker())

    assert report["summary"]["skipped"] == 1
    assert json.loads(json_path.read_text(encoding="utf-8"))["report"] == "forward_bars_backfill"
    assert "Forward Bars Backfill - 2026-07-23 user=live_bot" in text_path.read_text(encoding="utf-8")
