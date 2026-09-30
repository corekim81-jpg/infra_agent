"""Network Agent 테스트 (가상 데이터, 실제 카탈로그 조회식 사용)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from expr_prom import ExprProm
from infra_agent.agents.network import (
    COUNT_NOTE,
    DNS_ZERO_NOTE,
    DROP_REASON_NOTE,
    UNKNOWN_PEER,
    NetworkAgent,
    network_entity,
)
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
    JudgementBasis,
    Severity,
    TargetKind,
    TimeRange,
)
from infra_agent.tools import CatalogQueryTool, QueryMode, ToolBudget

ROOT = Path(__file__).resolve().parents[2]
CATALOG = load_catalog(ROOT / "config/catalog/otel-demo.yaml")
NOW = datetime(2026, 9, 29, 3, 0, tzinfo=UTC)
CFG = HttpDatasourceConfig(enabled=True, url="http://prom.synthetic.test")
TASK = AgentTask(task_id="network-1", agent=AgentName.NETWORK, objective="test")
WINDOW_ITEMS = (
    "network.drops_increase",
    "network.node_interface_errors_increase",
    "network.pod_network_errors_increase",
    "network.container_packet_drops_increase",
    "network.hubble_lost_events_increase",
)
FLOW = {"source_namespace": "otel-demo", "destination_namespace": "otel-demo"}
SYSTEM = {"source_namespace": "kube-system", "destination_namespace": "kube-system"}


def _ctx() -> AnalysisContext:
    return AnalysisContext(
        request_id="r1",
        question="네트워크 상태",
        intent=Intent.STATUS,
        time_range=TimeRange.last(timedelta(minutes=30), NOW),
        budget=Budget(max_llm_calls=0, max_tool_calls=100, deadline=NOW),
    )


def _expr(key: str, ctx: AnalysisContext) -> str:
    tool = CatalogQueryTool(
        CATALOG,
        None,  # type: ignore[arg-type]
        agent=AgentName.NETWORK,
        budget=ToolBudget(1),
        timeout_seconds=1,
    )
    mode = QueryMode.WINDOW if key in WINDOW_ITEMS else QueryMode.CURRENT
    return tool.build_expr(tool.item(key), "", mode, ctx)


def _fill(ctx: AnalysisContext, fake: ExprProm, *, lost: float = 0.0) -> None:
    fake.add(
        _expr("network.hubble_lost_events_increase", ctx),
        [({"k8s_node_name": "k3d-a-0", "source": "perf_event_ring_buffer"}, lost)],
    )
    fake.add(
        _expr("network.drops_increase", ctx),
        [
            (
                {
                    "reason": "POLICY_DENIED",
                    "source_namespace": "otel-demo",
                    "source_workload": "frontend",
                    "destination_namespace": "otel-demo",
                    "destination_workload": "cart",
                },
                12.0,
            ),
            (
                {
                    "reason": "UNSUPPORTED_L3_PROTOCOL",
                    "source_namespace": "",
                    "source_workload": "",
                    "destination_namespace": "",
                    "destination_workload": "",
                },
                0.2,  # 추정 오차 수준 → 발생으로 보지 않음
            ),
        ],
    )
    fake.add(
        _expr("network.node_interface_errors_increase", ctx),
        [({"k8s_node_name": "k3d-a-0", "interface": "eth0", "direction": "receive"}, 0.0)],
    )
    fake.add(
        _expr("network.pod_network_errors_increase", ctx),
        [
            (
                {
                    "k8s_namespace_name": "otel-demo",
                    "k8s_pod_name": "cart-x",
                    "direction": "transmit",
                },
                3.0,
            )
        ],
    )
    fake.add(
        _expr("network.container_packet_drops_increase", ctx),
        [({"k8s_namespace_name": "otel-demo", "k8s_pod_name": "cart-x"}, 0.0)],
    )
    fake.add(
        _expr("network.flows_by_verdict", ctx),
        [
            ({**FLOW, "verdict": "FORWARDED"}, 10.0),
            ({**FLOW, "verdict": "DROPPED"}, 1.0),
            ({**SYSTEM, "verdict": "FORWARDED"}, 5.0),
        ],
    )
    fake.add(
        _expr("network.tcp_flags_rate", ctx),
        [
            ({**FLOW, "flag": "SYN"}, 4.0),
            ({**FLOW, "flag": "RST"}, 0.5),
        ],
    )
    fake.add(
        _expr("network.dns_query_rate", ctx),
        [
            ({"source_namespace": "otel-demo", "destination_namespace": "", "qtypes": "A"}, 2.0),
            ({"source_namespace": "kube-system", "destination_namespace": "", "qtypes": "A"}, 0.5),
        ],
    )


async def _run(
    ctx: AnalysisContext, fake: ExprProm, analysis: AnalysisConfig | None = None
) -> AgentResult:
    async with PrometheusClient.from_config(CFG, transport=fake.transport()) as prom:
        tool = CatalogQueryTool(
            CATALOG, prom, agent=AgentName.NETWORK, budget=ToolBudget(100), timeout_seconds=5
        )
        return await NetworkAgent(tool, analysis or AnalysisConfig()).run(TASK, ctx, {})


async def test_counts_ratios_and_context() -> None:
    ctx = _ctx()
    fake = ExprProm()
    _fill(ctx, fake)
    result = await _run(ctx, fake)
    assert result.status is AgentStatus.SUCCESS, result.errors
    by = {f.statement: f for f in result.findings}
    drop = by[
        "Hubble 패킷 드롭 (최근 30분): otel-demo/frontend → otel-demo/cart 12회 "
        "(사유 POLICY_DENIED)"
    ]
    assert drop.severity is Severity.WARNING and drop.targets[0].kind is TargetKind.WORKLOAD
    assert not any(UNKNOWN_PEER in t and "Hubble 패킷 드롭" in t for t in by)  # 0.2는 발생 아님
    assert "Pod 네트워크 오류 (최근 30분): otel-demo/cart-x (transmit) 3회" in by
    assert "노드 네트워크 인터페이스 오류 (최근 30분): 발생 없음 (대상 1개)" in by
    assert "컨테이너 패킷 드롭 (최근 30분): 발생 없음 (대상 1개)" in by
    assert "Hubble 이벤트 유실 (최근 30분): 없음" in by
    ratio = by[
        "흐름 드롭·오류 판정 비율(현재, 5분): otel-demo → otel-demo 9.1% "
        "(드롭·오류 1건/초, 기준 5.0% 이상)"
    ]
    assert ratio.severity is Severity.WARNING and ratio.basis is JudgementBasis.THRESHOLD
    assert "TCP RST 패킷(현재, 5분, 판정 기준 미적용): otel-demo → otel-demo 0.5건/초" in by
    assert (
        "DNS 질의(현재, 5분): 전체 2.5건/초, 출발 네임스페이스별 otel-demo 2건/초, "
        "kube-system 0.5건/초"
    ) in by
    assert COUNT_NOTE in result.limitations and DROP_REASON_NOTE in result.limitations
    assert any("DNS 응답 코드" in x for x in result.limitations)
    queried = [q for q, _ in fake.queries]
    assert any("increase(hubble_drop_total{}[1800s])" in q for q in queried)
    assert any("rate(hubble_flows_processed_total{}[5m])" in q for q in queried)


async def test_lost_events_and_stale_data() -> None:
    ctx = _ctx()
    fake = ExprProm()
    _fill(ctx, fake, lost=40.0)
    result = await _run(ctx, fake)
    assert "Hubble 이벤트 유실 (최근 30분): 40회 (유실 위치: perf_event_ring_buffer)" in [
        f.statement for f in result.findings
    ]
    assert any(x.startswith("Hubble 이벤트가 유실되어") for x in result.limitations)
    stale = ExprProm(freshness_seconds=3600)
    _fill(ctx, stale)
    old = await _run(ctx, stale)
    texts = [f.statement for f in old.findings]
    assert not any("발생 없음" in t or "모두 기준" in t or t.endswith(": 없음") for t in texts)
    assert any("최신성을 확인하지 못해" in x for x in old.limitations)


async def test_missing_data_and_partial_failure() -> None:
    ctx = _ctx()
    empty = await _run(ctx, ExprProm())
    assert not any("발생 없음" in f.statement or "모두 기준" in f.statement for f in empty.findings)
    assert "network.flows_by_verdict: 결과가 없어 판단하지 않음" in empty.limitations
    fake = ExprProm()
    _fill(ctx, fake)
    fake.fail(_expr("network.tcp_flags_rate", ctx))
    partial = await _run(ctx, fake)
    assert partial.status is AgentStatus.PARTIAL
    assert any(x.startswith("network.tcp_flags_rate: 조회 실패") for x in partial.limitations)


async def test_normal_flows_and_no_rst() -> None:
    ctx = _ctx()
    fake = ExprProm()
    _fill(ctx, fake)
    fake.add(
        _expr("network.flows_by_verdict", ctx),
        [({**FLOW, "verdict": "FORWARDED"}, 10.0), ({**FLOW, "verdict": "DROPPED"}, 0.1)],
    )
    fake.add(_expr("network.tcp_flags_rate", ctx), [({**FLOW, "flag": "SYN"}, 4.0)])
    result = await _run(ctx, fake)
    texts = [f.statement for f in result.findings]
    assert (
        "흐름 드롭·오류 판정 비율(현재, 5분): 네임스페이스 쌍 1개 모두 기준(5.0%) 미만, "
        "최대 otel-demo → otel-demo 1.0% (확인한 판정 값: DROPPED, FORWARDED)"
    ) in texts
    assert "TCP RST 패킷(현재, 5분): 없음 (확인한 플래그: SYN)" in texts


async def test_reason_only_drop_is_benign_and_dns_zero_is_limitation() -> None:
    """live 형태: Prometheus는 빈 라벨을 빼므로 출발·도착을 모르는 드롭 행에는 reason만 남음."""
    ctx = _ctx()
    fake = ExprProm()
    _fill(ctx, fake)
    fake.add(
        _expr("network.drops_increase", ctx),
        [({"reason": "UNSUPPORTED_L3_PROTOCOL"}, 77.6), ({"reason": "POLICY_DENIED"}, 3.0)],
    )
    fake.add(_expr("network.dns_query_rate", ctx), [({"source_namespace": "otel-demo"}, 0.0)])
    result = await _run(ctx, fake)
    by = {f.statement: f for f in result.findings}
    unknown = f"{UNKNOWN_PEER} → {UNKNOWN_PEER}"
    denied = by[f"Hubble 패킷 드롭 (최근 30분): {unknown} 3회 (사유 POLICY_DENIED)"]
    assert denied.severity is Severity.WARNING and denied.targets[0].kind is TargetKind.NAMESPACE
    benign = by[
        f"Hubble 패킷 드롭 (최근 30분): {unknown} 약 77.6회 (사유 UNSUPPORTED_L3_PROTOCOL, "
        "비장애성 사유로 설정되어 정보로 표시)"
    ]
    assert benign.severity is Severity.INFO
    # 경고 대상이 먼저 표시됨
    order = [f.statement for f in result.findings if "Hubble 패킷 드롭" in f.statement]
    assert order[0].endswith("(사유 POLICY_DENIED)")
    assert any("analysis.benign_drop_reasons" in x for x in result.limitations)
    assert not any("{" in f.statement for f in result.findings)
    # DNS 0은 "질의 없음" 사실이 아니라 수집 범위 한계
    assert not any(f.statement.startswith("DNS 질의") for f in result.findings)
    assert DNS_ZERO_NOTE in result.limitations

    strict = await _run(ctx, fake, AnalysisConfig(benign_drop_reasons=()))
    l3 = [f for f in strict.findings if "UNSUPPORTED_L3_PROTOCOL" in f.statement]
    assert [f.severity for f in l3] == [Severity.WARNING]


def test_entity_names() -> None:
    assert network_entity({"reason": "X"}).kind is TargetKind.HOST  # Hubble 항목이 아니면 흐름 아님
    assert network_entity({"reason": "X"}, flow=True).name == f"{UNKNOWN_PEER} → {UNKNOWN_PEER}"
    world = network_entity({"source_namespace": "", "destination_namespace": "otel-demo"})
    assert world.name == f"{UNKNOWN_PEER} → otel-demo" and world.kind is TargetKind.NAMESPACE
    node = network_entity({"k8s_node_name": "k3d-a-0", "interface": "eth0", "direction": "receive"})
    assert (node.kind, node.name) == (TargetKind.NODE, "k3d-a-0 eth0 (receive)")


async def test_network_question_runs_only_network_agent() -> None:
    settings = load_settings(
        environ={
            "INFRA_AGENT_PROFILE": "dev-tunnel",
            "INFRA_AGENT__DATASOURCES__PROMETHEUS__ENABLED": "true",
            "INFRA_AGENT__DATASOURCES__PROMETHEUS__URL": "http://prom.synthetic.test",
        }
    )
    bundle = await answer_question(
        "네트워크 패킷 드롭이나 DNS 문제가 있어?",
        settings,
        CATALOG,
        now=NOW,
        transport=ExprProm().transport(),
    )
    assert [r.agent for r in bundle.results] == [AgentName.NETWORK]
    assert bundle.answer.unverified_areas == ()
    assert "(에이전트 실행: network 성공 " in render_text(bundle)


async def test_namespace_filter_is_destination_based() -> None:
    from infra_agent.agents.network import HUBBLE_FILTER_NOTE
    from infra_agent.schemas import TargetRef

    ctx = _ctx().model_copy(
        update={"targets": (TargetRef(kind=TargetKind.NAMESPACE, name="otel-demo"),)}
    )
    tool = CatalogQueryTool(
        CATALOG,
        None,  # type: ignore[arg-type]
        agent=AgentName.NETWORK,
        budget=ToolBudget(1),
        timeout_seconds=1,
    )

    def filtered(key: str) -> str:
        item = tool.item(key)
        selector, _ = item.selector_for({TargetKind.NAMESPACE: "otel-demo"})
        mode = QueryMode.WINDOW if key in WINDOW_ITEMS else QueryMode.CURRENT
        return tool.build_expr(item, selector, mode, ctx)

    fake = ExprProm()
    fake.add(
        _expr("network.hubble_lost_events_increase", ctx),  # 대상 필터 없이 전체를 봄
        [({"k8s_node_name": "k3d-a-0", "source": ""}, 0.3), ({"k8s_node_name": "b"}, 0.4)],
    )
    fake.add(filtered("network.drops_increase"), [({**FLOW, "reason": "X"}, 0.0)])
    fake.add(
        filtered("network.flows_by_verdict"),
        [
            ({**FLOW, "verdict": "FORWARDED"}, 10.0),
            (
                {
                    "source_namespace": "a",
                    "destination_namespace": "otel-demo",
                    "verdict": "DROPPED",
                },
                0.001,
            ),
        ],
    )
    fake.add(filtered("network.tcp_flags_rate"), [(FLOW, 1.0)])  # flag 라벨 없음
    result = await _run(ctx, fake)
    texts = [f.statement for f in result.findings]
    assert HUBBLE_FILTER_NOTE in result.limitations
    assert "Hubble 패킷 드롭 (최근 30분) (도착 기준): 발생 없음 (대상 1개)" in texts
    # 행별로는 작아도 합계가 0.5 이상이면 유실로 보고, 위치가 비어 있으면 "?"로 표시
    assert "Hubble 이벤트 유실 (최근 30분): 약 0.7회 (유실 위치: ?)" in texts
    assert any(
        t.startswith("흐름 드롭·오류 판정 비율(현재, 5분) (도착 기준): 네임스페이스 쌍 1개")
        for t in texts
    )
    assert any(
        "비율을 판정하지 않음 (그중 드롭·오류 판정이 있는 쌍 1개)" in x for x in result.limitations
    )
    assert "network.tcp_flags_rate: 결과에 TCP 플래그 값이 없어 RST 여부를 판단하지 않음" in (
        result.limitations
    )
    assert not any(t.startswith("TCP RST") for t in texts)
