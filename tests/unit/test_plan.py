"""실행 계획 템플릿·검증 테스트."""

from __future__ import annotations

import pytest

from infra_agent.orchestration.plan import DOMAIN_AGENTS, ExecutionPlan, PlanError, build_plan
from infra_agent.orchestration.rules import IMPLEMENTED_DOMAINS
from infra_agent.orchestration.runner import AGENT_BUILDERS
from infra_agent.schemas import AgentName, AgentTask

ALL = tuple(AgentName)


def _task(task_id: str, *deps: str) -> AgentTask:
    return AgentTask(task_id=task_id, agent=AgentName.SERVER, objective="o", depends_on=deps)


def test_only_requested_and_available_agents() -> None:
    plan = build_plan({"server", "kubernetes"}, {AgentName.SERVER})
    assert [t.agent for t in plan.tasks] == [AgentName.SERVER]
    assert plan.tasks[0].depends_on == ()
    assert plan.unavailable == (AgentName.KUBERNETES,)
    assert build_plan({"unknown"}, ALL).tasks == ()


def test_service_before_network_and_db() -> None:
    plan = build_plan({"service", "network", "db", "server"}, ALL)
    deps = {t.agent: t.depends_on for t in plan.tasks}
    assert deps[AgentName.SERVICE] == ()
    assert deps[AgentName.SERVER] == ()
    assert deps[AgentName.NETWORK] == ("service-1",)
    assert deps[AgentName.DB] == ("service-1",)
    order = [t.task_id for t in plan.tasks]
    assert order.index("service-1") < order.index("network-1") < order.index("db-1")


def test_dependency_dropped_when_upstream_not_selected() -> None:
    plan = build_plan({"network", "db"}, ALL)  # Service 없이 요청 → 독립 실행
    assert all(t.depends_on == () for t in plan.tasks)
    unavailable_service = build_plan({"service", "db"}, {AgentName.DB})
    assert unavailable_service.tasks[0].depends_on == ()
    assert unavailable_service.unavailable == (AgentName.SERVICE,)


def test_validation_errors() -> None:
    with pytest.raises(PlanError, match="중복"):
        ExecutionPlan(tasks=(_task("a"), _task("a")))
    with pytest.raises(PlanError, match="계획에 없는 선행 작업"):
        ExecutionPlan(tasks=(_task("a", "missing"),))
    with pytest.raises(PlanError, match="순환"):
        ExecutionPlan(tasks=(_task("a", "b"), _task("b", "a"), _task("c")))
    ok = ExecutionPlan(tasks=(_task("a"), _task("b", "a"), _task("c", "a", "b")))
    assert ok.task("c").depends_on == ("a", "b")


def test_implemented_domains_match_registered_agents() -> None:
    """질문 해석의 '구현된 분야'와 실행 계층의 에이전트 등록이 어긋나지 않게 합니다."""
    registered = {d for d, a in DOMAIN_AGENTS.items() if a in AGENT_BUILDERS}
    assert registered == IMPLEMENTED_DOMAINS
