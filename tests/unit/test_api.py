"""HTTP API 테스트 (가상 Prometheus, 실제 카탈로그)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from expr_prom import ExprProm
from infra_agent.api.app import ApiConfigError, create_app
from infra_agent.catalog import load_catalog
from infra_agent.cli import main
from infra_agent.config import load_settings
from infra_agent.datasources.probe import SourceStatus
from infra_agent.orchestration.runner import AnswerBundle, answer_question

ROOT = Path(__file__).resolve().parents[2]
CATALOG = load_catalog(ROOT / "config/catalog/otel-demo.yaml")
NOW = datetime(2026, 9, 29, 3, 0, tzinfo=UTC)
TOKEN = "synthetic-api-token-0123456789"
ENV = {
    "INFRA_AGENT_PROFILE": "dev-tunnel",
    "INFRA_AGENT__DATASOURCES__PROMETHEUS__ENABLED": "true",
    "INFRA_AGENT__DATASOURCES__PROMETHEUS__URL": "http://prom.synthetic.test",
}
AUTH = {"Authorization": f"Bearer {TOKEN}"}


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
            version="3.0.0-synthetic",
        ),
        SourceStatus(name="loki", enabled=False),
    ]


def _client(extra: dict[str, str] | None = None, **kwargs: Any) -> TestClient:
    settings = load_settings(environ={**ENV, **(extra or {})})
    app = create_app(
        settings,
        CATALOG,
        environ={"INFRA_AGENT_API_TOKEN": TOKEN},
        answer=kwargs.pop("answer", _answer),
        check=_check,
        **kwargs,
    )
    return TestClient(app)


def test_requires_token_to_start_and_to_ask() -> None:
    settings = load_settings(environ=ENV)
    with pytest.raises(ApiConfigError, match="INFRA_AGENT_API_TOKEN"):
        create_app(settings, CATALOG, environ={})
    with pytest.raises(ApiConfigError, match="Prometheus"):
        create_app(load_settings(environ={}), CATALOG, environ={"INFRA_AGENT_API_TOKEN": TOKEN})
    client = _client()
    body = {"question": "현재 서버 상태가 어때?"}
    assert client.post("/v1/ask", json=body).status_code == 401
    assert (
        client.post("/v1/ask", json=body, headers={"Authorization": "Bearer x"}).status_code == 401
    )
    assert client.post("/v1/ask", json=body, headers={"Authorization": TOKEN}).status_code == 401
    assert client.get("/v1/check").status_code == 401
    # 상태 확인은 인증 없이, 내부 정보 없이
    assert client.get("/healthz").json() == {"status": "ok"}
    assert set(client.get("/readyz").json()) == {"status", "version"}
    # 문서·스키마 엔드포인트는 열지 않음
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_anonymous_must_be_explicit() -> None:
    settings = load_settings(environ={**ENV, "INFRA_AGENT__API__ALLOW_ANONYMOUS": "true"})
    client = TestClient(create_app(settings, CATALOG, environ={}, answer=_answer, check=_check))
    assert client.post("/v1/ask", json={"question": "현재 서버 상태가 어때?"}).status_code == 200


def test_ask_returns_answer_and_text() -> None:
    client = _client()
    response = client.post(
        "/v1/ask",
        json={"question": "현재 서버 상태가 어때?", "namespace": "otel-demo", "range": "1h"},
        headers=AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in ("ok", "partial")
    assert body["intent"] == "status" and body["request_id"]
    assert [e["agent"] for e in body["execution"]] == ["server"]
    assert "summary" in body["answer"] and "scope_notes" in body["answer"]
    assert body["text"].startswith("질문: 현재 서버 상태가 어때?")
    assert "namespace=otel-demo" in body["text"]
    assert TOKEN not in response.text
    no_text = client.post(
        "/v1/ask", json={"question": "현재 서버 상태가 어때?", "include_text": False}, headers=AUTH
    )
    assert "text" not in no_text.json()


def test_request_validation() -> None:
    client = _client({"INFRA_AGENT__API__MAX_QUESTION_CHARS": "20"})

    def post(body: dict[str, Any]) -> int:
        return client.post("/v1/ask", json=body, headers=AUTH).status_code

    assert post({"question": ""}) == 422
    assert post({"question": "x" * 21}) == 413
    assert post({"question": "상태", "range": "soon"}) == 422
    assert post({"question": "상태", "namespace": 'a"} or up{b="'}) == 422
    assert post({"question": "상태", "unknown": 1}) == 422


def test_concurrency_limit_and_internal_errors() -> None:
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(question: str, settings: Any, catalog: Any, **kwargs: Any) -> AnswerBundle:
        started.set()
        await release.wait()
        return await _answer(question, settings, catalog, **kwargs)

    settings = load_settings(environ={**ENV, "INFRA_AGENT__API__MAX_CONCURRENT_REQUESTS": "1"})
    app = create_app(
        settings, CATALOG, environ={"INFRA_AGENT_API_TOKEN": TOKEN}, answer=slow, check=_check
    )

    async def run() -> tuple[int, int, str | None]:
        import httpx

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
            body = {"question": "현재 서버 상태가 어때?"}
            first = asyncio.create_task(client.post("/v1/ask", json=body, headers=AUTH))
            await started.wait()
            busy = await client.post("/v1/ask", json=body, headers=AUTH)
            release.set()
            done = await first
            return done.status_code, busy.status_code, busy.headers.get("Retry-After")

    assert asyncio.run(run()) == (200, 429, "5")

    async def boom(*args: Any, **kwargs: Any) -> AnswerBundle:
        raise RuntimeError("내부 주소 http://10.0.0.1:9090 token=abcdef123456")

    client = _client(answer=boom)
    response = client.post("/v1/ask", json={"question": "현재 서버 상태가 어때?"}, headers=AUTH)
    assert response.status_code == 500
    assert set(response.json()) == {"error", "error_id"}
    assert "10.0.0.1" not in response.text and "abcdef" not in response.text


def test_check_endpoint_masks_credentials() -> None:
    response = _client().get("/v1/check", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["profile"] == "dev-tunnel"
    assert "secretpw" not in response.text


def test_serve_command_reports_missing_token(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("INFRA_AGENT_API_TOKEN", raising=False)
    config = tmp_path / "config.yaml"
    config.write_text(
        "profile: dev-tunnel\n"
        "datasources:\n  prometheus:\n    enabled: true\n    url: http://prom.synthetic.test\n"
        f"catalog:\n  path: {(ROOT / 'config/catalog/otel-demo.yaml').as_posix()}\n",
        encoding="utf-8",
    )
    assert main(["serve", "--config", str(config)]) == 2
    assert "INFRA_AGENT_API_TOKEN" in capsys.readouterr().err


# --- MCP over HTTP (/mcp, #49)

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _rpc(method: str, params: dict[str, Any], id_: int = 1) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params}


def test_mcp_endpoint_requires_token_and_calls_tools() -> None:
    settings = load_settings(environ={**ENV, "INFRA_AGENT__API__MCP_ENABLED": "true"})
    app = create_app(
        settings, CATALOG, environ={"INFRA_AGENT_API_TOKEN": TOKEN}, answer=_answer, check=_check
    )
    # 클러스터 서비스 이름으로 접속해도(로컬 주소가 아닌 Host) 토큰이 있으면 동작해야 함
    with TestClient(app, base_url="http://infra-agent.infra-agent.svc:8080") as client:
        listing = _rpc("tools/list", {})
        assert client.post("/mcp", json=listing, headers=MCP_HEADERS).status_code == 401
        wrong = {**MCP_HEADERS, "Authorization": "Bearer wrong"}
        assert client.post("/mcp", json=listing, headers=wrong).status_code == 401
        headers = {**MCP_HEADERS, **AUTH}
        tools = client.post("/mcp", json=listing, headers=headers).json()["result"]["tools"]
        assert {t["name"] for t in tools} == {"ask_infra", "check_infra_sources"}
        call = _rpc(
            "tools/call",
            {"name": "ask_infra", "arguments": {"question": "현재 서버 상태가 어때?"}},
            2,
        )
        result = client.post("/mcp", json=call, headers=headers).json()["result"]
        assert result["content"][0]["text"].startswith("질문: 현재 서버 상태가 어때?")
        # 기존 엔드포인트는 그대로
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.post("/v1/ask", json={"question": "상태"}).status_code == 401


def test_mcp_endpoint_is_off_by_default() -> None:
    response = _client().post("/mcp", json=_rpc("tools/list", {}), headers={**MCP_HEADERS, **AUTH})
    assert response.status_code == 404
