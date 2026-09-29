"""Claude Agent SDK 어댑터 (architecture.md 10.1절).

SDK는 코딩 에이전트용 내장 도구(파일·셸 등)와 로컬 설정 로딩 기능을 갖고 있으므로
다음을 **항상** 적용합니다.

1. 내장 도구 비활성화: `tools=[]` (CLI `--tools ""`), 알려진 도구 이름은 `disallowed_tools`에도 명시
2. 권한 콜백 `can_use_tool`이 모든 도구 사용 요청을 거부
3. 응답에 도구 사용 블록이 있으면 결과를 버리고 `LLMPolicyViolationError`
   (예외: 구조화 출력 전달용 내부 이름 `StructuredOutput`만 출력으로 받아들임)
4. 로컬 설정·프로젝트 파일·외부 MCP 미사용: `setting_sources=[]`, `strict_mcp_config=True`,
   `mcp_servers={}`, 빈 임시 디렉터리를 작업 디렉터리로 사용
5. 호출 비용 상한 `max_budget_usd`, 턴 수 제한

인증(`ANTHROPIC_API_KEY` 또는 Claude Code 로그인)은 SDK/CLI가 직접 처리하며
이 코드는 인증정보를 읽지 않습니다.
SDK는 선택 의존성(`pip install -e ".[llm]"`)이므로 실행 시점에 가져옵니다.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import tempfile
from collections.abc import AsyncIterator, Callable
from typing import Any

from infra_agent.llm.base import (
    LLMError,
    LLMOutputError,
    LLMPolicyViolationError,
    LLMRequest,
    LLMResponse,
    LLMUnavailableError,
)

BLOCKED_TOOLS: tuple[str, ...] = (
    "Bash",
    "BashOutput",
    "KillShell",
    "Read",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "Task",
    "TodoWrite",
    "Skill",
    "SlashCommand",
    "ExitPlanMode",
)

_TOOL_BLOCKS = ("ToolUseBlock", "ServerToolUseBlock")
STRUCTURED_OUTPUT_TOOL = "StructuredOutput"
"""구조화 출력(`--json-schema`)을 CLI가 전달하는 내부 도구 이름. 외부 작용이 없는 출력 형식이므로
이 이름만 허용하고 입력값을 구조화 출력으로 사용합니다(개발 서버 검증 중 확인, 2026-09-29)."""
MAX_TURNS = 3
"""구조화 출력 생성에 필요한 최소 턴. 도구가 없으므로 도구 루프는 발생하지 않습니다."""


def load_sdk() -> Any:
    try:
        return importlib.import_module("claude_agent_sdk")
    except ImportError as exc:
        raise LLMUnavailableError(
            'claude-agent-sdk가 설치되지 않았습니다. pip install -e ".[llm]"로 설치하세요.'
        ) from exc


class ClaudeAgentSDKClient:
    name = "claude_agent_sdk"

    def __init__(
        self,
        *,
        model: str | None = None,
        max_budget_usd: float | None = None,
        timeout_seconds: float = 60.0,
        sdk: Any | None = None,
        query_fn: Callable[..., AsyncIterator[Any]] | None = None,
    ) -> None:
        self._sdk = sdk if sdk is not None else load_sdk()
        self._query = query_fn if query_fn is not None else self._sdk.query
        self._model = model
        self._max_budget = max_budget_usd
        self._timeout = timeout_seconds
        self._workdir = tempfile.mkdtemp(prefix="infra-agent-llm-")
        self.tool_requests_denied: list[str] = []

    async def _deny_all_tools(self, tool_name: str, tool_input: Any, context: Any) -> Any:
        if tool_name == STRUCTURED_OUTPUT_TOOL:
            return self._sdk.PermissionResultAllow()
        self.tool_requests_denied.append(tool_name)
        return self._sdk.PermissionResultDeny(
            message="이 에이전트는 도구를 사용할 수 없습니다.", interrupt=True
        )

    def build_options(self, request: LLMRequest) -> Any:
        kwargs: dict[str, Any] = {
            "system_prompt": request.system,
            "tools": [],
            "allowed_tools": [],
            "disallowed_tools": list(BLOCKED_TOOLS),
            "mcp_servers": {},
            "strict_mcp_config": True,
            "setting_sources": [],
            "can_use_tool": self._deny_all_tools,
            "max_turns": MAX_TURNS,
            "cwd": self._workdir,
        }
        if self._model:
            kwargs["model"] = self._model
        if self._max_budget is not None:
            kwargs["max_budget_usd"] = self._max_budget
        if request.schema is not None:
            kwargs["output_format"] = {"type": "json_schema", "schema": request.schema}
        return self._sdk.ClaudeAgentOptions(**kwargs)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        try:
            return await asyncio.wait_for(self._run(request), timeout=self._timeout)
        except TimeoutError as exc:
            raise LLMError(f"모델 호출 제한 시간({self._timeout:.0f}초) 초과") from exc

    async def _run(self, request: LLMRequest) -> LLMResponse:
        options = self.build_options(request)
        texts: list[str] = []
        data: dict[str, Any] | None = None
        cost: float | None = None
        model: str | None = None
        try:
            async for message in self._query(prompt=request.prompt, options=options):
                kind = type(message).__name__
                if kind == "AssistantMessage":
                    model = getattr(message, "model", model)
                    for block in getattr(message, "content", []) or []:
                        block_kind = type(block).__name__
                        if block_kind in _TOOL_BLOCKS:
                            name = getattr(block, "name", "?")
                            block_input = getattr(block, "input", None)
                            if name == STRUCTURED_OUTPUT_TOOL and isinstance(block_input, dict):
                                data = block_input
                                continue
                            raise LLMPolicyViolationError(
                                f"모델이 도구({name}) 사용을 시도해 응답을 버렸습니다"
                            )
                        if block_kind == "TextBlock":
                            texts.append(str(getattr(block, "text", "")))
                elif kind == "ResultMessage":
                    if getattr(message, "is_error", False):
                        detail = getattr(message, "result", None) or getattr(
                            message, "subtype", "error"
                        )
                        raise LLMError(f"모델 호출 실패: {detail}")
                    structured = getattr(message, "structured_output", None)
                    if isinstance(structured, dict):
                        data = structured
                    cost = getattr(message, "total_cost_usd", None)
                    result_text = getattr(message, "result", None)
                    if result_text and not texts:
                        texts.append(str(result_text))
        except LLMError:
            raise
        except Exception as exc:
            kind = type(exc).__name__
            if kind in ("CLINotFoundError", "CLIConnectionError"):
                raise LLMUnavailableError(f"Claude Code CLI를 실행할 수 없습니다: {exc}") from exc
            raise LLMError(f"모델 호출 오류({kind}): {exc}") from exc
        if self.tool_requests_denied:
            raise LLMPolicyViolationError(
                "모델이 도구 사용을 요청해 거부했습니다: " + ", ".join(self.tool_requests_denied)
            )
        text = "\n".join(t for t in texts if t).strip()
        if request.schema is not None and data is None:
            data = _parse_json(text)
        return LLMResponse(text=text, data=data, cost_usd=cost, model=model)


def _parse_json(text: str) -> dict[str, Any]:
    """구조화 출력이 없을 때 본문에서 JSON 객체를 추출합니다."""
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        candidate = candidate.split("\n", 1)[1] if "\n" in candidate else candidate
    start, end = candidate.find("{"), candidate.rfind("}")
    if start < 0 or end <= start:
        raise LLMOutputError("모델 응답에서 JSON을 찾지 못했습니다")
    try:
        value = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMOutputError("모델 응답 JSON을 해석하지 못했습니다") from exc
    if not isinstance(value, dict):
        raise LLMOutputError("모델 응답 JSON이 객체가 아닙니다")
    return value
