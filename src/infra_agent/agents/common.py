"""전문 에이전트가 함께 쓰는 수집·조회·결과 정리 코드.

에이전트마다 판정 규칙과 지침은 다르지만, 근거 수집·조회 실패 처리·최신성 표시·상태 결정·
모델 해석 결합 방식은 같아야 답변 형식이 일관됩니다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from infra_agent.agents.explain import AgentExplainer
from infra_agent.config.settings import AnalysisConfig
from infra_agent.schemas import (
    AgentName,
    AgentResult,
    AgentStatus,
    AgentTask,
    AnalysisContext,
    ErrorInfo,
    Finding,
    TargetKind,
    ToolResult,
    ToolStatus,
    Usage,
)
from infra_agent.tools import CatalogQueryTool, QueryMode


@dataclass
class Collector:
    findings: list[Finding] = field(default_factory=list)
    evidence: dict[str, ToolResult] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    next_checks: list[str] = field(default_factory=list)
    errors: list[ErrorInfo] = field(default_factory=list)
    queries: int = 0
    failed_queries: int = 0

    def add_evidence(self, result: ToolResult) -> None:
        self.evidence[result.evidence_id] = result

    def limit(self, text: str) -> None:
        if text not in self.limitations:
            self.limitations.append(text)

    def suggest(self, text: str) -> None:
        if text not in self.next_checks:
            self.next_checks.append(text)


async def fetch_evidence(
    tool: CatalogQueryTool,
    cfg: AnalysisConfig,
    key: str,
    ctx: AnalysisContext,
    mode: QueryMode,
    targets: Mapping[TargetKind, str],
    col: Collector,
    *,
    at: datetime | None = None,
    tag: str | None = None,
) -> ToolResult | None:
    """조회하고 근거로 기록합니다. 조회하지 못했거나 실패하면 한계를 남기고 None.

    `at`·`tag`는 `CatalogQueryTool.query`와 같습니다(평가 시각 지정, 근거 ID 구분).
    """
    outcome = await tool.query(key, ctx, mode, targets, at=at, tag=tag)
    label = f"{key}:{tag}" if tag else key
    result = outcome.result
    if outcome.skipped:
        kinds = ", ".join(k.value for k in outcome.unsupported_targets)
        col.limit(f"{key}: 요청한 대상({kinds})으로 필터링할 수 없어 조회하지 않음")
        return None
    col.queries += 1
    col.add_evidence(result)
    if result.status in (ToolStatus.ERROR, ToolStatus.TIMEOUT):
        col.failed_queries += 1
        col.errors.append(ErrorInfo(code=result.status.value, message=f"{label}: {result.error}"))
        col.limit(f"{label}: 조회 실패로 확인하지 못함 ({result.error})")
        return None
    if result.freshness_seconds is None:
        col.limit(f"{label}: 데이터 최신성을 확인하지 못함")
    elif result.freshness_seconds > cfg.stale_after_seconds:
        col.limit(
            f"{label}: 최신 샘플이 {result.freshness_seconds:.0f}초 전으로 오래되어(기준 "
            f"{cfg.stale_after_seconds}초) 현재 상태를 반영하지 못할 수 있음"
        )
    return result


def is_fresh(result: ToolResult, cfg: AnalysisConfig) -> bool:
    return (
        result.freshness_seconds is not None and result.freshness_seconds <= cfg.stale_after_seconds
    )


def finish(task: AgentTask, agent: AgentName, col: Collector) -> AgentResult:
    """조회 성공·실패 수로 상태를 정하고 결과를 만듭니다. 모든 조회가 실패하면 판정을 버립니다."""
    status = AgentStatus.SUCCESS
    if col.failed_queries and col.failed_queries == col.queries:
        status = AgentStatus.FAILED
        col.findings.clear()
    elif col.failed_queries:
        status = AgentStatus.PARTIAL
    if status is not AgentStatus.SUCCESS and not col.errors:
        col.errors.append(ErrorInfo(code="partial", message="일부 조회를 완료하지 못함"))
    return AgentResult(
        task_id=task.task_id,
        agent=agent,
        status=status,
        findings=tuple(col.findings),
        evidence=tuple(col.evidence.values()),
        limitations=tuple(col.limitations),
        next_checks=tuple(col.next_checks),
        errors=tuple(col.errors),
        usage=Usage(tool_calls=col.queries),
    )


CAUSE_WORDS = ("원인", "이유", "왜", "cause", "why")
"""질문이 원인을 묻는지 판단하는 단어 (원인 후보를 제시하지 않은 이유를 알릴지 결정)."""
NO_ANOMALY_NO_HYPOTHESIS = "기준을 넘는 이상 징후가 없어 원인 후보(모델 해석)를 요청하지 않음"


def asks_cause(question: str) -> bool:
    text = question.lower()
    return any(word in text for word in CAUSE_WORDS)


async def with_explanation(
    result: AgentResult, explainer: AgentExplainer | None, ctx: AnalysisContext
) -> AgentResult:
    """모델 해석(원인 후보·추가 확인)을 덧붙입니다. 코드 판정(사실)은 바꾸지 않습니다.

    모델 해석은 이상 징후(경고·심각)가 있을 때만 요청합니다. 질문이 원인을 물었는데 이상 징후가
    없으면, 원인 후보가 빠진 이유를 한계에 적습니다(추측성 원인을 만들지 않음).
    """
    if explainer is None:
        return result
    if not explainer.needed(result):
        if asks_cause(ctx.question):
            return result.model_copy(
                update={"limitations": (*result.limitations, NO_ANOMALY_NO_HYPOTHESIS)}
            )
        return result
    extra = await explainer.explain(result, ctx)
    return result.model_copy(
        update={
            "findings": result.findings + tuple(extra.hypotheses),
            "limitations": result.limitations + tuple(extra.limitations),
            "next_checks": result.next_checks + tuple(extra.next_checks),
            "usage": result.usage.model_copy(update={"llm_calls": extra.llm_calls}),
            "rejected_hypotheses": tuple(extra.rejected),
        }
    )
