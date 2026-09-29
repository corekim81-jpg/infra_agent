"""모델 계층과 Claude Agent SDK 어댑터 테스트 (가짜 SDK, 네트워크·인증 없음)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from infra_agent.llm import (
    BudgetedLLM,
    LLMBudgetExceededError,
    LLMError,
    LLMOutputError,
    LLMPolicyViolationError,
    LLMRequest,
    LLMUnavailableError,
)
from infra_agent.llm.claude_sdk import BLOCKED_TOOLS, ClaudeAgentSDKClient
from infra_agent.llm.fake import FakeLLM


# 가짜 SDK 메시지 (어댑터는 클래스 이름으로 판별합니다)
@dataclass
class TextBlock:
    text: str


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class AssistantMessage:
    content: list[Any]
    model: str = "fake-model"


@dataclass
class ResultMessage:
    is_error: bool = False
    result: str | None = None
    structured_output: dict[str, Any] | None = None
    total_cost_usd: float | None = 0.001
    subtype: str = "success"


class CLINotFoundError(Exception):
    pass


def _fake_sdk(messages: list[Any], seen: dict[str, Any], *, call_tool: str | None = None) -> Any:
    def options_factory(**kwargs: Any) -> dict[str, Any]:
        seen["options"] = kwargs
        return kwargs

    async def query(*, prompt: str, options: dict[str, Any]) -> AsyncIterator[Any]:
        seen["prompt"] = prompt
        if call_tool:
            seen["deny"] = await options["can_use_tool"](call_tool, {}, None)
        for m in messages:
            if isinstance(m, Exception):
                raise m
            yield m

    return SimpleNamespace(
        ClaudeAgentOptions=options_factory,
        query=query,
        PermissionResultDeny=lambda **kw: SimpleNamespace(behavior="deny", **kw),
        PermissionResultAllow=lambda **kw: SimpleNamespace(behavior="allow", **kw),
    )


REQ = LLMRequest(purpose="p", system="SYS", prompt="질문", schema={"type": "object"})


async def test_options_disable_tools_and_local_settings() -> None:
    seen: dict[str, Any] = {}
    sdk = _fake_sdk([ResultMessage(structured_output={"a": 1})], seen)
    client = ClaudeAgentSDKClient(model="m1", max_budget_usd=0.5, sdk=sdk)
    resp = await client.complete(REQ)
    opts = seen["options"]
    assert opts["tools"] == [] and opts["allowed_tools"] == []
    assert (
        set(BLOCKED_TOOLS) <= set(opts["disallowed_tools"]) and "Bash" in opts["disallowed_tools"]
    )
    assert opts["setting_sources"] == [] and opts["strict_mcp_config"] is True
    assert opts["mcp_servers"] == {}
    assert opts["system_prompt"] == "SYS" and opts["model"] == "m1"
    assert opts["max_budget_usd"] == 0.5 and opts["max_turns"] <= 3
    assert opts["output_format"] == {"type": "json_schema", "schema": {"type": "object"}}
    assert callable(opts["can_use_tool"]) and "infra-agent-llm-" in opts["cwd"]
    assert resp.data == {"a": 1} and resp.cost_usd == 0.001


async def test_tool_use_block_rejected() -> None:
    seen: dict[str, Any] = {}
    sdk = _fake_sdk([AssistantMessage([ToolUseBlock(id="1", name="Bash")]), ResultMessage()], seen)
    with pytest.raises(LLMPolicyViolationError, match="Bash"):
        await ClaudeAgentSDKClient(sdk=sdk).complete(REQ)


async def test_tool_permission_denied_and_reported() -> None:
    seen: dict[str, Any] = {}
    sdk = _fake_sdk([ResultMessage(structured_output={"a": 1})], seen, call_tool="Read")
    with pytest.raises(LLMPolicyViolationError, match="Read"):
        await ClaudeAgentSDKClient(sdk=sdk).complete(REQ)
    assert seen["deny"].behavior == "deny" and seen["deny"].interrupt is True


async def test_text_json_fallback_and_errors() -> None:
    seen: dict[str, Any] = {}
    sdk = _fake_sdk(
        [AssistantMessage([TextBlock('```json\n{"intent": "status"}\n```')]), ResultMessage()],
        seen,
    )
    resp = await ClaudeAgentSDKClient(sdk=sdk).complete(REQ)
    assert resp.data == {"intent": "status"} and resp.model == "fake-model"

    bad = _fake_sdk([AssistantMessage([TextBlock("모르겠습니다")]), ResultMessage()], {})
    with pytest.raises(LLMOutputError):
        await ClaudeAgentSDKClient(sdk=bad).complete(REQ)

    failed = _fake_sdk([ResultMessage(is_error=True, result="auth failed")], {})
    with pytest.raises(LLMError, match="auth failed"):
        await ClaudeAgentSDKClient(sdk=failed).complete(REQ)

    missing = _fake_sdk([CLINotFoundError("claude not found")], {})
    with pytest.raises(LLMUnavailableError):
        await ClaudeAgentSDKClient(sdk=missing).complete(REQ)


def test_real_sdk_accepts_options() -> None:
    """설치된 실제 SDK가 있으면 옵션 이름이 맞는지 확인합니다 (없으면 건너뜀)."""
    sdk = pytest.importorskip("claude_agent_sdk")
    client = ClaudeAgentSDKClient(sdk=sdk)
    opts = client.build_options(REQ)
    assert isinstance(opts, sdk.ClaudeAgentOptions)
    assert opts.tools == [] and opts.setting_sources == [] and opts.strict_mcp_config


async def test_budget() -> None:
    fake = FakeLLM({"p": {"ok": True}})
    budgeted = BudgetedLLM(fake, max_calls=1)
    assert (await budgeted.complete(REQ)).data == {"ok": True}
    with pytest.raises(LLMBudgetExceededError):
        await budgeted.complete(REQ)
    assert budgeted.calls == 1 and budgeted.purposes == ["p"]


def test_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    from infra_agent.config import load_settings
    from infra_agent.llm import make_llm

    assert make_llm(load_settings(environ={})) is None  # fake → 모델 없음
    import infra_agent.llm.claude_sdk as mod

    def no_sdk() -> Any:
        raise LLMUnavailableError("claude-agent-sdk가 설치되지 않았습니다")

    monkeypatch.setattr(mod, "load_sdk", no_sdk)
    settings = load_settings(
        environ={
            "INFRA_AGENT_PROFILE": "dev-tunnel",
            "INFRA_AGENT__LLM__PROVIDER": "claude_agent_sdk",
        }
    )
    with pytest.raises(LLMUnavailableError):
        make_llm(settings)


async def test_structured_output_tool_is_output_not_tool_use() -> None:
    """CLI는 구조화 출력을 StructuredOutput 도구 블록으로 전달합니다 (실제 도구 아님)."""
    seen: dict[str, Any] = {}
    sdk = _fake_sdk(
        [
            AssistantMessage([ToolUseBlock(id="1", name="StructuredOutput", input={"a": "b"})]),
            ResultMessage(),
        ],
        seen,
        call_tool="StructuredOutput",
    )
    client = ClaudeAgentSDKClient(sdk=sdk)
    resp = await client.complete(REQ)
    assert resp.data == {"a": "b"}
    assert seen["deny"].behavior == "allow"  # 권한 콜백도 이 이름만 허용
    assert client.tool_requests_denied == []
    # 다른 도구가 섞이면 여전히 거부
    mixed = _fake_sdk(
        [
            AssistantMessage(
                [
                    ToolUseBlock(id="1", name="StructuredOutput", input={"a": "b"}),
                    ToolUseBlock(id="2", name="Write", input={"file_path": "x"}),
                ]
            ),
            ResultMessage(),
        ],
        {},
    )
    with pytest.raises(LLMPolicyViolationError, match="Write"):
        await ClaudeAgentSDKClient(sdk=mixed).complete(REQ)
