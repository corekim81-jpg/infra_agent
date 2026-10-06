"""데이터 접근 인터페이스 교체 테스트.

HTTP를 전혀 쓰지 않는 구현(메모리 안의 가상 값)을 조회 도구에 넣어, 조회 도구와 에이전트가
구체 클라이언트가 아니라 인터페이스(`datasources.base`)에만 의존함을 확인합니다. MCP 등 다른
데이터 접근 경로를 같은 자리에 넣을 수 있다는 뜻입니다. 모든 값은 가상입니다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from infra_agent.agents.server import ServerAgent
from infra_agent.catalog import load_catalog
from infra_agent.config.settings import AnalysisConfig
from infra_agent.datasources import DataSourceTimeoutError, MetricsSource
from infra_agent.datasources.prometheus import InstantResult, RangeSeries, Sample
from infra_agent.schemas import (
    AgentName,
    AgentStatus,
    AgentTask,
    AnalysisContext,
    Budget,
    Intent,
    Severity,
    TimeRange,
)
from infra_agent.tools import CatalogQueryTool, ToolBudget

ROOT = Path(__file__).resolve().parents[2]
CATALOG = load_catalog(ROOT / "config/catalog/otel-demo.yaml")
NOW = datetime(2026, 9, 29, 3, 0, tzinfo=UTC)
TASK = AgentTask(task_id="server-1", agent=AgentName.SERVER, objective="test")
CTX = AnalysisContext(
    request_id="r1",
    question="q",
    intent=Intent.STATUS,
    time_range=TimeRange.last(timedelta(minutes=30), NOW),
    budget=Budget(max_llm_calls=0, max_tool_calls=100, deadline=NOW),
)


class InMemoryMetrics:
    """HTTP 없이 조회식 일부 문자열로 가상 값을 돌려주는 지표 소스."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.queries: list[str] = []

    async def query(self, expr: str, time: datetime | None = None) -> InstantResult:
        self.queries.append(expr)
        if self.fail:
            raise DataSourceTimeoutError("in-memory", "가상 시간 초과")
        if expr.startswith("time() - max(timestamp("):
            return InstantResult("vector", [Sample({}, 10.0, NOW.timestamp())])
        if "k8s_node_cpu_usage" in expr and "allocatable" in expr:
            return InstantResult(
                "vector", [Sample({"k8s_node_name": "mem-node-1"}, 0.95, NOW.timestamp())]
            )
        return InstantResult("vector", [])

    async def query_range(
        self, expr: str, start: datetime, end: datetime, step_seconds: int
    ) -> list[RangeSeries]:
        self.queries.append(expr)
        return []

    async def scalar_or_none(self, expr: str, time: datetime | None = None) -> float | None:
        result = await self.query(expr, time)
        return result.samples[0].value if result.samples else None


def _tool(source: MetricsSource) -> CatalogQueryTool:
    return CatalogQueryTool(
        CATALOG, source, agent=AgentName.SERVER, budget=ToolBudget(100), timeout_seconds=5
    )


async def test_agent_runs_on_non_http_metrics_source() -> None:
    source = InMemoryMetrics()
    result = await ServerAgent(_tool(source), AnalysisConfig()).run(TASK, CTX, {})
    assert result.status is AgentStatus.SUCCESS and source.queries
    critical = [f for f in result.findings if f.severity is Severity.CRITICAL]
    assert len(critical) == 1 and "mem-node-1 95.0%" in critical[0].statement
    assert critical[0].evidence_ids == ("node.cpu_utilization@current",)


async def test_source_errors_become_failed_evidence_not_empty_results() -> None:
    """구현이 던진 오류는 '결과 없음'이 아니라 조회 실패로 드러나야 합니다."""
    result = await ServerAgent(_tool(InMemoryMetrics(fail=True)), AnalysisConfig()).run(
        TASK, CTX, {}
    )
    assert result.status is AgentStatus.FAILED and not result.findings
    assert any("조회 실패로 확인하지 못함" in x for x in result.limitations)
