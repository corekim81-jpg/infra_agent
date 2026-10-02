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


@pytest.fixture(scope="module")
def api_settings(settings: Settings) -> Settings:
    if not settings.datasources.kubernetes.enabled:
        pytest.skip("datasources.kubernetes.enabled=false (전용 읽기 계정 kubeconfig 미설정)")
    return settings


async def test_kubernetes_api_check(api_settings: Settings) -> None:
    """전용 읽기 계정으로 버전·권한 점검이 통과하는지 (쓰기·secrets 권한이 없어야 함)."""
    from infra_agent.datasources.probe import check_sources

    status = next(s for s in await check_sources(api_settings) if s.name == "kubernetes")
    assert status.reachable, f"[{status.error_code}] {status.error}"
    assert status.ready, f"[{status.error_code}] {status.error}"
    print(f"\nKubernetes {status.version}, 참고: {list(status.notes)}")


async def test_kubernetes_question_with_api(api_settings: Settings) -> None:
    """API 상세(Pod 상태·Warning 이벤트) 근거가 조회 실패 없이 붙는지."""
    assert api_settings.catalog.path is not None
    bundle = await answer_question(
        "Kubernetes에서 재시작하거나 Pending 상태인 Pod를 확인해 줘",
        api_settings,
        load_catalog(api_settings.catalog.path),
        use_llm=False,
    )
    result = bundle.results[0]
    assert result.status is AgentStatus.SUCCESS, [e.message for e in result.errors]
    by_id = {e.evidence_id: e for e in result.evidence}
    for key in ("k8s_api.permissions@current", "k8s_api.pods@current", "k8s_api.events@window"):
        assert key in by_id, (key, result.limitations)
        assert by_id[key].status.value in ("ok", "empty", "truncated"), by_id[key].error
    assert any("Kubernetes API" in f.statement for f in result.findings)
    print("\n" + render_text(bundle, show_queries=True))
