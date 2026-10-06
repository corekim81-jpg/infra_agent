"""MCP 서버 테스트 (MCP 클라이언트로 도구 호출, 가상 Prometheus)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.client.client import Client

from expr_prom import ExprProm
from infra_agent.catalog import load_catalog
from infra_agent.config import load_settings
from infra_agent.datasources.probe import SourceStatus
from infra_agent.mcp_server import create_mcp_server
from infra_agent.orchestration.runner import AnswerBundle, answer_question
from infra_agent.service import AskService

ROOT = Path(__file__).resolve().parents[2]
CATALOG = load_catalog(ROOT / "config/catalog/otel-demo.yaml")
NOW = datetime(2026, 9, 29, 3, 0, tzinfo=UTC)
ENV = {
    "INFRA_AGENT_PROFILE": "dev-tunnel",
    "INFRA_AGENT__DATASOURCES__PROMETHEUS__ENABLED": "true",
    "INFRA_AGENT__DATASOURCES__PROMETHEUS__URL": "http://prom.synthetic.test",
}


async def _answer(question: str, settings: Any, catalog: Any, **kwargs: Any) -> AnswerBundle:
    return await answer_question(
        question, settings, catalog, now=NOW, transport=ExprProm().transport(), **kwargs
    )


async def _check(settings: Any) -> list[SourceStatus]:
    return [
        SourceStatus(
            name="prometheus",
            enabled=True,
            url="http://user:secretpw@prom.synthetic.test",
            reachable=True,
            ready=True,
        )
    ]


def _service(extra: dict[str, str] | None = None, **kwargs: Any) -> AskService:
    settings = load_settings(environ={**ENV, **(extra or {})})
    return AskService(settings, CATALOG, answer=kwargs.get("answer", _answer), check=_check)


def _text(result: Any) -> str:
    return "".join(c.text for c in result.content if getattr(c, "type", "") == "text")


async def test_tools_are_listed_as_read_only() -> None:
    async with Client(create_mcp_server(_service())) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"ask_infra", "check_infra_sources"}
    for tool in tools.values():
        assert tool.annotations is not None and tool.annotations.read_only_hint is True
    assert tools["ask_infra"].input_schema["required"] == ["question"]


async def test_ask_returns_text_or_json() -> None:
    async with Client(create_mcp_server(_service())) as client:
        text = await client.call_tool(
            "ask_infra", {"question": "현재 서버 상태가 어때?", "namespace": "otel-demo"}
        )
        as_json = await client.call_tool(
            "ask_infra", {"question": "현재 서버 상태가 어때?", "format": "json"}
        )
    assert not text.is_error
    assert _text(text).startswith("질문: 현재 서버 상태가 어때?")
    assert "namespace=otel-demo" in _text(text)
    body = json.loads(_text(as_json))
    assert body["status"] in ("ok", "partial") and body["intent"] == "status"
    assert [e["agent"] for e in body["execution"]] == ["server"] and "text" not in body


async def test_invalid_input_and_limits_are_tool_errors() -> None:
    service = _service({"INFRA_AGENT__API__MAX_QUESTION_CHARS": "20"})
    async with Client(create_mcp_server(service)) as client:
        bad_range = await client.call_tool("ask_infra", {"question": "상태", "range": "soon"})
        bad_target = await client.call_tool(
            "ask_infra", {"question": "상태", "namespace": 'a"} or up{b="'}
        )
        too_long = await client.call_tool("ask_infra", {"question": "x" * 21})
    assert bad_range.is_error and "range" in _text(bad_range)
    assert bad_target.is_error and "대상 이름 형식" in _text(bad_target)
    assert too_long.is_error and "질문이 너무 깁니다" in _text(too_long)


async def test_internal_errors_are_not_exposed() -> None:
    async def boom(*args: Any, **kwargs: Any) -> AnswerBundle:
        raise RuntimeError("내부 주소 http://10.0.0.1:9090 token=abcdef123456")

    async with Client(create_mcp_server(_service(answer=boom))) as client:
        result = await client.call_tool("ask_infra", {"question": "현재 서버 상태가 어때?"})
    assert result.is_error and "error_id=" in _text(result)
    assert "10.0.0.1" not in _text(result) and "abcdef" not in _text(result)


async def test_check_tool_masks_credentials() -> None:
    async with Client(create_mcp_server(_service())) as client:
        result = await client.call_tool("check_infra_sources", {})
    body = json.loads(_text(result))
    assert body["ok"] is True and body["sources"][0]["name"] == "prometheus"
    assert "secretpw" not in _text(result)
