"""실행 계획 (architecture.md 4.2·10.3절).

모델이 실행 순서를 정하지 않고, 분야별 템플릿으로 계획을 만듭니다.
- 질문이 다루는 분야의 에이전트만 넣습니다 (모든 질문에 모든 에이전트를 쓰지 않음).
- 구현되지 않은 에이전트는 계획에 넣지 않고 "확인하지 못한 영역"으로 남깁니다.
- 의존 규칙: 선행 결과로 대상·시간을 좁혀야 하는 작업만 순차로 두고, 나머지는 병렬로 둡니다.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass

from infra_agent.schemas import AgentName, AgentTask

DOMAIN_AGENTS: Mapping[str, AgentName] = {
    "server": AgentName.SERVER,
    "kubernetes": AgentName.KUBERNETES,
    "network": AgentName.NETWORK,
    "db": AgentName.DB,
    "service": AgentName.SERVICE,
}

OBJECTIVES: Mapping[AgentName, str] = {
    AgentName.SERVER: "k3d 노드·Pod·컨테이너 자원 상태와 이상 징후 분석",
    AgentName.KUBERNETES: "Pod·노드·워크로드 상태, 재시작·Pending·OOM·이벤트 분석",
    AgentName.NETWORK: "네트워크 드롭·DNS·서비스 간 통신 분석",
    AgentName.DB: "DB 연결·커넥션 풀·쿼리 지연·캐시 분석",
    AgentName.SERVICE: "서비스 요청량·오류율·응답 시간·로그·트레이스 분석",
}

DEPENDENCIES: Mapping[AgentName, tuple[AgentName, ...]] = {
    # Service 분석으로 영향받은 서비스·시간대를 확정한 뒤 원인 분야를 확인합니다.
    AgentName.NETWORK: (AgentName.SERVICE,),
    AgentName.DB: (AgentName.SERVICE,),
}
"""두 에이전트가 모두 계획에 있을 때만 적용합니다. 선행 에이전트가 없으면 독립 실행합니다."""

OPTIONAL_UPSTREAM: frozenset[AgentName] = frozenset({AgentName.NETWORK, AgentName.DB})
"""선행 결과가 없어도 분석할 수 있는 에이전트. 선행 작업이 실패해도 건너뛰지 않고 실행합니다
(선행 Service 결과는 중복 조회 회피와 이상 서비스 집중 확인에 쓰며, 없으면 그 확인만 생략)."""

_AGENT_ORDER: tuple[AgentName, ...] = (
    AgentName.SERVER,
    AgentName.KUBERNETES,
    AgentName.SERVICE,
    AgentName.NETWORK,
    AgentName.DB,
)
"""계획·결과 표시 순서. 선행 작업(Service)이 의존 작업보다 앞에 오도록 둡니다."""


class PlanError(ValueError):
    """계획 구조 오류 (중복 ID, 없는 선행 작업, 순환)."""


@dataclass(frozen=True)
class ExecutionPlan:
    tasks: tuple[AgentTask, ...]
    unavailable: tuple[AgentName, ...] = ()
    """질문 분야에 해당하지만 구현되지 않아 계획에 넣지 않은 에이전트."""
    optional_upstream: frozenset[AgentName] = frozenset()
    """선행 작업이 실패해도 실행하는 에이전트 (`OPTIONAL_UPSTREAM` 중 계획에 있는 것)."""

    def __post_init__(self) -> None:
        validate(self.tasks)

    def task(self, task_id: str) -> AgentTask:
        for t in self.tasks:
            if t.task_id == task_id:
                return t
        raise KeyError(task_id)


def task_id_for(agent: AgentName) -> str:
    return f"{agent.value}-1"


def validate(tasks: Iterable[AgentTask]) -> None:
    items = list(tasks)
    ids = [t.task_id for t in items]
    duplicated = sorted({i for i in ids if ids.count(i) > 1})
    if duplicated:
        raise PlanError(f"작업 ID가 중복되었습니다: {', '.join(duplicated)}")
    known = set(ids)
    for t in items:
        missing = [d for d in t.depends_on if d not in known]
        if missing:
            raise PlanError(f"{t.task_id}: 계획에 없는 선행 작업 {', '.join(missing)}")
    # 순환 검사 (Kahn)
    remaining = {t.task_id: set(t.depends_on) for t in items}
    while remaining:
        ready = [i for i, deps in remaining.items() if not deps]
        if not ready:
            raise PlanError(f"작업 의존 관계에 순환이 있습니다: {', '.join(sorted(remaining))}")
        for i in ready:
            del remaining[i]
        for deps in remaining.values():
            deps.difference_update(ready)


def build_plan(domains: Collection[str], available: Collection[AgentName]) -> ExecutionPlan:
    """질문 분야와 구현된 에이전트로 실행 계획을 만듭니다."""
    wanted = {DOMAIN_AGENTS[d] for d in domains if d in DOMAIN_AGENTS}
    selected = [a for a in _AGENT_ORDER if a in wanted and a in available]
    unavailable = tuple(a for a in _AGENT_ORDER if a in wanted and a not in available)
    tasks = tuple(
        AgentTask(
            task_id=task_id_for(agent),
            agent=agent,
            objective=OBJECTIVES[agent],
            depends_on=tuple(
                task_id_for(dep) for dep in DEPENDENCIES.get(agent, ()) if dep in selected
            ),
        )
        for agent in selected
    )
    return ExecutionPlan(
        tasks=tasks,
        unavailable=unavailable,
        optional_upstream=frozenset(a for a in selected if a in OPTIONAL_UPSTREAM),
    )
