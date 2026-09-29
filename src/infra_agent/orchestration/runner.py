"""질문 처리 흐름: 해석 → 컨텍스트 → 실행 계획 → 실행기 → 종합.

- 에이전트는 요청마다 새로 만들고, 조회 예산(ToolBudget)·모델 호출 예산(BudgetedLLM)·
  데이터 소스 연결을 요청 단위로 공유합니다.
- 구현된 에이전트는 `AGENT_BUILDERS`에 등록합니다. 등록되지 않은 분야는 계획에 넣지 않고
  "확인하지 못한 영역"으로 답합니다.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

import httpx

from infra_agent.agents.base import Agent
from infra_agent.agents.explain import AgentExplainer
from infra_agent.agents.kubernetes import KubernetesAgent
from infra_agent.agents.prompts import (
    KUBERNETES_SYSTEM_PROMPT,
    SERVER_SYSTEM_PROMPT,
    SERVICE_SYSTEM_PROMPT,
)
from infra_agent.agents.server import ServerAgent
from infra_agent.agents.service import ServiceAgent
from infra_agent.answer.synthesis import synthesize
from infra_agent.catalog import Catalog
from infra_agent.config.settings import Settings
from infra_agent.datasources.loki import LokiClient
from infra_agent.datasources.prometheus import PrometheusClient
from infra_agent.datasources.tempo import TempoClient
from infra_agent.llm import BudgetedLLM, LLMClient, LLMUnavailableError, make_llm
from infra_agent.llm.policy import allows_observations
from infra_agent.orchestration.executor import Executor, TaskRun
from infra_agent.orchestration.llm_interpret import interpret_with_model
from infra_agent.orchestration.plan import ExecutionPlan, build_plan
from infra_agent.orchestration.rules import Interpretation, interpret
from infra_agent.schemas import (
    AgentName,
    AgentResult,
    AnalysisContext,
    Budget,
    FinalAnswer,
    TargetKind,
    TargetRef,
)
from infra_agent.timeutil import parse_duration, utc_now
from infra_agent.tools import CatalogQueryTool, LogQueryTool, ToolBudget, TraceSearchTool


@dataclass(frozen=True)
class AnswerBundle:
    interpretation: Interpretation
    context: AnalysisContext
    results: tuple[AgentResult, ...]
    answer: FinalAnswer
    llm_name: str | None = None
    llm_calls: int = 0
    llm_cost_usd: float = 0.0
    data_policy: str = "none"
    plan: ExecutionPlan | None = None
    runs: tuple[TaskRun, ...] = field(default_factory=tuple)


@dataclass
class AgentDeps:
    """요청 단위로 에이전트가 공유하는 자원."""

    settings: Settings
    catalog: Catalog
    prometheus: PrometheusClient
    tool_budget: ToolBudget
    llm: BudgetedLLM | None
    loki: LokiClient | None = None
    tempo: TempoClient | None = None


def _explainer(deps: AgentDeps, agent: AgentName, prompt: str) -> AgentExplainer | None:
    """에이전트별 지침으로 모델 해석기를 만듭니다. 조회 데이터를 보낼 수 없는 정책이면 None."""
    policy = deps.settings.llm.data_policy
    if deps.llm is None or not allows_observations(policy):
        return None
    return AgentExplainer(
        deps.llm, purpose=f"explain_{agent.value}", system_prompt=prompt, policy=policy
    )


def _tool(deps: AgentDeps, agent: AgentName) -> CatalogQueryTool:
    """에이전트 전용 조회 도구 (자기 분야 카탈로그 항목만 실행 가능, 조회 예산은 공유)."""
    return CatalogQueryTool(
        deps.catalog,
        deps.prometheus,
        agent=agent,
        budget=deps.tool_budget,
        timeout_seconds=deps.settings.execution.tool_timeout_seconds,
    )


def _build_server(deps: AgentDeps) -> Agent:
    return ServerAgent(
        _tool(deps, AgentName.SERVER),
        deps.settings.analysis,
        _explainer(deps, AgentName.SERVER, SERVER_SYSTEM_PROMPT),
    )


def _build_kubernetes(deps: AgentDeps) -> Agent:
    return KubernetesAgent(
        _tool(deps, AgentName.KUBERNETES),
        deps.settings.analysis,
        _explainer(deps, AgentName.KUBERNETES, KUBERNETES_SYSTEM_PROMPT),
    )


def _build_service(deps: AgentDeps) -> Agent:
    timeout = deps.settings.execution.tool_timeout_seconds
    logs = (
        LogQueryTool(
            deps.catalog,
            deps.loki,
            agent=AgentName.SERVICE,
            budget=deps.tool_budget,
            timeout_seconds=timeout,
        )
        if deps.loki is not None
        else None
    )
    traces = (
        TraceSearchTool(
            deps.tempo, agent=AgentName.SERVICE, budget=deps.tool_budget, timeout_seconds=timeout
        )
        if deps.tempo is not None
        else None
    )
    return ServiceAgent(
        _tool(deps, AgentName.SERVICE),
        deps.settings.analysis,
        logs=logs,
        traces=traces,
        explainer=_explainer(deps, AgentName.SERVICE, SERVICE_SYSTEM_PROMPT),
        # 상세 단계: 조회 1회 제한 시간 + 모델 해석 최소 시간(5초) + 여유(2초)
        detail_min_seconds=timeout + 7,
    )


AGENT_BUILDERS: Mapping[AgentName, Callable[[AgentDeps], Agent]] = {
    AgentName.SERVER: _build_server,
    AgentName.KUBERNETES: _build_kubernetes,
    AgentName.SERVICE: _build_service,
}
"""구현된 에이전트. 이후 DB·Network 에이전트를 여기에 추가합니다."""


def build_context(
    question: str,
    interp: Interpretation,
    settings: Settings,
    now: datetime,
    max_llm_calls: int = 0,
) -> AnalysisContext:
    return AnalysisContext(
        request_id=uuid.uuid4().hex[:12],
        question=question,
        intent=interp.intent,
        time_range=interp.time_range,
        baseline_range=interp.baseline_range,
        targets=tuple(TargetRef(kind=k, name=v) for k, v in interp.targets.items()),
        budget=Budget(
            max_llm_calls=max_llm_calls,
            max_tool_calls=settings.analysis.max_tool_calls,
            deadline=now + timedelta(seconds=settings.execution.request_timeout_seconds),
        ),
    )


def _prepare_llm(
    settings: Settings, llm: LLMClient | None, use_llm: bool
) -> tuple[BudgetedLLM | None, list[str]]:
    notes: list[str] = []
    if not use_llm or settings.llm.max_calls_per_request == 0:
        return None, notes
    client = llm
    if client is None:
        try:
            client = make_llm(settings)
        except LLMUnavailableError as exc:
            notes.append(f"모델 사용 불가: {exc.message}")
            return None, notes
    if client is None:
        return None, notes
    return BudgetedLLM(client, max_calls=settings.llm.max_calls_per_request), notes


async def answer_question(
    question: str,
    settings: Settings,
    catalog: Catalog,
    *,
    range_override: str | None = None,
    target_overrides: Mapping[TargetKind, str] | None = None,
    now: datetime | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    llm: LLMClient | None = None,
    use_llm: bool = True,
) -> AnswerBundle:
    current = now or utc_now()
    started = time.monotonic()

    def clock() -> datetime:
        """요청 기준 시각에서 흐른 시간만큼 진행하는 시계 (마감 시각 판정용)."""
        return current + timedelta(seconds=time.monotonic() - started)

    budgeted, notes = _prepare_llm(settings, llm, use_llm)
    default_range = parse_duration(settings.execution.default_time_range)
    override = parse_duration(range_override) if range_override else None
    if budgeted is not None:
        interp = await interpret_with_model(
            question,
            budgeted,
            current,
            default_range,
            range_override=override,
            target_overrides=target_overrides,
        )
    else:
        interp = interpret(
            question,
            current,
            default_range,
            range_override=override,
            target_overrides=target_overrides,
        )
    if notes:
        interp = replace(interp, method_note="; ".join(notes))
    ctx = build_context(question, interp, settings, current, budgeted.max_calls if budgeted else 0)
    plan = build_plan(interp.domains, AGENT_BUILDERS.keys())
    report = None
    if plan.tasks:
        async with AsyncExitStack() as stack:
            retries = settings.execution.max_retries
            sources = settings.datasources
            prom = await stack.enter_async_context(
                PrometheusClient.from_config(
                    sources.prometheus, max_retries=retries, transport=transport
                )
            )
            # 로그·트레이스는 Service Agent가 계획에 있고 데이터 소스가 켜져 있을 때만 연결합니다.
            wants_service = any(t.agent is AgentName.SERVICE for t in plan.tasks)
            loki = (
                await stack.enter_async_context(
                    LokiClient.from_config(sources.loki, max_retries=retries, transport=transport)
                )
                if wants_service and sources.loki.enabled
                else None
            )
            tempo = (
                await stack.enter_async_context(
                    TempoClient.from_config(sources.tempo, max_retries=retries, transport=transport)
                )
                if wants_service and sources.tempo.enabled
                else None
            )
            deps = AgentDeps(
                settings=settings,
                catalog=catalog,
                prometheus=prom,
                tool_budget=ToolBudget(max_calls=ctx.budget.max_tool_calls),
                llm=budgeted,
                loki=loki,
                tempo=tempo,
            )
            agents = {t.agent: AGENT_BUILDERS[t.agent](deps) for t in plan.tasks}
            executor = Executor(
                max_concurrency=settings.execution.max_concurrency,
                agent_timeout_seconds=settings.execution.agent_timeout_seconds,
                clock=clock,
            )
            report = await executor.run(plan, agents, ctx)
    results = list(report.results) if report else []
    answer = synthesize(ctx.request_id, interp, results)
    return AnswerBundle(
        interp,
        ctx,
        tuple(results),
        answer,
        llm_name=budgeted.name if budgeted else None,
        llm_calls=budgeted.calls if budgeted else 0,
        llm_cost_usd=budgeted.cost_usd if budgeted else 0.0,
        data_policy=settings.llm.data_policy.value,
        plan=plan,
        runs=report.runs if report else (),
    )
