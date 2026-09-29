from __future__ import annotations

from datetime import UTC, datetime, timedelta

from infra_agent.llm import LLMPolicyViolationError
from infra_agent.llm.fake import FakeLLM
from infra_agent.orchestration.llm_interpret import PURPOSE, interpret_with_model
from infra_agent.schemas import Intent, TargetKind

NOW = datetime(2026, 9, 29, 3, 0, tzinfo=UTC)
D30 = timedelta(minutes=30)


def _answer(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "intent": "status",
        "duration_minutes": None,
        "namespace": None,
        "node": None,
        "pod": None,
        "domains": ["server"],
    }
    base.update(kw)
    return base


async def test_model_interpretation_used() -> None:
    fake = FakeLLM(
        {
            PURPOSE: _answer(
                intent="anomaly",
                duration_minutes=60,
                namespace="otel-demo",
                domains=["server", "db"],
            )
        }
    )
    r = await interpret_with_model(
        "otel-demo 네임스페이스에서 지난 한 시간 동안 튄 게 있나", fake, NOW, D30
    )
    assert r.intent is Intent.ANOMALY
    assert r.time_range.duration == timedelta(hours=1) and r.baseline_range is not None
    assert r.targets == {TargetKind.NAMESPACE: "otel-demo"}
    assert r.unsupported_domains == {"db"}
    assert r.assumptions[0] == "질문 해석: 모델"
    req = fake.requests[0]
    assert req.prompt.startswith("otel-demo") and req.schema is not None
    assert "<observed_data>" not in req.prompt  # 질문 해석에는 조회 데이터를 보내지 않음


async def test_invalid_target_dropped_and_overrides_win() -> None:
    fake = FakeLLM({PURPOSE: _answer(pod="장바구니 파드", node="k3d-a-0")})
    r = await interpret_with_model(
        "q", fake, NOW, D30, target_overrides={TargetKind.NODE: "k3d-b-1"}
    )
    assert r.targets == {TargetKind.NODE: "k3d-b-1"}
    assert any("이름 형식이 아닌 값 제외" in a for a in r.assumptions)


async def test_range_clamped() -> None:
    fake = FakeLLM({PURPOSE: _answer(duration_minutes=43200)})
    r = await interpret_with_model("q", fake, NOW, D30)
    assert r.time_range.duration == timedelta(days=7)


async def test_fallback_to_rules_on_error_or_bad_output() -> None:
    q = "최근 30분 동안 CPU나 메모리가 비정상적으로 증가한 서버가 있어?"
    fake = FakeLLM({PURPOSE: LLMPolicyViolationError("모델이 도구(Bash) 사용을 시도")})
    r = await interpret_with_model(q, fake, NOW, D30)
    assert r.intent is Intent.ANOMALY
    assert r.assumptions[0].startswith("모델 해석 실패(모델이 도구(Bash)")
    bad = FakeLLM({PURPOSE: {"intent": "delete_everything", "domains": []}})
    r2 = await interpret_with_model(q, bad, NOW, D30)
    assert r2.assumptions[0].startswith("모델 해석 실패(모델 출력 형식 오류)")
