from src.agents.llm import OpenAIProvider
from src.intelligence.schemas import AgentRequest


def test_malformed_llm_output_is_failure(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    provider = OpenAIProvider()

    response = provider.reason(AgentRequest("critic", {"mock_response": "{not-json"}))

    assert not response.ok
    assert response.error == "malformed_llm_output"
