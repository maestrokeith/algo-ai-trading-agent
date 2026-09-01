"""Base protocols for Algo agents and LLM providers."""

from __future__ import annotations

from typing import Protocol

from src.intelligence.schemas import AgentRequest, AgentResponse


class LLMProvider(Protocol):
    """Structured reasoning provider. Trading must work without one."""

    def reason(self, request: AgentRequest) -> AgentResponse:
        ...


class DisabledLLMProvider:
    """Default provider used when no LLM is configured."""

    def reason(self, request: AgentRequest) -> AgentResponse:
        return AgentResponse(ok=False, payload={}, error="llm_disabled")
