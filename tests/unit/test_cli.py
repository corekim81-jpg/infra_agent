from __future__ import annotations

import json
from pathlib import Path

import pytest

from infra_agent.cli import EXIT_CONFIG_ERROR, EXIT_OK, main

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for key in list(os.environ):
        if key.startswith("INFRA_AGENT"):
            monkeypatch.delenv(key, raising=False)


def test_config_command_prints_settings(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config", "--config", str(EXAMPLE)]) == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["profile"] == "dev-tunnel"
    assert data["llm"]["data_policy"] == "none"


def test_config_command_reports_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("execution:\n  max_concurrency: 0\n", encoding="utf-8")
    assert main(["config", "--config", str(bad)]) == EXIT_CONFIG_ERROR
    assert "execution.max_concurrency" in capsys.readouterr().err


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    assert "infra-agent" in capsys.readouterr().out
