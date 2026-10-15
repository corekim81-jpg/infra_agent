"""배포된 infra_agent MCP 엔드포인트(`/mcp`)를 공식 MCP 클라이언트로 점검합니다.

선택 의존성 `[mcp]`가 필요합니다.

`infra-agent mcp-check --url <주소>`로 실행합니다. 다른 네임스페이스의 Pod 등 실제로 호출할
위치에서 실행해, 네트워크 경로·토큰 인증·MCP 연결·도구 실행이 되는지 한 번에 확인합니다.

- 호출하는 도구는 읽기 전용뿐입니다: 도구 목록, `check_infra_sources`, 그리고 `--ask`를 준 경우에만
  `ask_infra`(모델 호출 없이).
- 토큰은 호출하는 쪽이 넘겨줍니다(CLI는 환경 변수에서만 읽음). 결과·오류 문장에 토큰을 넣지 않고,
  주소는 마스킹해서 보여 줍니다.
- 실패하면 어느 단계에서 막혔는지(`error_code`)를 구분합니다: 연결 실패, 인증 거부, 경로 없음
  (`api.mcp_enabled`가 꺼짐), MCP 오류, 도구 오류.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import httpx2
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, Field

from infra_agent.security import redact

REQUIRED_TOOLS = ("ask_infra", "check_infra_sources")

_HINTS = {
    "connect_error": (
        "주소에 연결하지 못했습니다. Service 이름·포트, 그리고 호출하는 쪽과 infra-agent "
        "네임스페이스의 네트워크 정책을 확인하세요."
    ),
    "timeout": (
        "응답 시간이 초과됐습니다. 네트워크 정책에서 패킷이 버려지는 경우에도 이렇게 보입니다."
    ),
    "unauthorized": "토큰이 없거나 맞지 않습니다(infra-agent-api Secret의 값과 비교하세요).",
    "not_found": "/mcp 경로가 없습니다. 서버 설정 api.mcp_enabled가 true인지 확인하세요.",
}


class SourceState(BaseModel):
    name: str
    enabled: bool
    ok: bool


class McpCheckResult(BaseModel):
    """점검 결과. `ok`는 MCP 경로와 도구 실행, 데이터 소스 상태가 모두 정상일 때만 참입니다."""

    url: str
    ok: bool = False
    connected: bool = False
    tools: list[str] = Field(default_factory=list)
    missing_tools: list[str] = Field(default_factory=list)
    not_read_only: list[str] = Field(default_factory=list)
    sources: list[SourceState] = Field(default_factory=list)
    sources_ok: bool | None = None
    answer: str | None = None
    error_code: str | None = None
    error: str | None = None
    hint: str | None = None


def _leaves(exc: BaseException) -> Iterator[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            yield from _leaves(inner)
    else:
        yield exc


def _classify(exc: BaseException, statuses: list[int]) -> tuple[str, str]:
    """예외와 마지막 HTTP 상태 코드로 막힌 단계를 정합니다. 예외 문장은 마스킹합니다."""
    failed = [s for s in statuses if s >= 400]
    if failed:
        status = failed[-1]
        if status in (401, 403):
            return "unauthorized", f"HTTP {status}"
        if status == 404:
            return "not_found", "HTTP 404"
        return "http_error", f"HTTP {status}"
    for leaf in _leaves(exc):
        if isinstance(leaf, (TimeoutError, httpx2.TimeoutException)):
            return "timeout", type(leaf).__name__
        if isinstance(leaf, httpx2.TransportError):
            return "connect_error", redact(f"{type(leaf).__name__}: {leaf}")
        if isinstance(leaf, MCPError):
            return "protocol_error", redact(f"MCP 오류: {leaf}")
    first = next(_leaves(exc))
    return "error", redact(f"{type(first).__name__}: {first}")


def _text(result: Any) -> str:
    return "".join(getattr(part, "text", "") for part in result.content)


def _sources(text: str) -> tuple[bool, list[SourceState]]:
    report = json.loads(text)
    states = [
        SourceState(
            name=str(s.get("name")),
            enabled=bool(s.get("enabled")),
            ok=bool(s.get("reachable") and s.get("ready")),
        )
        for s in report.get("sources", [])
    ]
    return bool(report.get("ok")), states


async def _run(
    result: McpCheckResult, url: str, client: httpx2.AsyncClient, question: str | None
) -> None:
    async with Client(streamable_http_client(url, http_client=client)) as mcp:
        result.connected = True
        listing = await mcp.list_tools()
        result.tools = sorted(t.name for t in listing.tools)
        result.missing_tools = [n for n in REQUIRED_TOOLS if n not in result.tools]
        result.not_read_only = sorted(
            t.name
            for t in listing.tools
            if not (t.annotations is not None and t.annotations.read_only_hint)
        )
        if "check_infra_sources" in result.tools:
            checked = await mcp.call_tool("check_infra_sources", {})
            if checked.is_error:
                result.error_code = "tool_error"
                result.error = redact(f"check_infra_sources: {_text(checked)}")
                return
            try:
                result.sources_ok, result.sources = _sources(_text(checked))
            except (ValueError, AttributeError):
                result.error_code = "tool_error"
                result.error = "check_infra_sources 결과를 읽지 못함"
                return
        if question and "ask_infra" in result.tools:
            answered = await mcp.call_tool(
                "ask_infra", {"question": question, "use_llm": False, "format": "text"}
            )
            if answered.is_error:
                result.error_code = "tool_error"
                result.error = redact(f"ask_infra: {_text(answered)}")
                return
            result.answer = _text(answered)


async def check_mcp(
    url: str,
    token: str | None,
    *,
    question: str | None = None,
    timeout: float = 120.0,
    http_client: httpx2.AsyncClient | None = None,
) -> McpCheckResult:
    """`url`의 MCP 엔드포인트를 점검합니다.

    `http_client`는 테스트에서 전송 계층을 바꿀 때 씁니다.
    """
    result = McpCheckResult(url=redact(url))
    statuses: list[int] = []

    async def record(response: httpx2.Response) -> None:
        statuses.append(response.status_code)

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    client = http_client or httpx2.AsyncClient(
        timeout=httpx2.Timeout(30.0, read=timeout), follow_redirects=False
    )
    client.headers.update(headers)
    client.event_hooks.setdefault("response", []).append(record)
    try:
        async with client:
            await asyncio.wait_for(_run(result, url, client, question), timeout=timeout)
    except BaseException as exc:  # 어느 단계에서 막혔는지 구분해 보고
        if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
            raise
        result.error_code, result.error = _classify(exc, statuses)
    result.hint = _HINTS.get(result.error_code or "")
    result.ok = (
        result.error_code is None
        and not result.missing_tools
        and not result.not_read_only
        and result.sources_ok is True
        and (question is None or result.answer is not None)
    )
    return result


def render(result: McpCheckResult) -> str:
    """사람이 읽는 점검 결과."""
    lines = [f"MCP 엔드포인트: {result.url}"]
    lines.append(f"- 연결·MCP 초기화: {'성공' if result.connected else '실패'}")
    if result.tools:
        lines.append(f"- 도구: {', '.join(result.tools)}")
    if result.missing_tools:
        lines.append(f"- 없는 도구: {', '.join(result.missing_tools)}")
    if result.not_read_only:
        lines.append(f"- 읽기 전용 표시가 없는 도구: {', '.join(result.not_read_only)}")
    if result.sources_ok is not None:
        states = ", ".join(
            f"{s.name} {'정상' if s.ok else ('비활성' if not s.enabled else '사용 불가')}"
            for s in result.sources
        )
        lines.append(
            f"- check_infra_sources: {'정상' if result.sources_ok else '일부 사용 불가'} ({states})"
        )
    if result.answer is not None:
        lines.append("- ask_infra: 성공 (답변은 아래)")
    if result.error_code:
        lines.append(f"- 오류 [{result.error_code}]: {result.error}")
    if result.hint:
        lines.append(f"  → {result.hint}")
    lines.append(f"결과: {'정상' if result.ok else '실패'}")
    if result.answer is not None:
        lines += ["", result.answer]
    return "\n".join(lines)
