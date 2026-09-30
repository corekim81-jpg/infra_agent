"""개발 서버에서 DB Agent 확인 (실제 데이터, 읽기 전용, 모델 호출 없음).

특정 값을 기대하지 않고, DB 카탈로그 조회식이 오류 없이 실행되고 근거 있는 판정이 나오는지
확인합니다. 결과 원문은 저장소에 남기지 않습니다.
"""

from __future__ import annotations

import os

import pytest

from infra_agent.answer.render import render_text
from infra_agent.catalog import load_catalog
from infra_agent.config import Settings, load_settings
from infra_agent.datasources import PrometheusClient
from infra_agent.orchestration.runner import answer_question
from infra_agent.schemas import AgentName, AgentStatus
from infra_agent.timeutil import utc_now
from infra_agent.tools import CatalogQueryTool, ToolBudget

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def settings() -> Settings:
    if not os.environ.get("INFRA_AGENT_CONFIG"):
        pytest.skip("INFRA_AGENT_CONFIG가 설정되지 않았습니다")
    s = load_settings()
    if not s.datasources.prometheus.enabled or not s.catalog.path:
        pytest.skip("Prometheus 또는 catalog.path가 설정되지 않았습니다")
    return s


async def test_db_question(settings: Settings) -> None:
    assert settings.catalog.path is not None
    bundle = await answer_question(
        "DB 커넥션 풀이 부족하거나 쿼리가 느려진 징후가 있어?",
        settings,
        load_catalog(settings.catalog.path),
        use_llm=False,
    )
    assert [r.agent for r in bundle.results] == [AgentName.DB]
    result = bundle.results[0]
    assert result.status is AgentStatus.SUCCESS, [e.message for e in result.errors]
    assert result.evidence and bundle.answer.facts
    empty = [x for x in result.limitations if "결과가 없어" in x]
    print(f"\n결과가 없는 항목 {len(empty)}개: {empty}")
    print("\n" + render_text(bundle, show_queries=True))


async def test_service_then_db(settings: Settings) -> None:
    assert settings.catalog.path is not None
    bundle = await answer_question(
        "서비스 응답이 느려진 이유가 네트워크인지 DB인지 분석해 줘",
        settings,
        load_catalog(settings.catalog.path),
        use_llm=False,
    )
    assert [r.agent for r in bundle.results] == [AgentName.SERVICE, AgentName.DB]
    service, db = bundle.results
    # Service는 Loki·Tempo 일부 실패로 PARTIAL일 수 있음. DB는 Service가 끝난 뒤 실행되어야 함
    assert service.status in (AgentStatus.SUCCESS, AgentStatus.PARTIAL), service.errors
    assert db.status is AgentStatus.SUCCESS, [e.message for e in db.errors]
    print("\n" + render_text(bundle))


async def test_db_latency_bucket_bounds(settings: Settings) -> None:
    """DB 지연 히스토그램의 서비스별 버킷 경계를 출력합니다 (첫 구간 보간값 판단의 근거 확인용)."""
    assert settings.catalog.path is not None
    catalog = load_catalog(settings.catalog.path)
    async with PrometheusClient.from_config(settings.datasources.prometheus) as prom:
        tool = CatalogQueryTool(
            catalog, prom, agent=AgentName.DB, budget=ToolBudget(10), timeout_seconds=20
        )
        for key, label in (
            ("db.client_operation_latency_p95", "service_name"),
            ("db.span_latency_p95", "service"),
        ):
            groups = await tool.bucket_bounds_by(key, {}, utc_now(), label)
            assert groups is not None, key
            for name, bounds in sorted(groups.items()):
                print(f"\n{key} [{name}] 경계 {len(bounds)}개: {bounds[:4]} ... {bounds[-1]}")
