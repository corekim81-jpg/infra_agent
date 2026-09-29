"""Server Agent — k3d 노드·Pod·컨테이너 자원 분석 (모델 없이 결정적 판정).

- 사용 데이터: 카탈로그에서 agent=server로 지정된 항목만 (조회 도구가 강제)
- 판정: 설정(`analysis`)의 임계값, 직전 같은 길이 구간 평균 대비 증가
- 한계: k3d 노드는 물리 서버가 아님, 빈 결과·지연 데이터·필터할 수 없는 대상은 한계로 표시
- 데이터가 없거나 오래되면 "정상"으로 판단하지 않습니다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from infra_agent.agents.explain import AgentExplainer
from infra_agent.agents.formatting import entity_of, fmt_delta, fmt_value, row_key
from infra_agent.config.settings import AnalysisConfig
from infra_agent.schemas import (
    AgentName,
    AgentResult,
    AgentStatus,
    AgentTask,
    AnalysisContext,
    ErrorInfo,
    Finding,
    FindingKind,
    Intent,
    JudgementBasis,
    Severity,
    TargetKind,
    ToolResult,
    ToolStatus,
    Usage,
)
from infra_agent.tools import CatalogQueryTool, QueryMode, value_rows

SCOPE_NOTE = (
    "Server 분석 대상은 k3d 노드·Pod·컨테이너 자원입니다. k3d 노드는 같은 물리 서버를 공유하는 "
    "컨테이너이므로 물리 서버 전체 성능을 뜻하지 않습니다."
)


@dataclass(frozen=True)
class ThresholdCheck:
    key: str
    label: str
    kind: str  # "utilization" | "throttling"
    empty_note: str


@dataclass(frozen=True)
class IncreaseCheck:
    key: str
    label: str
    min_attr: str  # AnalysisConfig 속성 이름


THRESHOLD_CHECKS: tuple[ThresholdCheck, ...] = (
    ThresholdCheck(
        "node.cpu_utilization",
        "노드 CPU 사용률(할당 가능 CPU 대비)",
        "utilization",
        "노드 CPU 사용률 결과가 없어 확인할 수 없음",
    ),
    ThresholdCheck(
        "node.memory_utilization",
        "노드 메모리 사용률(할당 가능 메모리 대비)",
        "utilization",
        "노드 메모리 사용률 결과가 없어 확인할 수 없음",
    ),
    ThresholdCheck(
        "node.filesystem_utilization",
        "노드 파일시스템 사용률",
        "utilization",
        "노드 파일시스템 사용률 결과가 없어 확인할 수 없음",
    ),
    ThresholdCheck(
        "container.cpu_limit_utilization",
        "컨테이너 CPU limit 대비 사용률",
        "utilization",
        "CPU limit이 있는 컨테이너 결과가 없어 limit 대비 사용률을 확인할 수 없음",
    ),
    ThresholdCheck(
        "container.memory_limit_utilization",
        "컨테이너 메모리 limit 대비 사용률",
        "utilization",
        "메모리 limit이 있는 컨테이너 결과가 없어 limit 대비 사용률을 확인할 수 없음",
    ),
    ThresholdCheck(
        "container.cpu_throttled_ratio",
        "컨테이너 CPU 스로틀링 비율",
        "throttling",
        "CPU 스로틀링 결과가 없어 확인할 수 없음",
    ),
)

INCREASE_CHECKS: tuple[IncreaseCheck, ...] = (
    IncreaseCheck("node.cpu_usage", "노드 CPU 사용량", "min_cpu_increase_cores"),
    IncreaseCheck(
        "node.memory_working_set", "노드 메모리 working set", "min_memory_increase_bytes"
    ),
    IncreaseCheck("pod.cpu_usage", "Pod CPU 사용량", "min_cpu_increase_cores"),
    IncreaseCheck("pod.memory_working_set", "Pod 메모리 working set", "min_memory_increase_bytes"),
)

CONTEXT_ITEMS: tuple[str, ...] = ("node.cpu_usage", "node.memory_working_set")
"""상태 조회 시 현재 값을 함께 보여줄 항목."""


@dataclass
class _Collector:
    findings: list[Finding] = field(default_factory=list)
    evidence: dict[str, ToolResult] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    next_checks: list[str] = field(default_factory=list)
    errors: list[ErrorInfo] = field(default_factory=list)
    queries: int = 0
    failed_queries: int = 0

    def add_evidence(self, result: ToolResult) -> None:
        self.evidence[result.evidence_id] = result

    def limit(self, text: str) -> None:
        if text not in self.limitations:
            self.limitations.append(text)


class ServerAgent:
    name = AgentName.SERVER

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
        self, task: AgentTask, ctx: AnalysisContext, targets: Mapping[TargetKind, str]
    ) -> AgentResult:
        col = _Collector()
        col.limit(SCOPE_NOTE)
        if ctx.intent in (Intent.COMPARE, Intent.ANOMALY) and ctx.baseline_range is not None:
            for inc in INCREASE_CHECKS:
                await self._increase(inc, ctx, targets, col)
        for thr in THRESHOLD_CHECKS:
            await self._threshold(thr, ctx, targets, col)
        if ctx.intent is Intent.STATUS:
            for key in CONTEXT_ITEMS:
                await self._context_values(key, ctx, targets, col)

        status = AgentStatus.SUCCESS
        if col.failed_queries and col.failed_queries == col.queries:
            status = AgentStatus.FAILED
            col.findings.clear()
        elif col.failed_queries:
            status = AgentStatus.PARTIAL
        if status is not AgentStatus.SUCCESS and not col.errors:
            col.errors.append(ErrorInfo(code="partial", message="일부 조회를 완료하지 못함"))
        result = AgentResult(
            task_id=task.task_id,
            agent=self.name,
            status=status,
            findings=tuple(col.findings),
            evidence=tuple(col.evidence.values()),
            limitations=tuple(col.limitations),
            next_checks=tuple(col.next_checks),
            errors=tuple(col.errors),
            usage=Usage(tool_calls=col.queries),
        )
        if self._explainer is None or not self._explainer.needed(result):
            return result
        # 모델 해석: 원인 후보(추정)와 추가 확인만 덧붙이고, 코드 판정(사실)은 그대로 둡니다.
        extra = await self._explainer.explain(result, ctx)
        return result.model_copy(
            update={
                "findings": result.findings + tuple(extra.hypotheses),
                "limitations": result.limitations + tuple(extra.limitations),
                "next_checks": result.next_checks + tuple(extra.next_checks),
                "usage": Usage(tool_calls=col.queries, llm_calls=extra.llm_calls),
                "rejected_hypotheses": tuple(extra.rejected),
            }
        )

    # ------------------------------------------------------------------ 공통

    async def _fetch(
        self,
        key: str,
        ctx: AnalysisContext,
        mode: QueryMode,
        targets: Mapping[TargetKind, str],
        col: _Collector,
    ) -> ToolResult | None:
        outcome = await self._tool.query(key, ctx, mode, targets)
        result = outcome.result
        if outcome.skipped:
            kinds = ", ".join(k.value for k in outcome.unsupported_targets)
            col.limit(f"{key}: 요청한 대상({kinds})으로 필터링할 수 없어 조회하지 않음")
            return None
        col.queries += 1
        col.add_evidence(result)
        if result.status in (ToolStatus.ERROR, ToolStatus.TIMEOUT):
            col.failed_queries += 1
            col.errors.append(ErrorInfo(code=result.status.value, message=f"{key}: {result.error}"))
            col.limit(f"{key}: 조회 실패로 확인하지 못함 ({result.error})")
            return None
        if result.freshness_seconds is None:
            col.limit(f"{key}: 데이터 최신성을 확인하지 못함")
        elif result.freshness_seconds > self._cfg.stale_after_seconds:
            col.limit(
                f"{key}: 최신 샘플이 {result.freshness_seconds:.0f}초 전으로 오래되어(기준 "
                f"{self._cfg.stale_after_seconds}초) 현재 상태를 반영하지 못할 수 있음"
            )
        return result

    def _is_fresh(self, result: ToolResult) -> bool:
        return (
            result.freshness_seconds is not None
            and result.freshness_seconds <= self._cfg.stale_after_seconds
        )

    # ------------------------------------------------------------------ 임계값 판정

    async def _threshold(
        self,
        check: ThresholdCheck,
        ctx: AnalysisContext,
        targets: Mapping[TargetKind, str],
        col: _Collector,
    ) -> None:
        result = await self._fetch(check.key, ctx, QueryMode.CURRENT, targets, col)
        if result is None:
            return
        if result.status is ToolStatus.EMPTY:
            col.limit(f"{check.key}: {check.empty_note}")
            return
        unit = self._tool.item(check.key).unit
        if check.kind == "throttling":
            warn, crit = self._cfg.throttling_warning, None
        else:
            warn, crit = self._cfg.utilization_warning, self._cfg.utilization_critical
        rows = sorted(value_rows(result), key=lambda r: -r[1])
        exceeded = [r for r in rows if r[1] >= warn]
        for labels, value in exceeded[: self._cfg.top_n]:
            target = entity_of(check.key, labels)
            is_critical = crit is not None and value >= crit
            severity = Severity.CRITICAL if is_critical else Severity.WARNING
            limit_text = fmt_value(crit if is_critical and crit is not None else warn, unit)
            col.findings.append(
                Finding(
                    kind=FindingKind.FACT,
                    statement=f"{check.label}: {target.name} {fmt_value(value, unit)} "
                    f"(기준 {limit_text} 이상)",
                    severity=severity,
                    targets=(target,),
                    evidence_ids=(result.evidence_id,),
                    basis=JudgementBasis.THRESHOLD,
                )
            )
        if len(exceeded) > self._cfg.top_n:
            col.limit(
                f"{check.key}: 기준 초과 대상 {len(exceeded)}개 중 상위 {self._cfg.top_n}개만 표시"
            )
        if exceeded:
            if check.key.startswith("container.") and "limit" in check.key:
                col.next_checks.append(
                    "limit 근접 컨테이너의 OOM·재시작 여부 확인 (Kubernetes Agent, 미구현)"
                )
            return
        if not self._is_fresh(result):
            return  # 오래되었거나 최신성을 모르는 데이터로 "기준 미만"이라고 판단하지 않음
        top_labels, top_value = rows[0]
        top_target = entity_of(check.key, top_labels)
        col.findings.append(
            Finding(
                kind=FindingKind.FACT,
                statement=f"{check.label}: 대상 {len(rows)}개 모두 기준({fmt_value(warn, unit)}) "
                f"미만, 최대 {top_target.name} {fmt_value(top_value, unit)}",
                severity=Severity.INFO,
                evidence_ids=(result.evidence_id,),
                basis=JudgementBasis.THRESHOLD,
            )
        )

    # ------------------------------------------------------------------ 현재 값

    async def _context_values(
        self,
        key: str,
        ctx: AnalysisContext,
        targets: Mapping[TargetKind, str],
        col: _Collector,
    ) -> None:
        result = await self._fetch(key, ctx, QueryMode.CURRENT, targets, col)
        if result is None or result.status is ToolStatus.EMPTY:
            if result is not None:
                col.limit(f"{key}: 결과가 없어 현재 값을 확인할 수 없음")
            return
        unit = self._tool.item(key).unit
        rows = sorted(value_rows(result), key=lambda r: -r[1])
        parts = [
            f"{entity_of(key, labels).name} {fmt_value(value, unit)}"
            for labels, value in rows[: self._cfg.top_n]
        ]
        label = "노드 CPU 사용량" if key == "node.cpu_usage" else "노드 메모리 working set"
        col.findings.append(
            Finding(
                kind=FindingKind.FACT,
                statement=f"{label} 현재 값: " + ", ".join(parts),
                severity=Severity.INFO,
                evidence_ids=(result.evidence_id,),
                basis=JudgementBasis.STATE,
            )
        )

    # ------------------------------------------------------------------ 기준 구간 대비 증가

    async def _increase(
        self,
        check: IncreaseCheck,
        ctx: AnalysisContext,
        targets: Mapping[TargetKind, str],
        col: _Collector,
    ) -> None:
        current = await self._fetch(check.key, ctx, QueryMode.WINDOW_AVG, targets, col)
        baseline = await self._fetch(check.key, ctx, QueryMode.BASELINE_AVG, targets, col)
        if current is None or baseline is None:
            return
        if current.status is ToolStatus.EMPTY or baseline.status is ToolStatus.EMPTY:
            col.limit(f"{check.key}: 분석 구간 또는 기준 구간 결과가 없어 비교할 수 없음")
            return
        unit = self._tool.item(check.key).unit
        min_abs = float(getattr(self._cfg, check.min_attr))
        base_by_key = {row_key(labels): value for labels, value in value_rows(baseline)}
        increases: list[tuple[float, float, float, dict[str, str]]] = []
        new_targets = 0
        compared = 0
        for labels, cur in value_rows(current):
            base = base_by_key.get(row_key(labels))
            if base is None:
                new_targets += 1
                continue
            compared += 1
            delta = cur - base
            ratio = delta / base if base > 0 else float("inf")
            if delta >= min_abs and ratio >= self._cfg.increase_ratio:
                increases.append((ratio, cur, base, labels))
        increases.sort(key=lambda x: (-x[0], -x[1]))
        evidence_ids = (current.evidence_id, baseline.evidence_id)
        for ratio, cur, base, labels in increases[: self._cfg.top_n]:
            target = entity_of(check.key, labels)
            pct = "기준 구간 0에서 증가" if ratio == float("inf") else f"+{ratio * 100:.0f}%"
            col.findings.append(
                Finding(
                    kind=FindingKind.FACT,
                    statement=f"{check.label} 증가: {target.name} 평균 {fmt_value(base, unit)} → "
                    f"{fmt_value(cur, unit)} ({fmt_delta(cur - base, unit)}, {pct})",
                    severity=Severity.WARNING,
                    targets=(target,),
                    evidence_ids=evidence_ids,
                    basis=JudgementBasis.BASELINE,
                )
            )
        if len(increases) > self._cfg.top_n:
            col.limit(
                f"{check.key}: 증가 대상 {len(increases)}개 중 상위 {self._cfg.top_n}개만 표시"
            )
        if new_targets:
            col.limit(
                f"{check.key}: 기준 구간에 없던 대상 {new_targets}개"
                "(새로 생성된 Pod 등)는 비교하지 않음"
            )
        if increases:
            col.next_checks.append(
                "증가한 대상의 요청량·오류·로그를 확인해 원인 후보 좁히기 (Service Agent, 미구현)"
            )
        elif compared and self._is_fresh(current):
            col.findings.append(
                Finding(
                    kind=FindingKind.FACT,
                    statement=f"{check.label}: 비교 대상 {compared}개 중 직전 구간 대비 "
                    f"{self._cfg.increase_ratio * 100:.0f}% 이상이면서 "
                    f"{fmt_value(min_abs, unit)} 이상 증가한 대상 없음",
                    severity=Severity.INFO,
                    evidence_ids=evidence_ids,
                    basis=JudgementBasis.BASELINE,
                )
            )
