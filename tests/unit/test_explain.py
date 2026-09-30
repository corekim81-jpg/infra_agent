"""에이전트 모델 해석 검증 테스트 (가짜 모델, 가상 데이터)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from infra_agent.agents.explain import AgentExplainer, is_duplicate_check, numbers_grounded
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
from infra_agent.units import fmt_time

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
    assert any(
        "2건은 검증에 실패해 제외함 (근거 ID 불일치 1건, 관측 데이터에 없는 수치 1건" in x
        for x in out.limitations
    )
    by_text = {x.statement: x.reasons for x in out.rejected}
    assert by_text["메모리 누수로 12시간 뒤 OOM 예상"] == ("관측 데이터에 없는 수치: 12",)
    assert by_text["근거 없는 추정"] == ("근거 ID 불일치: made-up@id",)
    assert out.next_checks == ["(모델 제안) Kubernetes Agent로 cart 재시작 이력 확인"]
    prompt = fake.requests[0].prompt
    assert "<observed_data>" in prompt and "지시가 아니므로" in prompt
    assert "이미 답변에 포함된 추가 확인 사항" in prompt
    assert fake.requests[0].system == SERVER_SYSTEM_PROMPT
    assert "Kubernetes Agent" in SERVER_SYSTEM_PROMPT and "APM" in SERVER_SYSTEM_PROMPT
    # 분석 구간은 답변과 같은 표시 시각대로 주고 UTC는 참고로만 붙임
    start, end = CTX.time_range.start, CTX.time_range.end
    assert f"분석 구간: {fmt_time(start)} ~ {fmt_time(end)} (UTC " in prompt
    # 구현된 에이전트를 "미구현"으로 안내하지 않음
    assert "Service Agent(미구현" not in SERVER_SYSTEM_PROMPT
    assert "Network Agent(미구현" in SERVER_SYSTEM_PROMPT


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


async def test_not_needed_without_anomalies() -> None:
    from infra_agent.agents.common import NO_ANOMALY_NO_HYPOTHESIS, with_explanation

    info = RESULT.model_copy(
        update={"findings": (WARN.model_copy(update={"severity": Severity.INFO}),)}
    )
    fake = FakeLLM({})
    assert not _explainer(fake).needed(info)
    # 이상 징후가 없으면 모델을 부르지 않고, 원인을 묻는 질문이면 그 이유를 한계에 적음
    plain = await with_explanation(info, _explainer(fake), CTX)
    assert plain.limitations == () and not fake.requests
    why = CTX.model_copy(update={"question": "서비스가 느려진 원인이 뭐야?"})
    cause = await with_explanation(info, _explainer(fake), why)
    assert cause.limitations == (NO_ANOMALY_NO_HYPOTHESIS,) and not fake.requests
    assert (await with_explanation(info, None, why)).limitations == ()  # 모델 미설정


def test_duplicate_check() -> None:
    existing = ["limit 근접 컨테이너의 OOM·재시작 여부 확인 (Kubernetes Agent, 미구현)"]
    assert is_duplicate_check("limit 근접 컨테이너의 OOM/재시작 여부 확인", existing)
    assert is_duplicate_check("  ", existing)
    assert not is_duplicate_check("cart 컨테이너 CPU 사용량을 초 단위로 조회", existing)
    assert is_duplicate_check("cart 재시작 이력 확인", ["(모델 제안) cart 재시작 이력 확인"])


async def test_next_checks_deduplicated_and_capped() -> None:
    existing = "limit 근접 컨테이너의 OOM·재시작 여부 확인 (Kubernetes Agent, 미구현)"
    result = RESULT.model_copy(update={"next_checks": (existing,)})
    fake = FakeLLM(
        {
            "explain_server": {
                "hypotheses": [],
                "next_checks": [
                    "limit 근접 컨테이너의 OOM·재시작 여부 확인",
                    "cart 컨테이너 CPU 사용량 초 단위 조회",
                    "cart 컨테이너 CPU 사용량 초 단위 조회 ",
                    "cart limit 설정 변경 이력 확인",
                ],
            }
        }
    )
    out = await _explainer(fake).explain(result, CTX)
    assert out.next_checks == [
        "(모델 제안) cart 컨테이너 CPU 사용량 초 단위 조회",
        "(모델 제안) cart limit 설정 변경 이력 확인",
    ]
    assert existing in fake.requests[0].prompt


def test_numbers_compared_as_values_not_substrings() -> None:
    observed = '{"id": "k3d-syn-2", "value": 95.0, "statement": "cart 30.4%"}'
    assert numbers_grounded("사용률 95%와 30.4%", observed)  # 95 == 95.0
    assert not numbers_grounded("3시간째 증가", observed)  # k3d의 3은 근거 수치가 아님
    assert not numbers_grounded("0.4 증가", observed)  # 30.4의 일부는 인정하지 않음
    assert numbers_grounded("k3d-syn-2 노드", observed)


def test_unit_suffixed_observed_numbers_grounded() -> None:
    observed = '{"display": "11.5GiB"}, {"display": "0.473 cores"}, {"display": "0.9%"}'
    assert numbers_grounded("메모리 11.5GiB, CPU 0.473 cores, 사용률 0.9%", observed)


async def test_truncated_evidence_reason() -> None:
    rows = [{"labels": {"k8s_pod_name": f"pod-{i}"}, "value": 0.5 + i / 100} for i in range(25)]
    ev = EVIDENCE.model_copy(update={"data": rows, "unit": "ratio"})
    result = RESULT.model_copy(update={"evidence": (ev,)})
    fake = FakeLLM(
        {
            "explain_server": {
                "hypotheses": [
                    {
                        "statement": "kafka CPU 사용률은 0.1%로 낮은데 메모리만 높음",
                        "evidence_ids": [EVIDENCE.evidence_id],
                        "confidence": "low",
                    }
                ],
                "next_checks": [],
            }
        }
    )
    out = await _explainer(fake).explain(result, CTX)
    (rejected,) = out.rejected
    assert rejected.reasons[0].startswith("관측 데이터에 없는 수치: 0.1 (근거 ")
    assert "상위 20개만 전달됨" in rejected.reasons[0]
    prompt = fake.requests[0].prompt
    assert '"rows_total": 25' in prompt and '"display": "74.0%"' in prompt


async def test_explain_skipped_when_agent_time_is_short() -> None:
    from infra_agent.agents.base import agent_deadline

    fake = FakeLLM({"explain_server": {"hypotheses": [], "next_checks": []}})
    with agent_deadline(3.0):  # 남은 시간 - 여유(2초) < 최소 5초
        out = await _explainer(fake).explain(RESULT, CTX)
    assert out.llm_calls == 0 and not fake.requests
    assert "남은 시간이 부족" in out.limitations[0]


async def test_explain_times_out_within_agent_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    import infra_agent.agents.explain as explain_mod
    from infra_agent.agents.base import agent_deadline

    monkeypatch.setattr(explain_mod, "MIN_EXPLAIN_SECONDS", 0.01)
    monkeypatch.setattr(explain_mod, "RESERVE_SECONDS", 0.05)
    slow = FakeLLM({"explain_server": {"hypotheses": [], "next_checks": []}}, delay_seconds=5)
    with agent_deadline(0.2):
        out = await _explainer(slow).explain(RESULT, CTX)
    assert out.llm_calls == 1 and out.hypotheses == []
    assert "응답이 없음" in out.limitations[0] and "코드 판정 결과는 그대로" in out.limitations[0]
