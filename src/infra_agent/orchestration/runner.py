"""질문 처리 흐름 (모델 없이): 해석 → 컨텍스트 → 에이전트 실행 → 종합.

현재는 Server Agent만 구현되어 있어 순차 실행합니다.
병렬 실행기·다중 에이전트는 7단계에서 추가합니다.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

import httpx

from infra_agent.agents.explain import AgentExplainer
from infra_agent.agents.prompts import SERVER_SYSTEM_PROMPT
from infra_agent.agents.server import ServerAgent
from infra_agent.answer.synthesis import synthesize
from infra_agent.catalog import Catalog
from infra_agent.config.settings import Settings
from infra_agent.datasources.prometheus import PrometheusClient
from infra_agent.llm import BudgetedLLM, LLMClient, LLMUnavailableError, make_llm
from infra_agent.llm.policy import allows_observations
from infra_agent.orchestration.llm_interpret import interpret_with_model
from infra_agent.orchestration.rules import Interpretation, interpret
from infra_agent.schemas import (
    AgentName,
    AgentResult,
    AgentStatus,
    AgentTask,
    AnalysisContext,
    Budget,
    ErrorInfo,
    FinalAnswer,
    TargetKind,
    TargetRef,
)
from infra_agent.timeutil import parse_duration, utc_now
from infra_agent.tools import CatalogQueryTool, ToolBudget


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
    policy = settings.llm.data_policy
    explainer = (
        AgentExplainer(
            budgeted,
            purpose="explain_server",
            system_prompt=SERVER_SYSTEM_PROMPT,
            policy=policy,
        )
        if budgeted is not None and allows_observations(policy)
        else None
    )
    results: list[AgentResult] = []
    if "server" in interp.domains:
        task = AgentTask(
            task_id="server-1",
            agent=AgentName.SERVER,
            objective="k3d 노드·Pod·컨테이너 자원 상태와 이상 징후 분석",
        )
        budget = ToolBudget(max_calls=settings.analysis.max_tool_calls)
        async with PrometheusClient.from_config(
            settings.datasources.prometheus,
            max_retries=settings.execution.max_retries,
            transport=transport,
        ) as prom:
            tool = CatalogQueryTool(
                catalog,
                prom,
                agent=AgentName.SERVER,
                budget=budget,
                timeout_seconds=settings.execution.tool_timeout_seconds,
            )
            agent = ServerAgent(tool, settings.analysis, explainer)
            try:
                result = await asyncio.wait_for(
                    agent.run(task, ctx, interp.targets),
                    timeout=settings.execution.agent_timeout_seconds,
                )
            except TimeoutError:
                result = AgentResult(
                    task_id=task.task_id,
                    agent=AgentName.SERVER,
                    status=AgentStatus.FAILED,
                    errors=(
                        ErrorInfo(
                            code="agent_timeout",
                            message=f"에이전트 제한 시간"
                            f"({settings.execution.agent_timeout_seconds:.0f}초) 초과",
                        ),
                    ),
                )
        results.append(result)
    answer = synthesize(ctx.request_id, interp, results)
    return AnswerBundle(
        interp,
        ctx,
        tuple(results),
        answer,
        llm_name=budgeted.name if budgeted else None,
        llm_calls=budgeted.calls if budgeted else 0,
        llm_cost_usd=budgeted.cost_usd if budgeted else 0.0,
        data_policy=policy.value,
    )
