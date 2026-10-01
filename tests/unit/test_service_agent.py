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
from infra_agent.units import fmt_time

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
TRACE_A = "0bf92f3577b34da6a3ce929d0e0e4736"  # 앞자리 0: Tempo는 이를 뺀 31자리로 돌려줌
TRACE_B = "00f067aa0ba902b7a3ce929d0e0e4700"
SLOW_TRACE = "44" * 16


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
        "count by (le) (traces_spanmetrics_latency_bucket{})",
        [({"le": le}, 3.0) for le in ("0.1", "1", "10", "+Inf")],
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
            "traceID": TRACE_A.lstrip("0"),  # Tempo 검색 응답 형식 (앞자리 0 생략)
            "rootServiceName": "frontend",
            "rootTraceName": "GET /api/cart",
            "startTimeUnixNano": str(int(PEAK.timestamp() * 1e9)),
            "durationMs": 1234,
        },
        {"traceID": "11111111111111111111111111111111", "rootServiceName": "frontend"},
    ]
    # 응답 지연(현재 1.5초, 기준 초과) → 지연 최고 시점과 그 시점 p95 이상 걸린 SERVER span 검색
    fake.add_range(
        _expr("service.latency_p95", ctx, sel='service="cart"'),
        [
            (
                {"service": "cart", "span_kind": SERVER},
                [(NOW - timedelta(minutes=20), 0.3), (PEAK, 2.0), (NOW, 1.5)],
            ),
            # 오래 열린 CLIENT 호출은 서비스 응답 지연 기준(SERVER)이 아니므로 무시
            ({"service": "cart", "span_kind": "SPAN_KIND_CLIENT"}, [(PEAK, 9.0)]),
        ],
    )
    fake.tempo[build_traceql("cart", min_duration_ms=2000, server_only=True)] = [
        {
            "traceID": SLOW_TRACE,
            "rootServiceName": "frontend",
            "rootTraceName": "GET /api/cart",
            "durationMs": 3500,
        },
        {"traceID": "55" * 16, "rootServiceName": "frontend", "durationMs": 2100},
    ]


async def _run(
    ctx: AnalysisContext,
    fake: FakeBackend,
    *,
    logs: bool = True,
    traces: bool = True,
    analysis: AnalysisConfig | None = None,
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
            analysis or AnalysisConfig(),
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
    # Tempo의 31자리 trace_id를 32자리로 맞춰 표시하고, 로그의 trace_id와 연결
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
    # 느린 트레이스: SERVER span 기준 최고 시점(2초)으로 검색, CLIENT 9초는 무시
    slow_peak = next(t for t in texts if t.startswith("응답 지연 최고 시점 (cart"))
    assert "응답 지연 p95 기준 초과" in slow_peak and " 2초 (5분 p95 기준)" in slow_peak
    assert "2초 이상 걸린 SERVER span을 검색" in slow_peak
    slow = next(t for t in texts if t.startswith("느린 트레이스 (cart, "))
    assert "검색 2건, 트레이스 최장 3.5초" in slow and SLOW_TRACE in slow
    # 근거 ID는 중복 없이 대상별로 구분
    ids = [e.evidence_id for e in result.evidence]
    assert len(ids) == len(set(ids)) and "service.error_ratio@series:cart" in ids
    assert "service.latency_p95@series:cart" in ids and "trace.slow@cart" in ids
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


async def test_error_increase_without_compared_services_is_not_judged() -> None:
    """비교할 서비스가 없으면(구간 평균 결과 없음) "증가한 대상 없음"이라고 하지 않음."""
    ctx = _ctx(Intent.ANOMALY)
    fake = FakeBackend()
    _red(ctx, fake)
    assert ctx.baseline_range is not None
    for mode, at in (
        (QueryMode.WINDOW_AVG, ctx.time_range.end),
        (QueryMode.BASELINE_AVG, ctx.baseline_range.end),
    ):
        fake.add(_expr("service.error_ratio", ctx, mode), [], at=at)
    result = await _run(ctx, fake, logs=False, traces=False)
    assert not any(
        f.statement.startswith("서비스 오류율(SERVER span): 비교 대상") for f in result.findings
    )
    assert (
        "service.error_ratio: 직전 구간과 비교할 서비스 결과가 없어 오류율 증가를 판단하지 않음"
    ) in result.limitations


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
    _red(ctx, fake, cart_error=0.0)  # SERVER span 오류 없음, CLIENT span 오류 90%
    fake.add(_expr("service.dependency_failed_rate", ctx), [])  # 호출 실패도 없음
    # 요청을 받지 않고 호출·소비만 하는 서비스(SERVER span 없음)도 트레이스 대상임
    fake.responses[(_expr("service.request_rate", ctx), None)].append(
        ({"service": "worker", "span_kind": "SPAN_KIND_CONSUMER", "status_code": "x"}, 0.5)
    )
    fake.responses[(_expr("service.error_ratio", ctx), None)].append(
        ({"service": "worker", "span_kind": "SPAN_KIND_CLIENT"}, 1.0)
    )
    window = ctx.time_range
    everyone = 'service_name=~".+"'
    events = "kubernetes-cluster"  # 트레이스 지표가 없는 로그 출처 (가상)
    fake.loki_metrics[_loki("log.lines_total", window, everyone)] = [
        ({"service_name": "cart"}, 500),
        ({"service_name": events}, 300),
    ]
    fake.loki_metrics[_loki("log.error_lines", window, everyone)] = [
        ({"service_name": events}, 20),
        ({"service_name": "cart"}, 7),
    ]
    for name, total, errors in ((events, 300, 20), ("cart", 500, 7)):
        sel = f'service_name="{name}"'
        fake.loki_metrics[_loki("log.lines_total", window, sel)] = [({"service_name": name}, total)]
        fake.loki_metrics[_loki("log.error_lines", window, sel)] = [
            ({"service_name": name}, errors)
        ]
    fake.loki_logs[_loki("log.error_samples", window, 'service_name="cart"')] = [
        ({"service_name": "cart", "trace_id": TRACE_A}, [(PEAK, "error: redis timeout")])
    ]
    fake.loki_logs[_loki("log.error_samples", window, f'service_name="{events}"')] = [
        (
            {"service_name": events},
            [(PEAK, "Warning BackOff: back-off restarting failed container")],
        )
    ]
    fake.tempo[build_traceql(None, errors=True)] = [
        {"traceID": TRACE_A, "rootServiceName": "frontend"}
    ]
    fake.tempo[build_traceql("cart", errors=True)] = [
        {"traceID": TRACE_A, "rootServiceName": "frontend"}
    ]
    result = await _run(ctx, fake)
    texts = [f.statement for f in result.findings]
    # SERVER span 기준으로는 오류가 없지만, 다른 span 종류의 오류를 "없음"으로 숨기지 않음
    assert any("SERVER span 오류 없음" in t for t in texts)
    assert (
        "SERVER 외 span의 오류율(현재, 판정 기준 미적용): worker(CLIENT) 100.0%, cart(CLIENT) 90.0%"
    ) in texts
    assert not any("요청이 있는 서비스 모두 오류 span 없음" in t for t in texts)
    assert any(
        t.startswith("오류 키워드 로그가 많은 서비스") and f"{events} 20건, cart 7건" in t
        for t in texts
    )
    assert any(t.startswith("오류 트레이스 (") and "frontend 1건" in t for t in texts)
    # 오류 로그가 많은 서비스 기준으로 로그 샘플·트레이스를 보고 같은 trace_id를 연결.
    # 트레이스 지표가 없는 로그 출처는 서비스가 아니므로 상세 확인에서 제외
    assert not any(t.startswith(f"오류 키워드 로그 ({events}, ") for t in texts)
    assert any(t.startswith("오류 키워드 로그 (cart, ") for t in texts)
    assert any(
        t.startswith("오류 로그와 오류 트레이스가 같은 trace_id로 연결됨 (cart") for t in texts
    )
    assert any(
        x == f"트레이스 지표가 없는 로그 출처는 서비스 상세 확인에서 제외함: {events}"
        for x in result.limitations
    )
    # 선택 순서: 오류 로그 → SERVER 외 span 오류(현재) → 구간 오류 트레이스의 루트 서비스
    # 무엇을 대신 확인했는지는 한계가 아니라 사실로 답변 본문에 표시 (#33)
    picked = next(f for f in result.findings if f.statement.startswith("오류율 기준 초과·증가"))
    assert (
        "오류율 기준 초과·증가 서비스 없음 → 오류 로그가 많은 서비스·SERVER 외 span 오류가 "
        "있는 서비스·오류 트레이스의 루트 서비스 기준으로"
    ) in picked.statement
    assert picked.statement.endswith(": cart, worker, frontend")
    assert picked.evidence_ids == ("log.error_lines@window", "trace.errors@window")
    assert not any(q == build_traceql(events, errors=True) for _, q in fake.calls)
    assert any(q == build_traceql("worker", errors=True) for _, q in fake.calls)
    assert any(q == build_traceql("frontend", errors=True) for _, q in fake.calls)
    ids = [e.evidence_id for e in result.evidence]
    assert len(ids) == len(set(ids))


async def test_overview_falls_back_to_error_trace_roots() -> None:
    """현재 5분 오류율에는 없지만 분석 구간에 오류 트레이스가 있으면 그 루트 서비스를 확인."""
    ctx = _ctx(question="오류가 증가한 시간대의 로그와 트레이스를 연결해서 원인 후보를 알려줘")
    fake = FakeBackend()
    _red(ctx, fake, cart_error=0.0)
    fake.add(_expr("service.error_ratio", ctx), [])  # 현재(5분) 오류 span 없음
    fake.add(_expr("service.dependency_failed_rate", ctx), [])
    window = ctx.time_range
    everyone = 'service_name=~".+"'
    fake.loki_metrics[_loki("log.lines_total", window, everyone)] = [({"service_name": "cart"}, 9)]
    fake.loki_metrics[_loki("log.error_lines", window, everyone)] = []  # 오류 로그 없음
    fake.tempo[build_traceql(None, errors=True)] = [
        {"traceID": TRACE_A, "rootServiceName": "cart"},
        {"traceID": TRACE_B, "rootServiceName": "cart"},
        # 현재 5분 지표에는 없는 서비스도 트레이스에 나타났으면 서비스로 인정
        {"traceID": "22" * 16, "rootServiceName": "load-generator"},
        # 루트 span을 아직 받지 못한 트레이스 (Tempo 표시 문자열) → 서비스가 아님
        {"traceID": "33" * 16, "rootServiceName": "<root span not yet received>"},
    ]
    fake.tempo[build_traceql("cart", errors=True)] = [{"traceID": TRACE_A}]
    result = await _run(ctx, fake)
    note = next(
        f.statement for f in result.findings if f.statement.startswith("오류율 기준 초과·증가")
    )
    assert "오류 트레이스의 루트 서비스 기준으로" in note
    assert note.endswith(": cart, load-generator")
    assert any(
        t.startswith("오류 트레이스 (cart, ") for t in (f.statement for f in result.findings)
    )
    assert any(q == build_traceql("load-generator", errors=True) for _, q in fake.calls)
    assert not any("로그 출처는 서비스 상세 확인에서 제외" in x for x in result.limitations)
    overview = next(
        f.statement
        for f in result.findings
        if f.statement.startswith("오류 트레이스 (") and "루트 서비스별" in f.statement
    )
    assert "(루트 span 미수신) 1건" in overview and "not yet received" not in overview
    assert not any("형식이 올바르지 않습니다" in x for x in result.limitations)
    # 후보가 전혀 없으면 연결하지 않은 이유를 한계에 적음
    fake.tempo[build_traceql(None, errors=True)] = []
    empty = await _run(ctx, fake)
    assert any("상세 확인할 서비스를 찾지 못해" in x for x in empty.limitations)


def test_traceql_rejects_injection() -> None:
    assert (
        build_traceql("cart", errors=True) == '{ resource.service.name = "cart" && status = error }'
    )
    assert (
        build_traceql("cart", min_duration_ms=1500, server_only=True)
        == '{ resource.service.name = "cart" && kind = server && duration > 1500ms }'
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
    # 시각·지속 시간은 답변과 같은 표시값을 함께 전달 (모델이 UTC를 쓰거나 환산하지 않게)
    assert f'"start_display": "{fmt_time(PEAK)}"' in full
    assert '"duration_display": "1.23초"' in full
    assert f'"time_display": "{fmt_time(PEAK)}"' in full
    assert f'"time_range_display": "{fmt_time(FOCUS.start)} ~ {fmt_time(FOCUS.end)}"' in full


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
    assert " 구간 검색, " in text  # Tempo 검색은 "구간 집계"가 아님


async def test_slow_detail_without_matching_traces() -> None:
    """지연 기준 초과 서비스의 느린 트레이스가 없으면 없다고만 쓰고 정상으로 보지 않음."""
    ctx = _ctx()
    fake = FakeBackend()
    _red(ctx, fake, cart_error=0.0)
    fake.add(_expr("service.dependency_failed_rate", ctx), [])
    fake.add_range(
        _expr("service.latency_p95", ctx, sel='service="cart"'),
        [({"service": "cart", "span_kind": SERVER}, [(PEAK, 1.5), (NOW, 1.5)])],
    )
    result = await _run(ctx, fake)
    queries = [q for path, q in fake.calls if path.startswith("/api/search")]
    assert queries == [build_traceql("cart", min_duration_ms=1500, server_only=True)]
    assert any(
        x.startswith("cart: ") and "1.5초 이상 걸린 SERVER span 트레이스 검색 결과 없음" in x
        for x in result.limitations
    )
    assert not any(f.statement.startswith("느린 트레이스") for f in result.findings)


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


async def test_histogram_saturation_is_reported_as_lower_bound() -> None:
    ctx = _ctx()
    fake = FakeBackend()
    _red(ctx, fake, cart_error=0.0)
    fake.add(_expr("service.dependency_failed_rate", ctx), [])
    fake.add(
        _expr("service.dependency_latency_p95", ctx),
        [({"client": "ad", "server": "flagd"}, 12.8), ({"client": "cart", "server": "redis"}, 2.0)],
    )
    fake.add(
        "count by (le) (traces_service_graph_request_server_seconds_bucket{})",
        [({"le": le}, 5.0) for le in ("0.1", "1.6", "12.8", "+Inf")],
    )
    result = await _run(ctx, fake, logs=False, traces=False)
    texts = [f.statement for f in result.findings]
    assert (
        "서비스 간 호출 지연 p95(서버 측)(현재): ad → flagd 12.8초 이상(히스토그램 최대 구간) "
        "(기준 1초 이상)"
    ) in texts
    assert "서비스 간 호출 지연 p95(서버 측)(현재): cart → redis 2초 (기준 1초 이상)" in texts
    assert any("최대 유한 구간 경계(12.8초)와 같은 대상 1개" in x for x in result.limitations)


async def test_failing_call_to_untraced_server_focuses_on_client() -> None:
    """DB처럼 계측되지 않은 대상으로의 호출 실패는 호출한 쪽(client) 서비스로 상세 확인."""
    ctx = _ctx()
    fake = FakeBackend()
    _red(ctx, fake, cart_error=0.0)
    fake.responses[(_expr("service.request_rate", ctx), None)].append(
        ({"service": "accounting", "span_kind": "SPAN_KIND_CONSUMER", "status_code": "x"}, 0.2)
    )
    fake.add(
        _expr("service.dependency_request_rate", ctx),
        [({"client": "accounting", "server": "postgresql", "connection_type": "database"}, 0.06)],
    )
    fake.add(
        _expr("service.dependency_failed_rate", ctx),
        [({"client": "accounting", "server": "postgresql"}, 0.0039)],
    )
    fake.add_range(
        _expr("service.error_ratio", ctx, sel='service="accounting"'),
        [
            # 호출한 쪽 상세이므로 CLIENT span의 최고 시점을 씀 (CONSUMER의 더 큰 값은 무시)
            (
                {"service": "accounting", "span_kind": "SPAN_KIND_CLIENT"},
                [(NOW - timedelta(minutes=20), 0.01), (PEAK, 0.3), (NOW, 0.06)],
            ),
            (
                {"service": "accounting", "span_kind": "SPAN_KIND_CONSUMER"},
                [(NOW - timedelta(minutes=25), 0.9)],
            ),
        ],
    )
    sel = 'service_name="accounting"'
    fake.loki_metrics[_loki("log.lines_total", FOCUS, sel)] = [({"service_name": "accounting"}, 40)]
    fake.loki_metrics[_loki("log.error_lines", FOCUS, sel)] = [({"service_name": "accounting"}, 0)]
    fake.tempo[build_traceql("accounting", errors=True)] = [
        {"traceID": TRACE_A, "rootServiceName": "accounting", "rootTraceName": "order-consumed"}
    ]
    result = await _run(ctx, fake)
    texts = [f.statement for f in result.findings]
    assert any(
        t.startswith("서비스 간 호출 실패율(현재): accounting → postgresql 6.5%") for t in texts
    )
    peak = next(t for t in texts if t.startswith("오류율 최고 시점 (accounting"))
    assert "accounting → postgresql 호출 실패, 호출한 쪽" in peak and "30.0%" in peak
    assert any(t.startswith("오류 트레이스 (accounting, ") for t in texts)
    assert any(q == build_traceql("accounting", errors=True) for _, q in fake.calls)
    assert not any(q == build_traceql("postgresql", errors=True) for _, q in fake.calls)


async def test_failing_call_path_focuses_on_server_side() -> None:
    ctx = _ctx(question="오류 로그와 트레이스 연결해줘")
    fake = FakeBackend()
    _red(ctx, fake, cart_error=0.0)  # 오류율 기준 초과 없음, frontend → cart 호출 실패 15%
    window = ctx.time_range
    sel = 'service_name="cart"'
    fake.loki_metrics[_loki("log.lines_total", window, sel)] = [({"service_name": "cart"}, 100)]
    fake.loki_metrics[_loki("log.error_lines", window, sel)] = [({"service_name": "cart"}, 0)]
    fake.add_range(
        _expr("service.error_ratio", ctx, sel='service="cart"'),
        [({"service": "cart", "span_kind": SERVER}, [(PEAK, 0.0), (NOW, 0.0)])],
    )
    result = await _run(ctx, fake)
    texts = [f.statement for f in result.findings]
    # 호출 실패를 응답한 cart를 상세 대상으로 삼되, 오류율이 0뿐이면 "최고 시점"을 만들지 않음
    assert not any(t.startswith("오류율 최고 시점") for t in texts)
    assert any("0보다 큰 시점이 없어 구간 전체로" in x for x in result.limitations)
    assert any(t.startswith("오류 키워드 로그 (cart, ") and "0건 / 전체 100건" in t for t in texts)
    assert not any(t.startswith("오류율 기준 초과·증가 서비스 없음") for t in texts)


async def test_streaming_calls_are_not_judged_for_latency() -> None:
    """설정한 스트리밍 서비스로의 호출 지연은 경고·이상 대상에서 빼고 정보로 표시 (#33)."""
    from infra_agent.agents.upstream import service_issues

    ctx = _ctx()
    fake = FakeBackend()
    _red(ctx, fake)
    fake.add(
        _expr("service.dependency_latency_p95", ctx),
        [
            ({"client": "ad", "server": "flagd"}, 12.8),
            ({"client": "frontend", "server": "cart"}, 2.0),
        ],
    )
    default = await _run(ctx, fake, logs=False, traces=False)
    warned = [f.statement for f in default.findings if f.severity is Severity.WARNING]
    assert any("ad → flagd" in t for t in warned)  # 설정이 없으면 기존대로 경고

    cfg = AnalysisConfig(streaming_services=("FLAGD",))  # 대소문자 무시
    result = await _run(ctx, fake, logs=False, traces=False, analysis=cfg)
    warned = [f.statement for f in result.findings if f.severity is Severity.WARNING]
    assert not any("flagd" in t for t in warned)
    assert any("frontend → cart 2초" in t for t in warned)  # 다른 호출은 계속 판정
    info = next(
        f for f in result.findings if "스트리밍 호출로 설정되어 판정 기준 미적용" in f.statement
    )
    assert info.severity is Severity.INFO and "ad → flagd 12.8초" in info.statement
    assert any("analysis.streaming_services" in x and "flagd" in x for x in result.limitations)
    assert "flagd" not in service_issues([result]) and "cart" in service_issues([result])


async def test_cause_question_without_log_words_skips_overview() -> None:
    """질문의 "원인"만으로는 로그·트레이스 개요를 조회하지 않음 (#33)."""
    ctx = _ctx(question="서비스 응답이 느려진 원인이 뭐야?")
    fake = FakeBackend()
    _red(ctx, fake, cart_error=0.0)
    fake.add(_expr("service.dependency_failed_rate", ctx), [])
    result = await _run(ctx, fake)
    assert not any(path.startswith("/loki") for path, _ in fake.calls)
    assert not any("오류 키워드 로그가 많은 서비스" in f.statement for f in result.findings)
