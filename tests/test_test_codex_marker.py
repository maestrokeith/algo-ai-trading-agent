from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_CODEX = PROJECT_ROOT / "TEST_CODEX.md"


def test_test_codex_contains_issue_133_marker() -> None:
    text = TEST_CODEX.read_text(encoding="utf-8")

    assert "Harmless marker line for issue #133." in text
