from __future__ import annotations

import json

from src.brokers.redaction import redact, redact_text


def test_recursive_redaction_masks_account_and_tokens() -> None:
    raw = {
        "account_number": "fake-agentic-2887",
        "nested": [{"broker_account_number": "fake-agentic-2887", "access_token": "secret-token"}],
        "symbol": "AAPL",
    }
    clean = redact(raw)
    rendered = json.dumps(clean)
    assert "fake-agentic-2887" not in rendered
    assert "secret-token" not in rendered
    assert clean["account_number"] == "••••2887"
    assert clean["symbol"] == "AAPL"


def test_exception_text_redacts_known_account_number() -> None:
    clean = redact_text("failed for fake-agentic-2887", secrets=("fake-agentic-2887",))
    assert clean == "failed for ••••2887"
