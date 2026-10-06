"""infra_agent를 MCP 서버로 제공합니다 (선택 의존성 `[mcp]`).

다른 에이전트·MCP 클라이언트가 도구로 호출합니다. 도구는 두 개이며 모두 읽기 전용입니다.
- `ask_infra`: 인프라 질문 한 건을 분석해 근거가 있는 답변을 돌려줍니다 (CLI `ask`와 같은 흐름).
- `check_infra_sources`: 데이터 소스 연결 상태를 돌려줍니다 (CLI `check`와 같음).

실행 방식:
- stdio: `infra-agent mcp` — 같은 컴퓨터의 MCP 클라이언트가 프로세스로 실행합니다.
- HTTP: `infra-agent serve`에 `api.mcp_enabled: true` — `/mcp`로 제공하며 HTTP API와 같은 Bearer
  토큰이 필요합니다 (`api/app.py`).

입력 검증·동시 처리 제한·오류 처리는 HTTP API와 같은 `service.AskService`를 씁니다. 도구 결과는
마스킹된 값이며, 내부 오류 내용은 내보내지 않습니다.
"""

from __future__ import annotations

import json
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from infra_agent import __version__
from infra_agent.service import AskRequest, AskService, ServiceError

INSTRUCTIONS = (
    "인프라(서버 자원, Kubernetes, 서비스, DB, 네트워크)의 현재 상태와 이상 징후를 실제 관측 "
    "데이터로 분석하는 읽기 전용 도구입니다. 답변은 확인된 사실, 추정(원인 후보), 한계를 "
    "구분합니다. 한계와 '확인하지 못한 영역'에 적힌 내용은 정상이라는 뜻이 아닙니다."
)
READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


def _validation_message(exc: ValidationError) -> str:
    parts = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
    return "입력이 올바르지 않습니다 — " + "; ".join(parts)


def create_mcp_server(service: AskService) -> MCPServer:
    server = MCPServer(
        name="infra-agent", instructions=INSTRUCTIONS, version=__version__, log_level="WARNING"
    )

    @server.tool(
        name="ask_infra",
        title="인프라 상태 질문",
        description=(
            "인프라 운영 질문 한 건을 분석합니다(읽기 전용). 예: '현재 서버 상태가 어때?', "
            "'Kubernetes에서 재시작하거나 Pending 상태인 Pod를 확인해 줘', '서비스 응답이 느려진 "
            "이유가 네트워크인지 DB인지 분석해 줘'. range는 분석 구간(예: 30m, 1h), "
            "namespace·node·pod는 대상을 좁힐 때 씁니다. format=json이면 구조화된 결과를 "
            "JSON 문자열로 돌려줍니다."
        ),
        annotations=READ_ONLY,
        structured_output=False,
    )
    async def ask_infra(
        question: str,
        range: str | None = None,
        namespace: str | None = None,
        node: str | None = None,
        pod: str | None = None,
        use_llm: bool = True,
        format: Literal["text", "json"] = "text",
    ) -> str:
        try:
            req = AskRequest(
                question=question,
                range=range,
                namespace=namespace,
                node=node,
                pod=pod,
                use_llm=use_llm,
            )
        except ValidationError as exc:
            raise ToolError(_validation_message(exc)) from None
        try:
            bundle = await service.ask(req)
        except ServiceError as exc:
            # 호출자에게 보여도 되는 문장만 담긴 오류입니다 (내부 오류는 error_id만).
            # ToolError가 아닌 예외는 SDK가 내용을 숨기고 일반 오류 문구만 돌려줍니다.
            raise ToolError(exc.message) from None
        if format == "json":
            return json.dumps(service.payload(bundle, include_text=False), ensure_ascii=False)
        return service.text(bundle)

    @server.tool(
        name="check_infra_sources",
        title="데이터 소스 연결 점검",
        description=(
            "분석에 쓰는 데이터 소스(Prometheus, Loki, Tempo, Kubernetes API)의 연결 상태를 "
            "JSON 문자열로 돌려줍니다(읽기 전용). ask_infra가 조회 실패를 알릴 때 원인을 "
            "확인하는 데 씁니다."
        ),
        annotations=READ_ONLY,
        structured_output=False,
    )
    async def check_infra_sources() -> str:
        return json.dumps(await service.check(), ensure_ascii=False)

    return server
