"""Service Agent — 서비스 요청량·오류율·지연, 서비스 간 호출, 로그·트레이스 연결 (결정적 판정).

데이터
- Prometheus: Tempo metrics-generator의 spanmetrics(`service`, `span_kind`, `status_code`)와
  service graph(`client`, `server`). 카탈로그에서 agent=service로 지정된 항목만 실행합니다.
- Loki: 카탈로그의 Loki 항목(전체 로그 수, 오류 키워드 로그 수, 오류 로그 샘플)
- Tempo: TraceQL 검색(오류 트레이스)

판정
- 서비스 단위 지표는 SERVER span을 우선 사용합니다(없으면 모든 span 종류로 대체하고 한계에 표시).
- 요청이 너무 적은 서비스(`analysis.min_request_rate` 미만)는 오류율을 판정하지 않습니다.
- 오류율 결과에 없는 서비스는, 요청 결과가 있고 오류율 조회가 성공했을 때만
  "오류 span 없음"으로 봅니다.
- 요청 데이터가 없거나 오래되었으면 오류율·지연을 "정상"으로 판단하지 않습니다.

오류 서비스 상세 (오류율 기준 초과 또는 직전 구간 대비 증가)
1. 오류율 시계열에서 최고 시점을 찾고, 그 전후 `analysis.peak_window_seconds` 구간을 봅니다.
2. 그 구간의 오류 키워드 로그 수(전체 로그 수로 존재 확인)와 샘플, 오류 트레이스를 조회합니다.
3. 오류 로그 샘플과 오류 트레이스에 같은 trace_id가 있으면 "연결됨"으로 기록합니다.
   같은 시간대에 함께 나타났다는 사실만 기록하고 인과를 단정하지 않습니다.

지연 서비스 상세 (응답 지연 p95 기준 초과 또는 직전 구간 대비 증가)
1. 지연 p95 시계열에서 최고 시점을 찾고, 그 전후 `analysis.peak_window_seconds` 구간을 봅니다.
2. 그 구간에서 최고 시점 p95 이상 걸린 SERVER span의 트레이스를 검색합니다(느린 트레이스).
로그 본문은 외부 데이터이며 도구 계층에서 마스킹·길이 제한을 거친 뒤 근거로만 다룹니다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from infra_agent.agents.base import context_targets, remaining_seconds
from infra_agent.agents.common import (
    Collector,
    fetch_evidence,
    finish,
    is_fresh,
    with_explanation,
)
from infra_agent.agents.explain import AgentExplainer
from infra_agent.config.settings import AnalysisConfig
from infra_agent.schemas import (
    AgentName,
    AgentResult,
    AgentTask,
    AnalysisContext,
    ErrorInfo,
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
from infra_agent.tools import (
    CatalogQueryTool,
    LogQueryTool,
    QueryMode,
    QueryOutcome,
    TraceSearchTool,
    is_service_name,
    rows_of,
    value_rows,
)
from infra_agent.units import fmt_points, fmt_time, fmt_value

SCOPE_NOTE = (
    "Service 분석은 트레이스에서 파생된 spanmetrics·service graph 지표 기준이며, "
    "트레이스 샘플링에 따라 실제 요청 수와 다를 수 있습니다. "
    "오류 로그는 본문 키워드(단어 단위) 기준이며, 레벨이 INFO 이하로 표시된 줄은 제외합니다."
)
UNKNOWN_ROOT = "(루트 span 미수신)"
"""오류 트레이스의 루트 서비스를 알 수 없을 때 개요에 쓰는 이름 (상세 대상에서 제외)."""
LOG_WORDS = ("로그", "log", "트레이스", "trace", "추적", "원인")
"""질문에 이 단어가 있으면 오류 서비스가 없어도 로그·트레이스 개요를 조회합니다."""
TRACE_LINK_SEARCH_LIMIT = 20
"""trace_id 연결 확인용 오류 트레이스 검색 건수 (표시는 `trace_sample_limit`개)."""


def _span_kind_rows(
    rows: list[tuple[dict[str, str], float]],
) -> tuple[list[tuple[dict[str, str], float]], bool]:
    """SERVER span 행만 고릅니다. 없으면 전체 행과 False(대체 사용)를 돌려줍니다."""
    has_kind = any("span_kind" in labels for labels, _ in rows)
    if not has_kind:
        return rows, True
    server = [(labels, v) for labels, v in rows if "SERVER" in labels.get("span_kind", "").upper()]
    return (server, True) if server else (rows, False)


def _by_service(rows: Iterable[tuple[dict[str, str], float]], combine: str) -> dict[str, float]:
    """서비스별로 합치거나(sum) 최댓값(max)을 씁니다."""
    out: dict[str, float] = {}
    for labels, value in rows:
        name = labels.get("service")
        if not name:
            continue
        if name in out:
            out[name] = out[name] + value if combine == "sum" else max(out[name], value)
        else:
            out[name] = value
    return out


def service_entity(labels: Mapping[str, str]) -> TargetRef:
    """결과 라벨로 대상 이름을 만듭니다: 호출 경로(client → server), DB 호출, 서비스."""
    if "client" in labels and "server" in labels:
        name = f"{labels['client']} → {labels['server']}"
    elif labels.get("db_system_name"):
        span = f" ({labels['span_name']})" if labels.get("span_name") else ""
        name = f"{labels.get('service', '?')} → {labels['db_system_name']}{span}"
    else:
        name = labels.get("service") or labels.get("service_name") or "?"
    used = {
        k: v
        for k, v in labels.items()
        if k in ("service", "client", "server", "db_system_name", "span_name")
    }
    return TargetRef(kind=TargetKind.SERVICE, name=name, labels=used)


def _hm(value: datetime) -> str:
    return fmt_time(value, seconds=False)


@dataclass
class _Focus:
    service: str
    ratio: float
    reason: str
    client_side: bool = False
    """호출한 쪽(client)으로 상세 확인하는지. 이때는 오류율 최고 시점을 CLIENT span에서 찾습니다."""


@dataclass
class _State:
    """한 번의 실행에서 판정 사이에 공유하는 값."""

    rates: dict[str, float] = field(default_factory=dict)
    rates_ok: bool = False
    focus: dict[str, _Focus] = field(default_factory=dict)
    other_errors: dict[str, float] = field(default_factory=dict)
    """SERVER 외 span 종류(CLIENT·INTERNAL 등)에서 오류가 있는 서비스와 최대 오류율."""
    slow: dict[str, _Focus] = field(default_factory=dict)
    """응답 지연이 기준을 넘거나 직전 구간보다 늘어난 서비스 (느린 트레이스 상세 대상)."""
    traced: set[str] = field(default_factory=set)
    """span 종류와 관계없이 spanmetrics에 나타난 서비스 (트레이스 검색 대상).
    요청을 받지 않고 호출·소비만 하는 서비스(CLIENT·CONSUMER span만 있음)도 포함합니다."""


@dataclass(frozen=True)
class LatencyCheck:
    key: str
    label: str
    service_level: bool
    """True면 SERVER span 선택·서비스별 합치기를 적용합니다."""


LATENCY_CHECKS = (
    LatencyCheck("service.latency_p95", "서비스 응답 지연 p95", True),
    LatencyCheck("service.db_span_latency_p95", "DB 호출 span 지연 p95", False),
    LatencyCheck("service.dependency_latency_p95", "서비스 간 호출 지연 p95(서버 측)", False),
)


class ServiceAgent:
    name = AgentName.SERVICE

    def __init__(
        self,
        tool: CatalogQueryTool,
        analysis: AnalysisConfig,
        *,
        logs: LogQueryTool | None = None,
        traces: TraceSearchTool | None = None,
        explainer: AgentExplainer | None = None,
        detail_min_seconds: float = 30.0,
    ) -> None:
        self._tool = tool
        self._cfg = analysis
        self._logs = logs
        self._traces = traces
        self._explainer = explainer
        self._detail_min = detail_min_seconds
        """로그·트레이스 상세 한 단계를 시작하는 데 필요한 에이전트 남은 시간(초)."""

    def _time_for_detail(self, col: Collector, what: str) -> bool:
        """남은 시간이 부족하면 상세 조회를 생략합니다.

        이미 판정한 결과가 에이전트 제한 시간 초과로 통째로 버려지지 않게 하기 위함입니다.
        """
        left = remaining_seconds()
        if left is None or left >= self._detail_min:
            return True
        col.limit(f"에이전트 제한 시간 안에 남은 시간이 부족해 {what}을(를) 생략함")
        return False

    async def run(
        self,
        task: AgentTask,
        ctx: AnalysisContext,
        upstream: Mapping[str, AgentResult] | None = None,
    ) -> AgentResult:
        """선행 작업이 없는 첫 단계 분석이므로 `upstream`은 사용하지 않습니다."""
        targets = context_targets(ctx)
        col = Collector()
        col.limit(SCOPE_NOTE)
        state = _State()
        await self._request_rates(ctx, targets, col, state)
        if state.rates_ok:
            await self._error_ratios(ctx, targets, col, state)
            for check in LATENCY_CHECKS:
                await self._latency(check, ctx, targets, col, state)
            if ctx.intent in (Intent.COMPARE, Intent.ANOMALY) and ctx.baseline_range is not None:
                await self._error_increase(ctx, targets, col, state)
                await self._latency_increase(ctx, targets, col, state)
        await self._dependencies(ctx, targets, col, state)
        focus = sorted(state.focus.values(), key=lambda f: -f.ratio)[: self._cfg.detail_services]
        detailed: list[str] = []
        for item in focus:
            if not self._time_for_detail(col, f"{item.service} 로그·트레이스 상세"):
                break
            await self._detail(item, ctx, targets, col)
            detailed.append(item.service)
        slow = sorted(state.slow.values(), key=lambda f: -f.ratio)[: self._cfg.detail_services]
        for item in slow:
            if not self._time_for_detail(col, f"{item.service} 느린 트레이스 확인"):
                break
            await self._slow_detail(item, ctx, targets, col)
        asks_logs = any(w in ctx.question.lower() for w in LOG_WORDS)
        if asks_logs and self._time_for_detail(col, "로그·트레이스 개요"):
            await self._overview(ctx, targets, col, state, skip=detailed)
        result = finish(task, self.name, col)
        return await with_explanation(result, self._explainer, ctx)

    # ------------------------------------------------------------------ 요청량

    async def _request_rates(
        self,
        ctx: AnalysisContext,
        targets: Mapping[TargetKind, str],
        col: Collector,
        state: _State,
    ) -> None:
        key = "service.request_rate"
        result = await fetch_evidence(
            self._tool, self._cfg, key, ctx, QueryMode.CURRENT, targets, col
        )
        if result is None:
            return
        all_rows = value_rows(result)
        state.traced = {labels["service"] for labels, _ in all_rows if labels.get("service")}
        rows, server_only = _span_kind_rows(all_rows)
        if not server_only:
            col.limit(f"{key}: SERVER span이 없어 모든 span 종류의 호출로 판단함")
        rates = _by_service(rows, "sum")
        if not rates:
            await self._no_requests(key, result, ctx, targets, col)
            return
        if not is_fresh(result, self._cfg):
            col.limit(
                f"{key}: 데이터가 오래되었거나 최신성을 확인하지 못해 오류율·지연을 판정하지 않음"
            )
            return
        state.rates, state.rates_ok = rates, True
        top = sorted(rates.items(), key=lambda kv: -kv[1])[: self._cfg.top_n]
        unit = self._tool.item(key).unit
        col.findings.append(
            Finding(
                kind=FindingKind.FACT,
                statement=f"서비스 요청량(현재, 5분 rate): 서비스 {len(rates)}개, 상위 "
                + ", ".join(f"{name} {fmt_value(v, unit)}" for name, v in top),
                severity=Severity.INFO,
                evidence_ids=(result.evidence_id,),
                basis=JudgementBasis.STATE,
            )
        )

    async def _no_requests(
        self,
        key: str,
        result: ToolResult,
        ctx: AnalysisContext,
        targets: Mapping[TargetKind, str],
        col: Collector,
    ) -> None:
        if targets:
            coverage = await self._tool.coverage(key, targets, ctx.time_range.end)
            if coverage == 0:
                names = ", ".join(f"{k.value}={v}" for k, v in targets.items())
                col.limit(
                    f"{key}: 요청 대상({names})에 해당하는 서비스 지표가 없어 판단하지 않음 "
                    "(spanmetrics에 해당 라벨이 없을 수 있음)"
                )
                return
        col.limit(f"{key}: 서비스 요청 데이터가 없어 오류율·지연을 판단하지 않음")

    # ------------------------------------------------------------------ 오류율

    async def _error_ratios(
        self,
        ctx: AnalysisContext,
        targets: Mapping[TargetKind, str],
        col: Collector,
        state: _State,
    ) -> None:
        key = "service.error_ratio"
        result = await fetch_evidence(
            self._tool, self._cfg, key, ctx, QueryMode.CURRENT, targets, col
        )
        if result is None:
            return
        all_rows = value_rows(result)
        rows, server_only = _span_kind_rows(all_rows)
        ratios = _by_service(rows, "max")
        if server_only:
            self._other_span_errors(all_rows, result, col, state)
        judged = {s: r for s, r in state.rates.items() if r >= self._cfg.min_request_rate}
        skipped = len(state.rates) - len(judged)
        if skipped:
            col.limit(
                f"{key}: 요청이 적어({fmt_value(self._cfg.min_request_rate, 'calls/s')} 미만) "
                f"오류율을 판단하지 않은 서비스 {skipped}개"
            )
        values = {s: ratios.get(s, 0.0) for s in judged}
        warn, crit = self._cfg.error_ratio_warning, self._cfg.error_ratio_critical
        exceeded = sorted(((s, v) for s, v in values.items() if v >= warn), key=lambda x: -x[1])
        for name, value in exceeded[: self._cfg.top_n]:
            severity = Severity.CRITICAL if value >= crit else Severity.WARNING
            limit = crit if severity is Severity.CRITICAL else warn
            col.findings.append(
                Finding(
                    kind=FindingKind.FACT,
                    statement="서비스 오류율(현재, SERVER span): "
                    f"{name} {fmt_value(value, 'ratio')} "
                    f"(기준 {fmt_value(limit, 'ratio')} 이상, 요청 "
                    f"{fmt_value(state.rates[name], 'calls/s')})",
                    severity=severity,
                    targets=(TargetRef(kind=TargetKind.SERVICE, name=name),),
                    evidence_ids=(result.evidence_id, f"service.request_rate@{QueryMode.CURRENT}"),
                    basis=JudgementBasis.THRESHOLD,
                )
            )
            state.focus[name] = _Focus(name, value, "오류율 기준 초과")
        if len(exceeded) > self._cfg.top_n:
            col.limit(
                f"{key}: 기준 초과 서비스 {len(exceeded)}개 중 상위 {self._cfg.top_n}개만 표시"
            )
        if exceeded or not values:
            return
        if not is_fresh(result, self._cfg):
            return
        top_name, top_value = max(values.items(), key=lambda kv: kv[1])
        text = (
            "SERVER span 오류 없음"
            if top_value == 0
            else f"최대 {top_name} {fmt_value(top_value, 'ratio')}"
        )
        col.findings.append(
            Finding(
                kind=FindingKind.FACT,
                statement=f"서비스 오류율(현재, SERVER span): 서비스 {len(values)}개 모두 기준"
                f"({fmt_value(warn, 'ratio')}) 미만, {text}",
                severity=Severity.INFO,
                evidence_ids=(result.evidence_id, f"service.request_rate@{QueryMode.CURRENT}"),
                basis=JudgementBasis.THRESHOLD,
            )
        )

    def _other_span_errors(
        self,
        rows: list[tuple[dict[str, str], float]],
        result: ToolResult,
        col: Collector,
        state: _State,
    ) -> None:
        """SERVER 외 span 종류의 오류를 따로 보여 줍니다.

        서비스 단위 판정은 SERVER span 기준이지만, 호출(CLIENT)·내부(INTERNAL) span의 오류가
        있으면 "오류 없음"으로 읽히지 않도록 사실로 남깁니다(기준 판정은 하지 않음).
        """
        errors = sorted(
            (
                (labels.get("service", "?"), labels.get("span_kind", "?"), v)
                for labels, v in rows
                if v > 0 and "SERVER" not in labels.get("span_kind", "").upper()
            ),
            key=lambda x: -x[2],
        )
        for service, _, value in errors:
            state.other_errors[service] = max(state.other_errors.get(service, 0.0), value)
        if not errors:
            return
        shown = ", ".join(
            f"{svc}({kind.removeprefix('SPAN_KIND_')}) {fmt_value(v, 'ratio')}"
            for svc, kind, v in errors[: self._cfg.top_n]
        )
        more = f" 외 {len(errors) - self._cfg.top_n}개" if len(errors) > self._cfg.top_n else ""
        col.findings.append(
            Finding(
                kind=FindingKind.FACT,
                statement=f"SERVER 외 span의 오류율(현재, 판정 기준 미적용): {shown}{more}",
                severity=Severity.INFO,
                evidence_ids=(result.evidence_id,),
                basis=JudgementBasis.STATE,
            )
        )

    # ------------------------------------------------------------------ 지연

    async def _latency(
        self,
        check: LatencyCheck,
        ctx: AnalysisContext,
        targets: Mapping[TargetKind, str],
        col: Collector,
        state: _State,
    ) -> None:
        result = await fetch_evidence(
            self._tool, self._cfg, check.key, ctx, QueryMode.CURRENT, targets, col
        )
        if result is None:
            return
        rows = value_rows(result)
        if check.service_level:
            rows, _ = _span_kind_rows(rows)
            merged = _by_service(rows, "max")
            items = [(TargetRef(kind=TargetKind.SERVICE, name=n), v) for n, v in merged.items()]
        else:
            items = [(service_entity(labels), v) for labels, v in rows]
        if not items:
            col.limit(f"{check.key}: 결과가 없어 지연을 판단하지 않음")
            return
        warn = self._cfg.latency_p95_warning_seconds
        items.sort(key=lambda x: -x[1])
        exceeded = [x for x in items if x[1] >= warn]
        top_bound = (
            await self._tool.max_bucket_bound(check.key, targets, ctx.time_range.end)
            if exceeded
            else None
        )
        saturated = 0
        for target, value in exceeded[: self._cfg.top_n]:
            if check.service_level:
                state.slow.setdefault(
                    target.name, _Focus(target.name, value, "응답 지연 p95 기준 초과")
                )
            shown = fmt_value(value, "seconds")
            if top_bound is not None and value >= top_bound * (1 - 1e-9):
                # 분위수가 마지막 유한 버킷 경계에 걸림 → 실제 값은 그 이상
                shown = f"{shown} 이상(히스토그램 최대 구간)"
                saturated += 1
            col.findings.append(
                Finding(
                    kind=FindingKind.FACT,
                    statement=f"{check.label}(현재): {target.name} {shown} "
                    f"(기준 {fmt_value(warn, 'seconds')} 이상)",
                    severity=Severity.WARNING,
                    targets=(target,),
                    evidence_ids=(result.evidence_id,),
                    basis=JudgementBasis.THRESHOLD,
                )
            )
        if len(exceeded) > self._cfg.top_n:
            col.limit(
                f"{check.key}: 기준 초과 {len(exceeded)}개 중 상위 {self._cfg.top_n}개만 표시"
            )
        if saturated:
            col.limit(
                f"{check.key}: p95가 히스토그램 최대 유한 구간 경계"
                f"({fmt_value(top_bound or 0.0, 'seconds')})와 같은 대상 {saturated}개는 "
                "실제 값이 그보다 클 수 있음 (스트리밍처럼 오래 열린 호출이면 지연이 아니라 "
                "연결 유지 시간일 수 있음)"
            )
        if exceeded or not is_fresh(result, self._cfg):
            return
        top, value = items[0]
        col.findings.append(
            Finding(
                kind=FindingKind.FACT,
                statement=f"{check.label}(현재): 대상 {len(items)}개 모두 기준"
                f"({fmt_value(warn, 'seconds')}) 미만, "
                f"최대 {top.name} {fmt_value(value, 'seconds')}",
                severity=Severity.INFO,
                evidence_ids=(result.evidence_id,),
                basis=JudgementBasis.THRESHOLD,
            )
        )

    # ------------------------------------------------------------------ 직전 구간 대비

    async def _window_pair(
        self, key: str, ctx: AnalysisContext, targets: Mapping[TargetKind, str], col: Collector
    ) -> tuple[dict[str, float], dict[str, float], tuple[str, str]] | None:
        cur = await fetch_evidence(
            self._tool, self._cfg, key, ctx, QueryMode.WINDOW_AVG, targets, col
        )
        base = await fetch_evidence(
            self._tool, self._cfg, key, ctx, QueryMode.BASELINE_AVG, targets, col
        )
        if cur is None or base is None:
            return None
        cur_rows, _ = _span_kind_rows(value_rows(cur))
        base_rows, _ = _span_kind_rows(value_rows(base))
        return (
            _by_service(cur_rows, "max"),
            _by_service(base_rows, "max"),
            (cur.evidence_id, base.evidence_id),
        )

    async def _error_increase(
        self,
        ctx: AnalysisContext,
        targets: Mapping[TargetKind, str],
        col: Collector,
        state: _State,
    ) -> None:
        key = "service.error_ratio"
        pair = await self._window_pair(key, ctx, targets, col)
        if pair is None:
            return
        cur, base, ids = pair
        compared = {s: v for s, v in cur.items() if s in state.rates}
        increased = []
        for name, value in compared.items():
            before = base.get(name, 0.0)
            if value - before >= self._cfg.min_error_ratio_increase:
                increased.append((name, before, value))
        increased.sort(key=lambda x: -(x[2] - x[1]))
        for name, before, value in increased[: self._cfg.top_n]:
            col.findings.append(
                Finding(
                    kind=FindingKind.FACT,
                    statement=f"서비스 오류율 증가: {name} 평균 {fmt_value(before, 'ratio')} → "
                    f"{fmt_value(value, 'ratio')} ({fmt_points(value - before)})",
                    severity=Severity.WARNING,
                    targets=(TargetRef(kind=TargetKind.SERVICE, name=name),),
                    evidence_ids=ids,
                    basis=JudgementBasis.BASELINE,
                )
            )
            if name not in state.focus:
                state.focus[name] = _Focus(name, value, "직전 구간 대비 오류율 증가")
        if not increased:
            col.findings.append(
                Finding(
                    kind=FindingKind.FACT,
                    statement=f"서비스 오류율(SERVER span): 비교 대상 {len(compared)}개 중 "
                    "직전 구간 대비 "
                    f"{fmt_points(self._cfg.min_error_ratio_increase)} 이상 증가한 대상 없음",
                    severity=Severity.INFO,
                    evidence_ids=ids,
                    basis=JudgementBasis.BASELINE,
                )
            )

    async def _latency_increase(
        self,
        ctx: AnalysisContext,
        targets: Mapping[TargetKind, str],
        col: Collector,
        state: _State,
    ) -> None:
        key = "service.latency_p95"
        pair = await self._window_pair(key, ctx, targets, col)
        if pair is None:
            return
        cur, base, ids = pair
        compared = [(s, base[s], v) for s, v in cur.items() if s in base and base[s] > 0]
        if len(compared) < len(cur):
            col.limit(
                f"{key}: 기준 구간에 없던 서비스 {len(cur) - len(compared)}개는 비교하지 않음"
            )
        increased = [
            (s, b, v)
            for s, b, v in compared
            if v >= b * (1 + self._cfg.increase_ratio)
            and v - b >= self._cfg.min_latency_increase_seconds
        ]
        increased.sort(key=lambda x: -(x[2] - x[1]))
        for name, before, value in increased[: self._cfg.top_n]:
            pct = f"+{(value / before - 1) * 100:.0f}%"
            col.findings.append(
                Finding(
                    kind=FindingKind.FACT,
                    statement=f"서비스 응답 지연 p95 증가: {name} "
                    f"평균 {fmt_value(before, 'seconds')} → "
                    f"{fmt_value(value, 'seconds')} ({pct})",
                    severity=Severity.WARNING,
                    targets=(TargetRef(kind=TargetKind.SERVICE, name=name),),
                    evidence_ids=ids,
                    basis=JudgementBasis.BASELINE,
                )
            )
            state.slow.setdefault(name, _Focus(name, value, "직전 구간 대비 응답 지연 증가"))
        if not increased and compared:
            col.findings.append(
                Finding(
                    kind=FindingKind.FACT,
                    statement=f"서비스 응답 지연 p95: 비교 대상 {len(compared)}개 중 "
                    f"직전 구간 대비 {self._cfg.increase_ratio * 100:.0f}% 이상이면서 "
                    f"{fmt_value(self._cfg.min_latency_increase_seconds, 'seconds')} 이상 "
                    "증가한 대상 없음",
                    severity=Severity.INFO,
                    evidence_ids=ids,
                    basis=JudgementBasis.BASELINE,
                )
            )
        if increased:
            col.suggest(
                "지연이 늘어난 서비스의 DB 호출·서비스 간 호출 지연 비교 "
                "(이번 답변의 DB 호출 span·서비스 간 호출 지연 항목, "
                "DB 내부 지표는 DB Agent, Network Agent는 미구현)"
            )

    # ------------------------------------------------------------------ 서비스 간 호출

    async def _dependencies(
        self,
        ctx: AnalysisContext,
        targets: Mapping[TargetKind, str],
        col: Collector,
        state: _State,
    ) -> None:
        total = await fetch_evidence(
            self._tool,
            self._cfg,
            "service.dependency_request_rate",
            ctx,
            QueryMode.CURRENT,
            targets,
            col,
        )
        failed = await fetch_evidence(
            self._tool,
            self._cfg,
            "service.dependency_failed_rate",
            ctx,
            QueryMode.CURRENT,
            targets,
            col,
        )
        if total is None or failed is None:
            return
        totals: dict[tuple[str, str], float] = {}
        for labels, value in value_rows(total):
            edge = (labels.get("client", "?"), labels.get("server", "?"))
            totals[edge] = totals.get(edge, 0.0) + value
        fails = {
            (labels.get("client", "?"), labels.get("server", "?")): value
            for labels, value in value_rows(failed)
        }
        edges = {e: t for e, t in totals.items() if t >= self._cfg.min_request_rate}
        if not edges:
            if totals or not is_fresh(total, self._cfg):
                return
            col.limit("service.dependency_request_rate: 서비스 간 호출 데이터가 없어 판단하지 않음")
            return
        ratios = {e: fails.get(e, 0.0) / t for e, t in edges.items()}
        warn, crit = self._cfg.error_ratio_warning, self._cfg.error_ratio_critical
        exceeded = sorted(((e, r) for e, r in ratios.items() if r >= warn), key=lambda x: -x[1])
        ids = (total.evidence_id, failed.evidence_id)
        for (client, server), ratio in exceeded[: self._cfg.top_n]:
            severity = Severity.CRITICAL if ratio >= crit else Severity.WARNING
            target = service_entity({"client": client, "server": server})
            col.findings.append(
                Finding(
                    kind=FindingKind.FACT,
                    statement=f"서비스 간 호출 실패율(현재): {target.name} "
                    f"{fmt_value(ratio, 'ratio')} "
                    f"(실패 {fmt_value(fails.get((client, server), 0.0), 'requests/s')}, "
                    f"기준 {fmt_value(warn, 'ratio')} 이상)",
                    severity=severity,
                    targets=(target,),
                    evidence_ids=ids,
                    basis=JudgementBasis.THRESHOLD,
                )
            )
            # 실패를 응답한 쪽(server)이 트레이스 지표가 있는 서비스면 그 서비스를, 아니면
            # (DB·외부 시스템처럼 계측되지 않은 대상) 호출한 쪽(client)을 상세 대상으로
            reason = f"{client} → {server} 호출 실패"
            if server in state.traced:
                if server not in state.focus:
                    state.focus[server] = _Focus(server, ratio, reason)
            elif client in state.traced and client not in state.focus:
                state.focus[client] = _Focus(
                    client, ratio, f"{reason}, 호출한 쪽", client_side=True
                )
        if exceeded or not (is_fresh(total, self._cfg) and is_fresh(failed, self._cfg)):
            return
        worst = max(ratios.items(), key=lambda kv: kv[1])
        worst_text = (
            "실패 호출 없음"
            if worst[1] == 0
            else f"최대 {worst[0][0]} → {worst[0][1]} {fmt_value(worst[1], 'ratio')}"
        )
        col.findings.append(
            Finding(
                kind=FindingKind.FACT,
                statement=f"서비스 간 호출 실패율(현재): 호출 경로 {len(ratios)}개 모두 기준"
                f"({fmt_value(warn, 'ratio')}) 미만, {worst_text}",
                severity=Severity.INFO,
                evidence_ids=ids,
                basis=JudgementBasis.THRESHOLD,
            )
        )

    # ------------------------------------------------------------------ 오류 서비스 상세

    async def _detail(
        self,
        focus: _Focus,
        ctx: AnalysisContext,
        targets: Mapping[TargetKind, str],
        col: Collector,
    ) -> None:
        service_targets = {**targets, TargetKind.SERVICE: focus.service}
        window = await self._peak_window(focus, ctx, service_targets, col)
        log_ids = await self._logs_for(focus.service, window, service_targets, col)
        trace_ids = await self._traces_for(focus.service, window, col, expect_errors=True)
        self._link(focus.service, window, log_ids, trace_ids, col)

    async def _slow_detail(
        self,
        focus: _Focus,
        ctx: AnalysisContext,
        targets: Mapping[TargetKind, str],
        col: Collector,
    ) -> None:
        """응답 지연 최고 시점 전후 구간에서 그 시점 p95 이상 걸린 span의 트레이스를 찾습니다.

        지연 p95는 SERVER span 기준이므로 SERVER span만 검색합니다(없으면 모든 span).
        오래 열린 스트리밍 호출(CLIENT span)이 느린 요청으로 섞이지 않게 하기 위함입니다.
        """
        service = focus.service
        if self._traces is None:
            col.limit(f"{service}: Tempo가 설정되지 않아 느린 트레이스를 확인하지 않음")
            return
        if self._cfg.trace_sample_limit == 0:
            return
        service_targets = {**targets, TargetKind.SERVICE: service}
        key = "service.latency_p95"
        result = self._record(
            await self._tool.series_peaks(key, ctx, service_targets, ctx.time_range, label=service),
            key,
            col,
        )
        if result is None:
            return
        rows = [r for r in rows_of(result) if isinstance(r.get("value"), int | float)]
        server = [r for r in rows if "SERVER" in str(r.get("labels", {})).upper()]
        candidates = server or rows
        peak = max(candidates, key=lambda r: float(r["value"]), default=None)  # type: ignore[arg-type]
        if peak is None or float(peak["value"]) <= 0:  # type: ignore[arg-type]
            col.limit(
                f"{service}: 분석 구간 지연 시계열에서 최고 시점을 찾지 못해 "
                "느린 트레이스를 검색하지 않음"
            )
            return
        value = float(peak["value"])  # type: ignore[arg-type]
        peak_at = datetime.fromisoformat(str(peak["peak_at"]))
        half = timedelta(seconds=self._cfg.peak_window_seconds / 2)
        start = max(ctx.time_range.start, peak_at - half)
        end = min(ctx.time_range.end, peak_at + half)
        window = TimeRange(start=start, end=end) if end > start else ctx.time_range
        span = f"{_hm(window.start)}~{_hm(window.end)}"
        kind = "SERVER span" if server else "span"
        col.findings.append(
            Finding(
                kind=FindingKind.FACT,
                statement=f"응답 지연 최고 시점 ({service}, {focus.reason}): {_hm(peak_at)} "
                f"{fmt_value(value, 'seconds')} (5분 p95 기준). 느린 트레이스는 {span} 구간에서 "
                f"{fmt_value(value, 'seconds')} 이상 걸린 {kind}을 검색",
                severity=Severity.INFO,
                targets=(TargetRef(kind=TargetKind.SERVICE, name=service),),
                evidence_ids=(result.evidence_id,),
                basis=JudgementBasis.STATE,
            )
        )
        traces = self._record(
            await self._traces.search(
                f"trace.slow@{service}",
                window,
                service=service,
                min_duration_ms=max(1, int(value * 1000)),
                server_only=bool(server),
                limit=max(self._cfg.trace_sample_limit, TRACE_LINK_SEARCH_LIMIT),
            ),
            "trace.slow",
            col,
        )
        if traces is None:
            return
        found = rows_of(traces)
        if not found:
            col.limit(
                f"{service}: {span} 구간에서 {fmt_value(value, 'seconds')} 이상 걸린 {kind} "
                "트레이스 검색 결과 없음 (p95는 히스토그램 구간으로 계산한 근사값이라 실제 span "
                "시간과 다를 수 있음)"
            )
            return
        durations = [float(d) for r in found if isinstance(d := r.get("duration_ms"), int | float)]
        longest = (
            f", 트레이스 최장 {fmt_value(max(durations) / 1000, 'seconds')}" if durations else ""
        )
        shown = found[: self._cfg.trace_sample_limit]
        col.findings.append(
            Finding(
                kind=FindingKind.FACT,
                statement=f"느린 트레이스 ({service}, {span}, {fmt_value(value, 'seconds')} 이상): "
                f"검색 {len(found)}건"
                + ("(검색 상한)" if len(found) >= TRACE_LINK_SEARCH_LIMIT else "")
                + f"{longest}, 예: "
                + ", ".join(str(r["trace_id"]) for r in shown),
                severity=Severity.INFO,
                targets=(TargetRef(kind=TargetKind.SERVICE, name=service),),
                evidence_ids=(traces.evidence_id,),
                basis=JudgementBasis.STATE,
            )
        )

    def _link(
        self,
        service: str,
        window: TimeRange,
        log_ids: list[str],
        trace_ids: list[str],
        col: Collector,
    ) -> None:
        """오류 로그 샘플과 오류 트레이스에 같은 trace_id가 있는지 기록합니다 (인과 판단 없음)."""
        linked = sorted(set(log_ids) & set(trace_ids))
        if linked:
            col.findings.append(
                Finding(
                    kind=FindingKind.FACT,
                    statement="오류 로그와 오류 트레이스가 같은 trace_id로 연결됨 "
                    f"({service}, {_hm(window.start)}~{_hm(window.end)}): " + ", ".join(linked[:3]),
                    severity=Severity.INFO,
                    targets=(TargetRef(kind=TargetKind.SERVICE, name=service),),
                    evidence_ids=(f"log.error_samples@{service}", f"trace.errors@{service}"),
                    basis=JudgementBasis.STATE,
                )
            )
        elif log_ids and trace_ids:
            col.limit(
                f"{service}: 오류 로그 샘플의 trace_id가 조회한 오류 트레이스 "
                f"{len(trace_ids)}건과 겹치지 않음 (샘플 범위가 달라 연결하지 못했을 수 있음)"
            )

    async def _peak_window(
        self,
        focus: _Focus,
        ctx: AnalysisContext,
        targets: Mapping[TargetKind, str],
        col: Collector,
    ) -> TimeRange:
        """오류율 최고 시점 전후 구간. 찾지 못하면 분석 구간 전체."""
        outcome = await self._tool.series_peaks(
            "service.error_ratio", ctx, targets, ctx.time_range, label=focus.service
        )
        result = self._record(outcome, "service.error_ratio", col)
        if result is None:
            return ctx.time_range
        rows = [r for r in rows_of(result) if isinstance(r.get("value"), int | float)]
        kind = "CLIENT" if focus.client_side else "SERVER"
        preferred = [r for r in rows if kind in str(r.get("labels", {})).upper()]
        candidates = preferred or rows
        if not candidates:
            col.limit(f"{focus.service}: 오류율 시계열이 없어 분석 구간 전체로 로그·트레이스를 봄")
            return ctx.time_range
        peak = max(candidates, key=lambda r: float(r["value"]))  # type: ignore[arg-type]
        if float(peak["value"]) <= 0:  # type: ignore[arg-type]
            which = f"{kind} span " if preferred else ""
            col.limit(
                f"{focus.service}: 분석 구간에 {which}오류율이 0보다 큰 시점이 없어 "
                "구간 전체로 로그·트레이스를 봄"
            )
            return ctx.time_range
        peak_at = datetime.fromisoformat(str(peak["peak_at"]))
        half = timedelta(seconds=self._cfg.peak_window_seconds / 2)
        start = max(ctx.time_range.start, peak_at - half)
        end = min(ctx.time_range.end, peak_at + half)
        if end <= start:
            return ctx.time_range
        col.findings.append(
            Finding(
                kind=FindingKind.FACT,
                statement=f"오류율 최고 시점 ({focus.service}, {focus.reason}): {_hm(peak_at)} "
                f"{fmt_value(float(peak['value']), 'ratio')} (5분 rate 기준). "  # type: ignore[arg-type]
                f"로그·트레이스는 {_hm(start)}~{_hm(end)} 구간을 확인",
                severity=Severity.INFO,
                targets=(TargetRef(kind=TargetKind.SERVICE, name=focus.service),),
                evidence_ids=(result.evidence_id,),
                basis=JudgementBasis.STATE,
            )
        )
        return TimeRange(start=start, end=end)

    def _record(self, outcome: QueryOutcome, key: str, col: Collector) -> ToolResult | None:
        """Prometheus 외 도구 결과를 근거로 기록합니다. 실패하면 한계를 남기고 None."""
        result = outcome.result
        if outcome.skipped:
            col.limit(f"{key}: {result.error}")
            return None
        col.queries += 1
        if result.evidence_id not in col.evidence:
            col.add_evidence(result)
        if result.status in (ToolStatus.ERROR, ToolStatus.TIMEOUT):
            col.failed_queries += 1
            col.errors.append(ErrorInfo(code=result.status.value, message=f"{key}: {result.error}"))
            col.limit(f"{key}: 조회 실패로 확인하지 못함 ({result.error})")
            return None
        return result

    async def _logs_for(
        self,
        service: str,
        window: TimeRange,
        targets: Mapping[TargetKind, str],
        col: Collector,
    ) -> list[str]:
        """구간의 오류 키워드 로그 수·샘플. 샘플에서 찾은 trace_id 목록을 돌려줍니다."""
        if self._logs is None:
            col.limit(f"{service}: Loki가 설정되지 않아 로그를 확인하지 않음")
            return []
        span = f"{_hm(window.start)}~{_hm(window.end)}"
        total = self._record(
            await self._logs.count(
                "log.lines_total", window, targets, f"log.lines_total@{service}"
            ),
            "log.lines_total",
            col,
        )
        errors = self._record(
            await self._logs.count(
                "log.error_lines", window, targets, f"log.error_lines@{service}"
            ),
            "log.error_lines",
            col,
        )
        if total is None or errors is None:
            return []
        total_n = sum(v for _, v in value_rows(total))
        error_n = sum(v for _, v in value_rows(errors))
        if total_n <= 0:
            col.limit(
                f"{service}: {span} 구간의 로그가 없어 오류 로그를 판단하지 않음 "
                "(로그 수집 대상인지, service_name 라벨이 같은지 확인 필요)"
            )
            return []
        ids = (total.evidence_id, errors.evidence_id)
        col.findings.append(
            Finding(
                kind=FindingKind.FACT,
                statement=f"오류 키워드 로그 ({service}, {span}): "
                f"{error_n:.0f}건 / 전체 {total_n:.0f}건",
                severity=Severity.INFO,
                targets=(TargetRef(kind=TargetKind.SERVICE, name=service),),
                evidence_ids=ids,
                basis=JudgementBasis.STATE,
            )
        )
        if error_n <= 0 or self._cfg.log_sample_limit == 0:
            return []
        samples = self._record(
            await self._logs.samples(
                "log.error_samples",
                window,
                targets,
                f"log.error_samples@{service}",
                self._cfg.log_sample_limit,
            ),
            "log.error_samples",
            col,
        )
        if samples is None:
            return []
        trace_ids = [str(r["trace_id"]) for r in rows_of(samples) if r.get("trace_id")]
        if not trace_ids:
            col.limit(
                f"{service}: 오류 로그 샘플에서 trace_id를 찾지 못해 트레이스와 연결하지 못함"
            )
        return trace_ids

    async def _traces_for(
        self, service: str, window: TimeRange, col: Collector, *, expect_errors: bool
    ) -> list[str]:
        """서비스의 오류 트레이스 검색. `expect_errors`면 결과가 없을 때 지표와의 불일치로 표시."""
        if self._traces is None:
            col.limit(f"{service}: Tempo가 설정되지 않아 트레이스를 확인하지 않음")
            return []
        if self._cfg.trace_sample_limit == 0:
            return []
        span = f"{_hm(window.start)}~{_hm(window.end)}"
        result = self._record(
            await self._traces.search(
                f"trace.errors@{service}",
                window,
                service=service,
                errors=True,
                limit=max(self._cfg.trace_sample_limit, TRACE_LINK_SEARCH_LIMIT),
            ),
            "trace.errors",
            col,
        )
        if result is None:
            return []
        rows = rows_of(result)
        if not rows:
            col.limit(
                f"{service}: 오류율은 있지만 {span} 구간 오류 트레이스 검색 결과가 없음 "
                "(트레이스 보존·샘플링·검색 지연 가능)"
                if expect_errors
                else f"{service}: {span} 구간 오류 트레이스 검색 결과 없음"
            )
            return []
        shown = rows[: self._cfg.trace_sample_limit]
        col.findings.append(
            Finding(
                kind=FindingKind.FACT,
                statement=f"오류 트레이스 ({service}, {span}): 검색 {len(rows)}건"
                + ("(검색 상한)" if len(rows) >= TRACE_LINK_SEARCH_LIMIT else "")
                + ", 예: "
                + ", ".join(str(r["trace_id"]) for r in shown),
                severity=Severity.INFO,
                targets=(TargetRef(kind=TargetKind.SERVICE, name=service),),
                evidence_ids=(result.evidence_id,),
                basis=JudgementBasis.STATE,
            )
        )
        return [str(r["trace_id"]) for r in rows]

    # ------------------------------------------------------------------ 개요 (오류 서비스 없음)

    async def _overview(
        self,
        ctx: AnalysisContext,
        targets: Mapping[TargetKind, str],
        col: Collector,
        state: _State,
        skip: list[str],
    ) -> None:
        """질문이 로그·트레이스를 물을 때의 분석 구간 개요와, 남은 상세 자리 채우기.

        1. 분석 구간의 서비스별 오류 키워드 로그 수와 오류 트레이스(루트 서비스별) 개요
        2. 상세 자리(`detail_services`)가 남으면 다음 순서로 고른 서비스의 로그 샘플·오류 트레이스·
           trace_id 연결을 분석 구간 전체로 확인: 오류 로그가 많은 서비스 → SERVER 외 span 오류가
           있는 서비스(현재 5분) → 분석 구간 오류 트레이스의 루트 서비스.
           현재 오류율은 짧은 구간이라, 구간 중에만 오류가 있던 서비스는 오류 트레이스로 보완합니다.
           트레이스 지표가 없는 로그 출처(수집기·클러스터 객체 로그 등)는 서비스가 아니므로 제외
        """
        window = ctx.time_range
        span = f"{_hm(window.start)}~{_hm(window.end)}"
        log_counts = await self._overview_logs(window, targets, col, span)
        trace_roots = await self._overview_traces(window, col, span)
        slots = self._cfg.detail_services - len(skip)
        if slots <= 0:
            return
        basis_of: dict[str, str] = {}  # 서비스 → 선택 기준 (앞선 기준 우선)
        for name, n in log_counts:
            if n > 0:
                basis_of.setdefault(name, "오류 로그가 많은 서비스")
        for name, _ in sorted(state.other_errors.items(), key=lambda x: -x[1]):
            basis_of.setdefault(name, "SERVER 외 span 오류가 있는 서비스")
        for name in trace_roots:
            basis_of.setdefault(name, "오류 트레이스의 루트 서비스")
        # 트레이스에 나타난 이름은 서비스로 인정(현재 5분 지표에 없어도, 예: 가끔만 span을 남기는
        # 서비스). 로그에서만 나온 이름은 트레이스 지표가 있어야 서비스로 봄
        services = state.traced | set(trace_roots)
        non_service = [s for s in basis_of if s not in services]
        if non_service:
            col.limit(
                "트레이스 지표가 없는 로그 출처는 서비스 상세 확인에서 제외함: "
                + ", ".join(non_service[: self._cfg.top_n])
            )
        picked = [s for s in basis_of if s in services and s not in skip][:slots]
        if not skip:
            if picked:
                basis = "·".join(dict.fromkeys(basis_of[s] for s in picked))
                col.limit(
                    f"기준을 넘거나 증가한 오류 서비스가 없어, {basis} 기준으로 "
                    f"{span} 구간의 로그·트레이스를 확인함: " + ", ".join(picked)
                )
            else:
                col.limit(
                    "오류 로그·SERVER 외 span 오류·오류 트레이스 기준으로 상세 확인할 서비스를 "
                    "찾지 못해 서비스별 로그·트레이스 연결을 하지 않음"
                )
        for service in picked:
            if not self._time_for_detail(col, f"{service} 로그·트레이스 확인"):
                break
            service_targets = {**targets, TargetKind.SERVICE: service}
            log_ids = await self._logs_for(service, window, service_targets, col)
            trace_ids = await self._traces_for(service, window, col, expect_errors=False)
            self._link(service, window, log_ids, trace_ids, col)

    async def _overview_logs(
        self,
        window: TimeRange,
        targets: Mapping[TargetKind, str],
        col: Collector,
        span: str,
    ) -> list[tuple[str, float]]:
        """서비스별 오류 키워드 로그 수 (많은 순). 확인하지 못하면 빈 목록."""
        if self._logs is None:
            col.limit("Loki가 설정되지 않아 로그를 확인하지 않음")
            return []
        total = self._record(
            await self._logs.count("log.lines_total", window, targets, "log.lines_total@window"),
            "log.lines_total",
            col,
        )
        errors = self._record(
            await self._logs.count("log.error_lines", window, targets, "log.error_lines@window"),
            "log.error_lines",
            col,
        )
        if total is None or errors is None:
            return []
        if sum(v for _, v in value_rows(total)) <= 0:
            col.limit(f"{span} 구간의 로그가 없어 오류 로그를 판단하지 않음")
            return []
        counts = sorted(
            ((labels.get("service_name", "?"), v) for labels, v in value_rows(errors)),
            key=lambda x: -x[1],
        )
        text = (
            ", ".join(f"{n} {v:.0f}건" for n, v in counts[: self._cfg.top_n])
            if counts
            else "해당 로그 없음"
        )
        col.findings.append(
            Finding(
                kind=FindingKind.FACT,
                statement=f"오류 키워드 로그가 많은 서비스 ({span}): {text}",
                severity=Severity.INFO,
                evidence_ids=(total.evidence_id, errors.evidence_id),
                basis=JudgementBasis.STATE,
            )
        )
        return counts

    async def _overview_traces(self, window: TimeRange, col: Collector, span: str) -> list[str]:
        """분석 구간 오류 트레이스의 루트 서비스별 개요. 루트 서비스를 많은 순으로 돌려줍니다."""
        if self._traces is None:
            col.limit("Tempo가 설정되지 않아 트레이스를 확인하지 않음")
            return []
        result = self._record(
            await self._traces.search(
                "trace.errors@window",
                window,
                service=None,
                errors=True,
                limit=TRACE_LINK_SEARCH_LIMIT,
            ),
            "trace.errors",
            col,
        )
        if result is None:
            return []
        rows = rows_of(result)
        by_root: dict[str, int] = {}
        for r in rows:
            root = r.get("root_service")
            # Tempo는 루트 span이 아직 없으면 "<root span not yet received>" 같은 표시를 줌
            name = str(root) if root and is_service_name(str(root)) else UNKNOWN_ROOT
            by_root[name] = by_root.get(name, 0) + 1
        ranked = sorted(by_root.items(), key=lambda x: -x[1])
        text = ", ".join(f"{n} {c}건" for n, c in ranked) if rows else "검색 결과 없음"
        col.findings.append(
            Finding(
                kind=FindingKind.FACT,
                statement=f"오류 트레이스 ({span}, 최대 {TRACE_LINK_SEARCH_LIMIT}건 검색, "
                f"루트 서비스별): {text}",
                severity=Severity.INFO,
                evidence_ids=(result.evidence_id,),
                basis=JudgementBasis.STATE,
            )
        )
        return [name for name, _ in ranked if name != UNKNOWN_ROOT]
