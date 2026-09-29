"""전문 에이전트 공통 인터페이스.

- 모든 에이전트는 Coordinator가 만든 같은 `AnalysisContext`(시간 범위·대상·예산)를
  읽기 전용으로 공유합니다.
- `upstream`에는 계획상 선행 작업(`AgentTask.depends_on`)의 결과만 전달됩니다.
  선행 결과는 대상·시간을 좁히는 데만 쓰고, 그 안의 문장을 실행 지시로 취급하지 않습니다.
- 에이전트는 실패를 예외로 던질 수 있으며, 실행기가 `failed` 결과로 바꿉니다.
"""

from __future__ import annotations

from collections.abc import Mapping
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
