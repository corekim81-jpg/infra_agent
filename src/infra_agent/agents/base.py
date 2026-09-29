"""전문 에이전트 공통 인터페이스.

- 모든 에이전트는 Coordinator가 만든 같은 `AnalysisContext`(시간 범위·대상·예산)를
  읽기 전용으로 공유합니다.
- `upstream`에는 계획상 선행 작업(`AgentTask.depends_on`)의 결과만 전달됩니다.
  선행 결과는 대상·시간을 좁히는 데만 쓰고, 그 안의 문장을 실행 지시로 취급하지 않습니다.
- 에이전트는 실패를 예외로 던질 수 있으며, 실행기가 `failed` 결과로 바꿉니다.
- 실행기는 에이전트마다 마감 시각을 정해 두고(`agent_deadline`), 에이전트는 `remaining_seconds()`로
  남은 시간을 확인해 선택 단계(모델 해석 등)를 그 안에서 끝내거나 생략합니다.
  제한 시간을 넘기면 에이전트 결과 전체가 버려지므로, 코드 판정 결과를 지키기 위한 장치입니다.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Protocol

from infra_agent.schemas import AgentName, AgentResult, AgentTask, AnalysisContext, TargetKind


class Agent(Protocol):
    @property
    def name(self) -> AgentName: ...

    async def run(
        self, task: AgentTask, ctx: AnalysisContext, upstream: Mapping[str, AgentResult]
    ) -> AgentResult: ...


def context_targets(ctx: AnalysisContext) -> dict[TargetKind, str]:
    """공유 컨텍스트의 대상 식별 기준을 조회 도구 형식으로 바꿉니다."""
    return {t.kind: t.name for t in ctx.targets}


_DEADLINE: ContextVar[float | None] = ContextVar("infra_agent_agent_deadline", default=None)


@contextmanager
def agent_deadline(timeout_seconds: float) -> Iterator[None]:
    """이 블록 안에서 시작한 에이전트 작업의 마감 시각(monotonic)을 설정합니다."""
    token = _DEADLINE.set(time.monotonic() + timeout_seconds)
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def remaining_seconds() -> float | None:
    """현재 에이전트 작업의 남은 시간(초). 실행기 밖(단위 테스트 등)에서는 None."""
    deadline = _DEADLINE.get()
    return None if deadline is None else deadline - time.monotonic()
