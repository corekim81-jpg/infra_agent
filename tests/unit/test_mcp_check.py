"""`mcp-check` 테스트: 실제 HTTP 앱(/mcp)에 공식 MCP 클라이언트로 접속 (가상 데이터)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest

from expr_prom import ExprProm
from infra_agent.api.app import create_app
from infra_agent.catalog import load_catalog
from infra_agent.cli import main
from infra_agent.config import load_settings
from infra_agent.datasources.probe import SourceStatus
from infra_agent.mcp_check import McpCheckResult, check_mcp, render
from infra_agent.orchestration.runner import AnswerBundle, answer_question

ROOT = Path(__file__).resolve().parents[2]
CATALOG = load_catalog(ROOT / "config/catalog/otel-demo.yaml")
NOW = datetime(2026, 9, 29, 3, 0, tzinfo=UTC)
TOKEN = "synthetic-api-token-0123456789"
URL = "http://infra-agent.infra-agent.svc.synthetic.test:8080/mcp"
ENV = {
    "INFRA_AGENT_PROFILE": "dev-tunnel",
    "INFRA_AGENT__DATASOURCES__PROMETHEUS__ENABLED": "true",
    "INFRA_AGENT__DATASOURCES__PROMETHEUS__URL": "http://prom.synthetic.test",
}


async def _answer(question: str, settings: Any, catalog: Any, **kwargs: Any) -> AnswerBundle:
    return await answer_question(
        question, settings, catalog, now=NOW, transport=ExprProm().transport(), **kwargs
    )


def _checker(ready: bool) -> Any:
    async def check(settings: Any) -> list[SourceStatus]:
        return [
            SourceStatus(
                name="prometheus",
                enabled=True,
                url="http://prom.synthetic.test",
                reachable=True,
                ready=ready,
            ),
            SourceStatus(name="loki", enabled=False),
        ]

    return check


async def _run(
    token: str | None,
    *,
    mcp_enabled: bool = True,
    ready: bool = True,
    question: str | None = None,
) -> McpCheckResult:
    extra = {"INFRA_AGENT__API__MCP_ENABLED": "true"} if mcp_enabled else {}
    app = create_app(
        load_settings(environ={**ENV, **extra}),
        CATALOG,
        environ={"INFRA_AGENT_API_TOKEN": TOKEN},
        answer=_answer,
        check=_checker(ready),
    )
    async with app.router.lifespan_context(app):
        client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app))
        return await check_mcp(URL, token, question=question, timeout=30, http_client=client)


async def test_check_succeeds_over_http_with_token() -> None:
    result = await _run(TOKEN, question="현재 서버 상태가 어때?")
    assert result.ok, render(result)
    assert result.connected
    assert result.tools == ["ask_infra", "check_infra_sources"]
    assert not result.missing_tools and not result.not_read_only
    assert result.sources_ok is True
    assert [(s.name, s.ok) for s in result.sources] == [("prometheus", True), ("loki", False)]
    assert result.answer and "현재 서버 상태가 어때?" in result.answer
    text = render(result)
    assert "결과: 정상" in text and TOKEN not in text


async def test_without_question_ask_tool_is_not_called() -> None:
    result = await _run(TOKEN)
    assert result.ok and result.answer is None


@pytest.mark.parametrize("token", ["wrong-token", None])
async def test_rejected_token_is_reported_as_unauthorized(token: str | None) -> None:
    result = await _run(token)
    assert not result.ok and not result.connected
    assert result.error_code == "unauthorized"
    assert result.hint and "토큰" in result.hint
    assert TOKEN not in render(result) and "wrong-token" not in render(result)


async def test_disabled_mcp_is_reported_as_not_found() -> None:
    result = await _run(TOKEN, mcp_enabled=False)
    assert not result.ok and result.error_code == "not_found"
    assert result.hint and "mcp_enabled" in result.hint


async def test_unavailable_sources_fail_the_check() -> None:
    result = await _run(TOKEN, ready=False)
    assert result.connected and result.error_code is None
    assert result.sources_ok is False and not result.ok
    assert "일부 사용 불가" in render(result)


async def test_connection_failure_is_reported() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("synthetic refused", request=request)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(refuse))
    result = await check_mcp(
        "http://user:secretpw@mcp.synthetic.test/mcp", TOKEN, timeout=10, http_client=client
    )
    assert not result.ok and result.error_code == "connect_error"
    assert result.hint and "네트워크 정책" in result.hint
    text = render(result)
    assert "secretpw" not in text and "secretpw" not in result.model_dump_json()


def test_cli_requires_token_from_env(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("INFRA_AGENT_API_TOKEN", raising=False)
    assert main(["mcp-check", "--url", URL]) == 2
    assert "INFRA_AGENT_API_TOKEN" in capsys.readouterr().err


def test_cli_rejects_non_http_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INFRA_AGENT_API_TOKEN", TOKEN)
    assert main(["mcp-check", "--url", "file:///etc/passwd"]) == 2
