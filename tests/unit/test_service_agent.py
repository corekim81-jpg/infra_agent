"""Service Agent 테스트 (가상 데이터, 실제 카탈로그 조회식 사용)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fake_backend import FakeBackend
from infra_agent.agents.service import ServiceAgent
from infra_agent.answer.render import render_text
from infra_agent.catalog import load_catalog
from infra_agent.config import load_settings
from infra_agent.config.settings import AnalysisConfig, DataPolicy, HttpDatasourceConfig
from infra_agent.datasources import LokiClient, PrometheusClient, TempoClient
from infra_agent.llm.policy import build_observations
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
    TimeRange,
)
from infra_agent.tools import (
    CatalogQueryTool,
    LogQueryTool,
    QueryMode,
    ToolBudget,
    TraceSearchTool,
    build_traceql,
)

ROOT = Path(__file__).resolve().parents[2]
CATALOG = load_catalog(ROOT / "config/catalog/otel-demo.yaml")
NOW = datetime(2026, 9, 29, 3, 0, tzinfo=UTC)
PROM = HttpDatasourceConfig(enabled=True, url="http://prom.synthetic.test")
LOKI = HttpDatasourceConfig(enabled=True, url="http://loki.synthetic.test")
TEMPO = HttpDatasourceConfig(enabled=True, url="http://tempo.synthetic.test")
TASK = AgentTask(task_id="service-1", agent=AgentName.SERVICE, objective="test")
SERVER = "SPAN_KIND_SERVER"
PEAK = NOW - timedelta(minutes=10)
FOCUS = TimeRange(start=PEAK - timedelta(minutes=5), end=PEAK + timedelta(minutes=5))
TRACE_A = "4bf92f3577b34da6a3ce929d0e0e4736"
TRACE_B = "00f067aa0ba902b7a3ce929d0e0e4700"


def _ctx(intent: Intent = Intent.STATUS, question: str = "q") -> AnalysisContext:
    tr = TimeRange.last(timedelta(minutes=30), NOW)
    return AnalysisContext(
        request_id="r1",
        question=question,
        intent=intent,
        time_range=tr,
        baseline_range=tr.previous() if intent is not Intent.STATUS else None,
        budget=Budget(max_llm_calls=0, max_tool_calls=100, deadline=NOW),
    )


def _prom_tool() -> CatalogQueryTool:
    return CatalogQueryTool(
        CATALOG,
        None,  # type: ignore[arg-type]
        agent=AgentName.SERVICE,
        budget=ToolBudget(100),
        timeout_seconds=5,
    )


def _expr(
    key: str, ctx: AnalysisContext, mode: QueryMode = QueryMode.CURRENT, sel: str = ""
) -> str:
    tool = _prom_tool()
    return tool.build_expr(tool.item(key), sel, mode, ctx)


def _loki(key: str, window: TimeRange, sel: str = 'service_name="cart"') -> str:
    item = CATALOG.items[key]
    if item.uses_range:
        return item.render(sel, range=f"{int(window.duration.total_seconds())}s")
    return item.render(sel)


def _red(ctx: AnalysisContext, fake: FakeBackend, *, cart_error: float = 0.25) -> None:
    fake.add(
        _expr("service.request_rate", ctx),
        [
            ({"service": "cart", "span_kind": SERVER, "status_code": "STATUS_CODE_UNSET"}, 1.5),
            ({"service": "cart", "span_kind": SERVER, "status_code": "STATUS_CODE_ERROR"}, 0.5),
            ({"service": "cart", "span_kind": "SPAN_KIND_CLIENT", "status_code": "x"}, 9.0),
            ({"service": "frontend", "span_kind": SERVER, "status_code": "STATUS_CODE_UNSET"}, 5.0),
            ({"service": "tiny", "span_kind": SERVER, "status_code": "STATUS_CODE_UNSET"}, 0.001),
        ],
    )
    fake.add(
        _expr("service.error_ratio", ctx),
        [
            ({"service": "cart", "span_kind": SERVER}, cart_error),
            ({"service": "cart", "span_kind": "SPAN_KIND_CLIENT"}, 0.9),  # SERVER 우선이므로 무시
        ],
    )
    fake.add(
        _expr("service.latency_p95", ctx),
        [
            ({"service": "cart", "span_kind": SERVER}, 1.5),
            ({"service": "frontend", "span_kind": SERVER}, 0.2),
        ],
    )
    fake.add(
        _expr("service.dependency_request_rate", ctx),
        [({"client": "frontend", "server": "cart", "connection_type": ""}, 2.0)],
    )
    fake.add(
        _expr("service.dependency_failed_rate", ctx),
        [({"client": "frontend", "server": "cart"}, 0.3)],
    )


def _details(ctx: AnalysisContext, fake: FakeBackend, *, lines_total: float = 1000) -> None:
    fake.add_range(
        _expr("service.error_ratio", ctx, sel='service="cart"'),
        [
            (
                {"service": "cart", "span_kind": SERVER},
                [(NOW - timedelta(minutes=20), 0.05), (PEAK, 0.4), (NOW, 0.25)],
            )
        ],
    )
    fake.loki_metrics[_loki("log.lines_total", FOCUS)] = [({"service_name": "cart"}, lines_total)]
    fake.loki_metrics[_loki("log.error_lines", FOCUS)] = [({"service_name": "cart"}, 12)]
    fake.loki_logs[_loki("log.error_samples", FOCUS)] = [
        (
            {
                "service_name": "cart",
                "trace_id": TRACE_A,
                "k8s_pod_name": "cart-x",
                "secret_label": "x",
            },
            [(PEAK, "ERROR\x1b[31m redis timeout\n password=hunter22 while GetCart")],
        ),
        (
            {"service_name": "cart"},
            [(PEAK - timedelta(minutes=1), f"exception in handler trace_id={TRACE_B}")],
        ),
    ]
    fake.tempo[build_traceql("cart", errors=True)] = [
        {
            "traceID": TRACE_A,
            "rootServiceName": "frontend",
            "rootTraceName": "GET /api/cart",
            "startTimeUnixNano": str(int(PEAK.timestamp() * 1e9)),
            "durationMs": 1234,
        },
        {"traceID": "11111111111111111111111111111111", "rootServiceName": "frontend"},
    ]


async def _run(
    ctx: AnalysisContext, fake: FakeBackend, *, logs: bool = True, traces: bool = True
) -> AgentResult:
    budget = ToolBudget(100)
    async with (
        PrometheusClient.from_config(PROM, transport=fake.transport()) as prom,
        LokiClient.from_config(LOKI, transport=fake.transport()) as loki,
        TempoClient.from_config(TEMPO, transport=fake.transport()) as tempo,
    ):
        tool = CatalogQueryTool(
            CATALOG, prom, agent=AgentName.SERVICE, budget=budget, timeout_seconds=5
        )
        agent = ServiceAgent(
            tool,
            AnalysisConfig(),
            logs=LogQueryTool(
                CATALOG, loki, agent=AgentName.SERVICE, budget=budget, timeout_seconds=5
            )
            if logs
            else None,
            traces=TraceSearchTool(tempo, agent=AgentName.SERVICE, budget=budget, timeout_seconds=5)
            if traces
            else None,
        )
        return await agent.run(TASK, ctx, {})


def _by_text(result: AgentResult) -> dict[str, object]:
    return {f.statement: f for f in result.findings}


async def test_red_thresholds_and_error_detail() -> None:
    ctx = _ctx()
    fake = FakeBackend()
    _red(ctx, fake)
    _details(ctx, fake)
    result = await _run(ctx, fake)
    assert result.status is AgentStatus.SUCCESS, result.errors
    texts = [f.statement for f in result.findings]
    by = {f.statement: f for f in result.findings}
    cart_err = next(t for t in texts if t.startswith("서비스 오류율(현재, SERVER span): cart"))
    assert "25.0% (기준 20.0% 이상, 요청 2건/초)" in cart_err  # SERVER span만 합산(1.5+0.5)
    assert by[cart_err].severity is Severity.CRITICAL
    assert "서비스 응답 지연 p95(현재): cart 1.5초 (기준 1초 이상)" in texts
    assert any(t.startswith("서비스 간 호출 실패율(현재): frontend → cart 15.0%") for t in texts)
    assert any("요청이 적어" in x and "1개" in x for x in result.limitations)  # tiny
    assert any("service.db_span_latency_p95: 결과가 없어" in x for x in result.limitations)
    # 오류율 최고 시점 → 그 전후 10분 구간에서 로그·트레이스 확인
    peak = next(t for t in texts if t.startswith("오류율 최고 시점 (cart"))
    assert "40.0% (5분 rate 기준)" in peak
    assert any(
        t.startswith("오류 키워드 로그 (cart, ") and t.endswith("12건 / 전체 1000건") for t in texts
    )
    assert any(t.startswith("오류 트레이스 (cart, ") and TRACE_A in t for t in texts)
    linked = next(
        t for t in texts if t.startswith("오류 로그와 오류 트레이스가 같은 trace_id로 연결됨")
    )
    assert TRACE_A in linked and TRACE_B not in linked
    assert by[linked].basis is JudgementBasis.STATE
    # 로그 발췌: 제어문자 제거, 비밀값 마스킹, 불필요한 라벨 제외, 본문에서 trace_id 추출
    samples = next(e for e in result.evidence if e.evidence_id == "log.error_samples@cart")
    rows = samples.data
    assert isinstance(rows, list)
    assert "hunter22" not in json.dumps(rows) and "\x1b" not in rows[0]["line"]
    assert "secret_label" not in rows[0]["labels"]
    assert {r["trace_id"] for r in rows} == {TRACE_A, TRACE_B}
    # 근거 ID는 중복 없이 대상별로 구분
    ids = [e.evidence_id for e in result.evidence]
    assert len(ids) == len(set(ids)) and "service.error_ratio@series:cart" in ids
    # 등록하지 않은(빈 결과) 조회는 일부러 비워 둔 DB span·서비스 간 지연뿐
    assert all(
        ("db_system_name" in q or "service_graph_request_server_seconds" in q)
        for q in fake.unmatched
    ), fake.unmatched


async def test_no_request_data_is_not_normal() -> None:
    ctx = _ctx()
    fake = FakeBackend()
    result = await _run(ctx, fake)
    assert not any("기준" in f.statement and "미만" in f.statement for f in result.findings)
    assert any("서비스 요청 데이터가 없어" in x for x in result.limitations)
    assert not any(path.startswith("/loki") for path, _ in fake.calls)


async def test_stale_request_data_is_not_judged() -> None:
    ctx = _ctx()
    fake = FakeBackend(freshness_seconds=3600)
    _red(ctx, fake)
    result = await _run(ctx, fake, logs=False, traces=False)
    assert not any(f.statement.startswith("서비스 오류율") for f in result.findings)
    assert any("오래되었거나 최신성을 확인하지 못해" in x for x in result.limitations)


async def test_missing_logs_and_disabled_sources() -> None:
    ctx = _ctx()
    fake = FakeBackend()
    _red(ctx, fake)
    _details(ctx, fake, lines_total=0)
    result = await _run(ctx, fake, traces=False)
    assert not any(f.statement.startswith("오류 키워드 로그") for f in result.findings)
    assert any("구간의 로그가 없어 오류 로그를 판단하지 않음" in x for x in result.limitations)
    assert any("Tempo가 설정되지 않아" in x for x in result.limitations)
    no_loki = await _run(ctx, fake, logs=False)
    assert any("Loki가 설정되지 않아" in x for x in no_loki.limitations)


async def test_loki_failure_is_partial() -> None:
    ctx = _ctx()
    fake = FakeBackend()
    _red(ctx, fake)
    _details(ctx, fake)
    fake.loki_fail.add(_loki("log.error_lines", FOCUS))
    result = await _run(ctx, fake)
    assert result.status is AgentStatus.PARTIAL
    assert any(x.startswith("log.error_lines: 조회 실패") for x in result.limitations)


async def test_error_increase_drives_detail() -> None:
    ctx = _ctx(Intent.ANOMALY)
    fake = FakeBackend()
    _red(ctx, fake, cart_error=0.03)  # 현재 오류율은 경고 미만
    _details(ctx, fake)
    assert ctx.baseline_range is not None
    for mode, at, value, latency in (
        (QueryMode.WINDOW_AVG, ctx.time_range.end, 0.08, 0.9),
        (QueryMode.BASELINE_AVG, ctx.baseline_range.end, 0.01, 0.3),
    ):
        # 구간 평균과 기준 구간 평균은 조회식이 같고 평가 시각만 다름
        fake.add(
            _expr("service.error_ratio", ctx, mode),
            [({"service": "cart", "span_kind": SERVER}, value)],
            at=at,
        )
        fake.add(
            _expr("service.latency_p95", ctx, mode),
            [({"service": "cart", "span_kind": SERVER}, latency)],
            at=at,
        )
    result = await _run(ctx, fake)
    texts = [f.statement for f in result.findings]
    assert "서비스 오류율 증가: cart 평균 1.0% → 8.0% (+7.0%p)" in texts
    assert "서비스 응답 지연 p95 증가: cart 평균 0.3초 → 0.9초 (+200%)" in texts
    assert any(t.startswith("오류율 최고 시점 (cart, 직전 구간 대비 오류율 증가)") for t in texts)
    assert any("DB 호출·서비스 간 호출 지연 비교" in x for x in result.next_checks)


async def test_overview_when_question_asks_for_logs() -> None:
    ctx = _ctx(question="최근 로그와 트레이스 보여줘")
    fake = FakeBackend()
    _red(ctx, fake, cart_error=0.0)
    window = ctx.time_range
    sel = 'service_name=~".+"'
    fake.loki_metrics[_loki("log.lines_total", window, sel)] = [({"service_name": "cart"}, 500)]
    fake.loki_metrics[_loki("log.error_lines", window, sel)] = [({"service_name": "cart"}, 7)]
    fake.tempo[build_traceql(None, errors=True)] = [
        {"traceID": TRACE_A, "rootServiceName": "frontend"}
    ]
    result = await _run(ctx, fake)
    texts = [f.statement for f in result.findings]
    assert any(t.startswith("오류 키워드 로그가 많은 서비스") and "cart 7건" in t for t in texts)
    assert any(t.startswith("오류 트레이스 (") and "frontend 1건" in t for t in texts)
    assert any("요청이 있는 서비스 모두 오류 span 없음" in t for t in texts)


def test_traceql_rejects_injection() -> None:
    assert (
        build_traceql("cart", errors=True) == '{ resource.service.name = "cart" && status = error }'
    )
    for bad in ('cart" || true', "a b", ""):
        try:
            build_traceql(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


async def test_observations_include_samples_only_with_full_policy() -> None:
    ctx = _ctx()
    fake = FakeBackend()
    _red(ctx, fake)
    _details(ctx, fake)
    result = await _run(ctx, fake)
    full = build_observations([result], DataPolicy.FULL) or ""
    aggregated = build_observations([result], DataPolicy.AGGREGATED) or ""
    assert "redis timeout" in full and TRACE_A in full
    assert "redis timeout" not in aggregated and '"samples_count": 2' in aggregated
    assert "hunter22" not in full


async def test_answer_question_service_flow() -> None:
    settings = load_settings(
        environ={
            "INFRA_AGENT_PROFILE": "dev-tunnel",
            "INFRA_AGENT__DATASOURCES__PROMETHEUS__ENABLED": "true",
            "INFRA_AGENT__DATASOURCES__PROMETHEUS__URL": "http://prom.synthetic.test",
            "INFRA_AGENT__DATASOURCES__LOKI__ENABLED": "true",
            "INFRA_AGENT__DATASOURCES__LOKI__URL": "http://loki.synthetic.test",
            "INFRA_AGENT__DATASOURCES__TEMPO__ENABLED": "true",
            "INFRA_AGENT__DATASOURCES__TEMPO__URL": "http://tempo.synthetic.test",
        }
    )
    ctx = _ctx(Intent.ANOMALY)
    fake = FakeBackend()
    _red(ctx, fake)
    _details(ctx, fake)
    bundle = await answer_question(
        "오류가 증가한 시간대의 로그와 트레이스를 연결해서 원인 후보를 알려줘",
        settings,
        CATALOG,
        now=NOW,
        transport=fake.transport(),
    )
    assert [r.agent for r in bundle.results] == [AgentName.SERVICE]
    text = render_text(bundle)
    assert "(에이전트 실행: service " in text
    assert "· (로그 원문) " in text and "[cart]" in text
    assert "· (트레이스) " in text and TRACE_A in text
    assert "hunter22" not in text
    assert " 구간 조회, " in text and " 시계열(최고 시점 탐색), " in text


async def test_detail_skipped_when_agent_time_is_short() -> None:
    from infra_agent.agents.base import agent_deadline

    ctx = _ctx()
    fake = FakeBackend()
    _red(ctx, fake)
    _details(ctx, fake)
    with agent_deadline(5.0):  # 기본 상세 최소 시간(30초)보다 짧음
        result = await _run(ctx, fake)
    assert any(
        f.statement.startswith("서비스 오류율(현재, SERVER span): cart") for f in result.findings
    )
    assert not any(f.statement.startswith("오류율 최고 시점") for f in result.findings)
    assert any("남은 시간이 부족해 cart 로그·트레이스 상세" in x for x in result.limitations)
    assert not any(path.startswith("/loki") for path, _ in fake.calls)
