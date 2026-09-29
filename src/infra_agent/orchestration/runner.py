"""질문 처리 흐름 (모델 없이): 해석 → 컨텍스트 → 에이전트 실행 → 종합.

현재는 Server Agent만 구현되어 있어 순차 실행합니다.
병렬 실행기·다중 에이전트는 7단계에서 추가합니다.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

from infra_agent.agents.server import ServerAgent
from infra_agent.answer.synthesis import synthesize
from infra_agent.catalog import Catalog
from infra_agent.config.settings import Settings
from infra_agent.datasources.prometheus import PrometheusClient
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


def build_context(
    question: str, interp: Interpretation, settings: Settings, now: datetime
) -> AnalysisContext:
    return AnalysisContext(
        request_id=uuid.uuid4().hex[:12],
        question=question,
        intent=interp.intent,
        time_range=interp.time_range,
        baseline_range=interp.baseline_range,
        targets=tuple(TargetRef(kind=k, name=v) for k, v in interp.targets.items()),
        budget=Budget(
            # 이 흐름은 결정적 분석만 수행하며 모델을 호출하지 않습니다.
            max_llm_calls=0,
            max_tool_calls=settings.analysis.max_tool_calls,
            deadline=now + timedelta(seconds=settings.execution.request_timeout_seconds),
        ),
    )


async def answer_question(
    question: str,
    settings: Settings,
    catalog: Catalog,
    *,
    range_override: str | None = None,
    target_overrides: Mapping[TargetKind, str] | None = None,
    now: datetime | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AnswerBundle:
    current = now or utc_now()
    interp = interpret(
        question,
        current,
        parse_duration(settings.execution.default_time_range),
        range_override=parse_duration(range_override) if range_override else None,
        target_overrides=target_overrides,
    )
    ctx = build_context(question, interp, settings, current)
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
            agent = ServerAgent(tool, settings.analysis)
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
    return AnswerBundle(interp, ctx, tuple(results), answer)
