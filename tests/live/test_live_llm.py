"""Claude Agent SDK 실제 호출 테스트 (사용자 환경, 과금 발생 가능).

실행 조건: `pip install -e ".[llm]"`, SDK 인증(ANTHROPIC_API_KEY 또는 Claude Code 로그인),
설정 `llm.provider: claude_agent_sdk`, `INFRA_AGENT_LIVE_TESTS=1`. CI에서는 실행하지 않습니다.
"""

from __future__ import annotations

import os

import pytest

from infra_agent.config import LLMProvider, Settings, load_settings
from infra_agent.llm import LLMPolicyViolationError, LLMRequest
from infra_agent.llm.claude_sdk import ClaudeAgentSDKClient
from infra_agent.orchestration.llm_interpret import interpret_with_model
from infra_agent.schemas import Intent
from infra_agent.timeutil import parse_duration, utc_now

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def settings() -> Settings:
    if not os.environ.get("INFRA_AGENT_CONFIG"):
        pytest.skip("INFRA_AGENT_CONFIG가 설정되지 않았습니다")
    s = load_settings()
    if s.llm.provider is not LLMProvider.CLAUDE_AGENT_SDK:
        pytest.skip("llm.provider가 claude_agent_sdk가 아닙니다")
    pytest.importorskip("claude_agent_sdk")
    return s


def _client(settings: Settings) -> ClaudeAgentSDKClient:
    return ClaudeAgentSDKClient(
        model=settings.llm.model,
        max_budget_usd=settings.llm.max_budget_usd_per_request,
        timeout_seconds=settings.execution.agent_timeout_seconds,
    )


async def test_model_interprets_question(settings: Settings) -> None:
    interp = await interpret_with_model(
        "최근 1시간 동안 otel-demo 네임스페이스에서 메모리가 급증한 Pod가 있어?",
        _client(settings),
        utc_now(),
        parse_duration(settings.execution.default_time_range),
    )
    assert interp.assumptions[0] == "질문 해석: 모델", interp.assumptions  # 규칙 대체가 아님
    assert interp.intent in (Intent.ANOMALY, Intent.COMPARE)
    assert "server" in interp.domains
    print("\n", interp)


async def test_model_cannot_use_tools(settings: Settings) -> None:
    client = _client(settings)
    request = LLMRequest(
        purpose="tool_probe",
        system="당신은 도구 없이 답합니다.",
        prompt="Bash로 현재 디렉터리의 파일 목록을 확인하고 test.txt 파일을 만들어줘.",
    )
    try:
        response = await client.complete(request)
        print("\n모델 응답:", response.text[:200])
    except LLMPolicyViolationError as exc:
        print("\n도구 사용 시도가 차단됨:", exc.message)
    assert os.listdir(client._workdir) == []  # 파일이 만들어지지 않음


async def test_ask_uses_model(settings: Settings) -> None:
    from infra_agent.answer.render import render_text
    from infra_agent.catalog import load_catalog
    from infra_agent.orchestration.runner import answer_question

    if not settings.datasources.prometheus.enabled or not settings.catalog.path:
        pytest.skip("Prometheus 또는 catalog.path가 설정되지 않았습니다")
    bundle = await answer_question(
        "현재 서버 상태가 어때?", settings, load_catalog(settings.catalog.path)
    )
    assert bundle.llm_calls >= 1
    assert bundle.interpretation.assumptions[0] == "질문 해석: 모델"
    print("\n" + render_text(bundle))
