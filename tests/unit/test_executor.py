"""실행기 테스트 (가짜 에이전트, 실제 데이터 소스 없음)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from infra_agent.orchestration.executor import Executor
from infra_agent.orchestration.plan import ExecutionPlan, build_plan
from infra_agent.schemas import (
    AgentName,
    AgentResult,
    AgentStatus,
    AgentTask,
    AnalysisContext,
    Budget,
    Intent,
    TimeRange,
)

NOW = datetime(2026, 9, 29, 3, 0, tzinfo=UTC)
ALL = tuple(AgentName)


def _ctx(deadline_after: float = 60) -> AnalysisContext:
    return AnalysisContext(
        request_id="r",
        question="q",
        intent=Intent.STATUS,
        time_range=TimeRange.last(timedelta(minutes=30), NOW),
        budget=Budget(
            max_llm_calls=0, max_tool_calls=10, deadline=NOW + timedelta(seconds=deadline_after)
        ),
    )


@dataclass
class Log:
    events: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class FakeAgent:
    agent: AgentName
    log: Log
    delay: float = 0.02
    status: AgentStatus = AgentStatus.SUCCESS
    error: Exception | None = None
    wrong_task: bool = False
    seen_upstream: dict[str, AgentResult] = field(default_factory=dict)

    @property
    def name(self) -> AgentName:
        return self.agent

    async def run(
        self, task: AgentTask, ctx: AnalysisContext, upstream: Mapping[str, AgentResult]
    ) -> AgentResult:
        self.log.events.append(("start", task.task_id))
        self.seen_upstream = dict(upstream)
        await asyncio.sleep(self.delay)
        self.log.events.append(("end", task.task_id))
        if self.error is not None:
            raise self.error
        return AgentResult(
            task_id="other" if self.wrong_task else task.task_id,
            agent=self.agent,
            status=self.status,
            limitations=("부분 결과",) if self.status is AgentStatus.PARTIAL else (),
        )


def _clock() -> Callable[[], datetime]:
    return lambda: NOW


def _executor(max_concurrency: int = 4, timeout: float = 5) -> Executor:
    return Executor(max_concurrency=max_concurrency, agent_timeout_seconds=timeout, clock=_clock())


def _agents(log: Log, **overrides: FakeAgent) -> dict[AgentName, FakeAgent]:
    agents = {a: FakeAgent(a, log) for a in ALL}
    for key, agent in overrides.items():
        agents[AgentName(key)] = agent
    return agents


async def test_independent_tasks_run_in_parallel() -> None:
    log = Log()
    plan = build_plan({"server", "kubernetes"}, ALL)
    report = await _executor().run(plan, _agents(log), _ctx())
    assert report.peak_concurrency == 2
    assert [e[0] for e in log.events[:2]] == ["start", "start"]  # 둘 다 시작한 뒤 종료
    assert all(r.status is AgentStatus.SUCCESS for r in report.results)
    assert [r.task_id for r in report.results] == [t.task_id for t in plan.tasks]
    assert all(r.usage.elapsed_ms >= 15 for r in report.results)


async def test_concurrency_limit() -> None:
    log = Log()
    plan = build_plan({"server", "kubernetes", "service"}, ALL)
    report = await _executor(max_concurrency=1).run(plan, _agents(log), _ctx())
    assert report.peak_concurrency == 1
    kinds = [e[0] for e in log.events]
    assert kinds == ["start", "end"] * 3  # 한 번에 하나씩


async def test_dependencies_run_after_upstream_and_receive_results() -> None:
    log = Log()
    agents = _agents(log)
    plan = build_plan({"service", "network", "db"}, ALL)
    report = await _executor().run(plan, agents, _ctx())
    order = log.events
    service_end = order.index(("end", "service-1"))
    assert order.index(("start", "network-1")) > service_end
    assert order.index(("start", "db-1")) > service_end
    assert report.peak_concurrency == 2  # Network ∥ DB
    assert set(agents[AgentName.NETWORK].seen_upstream) == {"service-1"}
    assert agents[AgentName.SERVICE].seen_upstream == {}
    runs = {r.task_id: r for r in report.runs}
    assert runs["db-1"].depends_on == ("service-1",)


async def test_timeout_fails_and_dependents_are_skipped() -> None:
    log = Log()
    agents = _agents(log, service=FakeAgent(AgentName.SERVICE, log, delay=1.0))
    plan = build_plan({"service", "db", "server"}, ALL)
    report = await _executor(timeout=0.05).run(plan, agents, _ctx())
    by_id = {r.task_id: r for r in report.results}
    assert by_id["service-1"].status is AgentStatus.FAILED
    assert by_id["service-1"].errors[0].code == "agent_timeout"
    assert "에이전트 제한 시간" in by_id["service-1"].errors[0].message
    assert by_id["db-1"].status is AgentStatus.SKIPPED
    assert by_id["db-1"].errors[0].code == "dependency_not_completed"
    assert by_id["server-1"].status is AgentStatus.SUCCESS  # 독립 작업은 영향 없음
    assert ("start", "db-1") not in log.events
    run = {r.task_id: r for r in report.runs}["db-1"]
    assert run.reason is not None and "service-1" in run.reason


async def test_exception_is_isolated_and_redacted() -> None:
    log = Log()
    boom = FakeAgent(
        AgentName.SERVER,
        log,
        error=RuntimeError("연결 실패 password=hunter22 Bearer abcdef.123456"),
    )
    plan = build_plan({"server", "kubernetes"}, ALL)
    report = await _executor().run(plan, _agents(log, server=boom), _ctx())
    by_agent = {r.agent: r for r in report.results}
    failed = by_agent[AgentName.SERVER]
    assert failed.status is AgentStatus.FAILED and failed.errors[0].code == "agent_error"
    assert "hunter22" not in failed.errors[0].message
    assert "abcdef.123456" not in failed.errors[0].message
    assert by_agent[AgentName.KUBERNETES].status is AgentStatus.SUCCESS


async def test_partial_upstream_does_not_block() -> None:
    log = Log()
    agents = _agents(log, service=FakeAgent(AgentName.SERVICE, log, status=AgentStatus.PARTIAL))
    report = await _executor().run(build_plan({"service", "db"}, ALL), agents, _ctx())
    assert [r.status for r in report.results] == [AgentStatus.PARTIAL, AgentStatus.SUCCESS]


async def test_request_deadline() -> None:
    log = Log()
    plan = build_plan({"server"}, ALL)
    expired = await _executor().run(plan, _agents(log), _ctx(deadline_after=-1))
    assert expired.results[0].status is AgentStatus.SKIPPED
    assert expired.results[0].errors[0].code == "request_deadline"
    assert log.events == []
    # 마감까지 남은 시간이 에이전트 제한 시간보다 짧으면 마감 시간을 적용
    slow = FakeAgent(AgentName.SERVER, log, delay=1.0)
    near = await _executor(timeout=30).run(plan, _agents(log, server=slow), _ctx(0.05))
    assert near.results[0].errors[0].code == "agent_timeout"
    assert "요청 제한 시간" in near.results[0].errors[0].message


async def test_contract_violation_and_missing_agent() -> None:
    log = Log()
    wrong = FakeAgent(AgentName.SERVER, log, wrong_task=True)
    plan = build_plan({"server", "kubernetes"}, ALL)
    agents = _agents(log, server=wrong)
    del agents[AgentName.KUBERNETES]
    report = await _executor().run(plan, agents, _ctx())
    codes = {r.agent: r.errors[0].code for r in report.results}
    assert codes == {
        AgentName.SERVER: "agent_contract",
        AgentName.KUBERNETES: "agent_unavailable",
    }


async def test_empty_plan() -> None:
    report = await _executor().run(ExecutionPlan(tasks=()), {}, _ctx())
    assert report.results == () and report.runs == ()
