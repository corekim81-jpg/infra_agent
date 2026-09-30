"""DB Agent — PostgreSQL·앱 커넥션 풀·DB 작업 지연·Valkey 분석 (모델 없이 결정적 판정).

데이터
- 카탈로그에서 agent=db로 지정된 항목만 조회합니다(조회 도구가 강제). PostgreSQL에 직접 SQL로
  접속하지 않으며, 수집기(OTel postgresql·redis 수신기)와 애플리케이션 계측 지표만 씁니다.
- `pg_stat_statements`(쿼리별 통계), 실행 계획, 잠금 대기·잠금 그래프는 수집되지 않아
  특정 쿼리·잠금을 원인으로 판단하지 않습니다.

판정 (기준값은 `analysis` 설정)
- 높을수록 문제: PostgreSQL 연결 사용률·앱 커넥션 풀 사용률(`utilization_*`), 롤백 비율
  (`rollback_ratio_warning`), DB 작업·DB 호출 span 지연 p95(`latency_p95_warning_seconds`,
  Service Agent의 DB 호출 판정과 같은 기준)
- 낮을수록 문제: 버퍼 캐시 적중률(`cache_hit_ratio_warning`)
- 구간 내 발생 수(0보다 크면 경고): 데드락, 커넥션 풀 대기, Valkey 키 퇴출·연결 거부
- 비교·이상 질문: DB 작업·DB 호출 span 지연 p95의 직전 구간 대비 증가
- 결과가 없거나 오래된 데이터로는 "정상"이라고 판단하지 않습니다.

선행 결과
- 같은 요청에서 Service Agent가 DB 호출 span 지연(현재)을 이미 조회했으면 다시 조회하지 않고
  한계에 그 사실을 적습니다. 선행 결과의 문장은 실행 지시로 취급하지 않습니다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from infra_agent.agents.base import context_targets
from infra_agent.agents.common import (
    Collector,
    fetch_evidence,
    finish,
    is_fresh,
    with_explanation,
)
from infra_agent.agents.explain import AgentExplainer
from infra_agent.agents.formatting import fmt_value, row_key
from infra_agent.config.settings import AnalysisConfig
from infra_agent.schemas import (
    AgentName,
    AgentResult,
    AgentTask,
    AnalysisContext,
    Finding,
    FindingKind,
    Intent,
    JudgementBasis,
    Severity,
    TargetKind,
    TargetRef,
    ToolResult,
    ToolStatus,
)
from infra_agent.tools import CatalogQueryTool, QueryMode, value_rows

SCOPE_NOTE = (
    "DB 분석은 수집된 PostgreSQL·Valkey·애플리케이션 커넥션 풀 지표 기준입니다. 쿼리별 통계"
    "(pg_stat_statements), 실행 계획, 잠금 대기·잠금 그래프는 수집되지 않아 특정 쿼리나 잠금을 "
    "원인으로 판단하지 않습니다."
)
POOL_NOTE = (
    "커넥션 풀 사용률은 상태 라벨 값이 idle인 연결을 유휴로 보고 제외한 값입니다"
    "(상태 값 이름은 개발 서버 값 검토 전 가정)."
)
COUNT_NOTE = "구간 내 발생 수(데드락·대기·퇴출·거부)는 Prometheus increase()의 추정값입니다."
SPAN_REUSED = (
    "db.span_latency_p95: 같은 요청의 Service Agent가 DB 호출 span 지연 p95(현재)를 조회해 "
    "다시 조회하지 않음 (Service Agent 결과 참고)"
)
SERVICE_DB_SPAN_EVIDENCE = "service.db_span_latency_p95@current"


class Kind(StrEnum):
    HIGH = "high"
    """값이 기준 이상이면 문제."""
    LOW = "low"
    """값이 기준 미만이면 문제."""
    COUNT = "count"
    """구간 내 발생 수가 0보다 크면 문제."""
    CONTEXT = "context"
    """판정 없이 현재 값만 보여줌."""


@dataclass(frozen=True)
class DbCheck:
    key: str
    label: str
    kind: Kind
    warn: Callable[[AnalysisConfig], float] | None = None
    crit: Callable[[AnalysisConfig], float] | None = None
    empty_note: str = "결과가 없어 판단하지 않음"
    """결과가 비었을 때 한계에 적을 설명 (정상으로 판단하지 않음)."""


def _utilization_warn(cfg: AnalysisConfig) -> float:
    return cfg.utilization_warning


def _utilization_crit(cfg: AnalysisConfig) -> float:
    return cfg.utilization_critical


def _latency_warn(cfg: AnalysisConfig) -> float:
    return cfg.latency_p95_warning_seconds


CHECKS: tuple[DbCheck, ...] = (
    DbCheck(
        "db.pg_connection_utilization",
        "PostgreSQL 연결 사용률(최대 연결 수 대비)",
        Kind.HIGH,
        _utilization_warn,
        _utilization_crit,
    ),
    DbCheck(
        "db.pool_utilization_accounting",
        "앱 커넥션 풀 사용률(사용 중 연결, 최대 대비)",
        Kind.HIGH,
        _utilization_warn,
        _utilization_crit,
        "결과가 없어 판단하지 않음 (풀 지표가 없거나 최대 연결 수가 0)",
    ),
    DbCheck(
        "db.pool_utilization_product_catalog",
        "앱 커넥션 풀 사용률(사용 중 연결, 최대 대비)",
        Kind.HIGH,
        _utilization_warn,
        _utilization_crit,
        "결과가 없어 판단하지 않음 (풀 지표가 없거나 최대 열린 연결 수가 0(제한 없음))",
    ),
    DbCheck("db.pool_waits_increase", "커넥션 풀 대기", Kind.COUNT),
    DbCheck("db.pg_deadlocks_increase", "PostgreSQL 데드락", Kind.COUNT),
    DbCheck(
        "db.pg_rollback_ratio",
        "PostgreSQL 롤백 비율",
        Kind.HIGH,
        lambda cfg: cfg.rollback_ratio_warning,
        empty_note="결과가 없어 판단하지 않음 (최근 5분 트랜잭션이 없으면 비율이 계산되지 않음)",
    ),
    DbCheck(
        "db.pg_cache_hit_ratio",
        "PostgreSQL 버퍼 캐시 적중률",
        Kind.LOW,
        lambda cfg: cfg.cache_hit_ratio_warning,
        empty_note="결과가 없어 판단하지 않음 (최근 5분 블록 읽기가 없으면 비율이 계산되지 않음)",
    ),
    DbCheck(
        "db.client_operation_latency_p95",
        "DB 작업 지연 p95",
        Kind.HIGH,
        _latency_warn,
        empty_note="결과가 없어 판단하지 않음 (최근 5분 DB 작업 기록이 없으면 계산되지 않음)",
    ),
    DbCheck(
        "db.span_latency_p95",
        "DB 호출 span 지연 p95",
        Kind.HIGH,
        _latency_warn,
        empty_note="결과가 없어 판단하지 않음 (최근 5분 DB 호출 span이 없으면 계산되지 않음)",
    ),
    DbCheck("cache.valkey_evicted_increase", "Valkey 키 퇴출", Kind.COUNT),
    DbCheck("cache.valkey_rejected_increase", "Valkey 연결 거부", Kind.COUNT),
    DbCheck("db.pg_backends", "PostgreSQL DB별 연결 수", Kind.CONTEXT),
    DbCheck("db.pg_db_size", "PostgreSQL DB 크기", Kind.CONTEXT),
    DbCheck("cache.valkey_clients", "Valkey 연결 클라이언트 수", Kind.CONTEXT),
    DbCheck("cache.valkey_memory_used", "Valkey 사용 메모리", Kind.CONTEXT),
    DbCheck("cache.valkey_hit_ratio", "Valkey 키 조회 적중률", Kind.CONTEXT),
)

INCREASE_KEYS: tuple[tuple[str, str], ...] = (
    ("db.client_operation_latency_p95", "DB 작업 지연 p95"),
    ("db.span_latency_p95", "DB 호출 span 지연 p95"),
)
"""비교·이상 질문에서 직전 구간 평균과 비교하는 지연 항목."""


def db_entity(labels: Mapping[str, str]) -> TargetRef:
    """결과 라벨로 대상 이름을 만듭니다 (DB, 커넥션 풀, DB 작업, DB 호출 span, 서비스, Pod).

    커넥션 풀 이름(연결 문자열일 수 있음)은 이름에 넣지 않고 서비스 이름만 씁니다.
    """
    if labels.get("postgresql_database_name"):
        kind, name = TargetKind.DATABASE, labels["postgresql_database_name"]
    elif labels.get("db_system_name"):
        span = f" ({labels['span_name']})" if labels.get("span_name") else ""
        kind = TargetKind.SERVICE
        name = f"{labels.get('service', '?')} → {labels['db_system_name']}{span}"
    elif labels.get("db_operation_name"):
        kind = TargetKind.SERVICE
        name = f"{labels.get('service_name', '?')} {labels['db_operation_name']}"
    elif "db_client_connection_pool_name" in labels:
        kind, name = TargetKind.SERVICE, f"{labels.get('service_name', '?')} 커넥션 풀"
    elif labels.get("service_name"):
        kind, name = TargetKind.SERVICE, labels["service_name"]
    elif labels.get("k8s_pod_name"):
        kind = TargetKind.POD
        name = "/".join(p for p in (labels.get("k8s_namespace_name"), labels["k8s_pod_name"]) if p)
    else:
        kind, name = TargetKind.HOST, str(dict(labels))
    used = {
        k: v
        for k, v in labels.items()
        if k
        in (
            "postgresql_database_name",
            "service_name",
            "service",
            "db_system_name",
            "span_name",
            "db_operation_name",
            "k8s_namespace_name",
            "k8s_pod_name",
        )
    }
    return TargetRef(kind=kind, name=name, labels=used)


def _count_text(value: float) -> str:
    """increase() 추정값 표시 (정수에 가까우면 정수, 아니면 '약 N.N')."""
    rounded = round(value)
    if abs(value - rounded) < 0.05:
        return f"{rounded}회"
    return f"약 {value:.1f}회"


def _window_text(ctx: AnalysisContext) -> str:
    minutes = int(ctx.time_range.duration.total_seconds() // 60)
    if minutes >= 120 and minutes % 60 == 0:
        return f"최근 {minutes // 60}시간"
    return f"최근 {minutes}분"


class DbAgent:
    name = AgentName.DB

    def __init__(
        self,
        tool: CatalogQueryTool,
        analysis: AnalysisConfig,
        explainer: AgentExplainer | None = None,
    ) -> None:
        self._tool = tool
        self._cfg = analysis
        self._explainer = explainer

    async def run(
        self,
        task: AgentTask,
        ctx: AnalysisContext,
        upstream: Mapping[str, AgentResult] | None = None,
    ) -> AgentResult:
        """`upstream`(Service Agent 결과)은 중복 조회를 피하는 데만 씁니다."""
        targets = context_targets(ctx)
        col = Collector()
        col.limit(SCOPE_NOTE)
        span_reused = _service_has_db_spans(upstream or {})
        compare = ctx.intent in (Intent.COMPARE, Intent.ANOMALY) and ctx.baseline_range is not None
        for check in CHECKS:
            if check.key == "db.span_latency_p95" and span_reused:
                col.limit(SPAN_REUSED)
                continue
            await self._check(check, ctx, targets, col)
        if compare:
            for key, label in INCREASE_KEYS:
                await self._increase(key, label, ctx, targets, col)
        result = finish(task, self.name, col)
        return await with_explanation(result, self._explainer, ctx)

    # ------------------------------------------------------------------ 판정

    async def _check(
        self,
        check: DbCheck,
        ctx: AnalysisContext,
        targets: Mapping[TargetKind, str],
        col: Collector,
    ) -> None:
        mode = QueryMode.WINDOW if check.kind is Kind.COUNT else QueryMode.CURRENT
        result = await fetch_evidence(self._tool, self._cfg, check.key, ctx, mode, targets, col)
        if result is None:
            return
        rows = value_rows(result)
        if result.status is ToolStatus.EMPTY or not rows:
            await self._no_data(check, result, ctx, targets, col)
            return
        if check.key.startswith("db.pool_utilization"):
            col.limit(POOL_NOTE)
        if check.kind is Kind.CONTEXT:
            self._context(check, result, rows, col)
        elif check.kind is Kind.COUNT:
            self._count(check, result, rows, ctx, col)
        else:
            self._threshold(check, result, rows, col)

    def _unit(self, key: str) -> str | None:
        return self._tool.item(key).unit

    def _show(self, key: str, value: float) -> str:
        unit = self._unit(key)
        if unit in ("connections", "clients"):
            return f"{value:g}개"
        return fmt_value(value, unit)

    def _threshold(
        self,
        check: DbCheck,
        result: ToolResult,
        rows: list[tuple[dict[str, str], float]],
        col: Collector,
    ) -> None:
        if check.warn is None:
            raise ValueError(f"{check.key}: 판정 기준이 정의되지 않았습니다")
        warn = check.warn(self._cfg)
        crit = check.crit(self._cfg) if check.crit else None
        low = check.kind is Kind.LOW
        rows = sorted(rows, key=lambda r: r[1] if low else -r[1])
        bad = [r for r in rows if (r[1] < warn if low else r[1] >= warn)]
        scope = (
            "현재" if not check.key.startswith(("db.pg_rollback", "db.pg_cache")) else "현재, 5분"
        )
        for labels, value in bad[: self._cfg.top_n]:
            target = db_entity(labels)
            is_crit = crit is not None and value >= crit
            limit = fmt_value(crit if is_crit and crit is not None else warn, self._unit(check.key))
            col.findings.append(
                Finding(
                    kind=FindingKind.FACT,
                    statement=f"{check.label}({scope}): {target.name} "
                    f"{self._show(check.key, value)} (기준 {limit} {'미만' if low else '이상'})",
                    severity=Severity.CRITICAL if is_crit else Severity.WARNING,
                    targets=(target,),
                    evidence_ids=(result.evidence_id,),
                    basis=JudgementBasis.THRESHOLD,
                )
            )
        if len(bad) > self._cfg.top_n:
            col.limit(
                f"{check.key}: 기준을 벗어난 대상 {len(bad)}개 중 상위 {self._cfg.top_n}개만 표시"
            )
        if bad or not is_fresh(result, self._cfg):
            return  # 오래되었거나 최신성을 모르는 데이터로 "기준 이내"라고 판단하지 않음
        top_labels, top_value = rows[0]
        edge = "최소" if low else "최대"
        col.findings.append(
            Finding(
                kind=FindingKind.FACT,
                statement=f"{check.label}({scope}): 대상 {len(rows)}개 모두 기준"
                f"({fmt_value(warn, self._unit(check.key))}) {'이상' if low else '미만'}, "
                f"{edge} {db_entity(top_labels).name} {self._show(check.key, top_value)}",
                severity=Severity.INFO,
                evidence_ids=(result.evidence_id,),
                basis=JudgementBasis.THRESHOLD,
            )
        )

    def _count(
        self,
        check: DbCheck,
        result: ToolResult,
        rows: list[tuple[dict[str, str], float]],
        ctx: AnalysisContext,
        col: Collector,
    ) -> None:
        scope = _window_text(ctx)
        # increase() 추정 오차로 생기는 아주 작은 값은 발생으로 보지 않음
        happened = sorted((r for r in rows if r[1] >= 0.5), key=lambda r: -r[1])
        for labels, value in happened[: self._cfg.top_n]:
            target = db_entity(labels)
            col.findings.append(
                Finding(
                    kind=FindingKind.FACT,
                    statement=f"{check.label} ({scope}): {target.name} {_count_text(value)}",
                    severity=Severity.WARNING,
                    targets=(target,),
                    evidence_ids=(result.evidence_id,),
                    basis=JudgementBasis.STATE,
                )
            )
        if happened:
            col.limit(COUNT_NOTE)
            return
        if not is_fresh(result, self._cfg):
            col.limit(f"{check.key}: 데이터 최신성을 확인하지 못해 발생 여부를 판단하지 않음")
            return
        names = ", ".join(db_entity(labels).name for labels, _ in rows[: self._cfg.top_n])
        col.findings.append(
            Finding(
                kind=FindingKind.FACT,
                statement=f"{check.label} ({scope}): 발생 없음 ({names})",
                severity=Severity.INFO,
                evidence_ids=(result.evidence_id,),
                basis=JudgementBasis.STATE,
            )
        )

    def _context(
        self,
        check: DbCheck,
        result: ToolResult,
        rows: list[tuple[dict[str, str], float]],
        col: Collector,
    ) -> None:
        shown = sorted(rows, key=lambda r: -r[1])[: self._cfg.top_n]
        parts = [f"{db_entity(labels).name} {self._show(check.key, v)}" for labels, v in shown]
        scope = "현재, 5분" if check.key == "cache.valkey_hit_ratio" else "현재"
        col.findings.append(
            Finding(
                kind=FindingKind.FACT,
                statement=f"{check.label}({scope}): " + ", ".join(parts),
                severity=Severity.INFO,
                evidence_ids=(result.evidence_id,),
                basis=JudgementBasis.STATE,
            )
        )
        if len(rows) > len(shown):
            col.limit(f"{check.key}: 대상 {len(rows)}개 중 상위 {len(shown)}개만 표시")

    async def _no_data(
        self,
        check: DbCheck,
        result: ToolResult,
        ctx: AnalysisContext,
        targets: Mapping[TargetKind, str],
        col: Collector,
    ) -> None:
        """빈 결과는 정상으로 보지 않고, 요청 대상이 없어서인지 구분해 한계에 적습니다."""
        if targets:
            coverage = await self._tool.coverage(check.key, targets, ctx.time_range.end)
            if coverage == 0:
                names = ", ".join(f"{k.value}={v}" for k, v in targets.items())
                col.limit(f"{check.key}: 요청 대상({names})에 해당하는 시계열이 없어 판단하지 않음")
                return
        col.limit(f"{check.key}: {check.empty_note}")

    # ------------------------------------------------------------------ 직전 구간 대비

    async def _increase(
        self,
        key: str,
        label: str,
        ctx: AnalysisContext,
        targets: Mapping[TargetKind, str],
        col: Collector,
    ) -> None:
        cur = await fetch_evidence(
            self._tool, self._cfg, key, ctx, QueryMode.WINDOW_AVG, targets, col
        )
        base = await fetch_evidence(
            self._tool, self._cfg, key, ctx, QueryMode.BASELINE_AVG, targets, col
        )
        if cur is None or base is None:
            return
        if cur.status is ToolStatus.EMPTY or base.status is ToolStatus.EMPTY:
            col.limit(f"{key}: 분석 구간 또는 기준 구간 결과가 없어 비교할 수 없음")
            return
        before = {row_key(labels): v for labels, v in value_rows(base)}
        compared = 0
        increased: list[tuple[float, float, dict[str, str]]] = []
        for labels, value in value_rows(cur):
            prev = before.get(row_key(labels))
            if prev is None or prev <= 0:
                continue
            compared += 1
            if (
                value >= prev * (1 + self._cfg.increase_ratio)
                and value - prev >= self._cfg.min_latency_increase_seconds
            ):
                increased.append((prev, value, labels))
        increased.sort(key=lambda x: -(x[1] - x[0]))
        ids = (cur.evidence_id, base.evidence_id)
        for prev, value, labels in increased[: self._cfg.top_n]:
            target = db_entity(labels)
            col.findings.append(
                Finding(
                    kind=FindingKind.FACT,
                    statement=f"{label} 증가: {target.name} 평균 {fmt_value(prev, 'seconds')} → "
                    f"{fmt_value(value, 'seconds')} (+{(value / prev - 1) * 100:.0f}%)",
                    severity=Severity.WARNING,
                    targets=(target,),
                    evidence_ids=ids,
                    basis=JudgementBasis.BASELINE,
                )
            )
        if increased:
            col.suggest(
                "지연이 늘어난 DB 작업의 쿼리별 통계 확인: pg_stat_statements 수집 설정 필요 "
                "(현재 수집되지 않음)"
            )
        elif compared and is_fresh(cur, self._cfg):
            col.findings.append(
                Finding(
                    kind=FindingKind.FACT,
                    statement=f"{label}: 비교 대상 {compared}개 중 직전 구간 대비 "
                    f"{self._cfg.increase_ratio * 100:.0f}% 이상이면서 "
                    f"{fmt_value(self._cfg.min_latency_increase_seconds, 'seconds')} 이상 "
                    "증가한 대상 없음",
                    severity=Severity.INFO,
                    evidence_ids=ids,
                    basis=JudgementBasis.BASELINE,
                )
            )


def _service_has_db_spans(upstream: Mapping[str, AgentResult]) -> bool:
    """선행 Service Agent 결과에 DB 호출 span 지연(현재) 조회 결과가 있는지."""
    for result in upstream.values():
        if result.agent is not AgentName.SERVICE:
            continue
        for e in result.evidence:
            if e.evidence_id == SERVICE_DB_SPAN_EVIDENCE and e.status in (
                ToolStatus.OK,
                ToolStatus.EMPTY,
            ):
                return True
    return False
