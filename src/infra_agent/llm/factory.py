"""설정에 따라 모델 클라이언트를 만듭니다."""

from __future__ import annotations

from infra_agent.config.settings import LLMProvider, Settings
from infra_agent.llm.base import LLMClient


def make_llm(settings: Settings) -> LLMClient | None:
    """`fake`는 실행 환경에서 "모델 없음"으로 취급합니다(가짜 모델은 테스트에서 직접 주입).

    SDK가 없거나 CLI를 실행할 수 없으면 `LLMUnavailableError`를 발생시키며,
    호출자는 규칙 기반 경로로 계속하고 그 사실을 답변에 표시해야 합니다.
    """
    if settings.llm.provider is LLMProvider.FAKE:
        return None
    from infra_agent.llm.claude_sdk import ClaudeAgentSDKClient

    return ClaudeAgentSDKClient(
        model=settings.llm.model,
        max_budget_usd=settings.llm.max_budget_usd_per_request,
        timeout_seconds=settings.execution.agent_timeout_seconds,
    )
