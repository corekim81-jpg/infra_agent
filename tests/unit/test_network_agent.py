"""Network Agent 테스트 (가상 데이터, 실제 카탈로그 조회식 사용)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from expr_prom import ExprProm
from infra_agent.agents.network import (
    COUNT_NOTE,
    DNS_ZERO_NOTE,
    DROP_REASON_NOTE,
    FOCUS_NOTE,
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
    DataSourceKind,
    Finding,
    FindingKind,
    Intent,
    JudgementBasis,
    Severity,
    TargetKind,
    TargetRef,
    TimeRange,
    ToolResult,
    ToolStatus,
)
from infra_agent.tools import CatalogQueryTool, QueryMode, ToolBudget
from infra_agent.units import fmt_time

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
    ctx: AnalysisContext,
    fake: ExprProm,
    analysis: AnalysisConfig | None = None,
    upstream: dict[str, AgentResult] | None = None,
) -> AgentResult:
    async with PrometheusClient.from_config(CFG, transport=fake.transport()) as prom:
        tool = CatalogQueryTool(
            CATALOG, prom, agent=AgentName.NETWORK, budget=ToolBudget(100), timeout_seconds=5
        )
        agent = NetworkAgent(tool, analysis or AnalysisConfig())
        return await agent.run(TASK, ctx, upstream or {})


def _service_upstream(
    *names: str, peaks: dict[str, datetime] | None = None
) -> dict[str, AgentResult]:
    """선행 Service Agent 결과(가상): 이름마다 오류율 경고 1건, `peaks`가 있으면 최고 시점 사실."""
    evidence = ToolResult(
        evidence_id="service.error_ratio@current",
        source=DataSourceKind.PROMETHEUS,
        query="q",
        status=ToolStatus.OK,
        data=[],
        fetched_at=NOW,
        synthetic=True,
    )
    findings = tuple(
        Finding(
            kind=FindingKind.FACT,
            statement=f"서비스 오류율(현재, SERVER span): {n} 10.0%",
            severity=Severity.WARNING,
            targets=(TargetRef(kind=TargetKind.SERVICE, name=n),),
            evidence_ids=(evidence.evidence_id,),
            basis=JudgementBasis.THRESHOLD,
        )
        for n in names
    ) + tuple(
        Finding(
            kind=FindingKind.FACT,
            statement=f"오류율 최고 시점 ({n}): ...",
            targets=(TargetRef(kind=TargetKind.SERVICE, name=n),),
            evidence_ids=(evidence.evidence_id,),
            basis=JudgementBasis.STATE,
            observed_at=at,
        )
        for n, at in (peaks or {}).items()
    )
    return {
        "service-1": AgentResult(
            task_id="service-1",
            agent=AgentName.SERVICE,
            status=AgentStatus.SUCCESS,
            findings=findings,
            evidence=(evidence,),
        )
    }


def _workload_flows(ctx: AnalysisContext, fake: ExprProm) -> None:
    def out(name: str, verdict: str, ns: str = "otel-demo") -> dict[str, str]:
        return {"source_namespace": ns, "source_workload": name, "verdict": verdict}

    def into(name: str, verdict: str, ns: str = "otel-demo") -> dict[str, str]:
        return {"destination_namespace": ns, "destination_workload": name, "verdict": verdict}

    fake.add(
        _expr("network.workload_egress_by_verdict", ctx),
        [
            (out("frontend", "FORWARDED"), 10.0),
            (out("cart", "FORWARDED"), 2.0),
            (out("ad", "FORWARDED"), 0.005),
        ],
    )
    fake.add(
        _expr("network.workload_ingress_by_verdict", ctx),
        [
            (into("cart", "FORWARDED"), 9.0),
            (into("cart", "DROPPED"), 1.0),
            (into("valkey-cart", "FORWARDED"), 2.0),
            (into("flagd", "FORWARDED"), 0.005),
            (into("frontend", "FORWARDED"), 3.0),
            (into("frontend", "FORWARDED", ns="shop"), 1.0),
        ],
    )


async def test_service_focus_checks_upstream_issue_workloads() -> None:
    ctx = _ctx()
    fake = ExprProm()
    _fill(ctx, fake)
    _workload_flows(ctx, fake)
    upstream = _service_upstream("cart", "flagd", "checkout", "frontend")
    result = await _run(ctx, fake, upstream=upstream)
    by = {f.statement: f for f in result.findings}
    cart = by[
        "Service 이상 대상 cart(오류율)의 네트워크 [otel-demo](현재, 5분): "
        "나가는 흐름 2건/초(드롭·오류 판정 0.0%), "
        "들어오는 흐름 10건/초(드롭·오류 판정 10.0%, 기준 5.0% 이상); 최근 30분 드롭 "
        "otel-demo/frontend → otel-demo/cart 12회(사유 POLICY_DENIED) "
        "(Hubble 패킷 드롭 판정과 같은 결과)"
    ]
    assert cart.severity is Severity.WARNING  # 흐름 판정 비율 기준 초과
    assert cart.targets[0].labels == {"service": "cart"}
    assert cart.evidence_ids == (
        "network.workload_egress_by_verdict@current",
        "network.workload_ingress_by_verdict@current",
        "network.drops_increase@window",
    )
    # 데이터가 없는 방향은 "흐름 없음"이라고 단정하지 않음
    flagd = by[
        "Service 이상 대상 flagd(오류율)의 네트워크 [otel-demo](현재, 5분): "
        "나가는 흐름 결과 없음(같은 이름의 워크로드 시계열 없음), "
        "들어오는 흐름 0.005건/초(흐름이 적어 비율을 판정하지 않음); "
        "최근 30분 드롭 없음(비장애성 사유 제외)"
    ]
    assert flagd.severity is Severity.INFO
    # "결과 없음" 판단에도 근거가 있도록 조회한 두 방향의 근거를 모두 붙임
    assert flagd.evidence_ids == (
        "network.workload_egress_by_verdict@current",
        "network.workload_ingress_by_verdict@current",
        "network.drops_increase@window",
    )
    # 드롭만 있고 흐름 비율이 기준 미만이면 경고로 다시 세지 않음 (드롭 판정에 이미 경고)
    frontend = next(f for f in result.findings if "Service 이상 대상 frontend" in f.statement)
    assert frontend.severity is Severity.INFO
    assert "네트워크(현재, 5분)" in frontend.statement  # 여러 네임스페이스면 이름을 붙이지 않음
    assert (
        "Service 이상 대상 frontend: 이름이 같은 워크로드가 여러 네임스페이스(otel-demo, shop)에 "
        "있어 흐름을 합쳐 계산함"
    ) in result.limitations
    assert FOCUS_NOTE in result.limitations
    assert any(x.startswith("Service 이상 대상 checkout은") for x in result.limitations)
    # 워크로드 쌍이 아니라 한쪽 워크로드로만 집계 (시계열 수 제한)
    queried = [q for q, _ in fake.queries]
    assert any("sum by (verdict, source_namespace, source_workload) (rate(" in q for q in queried)


async def test_service_focus_uses_service_peak_time() -> None:
    """Service가 찾은 이상 최고 시점까지의 5분으로 흐름을 조회 (스파이크를 놓치지 않게)."""
    ctx = _ctx()
    fake = ExprProm()
    _fill(ctx, fake)
    peak = NOW - timedelta(minutes=8)
    egress = _expr("network.workload_egress_by_verdict", ctx)
    ingress = _expr("network.workload_ingress_by_verdict", ctx)
    # 최고 시점에는 들어오는 흐름의 드롭 비율이 높고, 현재는 정상
    fake.add(
        egress,
        [
            (
                {
                    "source_namespace": "otel-demo",
                    "source_workload": "cart",
                    "verdict": "FORWARDED",
                },
                2.0,
            )
        ],
    )
    fake.add(
        ingress,
        [
            (
                {
                    "destination_namespace": "otel-demo",
                    "destination_workload": "cart",
                    "verdict": "FORWARDED",
                },
                5.0,
            )
        ],
    )
    fake.add(
        ingress,
        [
            (
                {
                    "destination_namespace": "otel-demo",
                    "destination_workload": "cart",
                    "verdict": "FORWARDED",
                },
                5.0,
            ),
            (
                {
                    "destination_namespace": "otel-demo",
                    "destination_workload": "cart",
                    "verdict": "DROPPED",
                },
                5.0,
            ),
        ],
        at=peak,
    )
    upstream = _service_upstream("cart", peaks={"cart": peak})
    result = await _run(ctx, fake, upstream=upstream)
    [cart] = [f for f in result.findings if f.statement.startswith("Service 이상 대상 cart")]
    # 시간대 표시는 실행 환경마다 다름(예: Windows "Coordinated Universal Time")
    when = fmt_time(peak, seconds=False)
    assert f"의 네트워크 [otel-demo](이상 최고 시점 {when}까지 5분): " in cart.statement
    assert "들어오는 흐름 10건/초(드롭·오류 판정 50.0%, 기준 5.0% 이상)" in cart.statement
    assert cart.severity is Severity.WARNING and cart.observed_at == peak
    assert cart.evidence_ids[:2] == (
        "network.workload_egress_by_verdict@current:cart",
        "network.workload_ingress_by_verdict@current:cart",
    )
    peak_queries = [at for q, at in fake.queries if q == ingress]
    assert peak_queries and all(at is not None for at in peak_queries)
    # 최고 시점이 분석 구간 끝 무렵이면 현재 5분으로 조회
    late = _service_upstream("cart", peaks={"cart": NOW - timedelta(seconds=20)})
    now_result = await _run(ctx, fake, upstream=late)
    [cart2] = [f for f in now_result.findings if f.statement.startswith("Service 이상 대상 cart")]
    assert "(현재, 5분)" in cart2.statement and cart2.observed_at is None


async def test_service_focus_unjudged_fact_has_no_service_target() -> None:
    """비율도 드롭도 판정하지 못하면 서비스 대상을 붙이지 않음 ('이상 없음' 근거에서 제외)."""
    ctx = _ctx()
    fake = ExprProm()
    _fill(ctx, fake)
    _workload_flows(ctx, fake)
    fake.add(_expr("network.drops_increase", ctx), [])  # 드롭 결과 없음 → 드롭 판단 못함
    result = await _run(ctx, fake, upstream=_service_upstream("flagd"))
    [flagd] = [f for f in result.findings if f.statement.startswith("Service 이상 대상 flagd")]
    assert "구간 내 드롭은 확인하지 못함" in flagd.statement
    assert flagd.targets == ()


async def test_service_focus_drops_respect_namespace() -> None:
    ctx = _ctx()
    fake = ExprProm()
    _fill(ctx, fake)
    _workload_flows(ctx, fake)
    fake.add(
        _expr("network.drops_increase", ctx),
        [
            (
                {
                    "reason": "POLICY_DENIED",
                    "source_namespace": "other",
                    "source_workload": "x",
                    "destination_namespace": "other",
                    "destination_workload": "cart",
                },
                5.0,
            )
        ],
    )
    result = await _run(ctx, fake, upstream=_service_upstream("cart"))
    [cart] = [f for f in result.findings if f.statement.startswith("Service 이상 대상 cart")]
    # 흐름은 otel-demo의 cart, 드롭은 다른 네임스페이스의 cart이므로 섞지 않음
    assert cart.statement.endswith("최근 30분 드롭 없음(비장애성 사유 제외)")


async def test_service_focus_top_n_keeps_critical_first() -> None:
    ctx = _ctx()
    fake = ExprProm()
    _fill(ctx, fake)
    _workload_flows(ctx, fake)
    upstream = _service_upstream("a1", "a2", "cart")
    service = upstream["service-1"]
    critical = service.findings[2].model_copy(update={"severity": Severity.CRITICAL})
    upstream["service-1"] = service.model_copy(
        update={"findings": (*service.findings[:2], critical)}
    )
    result = await _run(ctx, fake, AnalysisConfig(top_n=1), upstream=upstream)
    assert any(f.statement.startswith("Service 이상 대상 cart") for f in result.findings)
    assert any("확인하지 않은 대상: a1, a2" in x for x in result.limitations)


async def test_service_focus_skipped_or_unlabeled() -> None:
    ctx = _ctx()
    fake = ExprProm()
    _fill(ctx, fake)
    _workload_flows(ctx, fake)
    plain = await _run(ctx, fake)  # 선행 Service 결과 없음 → 집중 확인 조회 안 함
    assert not any("source_workload) (rate(" in q for q, _ in fake.queries)
    assert FOCUS_NOTE not in plain.limitations

    fake2 = ExprProm()
    _fill(ctx, fake2)
    for key in ("network.workload_egress_by_verdict", "network.workload_ingress_by_verdict"):
        fake2.add(_expr(key, ctx), [({"verdict": "FORWARDED"}, 3.0)])
    result = await _run(ctx, fake2, upstream=_service_upstream("cart"))
    assert (
        "Service 이상 대상 cart: 흐름 결과에 워크로드 라벨이 없어 네트워크를 확인하지 못함"
    ) in result.limitations
    assert not any(f.statement.startswith("Service 이상 대상") for f in result.findings)

    # 한 방향만 워크로드 라벨이 없으면 그 방향은 "확인하지 못함"
    fake3 = ExprProm()
    _fill(ctx, fake3)
    _workload_flows(ctx, fake3)
    fake3.add(_expr("network.workload_egress_by_verdict", ctx), [({"verdict": "FORWARDED"}, 3.0)])
    one = await _run(ctx, fake3, upstream=_service_upstream("cart"))
    assert any(
        "나가는 흐름은 워크로드 라벨이 없어 확인하지 못함, 들어오는 흐름 10건/초" in f.statement
        for f in one.findings
    )

    # 한 방향 조회가 실패하면 그 방향은 "확인하지 못함"
    fake4 = ExprProm()
    _fill(ctx, fake4)
    _workload_flows(ctx, fake4)
    fake4.fail(_expr("network.workload_egress_by_verdict", ctx))
    failed = await _run(ctx, fake4, upstream=_service_upstream("cart"))
    assert any(
        "나가는 흐름은 확인하지 못함, 들어오는 흐름 10건/초" in f.statement for f in failed.findings
    )

    # 최신성을 확인하지 못하면 판단하지 않음
    stale = ExprProm(freshness_seconds=3600)
    _fill(ctx, stale)
    _workload_flows(ctx, stale)
    old = await _run(ctx, stale, upstream=_service_upstream("cart"))
    assert not any(f.statement.startswith("Service 이상 대상") for f in old.findings)
    assert any(
        "Service 이상 대상 cart: 흐름 데이터 최신성을 확인하지 못해" in x for x in old.limitations
    )


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
