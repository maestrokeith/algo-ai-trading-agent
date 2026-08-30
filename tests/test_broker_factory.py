from __future__ import annotations

import pytest

from src.brokers.base import BrokerUnavailable
from src.brokers.broker_factory import broker_provider, get_broker


def test_broker_provider_defaults_to_alpaca() -> None:
    assert broker_provider({}) == "alpaca"


def test_get_broker_rejects_unsupported_provider() -> None:
    with pytest.raises(BrokerUnavailable, match="Unsupported broker provider"):
        get_broker({"broker": {"provider": "legacy"}})
