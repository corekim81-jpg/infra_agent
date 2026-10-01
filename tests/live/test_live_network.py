"""개발 서버에서 Network Agent 확인 (실제 데이터, 읽기 전용, 모델 호출 없음).

특정 값을 기대하지 않고, 네트워크 카탈로그 조회식이 오류 없이 실행되고 근거 있는 판정이 나오는지
확인합니다. 결과 원문은 저장소에 남기지 않습니다.
"""

from __future__ import annotations

import os
from datetime import timedelta

import pytest

from infra_agent.answer.render import render_text
from infra_agent.catalog import load_catalog
from infra_agent.config import Settings, load_settings
from infra_agent.datasources import PrometheusClient
from infra_agent.orchestration.runner import answer_question
from infra_agent.schemas import (
    AgentName,
    AgentStatus,
    AnalysisContext,
    Budget,
    Intent,
    TimeRange,
)
from infra_agent.timeutil import utc_now
from infra_agent.tools import CatalogQueryTool, QueryMode, ToolBudget, value_rows

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def settings() -> Settings:
    if not os.environ.get("INFRA_AGENT_CONFIG"):
        pytest.skip("INFRA_AGENT_CONFIG가 설정되지 않았습니다")
    s = load_settings()
    if not s.datasources.prometheus.enabled or not s.catalog.path:
        pytest.skip("Prometheus 또는 catalog.path가 설정되지 않았습니다")
    return s


async def test_network_question(settings: Settings) -> None:
    assert settings.catalog.path is not None
    bundle = await answer_question(
        "네트워크 패킷 드롭이나 DNS 문제가 있어?",
        settings,
        load_catalog(settings.catalog.path),
        use_llm=False,
    )
    assert [r.agent for r in bundle.results] == [AgentName.NETWORK]
    result = bundle.results[0]
    assert result.status is AgentStatus.SUCCESS, [e.message for e in result.errors]
    assert result.evidence and bundle.answer.facts
    empty = [x for x in result.limitations if "결과가 없어" in x]
    print(f"\n결과가 없는 항목 {len(empty)}개: {empty}")
    print("\n" + render_text(bundle, show_queries=True))


async def test_service_then_network_and_db(settings: Settings) -> None:
    assert settings.catalog.path is not None
    bundle = await answer_question(
        "서비스 응답이 느려진 이유가 네트워크인지 DB인지 분석해 줘",
        settings,
        load_catalog(settings.catalog.path),
        use_llm=False,
    )
    agents = [r.agent for r in bundle.results]
    assert agents == [AgentName.SERVICE, AgentName.NETWORK, AgentName.DB]
    service, network, db = bundle.results
    assert service.status in (AgentStatus.SUCCESS, AgentStatus.PARTIAL), service.errors
    assert network.status is AgentStatus.SUCCESS, [e.message for e in network.errors]
    assert db.status is AgentStatus.SUCCESS, [e.message for e in db.errors]
    assert bundle.answer.unverified_areas == ()
    # 분야 간 교차 확인(#29): Service와 다른 분야가 함께 있으므로 항상 표시
    assert bundle.answer.cross_checks
    focus = [f.statement for f in network.findings if f.statement.startswith("Service 이상 대상")]
    print(f"\nNetwork 집중 확인 {len(focus)}건, 교차 원인 후보 {len(bundle.answer.correlations)}건")
    print("\n" + render_text(bundle))


async def test_workload_flow_labels(settings: Settings) -> None:
    """방향별 워크로드 흐름 조회의 결과 수와 워크로드 라벨 존재를 출력합니다 (caveat 확인용)."""
    assert settings.catalog.path is not None
    catalog = load_catalog(settings.catalog.path)
    async with PrometheusClient.from_config(settings.datasources.prometheus) as prom:
        tool = CatalogQueryTool(
            catalog, prom, agent=AgentName.NETWORK, budget=ToolBudget(10), timeout_seconds=20
        )
        now = utc_now()
        ctx = AnalysisContext(
            request_id="live",
            question="workload flows",
            intent=Intent.STATUS,
            time_range=TimeRange.last(timedelta(minutes=30), now),
            budget=Budget(max_llm_calls=0, max_tool_calls=10, deadline=now + timedelta(minutes=1)),
        )
        for key, label in (
            ("network.workload_egress_by_verdict", "source_workload"),
            ("network.workload_ingress_by_verdict", "destination_workload"),
        ):
            outcome = await tool.query(key, ctx, QueryMode.CURRENT)
            result = outcome.result
            assert result.status.value in ("ok", "empty"), result.error
            rows = value_rows(result)
            labeled = {labels[label] for labels, _ in rows if labels.get(label)}
            verdicts = sorted({labels.get("verdict", "") for labels, _ in rows})
            print(
                f"\n{key}: 결과 {len(rows)}개, {label} 값 {len(labeled)}개 "
                f"(예: {sorted(labeled)[:8]}), 판정 값 {verdicts}"
            )
