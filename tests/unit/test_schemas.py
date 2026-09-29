"""공통 스키마 검증 테스트. 모든 데이터는 가상 값입니다."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from infra_agent.schemas import (
    AgentName,
    AgentResult,
    AgentStatus,
    AgentTask,
    AnalysisContext,
    Budget,
    Confidence,
    DataSourceKind,
    ErrorInfo,
    FinalAnswer,
    Finding,
    FindingKind,
    Intent,
    JudgementBasis,
    TargetKind,
    TargetRef,
    TimeRange,
    ToolResult,
    ToolStatus,
)

NOW = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)
LAST_30M = TimeRange.last(timedelta(minutes=30), NOW)


def _budget() -> Budget:
    return Budget(max_llm_calls=4, max_tool_calls=20, deadline=NOW + timedelta(minutes=2))


def _evidence(eid: str = "ev-1") -> ToolResult:
    return ToolResult(
        evidence_id=eid,
        source=DataSourceKind.PROMETHEUS,
        query="k8s_node_cpu_usage",
        time_range=LAST_30M,
        status=ToolStatus.OK,
        data={"series": 3},
        fetched_at=NOW,
        synthetic=True,
    )


def _fact(eid: str = "ev-1") -> Finding:
    return Finding(
        kind=FindingKind.FACT,
        statement="노드 CPU 사용량이 임계값을 넘었습니다",
        evidence_ids=(eid,),
        basis=JudgementBasis.THRESHOLD,
    )


# ---------------------------------------------------------------- TimeRange


def test_time_range_last_and_previous() -> None:
    assert LAST_30M.duration == timedelta(minutes=30)
    prev = LAST_30M.previous()
    assert prev.end == LAST_30M.start
    assert prev.duration == LAST_30M.duration


def test_time_range_order() -> None:
    with pytest.raises(ValidationError, match="start"):
        TimeRange(start=NOW, end=NOW)


def test_time_range_rejects_naive() -> None:
    with pytest.raises(ValidationError):
        TimeRange(start=datetime(2026, 9, 28, 8, 0), end=NOW)


# ---------------------------------------------------------------- AnalysisContext


def test_analysis_context_compare_requires_baseline() -> None:
    with pytest.raises(ValidationError, match="baseline_range"):
        AnalysisContext(
            request_id="r1",
            question="직전 30분과 비교해줘",
            intent=Intent.COMPARE,
            time_range=LAST_30M,
            budget=_budget(),
        )
    ctx = AnalysisContext(
        request_id="r1",
        question="직전 30분과 비교해줘",
        intent=Intent.COMPARE,
        time_range=LAST_30M,
        baseline_range=LAST_30M.previous(),
        targets=(TargetRef(kind=TargetKind.NAMESPACE, name="otel-demo"),),
        budget=_budget(),
    )
    assert ctx.baseline_range is not None


def test_analysis_context_is_frozen() -> None:
    ctx = AnalysisContext(
        request_id="r1",
        question="현재 서버 상태가 어때?",
        intent=Intent.STATUS,
        time_range=LAST_30M,
        budget=_budget(),
    )
    with pytest.raises(ValidationError):
        ctx.question = "변경"  # type: ignore[misc]


def test_task_cannot_depend_on_itself() -> None:
    with pytest.raises(ValidationError):
        AgentTask(task_id="t1", agent=AgentName.DB, objective="x", depends_on=("t1",))


# ---------------------------------------------------------------- Finding


def test_correlation_cannot_be_fact() -> None:
    with pytest.raises(ValidationError, match="correlation"):
        Finding(
            kind=FindingKind.FACT,
            statement="동시에 증가했으므로 DB가 원인입니다",
            evidence_ids=("ev-1",),
            basis=JudgementBasis.CORRELATION,
        )


def test_hypothesis_requires_confidence() -> None:
    with pytest.raises(ValidationError, match="confidence"):
        Finding(
            kind=FindingKind.HYPOTHESIS,
            statement="DB 커넥션 대기가 지연의 원인 후보입니다",
            evidence_ids=("ev-1",),
            basis=JudgementBasis.CORRELATION,
        )
    hyp = Finding(
        kind=FindingKind.HYPOTHESIS,
        statement="DB 커넥션 대기가 지연의 원인 후보입니다",
        evidence_ids=("ev-1",),
        basis=JudgementBasis.CORRELATION,
        confidence=Confidence.LOW,
    )
    assert hyp.confidence is Confidence.LOW


def test_finding_requires_evidence() -> None:
    with pytest.raises(ValidationError):
        Finding(
            kind=FindingKind.FACT,
            statement="근거 없음",
            evidence_ids=(),
            basis=JudgementBasis.STATE,
        )


# ---------------------------------------------------------------- AgentResult


def test_agent_result_valid() -> None:
    result = AgentResult(
        task_id="t1",
        agent=AgentName.SERVER,
        status=AgentStatus.SUCCESS,
        findings=(_fact(),),
        evidence=(_evidence(),),
    )
    assert result.findings[0].evidence_ids == ("ev-1",)


def test_agent_result_rejects_unknown_evidence() -> None:
    with pytest.raises(ValidationError, match="ev-missing"):
        AgentResult(
            task_id="t1",
            agent=AgentName.SERVER,
            status=AgentStatus.SUCCESS,
            findings=(_fact("ev-missing"),),
            evidence=(_evidence(),),
        )


def test_agent_result_rejects_duplicate_evidence() -> None:
    with pytest.raises(ValidationError, match="중복"):
        AgentResult(
            task_id="t1",
            agent=AgentName.SERVER,
            status=AgentStatus.SUCCESS,
            evidence=(_evidence(), _evidence()),
        )


def test_failed_result_needs_error_and_no_findings() -> None:
    with pytest.raises(ValidationError):
        AgentResult(task_id="t1", agent=AgentName.DB, status=AgentStatus.FAILED)
    with pytest.raises(ValidationError):
        AgentResult(
            task_id="t1",
            agent=AgentName.DB,
            status=AgentStatus.FAILED,
            findings=(_fact(),),
            evidence=(_evidence(),),
            errors=(ErrorInfo(code="timeout", message="시간 초과"),),
        )
    ok = AgentResult(
        task_id="t1",
        agent=AgentName.DB,
        status=AgentStatus.FAILED,
        errors=(ErrorInfo(code="timeout", message="시간 초과"),),
    )
    assert ok.status is AgentStatus.FAILED


def test_partial_result_needs_limitations_or_errors() -> None:
    with pytest.raises(ValidationError):
        AgentResult(task_id="t1", agent=AgentName.NETWORK, status=AgentStatus.PARTIAL)
    res = AgentResult(
        task_id="t1",
        agent=AgentName.NETWORK,
        status=AgentStatus.PARTIAL,
        limitations=("hubble_* 지표 없음: 드롭 분석 확인 불가",),
    )
    assert res.limitations


# ---------------------------------------------------------------- FinalAnswer


def test_final_answer_separates_facts_and_hypotheses() -> None:
    with pytest.raises(ValidationError):
        FinalAnswer(request_id="r1", summary="s", time_range=LAST_30M, hypotheses=(_fact(),))
    answer = FinalAnswer(request_id="r1", summary="s", time_range=LAST_30M, facts=(_fact(),))
    assert answer.facts
