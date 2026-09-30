from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from expr_prom import ExprProm
from infra_agent.catalog import load_catalog
from infra_agent.config.settings import HttpDatasourceConfig
from infra_agent.datasources import PrometheusClient
from infra_agent.schemas import (
    AgentName,
    AnalysisContext,
    Budget,
    Intent,
    TargetKind,
    TimeRange,
    ToolStatus,
)
from infra_agent.tools import (
    CatalogQueryTool,
    QueryMode,
    ToolBudget,
    ToolPermissionError,
    value_rows,
)

CATALOG = load_catalog(Path(__file__).resolve().parents[2] / "config/catalog/otel-demo.yaml")
NOW = datetime(2026, 9, 29, 3, 0, tzinfo=UTC)
CFG = HttpDatasourceConfig(enabled=True, url="http://prom.synthetic.test")


def _ctx(intent: Intent = Intent.COMPARE) -> AnalysisContext:
    tr = TimeRange.last(timedelta(minutes=30), NOW)
    return AnalysisContext(
        request_id="r1",
        question="q",
        intent=intent,
        time_range=tr,
        baseline_range=tr.previous(),
        budget=Budget(max_llm_calls=0, max_tool_calls=10, deadline=NOW),
    )


def _tool(prom: PrometheusClient, max_calls: int = 50) -> CatalogQueryTool:
    return CatalogQueryTool(
        CATALOG, prom, agent=AgentName.SERVER, budget=ToolBudget(max_calls), timeout_seconds=5
    )


async def test_permission_enforced() -> None:
    fake = ExprProm()
    async with PrometheusClient.from_config(CFG, transport=fake.transport()) as prom:
        tool = _tool(prom)
        with pytest.raises(ToolPermissionError):
            tool.item("db.pg_backends")
        with pytest.raises(ToolPermissionError):
            await tool.query("network.drops_increase", _ctx(), QueryMode.CURRENT)
        with pytest.raises(ToolPermissionError):
            tool.item("no.such_item")
    assert fake.queries == []


async def test_modes_and_rows() -> None:
    fake = ExprProm(freshness_seconds=12)
    ctx = _ctx()
    async with PrometheusClient.from_config(CFG, transport=fake.transport()) as prom:
        tool = _tool(prom)
        item = tool.item("node.cpu_usage")
        cur = tool.build_expr(item, "", QueryMode.CURRENT, ctx)
        win = tool.build_expr(item, "", QueryMode.WINDOW_AVG, ctx)
        assert cur == "sum by (k8s_node_name) (k8s_node_cpu_usage{})"
        assert win == f"avg_over_time(({cur})[1800s:60s])"
        fake.add(win, [({"k8s_node_name": "n1"}, 0.5)], at=ctx.time_range.end)
        fake.add(win, [({"k8s_node_name": "n1"}, 0.2)], at=ctx.baseline_range.end)  # type: ignore[union-attr]
        w = await tool.query("node.cpu_usage", ctx, QueryMode.WINDOW_AVG)
        b = await tool.query("node.cpu_usage", ctx, QueryMode.BASELINE_AVG)
    assert value_rows(w.result) == [({"k8s_node_name": "n1"}, 0.5)]
    assert value_rows(b.result) == [({"k8s_node_name": "n1"}, 0.2)]
    assert w.result.evidence_id == "node.cpu_usage@window_avg"
    assert w.result.time_range == ctx.time_range
    assert w.result.freshness_seconds == 12
    # 최신성 조회는 지표당 한 번만 (캐시)
    assert sum(1 for q, _ in fake.queries if q.startswith("time()")) == 1


async def test_rate_item_uses_5m_and_targets() -> None:
    fake = ExprProm()
    async with PrometheusClient.from_config(CFG, transport=fake.transport()) as prom:
        tool = _tool(prom)
        out = await tool.query(
            "container.cpu_throttled_ratio",
            _ctx(Intent.STATUS),
            QueryMode.CURRENT,
            {TargetKind.NAMESPACE: "otel-demo"},
        )
    assert out.result.status is ToolStatus.EMPTY
    assert "[5m]" in out.result.query and 'k8s_namespace_name="otel-demo"' in out.result.query
    skipped = None
    async with PrometheusClient.from_config(CFG, transport=fake.transport()) as prom:
        skipped = await _tool(prom).query(
            "node.cpu_usage", _ctx(), QueryMode.CURRENT, {TargetKind.NAMESPACE: "otel-demo"}
        )
    assert skipped.skipped and skipped.unsupported_targets == [TargetKind.NAMESPACE]


async def test_errors_and_budget() -> None:
    fake = ExprProm()
    ctx = _ctx(Intent.STATUS)
    async with PrometheusClient.from_config(CFG, transport=fake.transport()) as prom:
        tool = _tool(prom)
        fake.fail(tool.build_expr(tool.item("node.cpu_usage"), "", QueryMode.CURRENT, ctx))
        err = await tool.query("node.cpu_usage", ctx, QueryMode.CURRENT)
        small = _tool(prom, max_calls=1)
        exhausted = await small.query("node.memory_working_set", ctx, QueryMode.CURRENT)
    assert err.result.status is ToolStatus.ERROR and "synthetic failure" in (err.result.error or "")
    assert exhausted.result.status is ToolStatus.ERROR
    assert "max_tool_calls" in (exhausted.result.error or "")
