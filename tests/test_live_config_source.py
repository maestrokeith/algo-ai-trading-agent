from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_live_bot_uses_default_yaml_only() -> None:
    live_files = [
        PROJECT_ROOT / "src" / "app" / "live_cycle.py",
        PROJECT_ROOT / "src" / "app" / "live_loop.py",
        PROJECT_ROOT / "scripts" / "run_alpaca_loop.py",
    ]
    combined = "\n".join(path.read_text() for path in live_files)

    assert "CONFIG_SOURCE=config/default.yaml" in combined
    assert "config\" / \"default.yaml" in combined
    assert "live.yaml" not in combined
