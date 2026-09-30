"""개발 서버에서 Kubernetes Agent 확인 (실제 데이터, 읽기 전용, 모델 호출 없음).

특정 값을 기대하지 않고, 조회 실패 없이 근거 있는 판정(문제 대상 또는 "해당 대상 없음")이
나오는지 확인합니다.
"""

from __future__ import annotations

import os

import pytest

from infra_agent.answer.render import render_text
from infra_agent.catalog import load_catalog
from infra_agent.config import Settings, load_settings
from infra_agent.orchestration.runner import answer_question
from infra_agent.schemas import AgentName, AgentStatus

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def settings() -> Settings:
    if not os.environ.get("INFRA_AGENT_CONFIG"):
        pytest.skip("INFRA_AGENT_CONFIG가 설정되지 않았습니다")
    s = load_settings()
    if not s.datasources.prometheus.enabled or not s.catalog.path:
        pytest.skip("Prometheus 또는 catalog.path가 설정되지 않았습니다")
    return s


async def test_kubernetes_question(settings: Settings) -> None:
    assert settings.catalog.path is not None
    bundle = await answer_question(
        "Kubernetes에서 재시작하거나 Pending 상태인 Pod를 확인해 줘",
        settings,
        load_catalog(settings.catalog.path),
        use_llm=False,
    )
    assert [r.agent for r in bundle.results] == [AgentName.KUBERNETES]
    result = bundle.results[0]
    assert result.status is AgentStatus.SUCCESS, [e.message for e in result.errors]
    assert result.evidence and bundle.answer.facts
    text = render_text(bundle, show_queries=True)
    assert "(에이전트 실행: kubernetes 성공 " in text
    print("\n" + text)


async def test_server_and_kubernetes_together(settings: Settings) -> None:
    assert settings.catalog.path is not None
    bundle = await answer_question(
        "서버 자원이랑 쿠버네티스 상태 알려줘",
        settings,
        load_catalog(settings.catalog.path),
        use_llm=False,
    )
    assert [r.agent for r in bundle.results] == [AgentName.SERVER, AgentName.KUBERNETES]
    assert all(r.status is AgentStatus.SUCCESS for r in bundle.results), [
        e.message for r in bundle.results for e in r.errors
    ]
    print("\n" + render_text(bundle))
