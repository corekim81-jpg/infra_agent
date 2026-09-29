"""개발 서버에서 질문 응답 흐름 확인 (실제 데이터, 읽기 전용, 모델 호출 없음).

특정 값을 기대하지 않고, 대표 질문에 대해 조회 실패 없이 근거가 있는 답변이 만들어지는지 확인합니다.
"""

from __future__ import annotations

import os

import pytest

from infra_agent.answer.render import render_text
from infra_agent.catalog import load_catalog
from infra_agent.config import Settings, load_settings
from infra_agent.orchestration.runner import answer_question
from infra_agent.schemas import AgentStatus, Intent

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def settings() -> Settings:
    if not os.environ.get("INFRA_AGENT_CONFIG"):
        pytest.skip("INFRA_AGENT_CONFIG가 설정되지 않았습니다")
    s = load_settings()
    if not s.datasources.prometheus.enabled or not s.catalog.path:
        pytest.skip("Prometheus 또는 catalog.path가 설정되지 않았습니다")
    return s


@pytest.mark.parametrize(
    ("question", "intent"),
    [
        ("현재 서버 상태가 어때?", Intent.STATUS),
        ("최근 30분 동안 CPU나 메모리가 비정상적으로 증가한 서버가 있어?", Intent.ANOMALY),
    ],
)
async def test_server_questions(settings: Settings, question: str, intent: Intent) -> None:
    assert settings.catalog.path is not None
    # 모델 설정과 무관하게 코드 판정 경로만 확인 (모델 경로는 test_live_llm.py)
    bundle = await answer_question(
        question, settings, load_catalog(settings.catalog.path), use_llm=False
    )
    assert bundle.context.intent is intent
    assert len(bundle.results) == 1
    result = bundle.results[0]
    assert result.status is AgentStatus.SUCCESS, [e.message for e in result.errors]
    assert result.evidence and bundle.answer.facts
    text = render_text(bundle)
    assert "[근거]" in text and "모델 호출 없음" in text
    assert "(에이전트 실행: server 성공 " in text
    print("\n" + text)  # pytest -s 로 실제 답변 확인
