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


def test_check_without_enabled_sources(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check"]) == 1
    captured = capsys.readouterr()
    assert "비활성" in captured.out
    assert "활성화된 데이터 소스가 없습니다" in captured.err


def test_check_reports_statuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from infra_agent.datasources.probe import SourceStatus

    async def fake_check(settings: object) -> list[SourceStatus]:
        return [
            SourceStatus(
                name="prometheus",
                enabled=True,
                url="http://127.0.0.1:19090",
                reachable=True,
                ready=True,
                version="3",
                latency_ms=5,
            ),
            SourceStatus(
                name="loki",
                enabled=True,
                url="http://127.0.0.1:13100",
                error_code="connect_error",
                error="연결 실패",
                hint="SSH 터널 확인",
            ),
            SourceStatus(name="tempo", enabled=False),
        ]

    monkeypatch.setattr("infra_agent.cli.check_sources", fake_check)
    assert main(["check", "--config", str(EXAMPLE)]) == 1
    out = capsys.readouterr().out
    assert "prometheus: 정상" in out
    assert "loki: 연결 실패" in out and "SSH 터널 확인" in out
    assert "tempo: 비활성" in out


def test_discover_writes_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from datetime import UTC, datetime

    from infra_agent.datasources.probe import SourceStatus
    from infra_agent.discovery.report import DiscoveryReport, PrometheusDiscovery

    seen = {}

    async def fake_discover(settings: object, options: object) -> DiscoveryReport:
        seen["options"] = options
        return DiscoveryReport(
            generated_at=datetime(2026, 9, 29, 1, 2, 3, tzinfo=UTC),
            tool_version="t",
            profile="dev-tunnel",
            lookback_seconds=1800,
            mask_ips=True,
            include_values=False,
            prometheus=PrometheusDiscovery(
                status=SourceStatus(name="prometheus", enabled=True, reachable=True, ready=True)
            ),
        )

    monkeypatch.setattr("infra_agent.cli.discover", fake_discover)
    out_dir = tmp_path / "disc"
    code = main(
        [
            "discover",
            "--config",
            str(EXAMPLE),
            "--output-dir",
            str(out_dir),
            "--lookback",
            "30m",
            "--no-values",
            "--include-prefix",
            "app",
        ]
    )
    assert code == 0
    assert (out_dir / "discovery-20260929T010203Z.md").is_file()
    assert (out_dir / "discovery-20260929T010203Z.json").is_file()
    opts = seen["options"]
    assert opts.include_values is False  # type: ignore[attr-defined]
    assert opts.extra_prefixes == frozenset({"app"})  # type: ignore[attr-defined]
    assert "커밋하지 마세요" in capsys.readouterr().out


def test_discover_rejects_bad_lookback(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["discover", "--config", str(EXAMPLE), "--lookback", "soon"]) == 2
