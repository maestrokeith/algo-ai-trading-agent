"""LLM provider adapters for agent reasoning."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from .base import DisabledLLMProvider, LLMProvider
from src.intelligence.schemas import AgentRequest, AgentResponse

log = logging.getLogger(__name__)


class LocalProvider(DisabledLLMProvider):
    """Placeholder deterministic local provider."""


class OpenAIProvider:
    """OpenAI adapter with strict JSON parsing and no secret logging."""

    def __init__(self, model: str = "gpt-5-mini") -> None:
        self.model = model
        self.api_key_present = bool(os.getenv("OPENAI_API_KEY"))

    def reason(self, request: AgentRequest) -> AgentResponse:
        if not self.api_key_present:
            return AgentResponse(ok=False, payload={}, error="missing_openai_api_key")
        try:
            raw = request.payload.get("mock_response")
            if raw is None:
                return AgentResponse(ok=False, payload={}, error="openai_runtime_not_configured")
            payload: Any = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as exc:
            log.warning("Malformed LLM output for agent=%s: %s", request.agent, type(exc).__name__)
            return AgentResponse(ok=False, payload={}, error="malformed_llm_output")
        if not isinstance(payload, dict):
            return AgentResponse(ok=False, payload={}, error="malformed_llm_output")
        return AgentResponse(ok=True, payload=payload)


def provider_from_config(config: dict[str, Any] | None) -> LLMProvider:
    cfg = ((config or {}).get("agents") or {}).get("llm") or {}
    if not bool(cfg.get("enabled", False)):
        return DisabledLLMProvider()
    provider = str(cfg.get("provider", "disabled")).strip().lower()
    if provider == "openai":
        return OpenAIProvider(model=str(cfg.get("model", "gpt-5-mini")))
    if provider == "local":
        return LocalProvider()
    return DisabledLLMProvider()
