"""Kubernetes Agent 테스트 (가상 데이터, 실제 카탈로그 조회식 사용)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from expr_prom import ExprProm
from infra_agent.agents.kubernetes import KubernetesAgent, k8s_entity
from infra_agent.agents.server import LIMIT_NEAR_CHECK
from infra_agent.answer.render import render_text
from infra_agent.catalog import load_catalog
from infra_agent.config import load_settings
from infra_agent.config.settings import AnalysisConfig, HttpDatasourceConfig
from infra_agent.datasources import PrometheusClient
from infra_agent.orchestration.runner import answer_question
from infra_agent.schemas import (
    AgentName,
    AgentResult,
    AgentStatus,
    AgentTask,
    AnalysisContext,
    Budget,
    Intent,
    Severity,
    TargetKind,
    TargetRef,
    TimeRange,
)
from infra_agent.tools import CatalogQueryTool, QueryMode, ToolBudget

ROOT = Path(__file__).resolve().parents[2]
CATALOG = load_catalog(ROOT / "config/catalog/otel-demo.yaml")
NOW = datetime(2026, 9, 29, 3, 0, tzinfo=UTC)
CFG = HttpDatasourceConfig(enabled=True, url="http://prom.synthetic.test")
TASK = AgentTask(task_id="kubernetes-1", agent=AgentName.KUBERNETES, objective="test")
KAFKA = {
    "k8s_namespace_name": "observability",
    "k8s_pod_name": "kafka-0",
    "k8s_container_name": "kafka",
}
WINDOW_ITEMS = ("k8s.container_restarts_increase", "k8s.container_oom_events")


def _ctx(targets: dict[TargetKind, str] | None = None) -> AnalysisContext:
    return AnalysisContext(
        request_id="r1",
        question="q",
        intent=Intent.STATUS,
        time_range=TimeRange.last(timedelta(minutes=30), NOW),
        targets=tuple(TargetRef(kind=k, name=v) for k, v in (targets or {}).items()),
        budget=Budget(max_llm_calls=0, max_tool_calls=100, deadline=NOW),
    )


def _tool(
    prom: PrometheusClient | None, agent: AgentName = AgentName.KUBERNETES
) -> CatalogQueryTool:
    return CatalogQueryTool(
        CATALOG,
        prom,
        agent=agent,
        budget=ToolBudget(100),
        timeout_seconds=5,  # type: ignore[arg-type]
    )


def _expr(
    key: str, ctx: AnalysisContext, selector: str = "", agent: AgentName = AgentName.KUBERNETES
) -> str:
    tool = _tool(None, agent)
    mode = QueryMode.WINDOW if key in WINDOW_ITEMS else QueryMode.CURRENT
    return tool.build_expr(tool.item(key), selector, mode, ctx)


def _problems(ctx: AnalysisContext, fake: ExprProm) -> None:
    fake.add(_expr("k8s.container_restarts_increase", ctx), [(KAFKA, 2.0)])
    fake.add(_expr("k8s.container_oom_events", ctx), [(KAFKA, 1.0)])
    fake.add(
        _expr("k8s.pod_phase", ctx),
        [
            ({"k8s_namespace_name": "otel-demo", "k8s_pod_name": "load-x"}, 1.0),
            ({"k8s_namespace_name": "otel-demo", "k8s_pod_name": "job-y"}, 4.0),
        ],
    )
    fake.add(
        _expr("k8s.deployment_unavailable", ctx),
        [({"k8s_namespace_name": "otel-demo", "k8s_deployment_name": "cart"}, 1.0)],
    )


async def _run(ctx: AnalysisContext, fake: ExprProm) -> AgentResult:
    async with PrometheusClient.from_config(CFG, transport=fake.transport()) as prom:
        return await KubernetesAgent(_tool(prom), AnalysisConfig()).run(TASK, ctx, {})


def _statements(result: AgentResult) -> list[str]:
    return [f.statement for f in result.findings]


async def test_problems_and_no_issue_facts() -> None:
    ctx = _ctx()
    fake = ExprProm()
    _problems(ctx, fake)
    result = await _run(ctx, fake)
    assert result.status is AgentStatus.SUCCESS
    by_text = {f.statement: f for f in result.findings}
    restart = by_text["재시작한 컨테이너 (최근 30분): observability/kafka-0/kafka 2회"]
    assert restart.severity is Severity.WARNING and restart.targets[0].kind is TargetKind.CONTAINER
    oom = by_text["OOM 이벤트가 발생한 컨테이너 (최근 30분): observability/kafka-0/kafka 1회"]
    assert oom.severity is Severity.CRITICAL
    assert by_text["Running·Succeeded가 아닌 Pod (현재): otel-demo/load-x Pending"].severity is (
        Severity.WARNING
    )
    assert by_text["Running·Succeeded가 아닌 Pod (현재): otel-demo/job-y Failed"].severity is (
        Severity.CRITICAL
    )
    assert "사용 가능 복제 수가 부족한 Deployment (현재): otel-demo/cart 1개 부족" in by_text
    # 최신 데이터의 빈 조건 조회 → "해당 없음" (정보)
    assert "Ready 상태가 아닌 노드 (현재): 해당 대상 없음" in by_text
    assert by_text["준비(ready)되지 않은 컨테이너 (현재): 해당 대상 없음"].severity is Severity.INFO
    assert any("Pod phase 값은" in x for x in result.limitations)
    assert any(x.startswith("OOM 이벤트와 재시작이 같은 구간에") for x in result.next_checks)
    # 재시작·OOM은 분석 구간 전체(30분)의 증가량으로 조회
    queried = [q for q, _ in fake.queries]
    assert any("increase(k8s_container_restarts{}[1800s])" in q for q in queried)
    assert any("increase(container_oom_events_total{}[1800s])" in q for q in queried)
    assert not any(q.startswith("count(") for q in queried)  # 대상 필터가 없으면 존재 확인 생략


async def test_stale_empty_results_are_not_reported_as_no_issue() -> None:
    ctx = _ctx()
    fake = ExprProm(freshness_seconds=3600)
    result = await _run(ctx, fake)
    assert not any("해당 없음" in s for s in _statements(result))
    assert any("최신성을 확인하지 못해 판단하지 않음" in x for x in result.limitations)


async def test_target_without_series_is_not_reported_as_no_issue() -> None:
    ctx = _ctx({TargetKind.NAMESPACE: "otel-demoo"})  # 오타난 namespace
    fake = ExprProm()
    result = await _run(ctx, fake)
    assert not any("해당 없음" in s for s in _statements(result))
    assert any("에 해당하는 시계열이 없어 판단하지 않음" in x for x in result.limitations)


async def test_target_with_series_and_unsupported_filter() -> None:
    ns = {TargetKind.NAMESPACE: "otel-demo"}
    ctx = _ctx(ns)
    fake = ExprProm()
    sel = 'k8s_namespace_name="otel-demo"'
    for metric in ("k8s_container_restarts", "container_oom_events_total", "k8s_pod_phase"):
        fake.add(f"count({metric}{{{sel}}})", [({}, 12.0)])
    result = await _run(ctx, fake)
    texts = _statements(result)
    assert "재시작한 컨테이너 (최근 30분): 해당 대상 없음" in texts
    assert "Running·Succeeded가 아닌 Pod (현재): 해당 대상 없음" in texts
    # Deployment 등은 시계열 존재 확인 결과가 없어(가짜 응답 미등록 → 0) 판단하지 않음
    assert "사용 가능 복제 수가 부족한 Deployment (현재): 해당 대상 없음" not in texts
    # 노드 대상 필터는 노드 라벨이 없는 워크로드 항목에 적용할 수 없어 조회하지 않음
    node_ctx = _ctx({TargetKind.NODE: "k3d-a-0"})
    node_result = await _run(node_ctx, ExprProm())
    assert any(
        x.startswith("k8s.deployment_unavailable: 요청한 대상(node)으로 필터링할 수 없어")
        for x in node_result.limitations
    )


async def test_partial_failure() -> None:
    ctx = _ctx()
    fake = ExprProm()
    fake.fail(_expr("k8s.pod_phase", ctx))
    result = await _run(ctx, fake)
    assert result.status is AgentStatus.PARTIAL
    assert any("k8s.pod_phase: 조회 실패" in x for x in result.limitations)


def test_entity_names() -> None:
    assert k8s_entity(KAFKA).name == "observability/kafka-0/kafka"
    assert k8s_entity({"k8s_node_name": "n1"}).kind is TargetKind.NODE
    wl = k8s_entity({"k8s_namespace_name": "ns", "k8s_statefulset_name": "db"})
    assert (wl.kind, wl.name) == (TargetKind.WORKLOAD, "ns/db")


def _settings():  # type: ignore[no-untyped-def]
    return load_settings(
        environ={
            "INFRA_AGENT_PROFILE": "dev-tunnel",
            "INFRA_AGENT__DATASOURCES__PROMETHEUS__ENABLED": "true",
            "INFRA_AGENT__DATASOURCES__PROMETHEUS__URL": "http://prom.synthetic.test",
        }
    )


async def test_kubernetes_question_runs_only_kubernetes_agent() -> None:
    ctx = _ctx()
    fake = ExprProm()
    _problems(ctx, fake)
    bundle = await answer_question(
        "Kubernetes에서 재시작하거나 Pending 상태인 Pod를 확인해 줘",
        _settings(),
        CATALOG,
        now=NOW,
        transport=fake.transport(),
    )
    assert [r.agent for r in bundle.results] == [AgentName.KUBERNETES]
    assert bundle.answer.unverified_areas == ()
    assert "기준을 넘는 이상 징후 5건(심각 2건, 경고 3건)" in bundle.answer.summary
    text = render_text(bundle)
    assert "(에이전트 실행: kubernetes 성공 " in text
    # 상태 질문이라도 재시작·OOM은 구간 전체로 집계했음을 밝히고, "평균"으로 표시하지 않음
    assert "- 구간 집계 항목(재시작·OOM·로그 수 등)의 구간: " in text
    restart_line = next(x for x in text.splitlines() if x.startswith("- k8s.container_restarts"))
    assert restart_line.endswith(")") and " 구간 집계, " in restart_line
    assert " 평균, " not in restart_line
    phase_line = next(x for x in text.splitlines() if x.startswith("- k8s.pod_phase@current"))
    assert " 시점, " in phase_line


async def test_server_and_kubernetes_run_together() -> None:
    ctx = _ctx()
    fake = ExprProm()
    _problems(ctx, fake)
    fake.add(
        _expr("container.memory_limit_utilization", ctx, agent=AgentName.SERVER),
        [(KAFKA, 0.97)],
    )
    bundle = await answer_question(
        "서버 자원이랑 쿠버네티스 상태 알려줘",
        _settings(),
        CATALOG,
        now=NOW,
        transport=fake.transport(),
    )
    assert [r.agent for r in bundle.results] == [AgentName.SERVER, AgentName.KUBERNETES]
    assert all(r.status is AgentStatus.SUCCESS for r in bundle.results)
    assert [run.depends_on for run in bundle.runs] == [(), ()]  # 독립 작업 (병렬)
    server = bundle.results[0]
    assert LIMIT_NEAR_CHECK in server.next_checks  # Server는 확인을 제안하지만
    assert LIMIT_NEAR_CHECK not in bundle.answer.next_checks  # Kubernetes가 확인했으므로 제외
    text = render_text(bundle)
    assert "(에이전트 실행: server 성공 " in text and ", kubernetes 성공 " in text


def test_window_mode_requires_range_item() -> None:
    import pytest

    ctx = _ctx()
    tool = _tool(None, AgentName.SERVER)
    with pytest.raises(ValueError, match="window"):
        tool.build_expr(tool.item("node.cpu_utilization"), "", QueryMode.WINDOW, ctx)
