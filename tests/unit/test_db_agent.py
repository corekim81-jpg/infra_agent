"""DB Agent 테스트 (가상 데이터, 실제 카탈로그 조회식 사용)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from expr_prom import ExprProm
from infra_agent.agents.db import COUNT_NOTE, POOL_NOTE, SPAN_REUSED, DbAgent, db_entity
from infra_agent.answer.render import render_text
from infra_agent.answer.synthesis import synthesize
from infra_agent.catalog import load_catalog
from infra_agent.config import load_settings
from infra_agent.config.settings import AnalysisConfig, HttpDatasourceConfig
from infra_agent.datasources import PrometheusClient
from infra_agent.orchestration.rules import interpret
from infra_agent.orchestration.runner import answer_question
from infra_agent.schemas import (
    AgentName,
    AgentResult,
    AgentStatus,
    AgentTask,
    AnalysisContext,
    Budget,
    DataSourceKind,
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

ROOT = Path(__file__).resolve().parents[2]
CATALOG = load_catalog(ROOT / "config/catalog/otel-demo.yaml")
NOW = datetime(2026, 9, 29, 3, 0, tzinfo=UTC)
CFG = HttpDatasourceConfig(enabled=True, url="http://prom.synthetic.test")
TASK = AgentTask(task_id="db-1", agent=AgentName.DB, objective="test")
PG = {"k8s_namespace_name": "otel-demo", "k8s_pod_name": "postgresql-0"}
COUNT_ITEMS = (
    "db.pool_waits_increase",
    "db.pg_deadlocks_increase",
    "cache.valkey_evicted_increase",
    "cache.valkey_rejected_increase",
)


def _ctx(intent: Intent = Intent.STATUS, targets: dict[TargetKind, str] | None = None):  # type: ignore[no-untyped-def]
    tr = TimeRange.last(timedelta(minutes=30), NOW)
    return AnalysisContext(
        request_id="r1",
        question="DB 상태",
        intent=intent,
        time_range=tr,
        baseline_range=tr.previous() if intent is not Intent.STATUS else None,
        targets=tuple(TargetRef(kind=k, name=v) for k, v in (targets or {}).items()),
        budget=Budget(max_llm_calls=0, max_tool_calls=100, deadline=NOW),
    )


def _tool(prom: PrometheusClient | None) -> CatalogQueryTool:
    return CatalogQueryTool(
        CATALOG,
        prom,  # type: ignore[arg-type]
        agent=AgentName.DB,
        budget=ToolBudget(100),
        timeout_seconds=5,
    )


def _expr(key: str, ctx: AnalysisContext, mode: QueryMode | None = None, selector: str = "") -> str:
    tool = _tool(None)
    if mode is None:
        mode = QueryMode.WINDOW if key in COUNT_ITEMS else QueryMode.CURRENT
    return tool.build_expr(tool.item(key), selector, mode, ctx)


def _fill(ctx: AnalysisContext, fake: ExprProm) -> None:
    """문제 대상과 정상 대상이 섞인 가상 결과."""
    fake.add(_expr("db.pg_connection_utilization", ctx), [(PG, 0.95)])
    fake.add(
        _expr("db.pool_utilization_accounting", ctx),
        [({"service_name": "accounting", "db_client_connection_pool_name": "Host=x"}, 0.85)],
    )
    fake.add(
        _expr("db.pool_utilization_product_catalog", ctx),
        [({"service_name": "product-catalog"}, 0.1)],
    )
    fake.add(_expr("db.pool_waits_increase", ctx), [({"service_name": "product-catalog"}, 0.0)])
    fake.add(
        _expr("db.pg_deadlocks_increase", ctx),
        [
            ({"k8s_pod_name": "postgresql-0", "postgresql_database_name": "otel"}, 2.0),
            ({"k8s_pod_name": "postgresql-0", "postgresql_database_name": "postgres"}, 0.0),
        ],
    )
    fake.add(_expr("db.pg_rollback_ratio", ctx), [({"postgresql_database_name": "otel"}, 0.2)])
    fake.add(_expr("db.pg_cache_hit_ratio", ctx), [({"postgresql_database_name": "otel"}, 0.5)])
    fake.add(
        _expr("db.client_operation_latency_p95", ctx),
        [({"service_name": "product-catalog", "db_operation_name": "SELECT"}, 1.5)],
    )
    fake.add(
        _expr("db.span_latency_p95", ctx),
        [
            (
                {"service": "accounting", "db_system_name": "postgresql", "span_name": "INSERT"},
                0.2,
            )
        ],
    )
    fake.add(_expr("cache.valkey_evicted_increase", ctx), [({"service_name": "valkey-cart"}, 0.0)])
    fake.add(
        _expr("cache.valkey_rejected_increase", ctx), [({"service_name": "valkey-cart"}, 3.02)]
    )
    fake.add(
        _expr("db.pg_backends", ctx),
        [
            ({"k8s_pod_name": "postgresql-0", "postgresql_database_name": "otel"}, 12.0),
            ({"k8s_pod_name": "postgresql-0", "postgresql_database_name": "postgres"}, 1.0),
        ],
    )
    fake.add(_expr("db.pg_db_size", ctx), [({"postgresql_database_name": "otel"}, 12 * 1024**2)])
    fake.add(_expr("cache.valkey_clients", ctx), [({"service_name": "valkey-cart"}, 5.0)])
    fake.add(_expr("cache.valkey_memory_used", ctx), [({"service_name": "valkey-cart"}, 2048.0)])
    fake.add(_expr("cache.valkey_hit_ratio", ctx), [({"service_name": "valkey-cart"}, 0.8)])


async def _run(
    ctx: AnalysisContext, fake: ExprProm, upstream: dict[str, AgentResult] | None = None
) -> AgentResult:
    async with PrometheusClient.from_config(CFG, transport=fake.transport()) as prom:
        return await DbAgent(_tool(prom), AnalysisConfig()).run(TASK, ctx, upstream or {})


async def test_thresholds_counts_and_context() -> None:
    ctx = _ctx()
    fake = ExprProm()
    _fill(ctx, fake)
    result = await _run(ctx, fake)
    assert result.status is AgentStatus.SUCCESS, result.errors
    by = {f.statement: f for f in result.findings}
    pg = by[
        "PostgreSQL 연결 사용률(최대 연결 수 대비)(현재): otel-demo/postgresql-0 95.0% "
        "(기준 90.0% 이상)"
    ]
    assert pg.severity is Severity.CRITICAL and pg.targets[0].kind is TargetKind.POD
    # 커넥션 풀: 풀 이름(연결 문자열일 수 있음)은 대상 이름에 넣지 않음
    pool = by[
        "앱 커넥션 풀 사용률(사용 중 연결, 최대 대비)(현재): accounting 커넥션 풀 85.0% "
        "(기준 80.0% 이상)"
    ]
    assert pool.severity is Severity.WARNING and "Host=x" not in pool.targets[0].name
    assert (
        "앱 커넥션 풀 사용률(사용 중 연결, 최대 대비)(현재): 대상 1개 모두 기준(80.0%) 미만, "
        "최대 product-catalog 10.0%"
    ) in by
    assert by["PostgreSQL 데드락 (최근 30분): otel 2회"].severity is Severity.WARNING
    assert "PostgreSQL 데드락 (최근 30분): postgres 0회" not in by  # 0은 발생으로 보지 않음
    assert "커넥션 풀 대기 (최근 30분): 발생 없음 (product-catalog)" in by
    assert by["PostgreSQL 롤백 비율(현재, 5분): otel 20.0% (기준 5.0% 이상)"].severity is (
        Severity.WARNING
    )
    cache = by["PostgreSQL 버퍼 캐시 적중률(현재, 5분): otel 50.0% (기준 90.0% 미만)"]
    assert cache.severity is Severity.WARNING and cache.basis is JudgementBasis.THRESHOLD
    assert "DB 작업 지연 p95(현재): product-catalog SELECT 1.5초 (기준 1초 이상)" in by
    assert (
        "DB 호출 span 지연 p95(현재): 대상 1개 모두 기준(1초) 미만, "
        "최대 accounting → postgresql (INSERT) 0.2초"
    ) in by
    assert "Valkey 키 퇴출 (최근 30분): 발생 없음 (valkey-cart)" in by
    assert "Valkey 연결 거부 (최근 30분): valkey-cart 3회" in by
    # 현재 값 (판정 없음)
    assert "PostgreSQL DB별 연결 수(현재): otel 12개, postgres 1개" in by
    assert "PostgreSQL DB 크기(현재): otel 12.0MiB" in by
    assert "Valkey 연결 클라이언트 수(현재): valkey-cart 5개" in by
    assert "Valkey 키 조회 적중률(현재, 5분): valkey-cart 80.0%" in by
    assert POOL_NOTE in result.scope_notes and COUNT_NOTE in result.scope_notes
    assert any("pg_stat_statements" in x for x in result.scope_notes)  # 수집 범위 안내
    # 발생 수는 분석 구간 전체(30분) 증가량, 비율·지연은 5분 rate
    queried = [q for q, _ in fake.queries]
    assert any("increase(postgresql_deadlocks_total{}[1800s])" in q for q in queried)
    assert any("rate(postgresql_rollbacks_total{}[5m])" in q for q in queried)
    ids = [e.evidence_id for e in result.evidence]
    assert len(ids) == len(set(ids)) and "db.pg_deadlocks_increase@window" in ids


async def test_stale_or_missing_data_is_not_normal() -> None:
    ctx = _ctx()
    stale = ExprProm(freshness_seconds=3600)
    _fill(ctx, stale)
    result = await _run(ctx, stale)
    texts = [f.statement for f in result.findings]
    assert not any("모두 기준" in t or "발생 없음" in t for t in texts)
    assert any("최신성을 확인하지 못해 발생 여부를 판단하지 않음" in x for x in result.limitations)
    # 문제 대상은 오래된 데이터라도 사실로 보여 주되 한계에 지연을 표시
    assert any(t.startswith("PostgreSQL 데드락 (최근 30분): otel") for t in texts)
    assert any("오래되어" in x for x in result.limitations)
    empty = await _run(ctx, ExprProm())
    assert not any("모두 기준" in f.statement or "발생 없음" in f.statement for f in empty.findings)
    assert any(
        x.startswith("db.pool_utilization_product_catalog: 결과가 없어 판단하지 않음")
        and "최대 열린 연결 수가 0" in x
        for x in empty.limitations
    )
    assert any(
        x.startswith("db.pg_rollback_ratio: 결과가 없어") and "트랜잭션" in x
        for x in empty.limitations
    )


async def test_target_without_series_and_partial_failure() -> None:
    ctx = _ctx(targets={TargetKind.NAMESPACE: "otel-demoo"})  # 오타난 namespace
    result = await _run(ctx, ExprProm())
    assert any(
        x.startswith("db.pg_connection_utilization: 요청 대상(namespace=otel-demoo)에 해당하는")
        for x in result.limitations
    )
    ctx2 = _ctx()
    fake = ExprProm()
    _fill(ctx2, fake)
    fake.fail(_expr("db.pg_rollback_ratio", ctx2))
    partial = await _run(ctx2, fake)
    assert partial.status is AgentStatus.PARTIAL
    assert any(x.startswith("db.pg_rollback_ratio: 조회 실패") for x in partial.limitations)
    assert any(f.statement.startswith("PostgreSQL 데드락") for f in partial.findings)


async def test_reuses_service_db_span_result() -> None:
    ctx = _ctx()
    fake = ExprProm()
    _fill(ctx, fake)
    service = AgentResult(
        task_id="service-1",
        agent=AgentName.SERVICE,
        status=AgentStatus.SUCCESS,
        evidence=(
            ToolResult(
                evidence_id="service.db_span_latency_p95@current",
                source=DataSourceKind.PROMETHEUS,
                query="q",
                status=ToolStatus.OK,
                data=[],
                fetched_at=NOW,
                synthetic=True,
            ),
        ),
    )
    result = await _run(ctx, fake, {"service-1": service})
    assert SPAN_REUSED in result.limitations
    assert not any("DB 호출 span 지연" in f.statement for f in result.findings)
    assert _expr("db.span_latency_p95", ctx) not in [q for q, _ in fake.queries]


async def test_latency_increase_for_anomaly_questions() -> None:
    ctx = _ctx(Intent.ANOMALY)
    fake = ExprProm()
    _fill(ctx, fake)
    assert ctx.baseline_range is not None
    op = {"service_name": "product-catalog", "db_operation_name": "SELECT"}
    span = {"service": "accounting", "db_system_name": "postgresql", "span_name": "INSERT"}
    new_op = {"service_name": "product-catalog", "db_operation_name": "UPDATE"}
    for mode, at, op_rows, span_value in (
        (QueryMode.WINDOW_AVG, ctx.time_range.end, [(op, 0.5), (new_op, 0.3)], 0.21),
        (QueryMode.BASELINE_AVG, ctx.baseline_range.end, [(op, 0.1)], 0.2),
    ):
        # 구간 평균과 기준 구간 평균은 조회식이 같고 평가 시각만 다름
        fake.add(_expr("db.client_operation_latency_p95", ctx, mode), op_rows, at=at)
        fake.add(_expr("db.span_latency_p95", ctx, mode), [(span, span_value)], at=at)
    result = await _run(ctx, fake)
    texts = [f.statement for f in result.findings]
    assert "DB 작업 지연 p95 증가: product-catalog SELECT 평균 0.1초 → 0.5초 (+400%)" in texts
    assert any(
        t.startswith("DB 호출 span 지연 p95: 비교 대상 1개 중") and "증가한 대상 없음" in t
        for t in texts
    )
    assert any("pg_stat_statements 수집 설정 필요" in x for x in result.next_checks)
    # 기준 구간에 없던 대상은 조용히 빼지 않고 한계에 적음
    assert (
        "db.client_operation_latency_p95: 기준 구간 값이 없거나 0인 대상 1개는 비교하지 않음"
    ) in result.limitations
    # 비교 질문 답변은 직전 구간 대비 결과를 먼저 요약 (DB만 실행된 경우도)
    interp = interpret("DB 쿼리 지연이 늘었어?", NOW, timedelta(minutes=30))
    assert interp.domains == {"db"} and interp.intent is Intent.ANOMALY
    answer = synthesize("r1", interp, [result])
    assert answer.summary.startswith("직전 같은 길이 구간 대비 기준 이상 증가한 대상 1건")


async def test_latency_at_histogram_top_bucket_is_lower_bound() -> None:
    ctx = _ctx()
    fake = ExprProm()
    _fill(ctx, fake)
    fake.add(
        _expr("db.client_operation_latency_p95", ctx),
        [({"service_name": "product-catalog", "db_operation_name": "SELECT"}, 10.0)],
    )
    fake.add(
        "count by (le) (db_client_operation_duration_seconds_bucket{})",
        [({"le": le}, 1.0) for le in ("0.1", "1", "10", "+Inf")],
    )
    result = await _run(ctx, fake)
    assert (
        "DB 작업 지연 p95(현재): product-catalog SELECT 10초 이상(히스토그램 최대 구간) "
        "(기준 1초 이상)"
    ) in [f.statement for f in result.findings]
    assert any(
        x.startswith("db.client_operation_latency_p95: p95가 히스토그램 최대 유한 구간 경계(10초)")
        for x in result.limitations
    )


def test_entity_names() -> None:
    assert db_entity({"postgresql_database_name": "otel"}).kind is TargetKind.DATABASE
    pool = db_entity({"service_name": "accounting", "db_client_connection_pool_name": "Host=x"})
    assert pool.name == "accounting 커넥션 풀" and "Host=x" not in str(pool.labels)
    assert db_entity(PG).name == "otel-demo/postgresql-0"


def _settings():  # type: ignore[no-untyped-def]
    return load_settings(
        environ={
            "INFRA_AGENT_PROFILE": "dev-tunnel",
            "INFRA_AGENT__DATASOURCES__PROMETHEUS__ENABLED": "true",
            "INFRA_AGENT__DATASOURCES__PROMETHEUS__URL": "http://prom.synthetic.test",
        }
    )


async def test_db_question_runs_only_db_agent() -> None:
    fake = ExprProm()
    bundle = await answer_question(
        "DB 커넥션 풀이 부족하거나 쿼리가 느려진 징후가 있어?",
        _settings(),
        CATALOG,
        now=NOW,
        transport=fake.transport(),
    )
    assert [r.agent for r in bundle.results] == [AgentName.DB]
    assert bundle.answer.unverified_areas == (
        "DB·캐시: 판단에 사용할 결과가 없어 확인하지 못함 (사유는 한계 참고)",
    )
    text = render_text(bundle)
    assert "(에이전트 실행: db 성공 " in text


async def test_slow_service_question_runs_service_then_network_and_db() -> None:
    bundle = await answer_question(
        "서비스 응답이 느려진 이유가 네트워크인지 DB인지 분석해 줘",
        _settings(),
        CATALOG,
        now=NOW,
        transport=ExprProm().transport(),
    )
    # Service 다음에 Network·DB (서로 독립이라 병렬)
    assert [r.agent for r in bundle.results] == [
        AgentName.SERVICE,
        AgentName.NETWORK,
        AgentName.DB,
    ]
    assert [run.depends_on for run in bundle.runs] == [(), ("service-1",), ("service-1",)]
    assert bundle.answer.unverified_areas == tuple(
        f"{d}: 판단에 사용할 결과가 없어 확인하지 못함 (사유는 한계 참고)"
        for d in ("서비스·로그·트레이스", "네트워크", "DB·캐시")
    )
    # 데이터가 없으면 교차 확인은 "이상 없음"이 아니라 판단 불가로 답함
    text = render_text(bundle)
    assert (
        "[분야 간 교차 확인]\n- 서비스: 오류율·응답 지연 판정 결과가 없어 이상 대상을 정하지 못함"
        in text
    )
    assert "판정 결과가 없어 분야 간 연결을 판단하지 않았습니다." in bundle.answer.summary


async def test_first_bucket_interpolation_is_not_judged() -> None:
    """p95가 넓은 첫 구간(0~5초) 안의 보간값이면 기준 초과로 판정하지 않음.

    개발 서버 live에서 product-catalog DB 작업 지연 p95가 여러 작업 모두 4.75초로 관측됨.
    """
    ctx = _ctx(Intent.ANOMALY)
    fake = ExprProm()
    _fill(ctx, fake)
    assert ctx.baseline_range is not None
    ops = [
        ({"service_name": "product-catalog", "db_operation_name": name}, 4.75)
        for name in ("sql.conn.query", "sql.rows")
    ]
    fake.add(_expr("db.client_operation_latency_p95", ctx), ops)
    for mode, at in (
        (QueryMode.WINDOW_AVG, ctx.time_range.end),
        (QueryMode.BASELINE_AVG, ctx.baseline_range.end),
    ):
        fake.add(_expr("db.client_operation_latency_p95", ctx, mode), ops, at=at)
    # 같은 지표를 다른 서비스가 좁은 경계(초 단위)로 보내면, 경계를 합쳤을 때 첫 구간이 좁아 보임.
    # 서비스별 경계로 판단해야 product-catalog의 넓은 첫 구간(0~5초)을 알아챔
    narrow = ("0.005", "0.01", "0.1", "1", "+Inf")
    wide = ("0", "5", "10", "25", "+Inf")
    fake.add(
        "count by (le) (db_client_operation_duration_seconds_bucket{})",
        [({"le": le}, 1.0) for le in narrow + wide],
    )
    fake.add(
        "count by (le, service_name) (db_client_operation_duration_seconds_bucket{})",
        [({"le": le, "service_name": "accounting"}, 1.0) for le in narrow]
        + [({"le": le, "service_name": "product-catalog"}, 1.0) for le in wide],
    )
    result = await _run(ctx, fake)
    texts = [f.statement for f in result.findings]
    assert not any(t.startswith("DB 작업 지연 p95") for t in texts)
    note = next(
        x for x in result.limitations if x.startswith("db.client_operation_latency_p95: 히스토그램")
    )
    assert "첫 구간이 0~5초로 넓어" in note and "product-catalog sql.conn.query" in note
    assert "4.75초" in note and "5초 이하" in note
    assert any("버킷 경계를 초 단위 지연에 맞게 설정" in x for x in result.next_checks)
    assert any(
        "첫 구간(0~5초) 안의 보간값인 대상 2개는 비교하지 않음" in x for x in result.limitations
    )


async def test_pool_with_unlimited_max_open() -> None:
    ctx = _ctx()
    fake = ExprProm()
    _fill(ctx, fake)
    fake.add(_expr("db.pool_utilization_product_catalog", ctx), [])  # 분모 0 → 무한대라 제외됨
    fake.add(
        _expr("db.pool_max_open_product_catalog", ctx), [({"service_name": "product-catalog"}, 0.0)]
    )
    result = await _run(ctx, fake)
    assert any(
        x.startswith(
            "db.pool_utilization_product_catalog: product-catalog의 최대 열린 연결 수 설정이 0"
        )
        for x in result.limitations
    )
    assert not any("풀 지표가 없거나" in x for x in result.limitations)
