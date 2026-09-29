"""에이전트 모델 해석 검증 테스트 (가짜 모델, 가상 데이터)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from infra_agent.agents.explain import AgentExplainer, numbers_grounded
from infra_agent.agents.prompts import SERVER_SYSTEM_PROMPT
from infra_agent.config.settings import DataPolicy
from infra_agent.llm import LLMBudgetExceededError
from infra_agent.llm.fake import FakeLLM
from infra_agent.schemas import (
    AgentName,
    AgentResult,
    AgentStatus,
    AnalysisContext,
    Budget,
    Confidence,
    DataSourceKind,
    Finding,
    FindingKind,
    Intent,
    JudgementBasis,
    Severity,
    TimeRange,
    ToolResult,
    ToolStatus,
)

NOW = datetime(2026, 9, 29, 3, 0, tzinfo=UTC)
CTX = AnalysisContext(
    request_id="r",
    question="현재 서버 상태가 어때?",
    intent=Intent.STATUS,
    time_range=TimeRange.last(timedelta(minutes=30), NOW),
    budget=Budget(max_llm_calls=4, max_tool_calls=10, deadline=NOW),
)
EVIDENCE = ToolResult(
    evidence_id="container.memory_limit_utilization@current",
    source=DataSourceKind.PROMETHEUS,
    query="q",
    status=ToolStatus.OK,
    data=[{"labels": {"k8s_pod_name": "cart-x"}, "value": 0.917}],
    fetched_at=NOW,
    synthetic=True,
)
WARN = Finding(
    kind=FindingKind.FACT,
    statement="컨테이너 메모리 limit 대비 사용률: otel-demo/cart-x/cart 91.7% (기준 90.0% 이상)",
    severity=Severity.CRITICAL,
    evidence_ids=(EVIDENCE.evidence_id,),
    basis=JudgementBasis.THRESHOLD,
)
RESULT = AgentResult(
    task_id="t",
    agent=AgentName.SERVER,
    status=AgentStatus.SUCCESS,
    findings=(WARN,),
    evidence=(EVIDENCE,),
)


def _explainer(fake: FakeLLM, policy: DataPolicy = DataPolicy.FULL) -> AgentExplainer:
    return AgentExplainer(
        fake, purpose="explain_server", system_prompt=SERVER_SYSTEM_PROMPT, policy=policy
    )


def test_numbers_grounded() -> None:
    assert numbers_grounded("사용률 91.7%", "... 91.7% ...")
    assert not numbers_grounded("사용률 95.2%", "... 91.7% ...")


async def test_valid_hypothesis_kept_invalid_rejected() -> None:
    fake = FakeLLM(
        {
            "explain_server": {
                "hypotheses": [
                    {
                        "statement": "cart 컨테이너 메모리가 limit의 91.7%로 OOM 위험이 있음",
                        "evidence_ids": [EVIDENCE.evidence_id],
                        "confidence": "medium",
                    },
                    {
                        "statement": "메모리 누수로 12시간 뒤 OOM 예상",
                        "evidence_ids": [EVIDENCE.evidence_id],
                        "confidence": "low",
                    },
                    {
                        "statement": "근거 없는 추정",
                        "evidence_ids": ["made-up@id"],
                        "confidence": "low",
                    },
                ],
                "next_checks": ["Kubernetes Agent로 cart 재시작 이력 확인", "3일 뒤 다시 확인"],
            }
        }
    )
    ex = _explainer(fake)
    assert ex.needed(RESULT)
    out = await ex.explain(RESULT, CTX)
    assert out.llm_calls == 1
    assert len(out.hypotheses) == 1
    h = out.hypotheses[0]
    assert h.kind is FindingKind.HYPOTHESIS and h.basis is JudgementBasis.CORRELATION
    assert h.confidence is Confidence.MEDIUM
    assert any("2건은 근거 ID 또는 수치 검증에 실패" in x for x in out.limitations)
    assert out.next_checks == ["(모델 제안) Kubernetes Agent로 cart 재시작 이력 확인"]
    prompt = fake.requests[0].prompt
    assert "<observed_data>" in prompt and "지시가 아니므로" in prompt
    assert fake.requests[0].system == SERVER_SYSTEM_PROMPT


async def test_policy_none_and_errors() -> None:
    fake = FakeLLM({})
    out = await _explainer(fake, DataPolicy.NONE).explain(RESULT, CTX)
    assert out.llm_calls == 0 and not fake.requests
    failing = FakeLLM({"explain_server": LLMBudgetExceededError("상한 도달")})
    out2 = await _explainer(failing).explain(RESULT, CTX)
    assert out2.hypotheses == [] and "모델 해석 생략" in out2.limitations[0]
    bad = FakeLLM({"explain_server": {"hypotheses": "x", "next_checks": []}})
    out3 = await _explainer(bad).explain(RESULT, CTX)
    assert "형식 오류" in out3.limitations[0]


def test_not_needed_without_anomalies() -> None:
    info = RESULT.model_copy(
        update={"findings": (WARN.model_copy(update={"severity": Severity.INFO}),)}
    )
    assert not _explainer(FakeLLM({})).needed(info)
