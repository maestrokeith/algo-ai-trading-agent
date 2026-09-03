from scripts.autonomous_research_report import generate_report


def test_cloud_report_stays_paper_only_and_keeps_history():
    previous = {
        "history": [
            {
                "generated_at": "2026-09-03T00:00:00+00:00",
                "leader": "EURUSD",
                "leader_score": 0.1,
                "average_score": 0.05,
                "total_trades": 3,
                "worst_drawdown": 0.01,
            }
        ]
    }
    report = generate_report(symbols=("EURUSD",), bars=3500, seed=11, previous=previous)

    assert report["paper_only"] is True
    assert report["live_execution"] is False
    assert report["mode"] == "paper_research"
    assert report["data_source"] == "deterministic_synthetic_research"
    assert report["ranking"][0]["symbol"] == "EURUSD"
    assert len(report["history"]) == 2
    assert any(agent["agent"] == "Safety Governor" for agent in report["agent_council"])


def test_cloud_report_history_is_bounded():
    previous = {
        "history": [
            {
                "generated_at": f"2026-09-02T{i:02d}:00:00+00:00",
                "leader": "XAUUSD",
                "leader_score": 0.01,
                "average_score": 0.01,
                "total_trades": 1,
                "worst_drawdown": 0.01,
            }
            for i in range(10)
        ]
    }
    report = generate_report(symbols=("XAUUSD",), bars=3500, seed=3, previous=previous, history_limit=5)
    assert len(report["history"]) == 5
