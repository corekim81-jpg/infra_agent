"""개발 서버에서 Service Agent·Loki·Tempo 조회 확인 (실제 데이터, 읽기 전용, 모델 호출 없음).

특정 값을 기대하지 않고, 카탈로그 Loki 조회식과 TraceQL이 오류 없이 실행되는지와
Service 질문이 근거 있는 답변을 만드는지 확인합니다. 결과 원문은 저장소에 남기지 않습니다.
"""

from __future__ import annotations

import os

import pytest

from infra_agent.answer.render import render_text
from infra_agent.catalog import load_catalog
from infra_agent.config import Settings, load_settings
from infra_agent.datasources import LokiClient, TempoClient
from infra_agent.orchestration.runner import answer_question
from infra_agent.schemas import AgentName, AgentStatus, TimeRange, ToolStatus
from infra_agent.timeutil import parse_duration, utc_now
from infra_agent.tools import LogQueryTool, ToolBudget, TraceSearchTool

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def settings() -> Settings:
    if not os.environ.get("INFRA_AGENT_CONFIG"):
        pytest.skip("INFRA_AGENT_CONFIG가 설정되지 않았습니다")
    s = load_settings()
    if not s.datasources.prometheus.enabled or not s.catalog.path:
        pytest.skip("Prometheus 또는 catalog.path가 설정되지 않았습니다")
    return s


async def test_loki_catalog_queries_run(settings: Settings) -> None:
    if not settings.datasources.loki.enabled or not settings.catalog.path:
        pytest.skip("Loki가 설정되지 않았습니다")
    catalog = load_catalog(settings.catalog.path)
    window = TimeRange.last(parse_duration("30m"), utc_now())
    async with LokiClient.from_config(settings.datasources.loki) as loki:
        tool = LogQueryTool(
            catalog, loki, agent=AgentName.SERVICE, budget=ToolBudget(10), timeout_seconds=20
        )
        total = await tool.count("log.lines_total", window, {}, "total")
        errors = await tool.count("log.error_lines", window, {}, "errors")
        samples = await tool.samples("log.error_samples", window, {}, "samples", 3)
    for outcome in (total, errors, samples):
        assert outcome.result.status in (ToolStatus.OK, ToolStatus.EMPTY), outcome.result.error
    assert total.result.status is ToolStatus.OK  # 개발 서버에는 최근 로그가 있어야 함
    with_trace = [r for r in samples.result.data or [] if r.get("trace_id")]
    print(
        f"\n로그 서비스 {len(total.result.data or [])}개, 오류 키워드 로그 서비스 "
        f"{len(errors.result.data or [])}개, 샘플 {len(samples.result.data or [])}건 "
        f"(trace_id 있음 {len(with_trace)}건)"
    )


async def test_tempo_error_search_runs(settings: Settings) -> None:
    if not settings.datasources.tempo.enabled:
        pytest.skip("Tempo가 설정되지 않았습니다")
    window = TimeRange.last(parse_duration("30m"), utc_now())
    async with TempoClient.from_config(settings.datasources.tempo) as tempo:
        tool = TraceSearchTool(
            tempo, agent=AgentName.SERVICE, budget=ToolBudget(5), timeout_seconds=20
        )
        outcome = await tool.search("errors", window, service=None, errors=True, limit=5)
    assert outcome.result.status in (ToolStatus.OK, ToolStatus.EMPTY), outcome.result.error
    rows = outcome.result.data or []
    # Tempo의 앞자리 0 생략 형식을 로그와 같은 32자리로 맞췄는지 확인
    assert all(len(str(r["trace_id"])) == 32 for r in rows)
    print(f"\n오류 트레이스 {len(rows)}건")


@pytest.mark.parametrize(
    "question",
    [
        "서비스 응답이 느려진 이유가 네트워크인지 DB인지 분석해 줘",
        "오류가 증가한 시간대의 로그와 트레이스를 연결해서 원인 후보를 알려줘",
    ],
)
async def test_service_questions(settings: Settings, question: str) -> None:
    assert settings.catalog.path is not None
    bundle = await answer_question(
        question, settings, load_catalog(settings.catalog.path), use_llm=False
    )
    service = [r for r in bundle.results if r.agent is AgentName.SERVICE]
    assert service, [r.agent for r in bundle.results]
    result = service[0]
    assert result.status is AgentStatus.SUCCESS, [e.message for e in result.errors]
    assert result.evidence and bundle.answer.facts
    print("\n" + render_text(bundle, show_queries=True))
