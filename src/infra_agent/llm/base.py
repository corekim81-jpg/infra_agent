"""모델 계층 공통 인터페이스.

- 에이전트·조정 코드는 이 인터페이스만 사용하고 특정 제공자 SDK에 의존하지 않습니다.
- 모델은 도구를 사용할 수 없습니다(조회는 코드의 도구 계층만 수행). 모델은 해석만 합니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from infra_agent.security import redact


class LLMError(Exception):
    code = "llm_error"

    def __init__(self, message: str) -> None:
        self.message = redact(message)
        super().__init__(self.message)


class LLMUnavailableError(LLMError):
    """SDK 미설치, CLI 없음, 인증 실패 등으로 모델을 사용할 수 없음."""

    code = "llm_unavailable"


class LLMBudgetExceededError(LLMError):
    code = "llm_budget_exceeded"


class LLMPolicyViolationError(LLMError):
    """모델이 도구를 사용하려 하는 등 허용되지 않은 동작을 시도함."""

    code = "llm_policy_violation"


class LLMOutputError(LLMError):
    """구조화 출력이 없거나 스키마 검증에 실패함."""

    code = "llm_output_error"


@dataclass(frozen=True)
class LLMRequest:
    purpose: str
    """호출 목적 식별자 (예: interpret_question, explain_server)."""
    system: str
    prompt: str
    schema: dict[str, Any] | None = None


@dataclass(frozen=True)
class LLMResponse:
    text: str
    data: dict[str, Any] | None = None
    cost_usd: float | None = None
    model: str | None = None


class LLMClient(Protocol):
    @property
    def name(self) -> str: ...

    async def complete(self, request: LLMRequest) -> LLMResponse: ...


@dataclass
class BudgetedLLM:
    """요청 단위 호출 수 상한을 적용하는 래퍼. 여러 에이전트가 공유합니다."""

    client: LLMClient
    max_calls: int
    calls: int = 0
    cost_usd: float = 0.0
    purposes: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.client.name

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if self.calls >= self.max_calls:
            raise LLMBudgetExceededError(
                f"요청당 모델 호출 상한({self.max_calls}회)에 도달했습니다"
            )
        self.calls += 1
        self.purposes.append(request.purpose)
        response = await self.client.complete(request)
        if response.cost_usd:
            self.cost_usd += response.cost_usd
        return response
