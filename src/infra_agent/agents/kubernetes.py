"""Kubernetes Agent — 클러스터 리소스 상태·재시작·OOM 분석 (모델 없이 결정적 판정).

- 사용 데이터: 카탈로그에서 agent=kubernetes로 지정된 항목만 (조회 도구가 강제).
  Prometheus의 k8s_cluster 수집 지표와 cAdvisor OOM 이벤트이며, Kubernetes API는 아직 쓰지 않습니다.
- 대부분의 항목은 **조건 조회**(문제가 있는 대상만 결과로 나옴)입니다. 빈 결과를 "해당 없음"으로
  판단하려면 다음이 모두 필요합니다. 하나라도 확인하지 못하면 "확인하지 못함"으로 표시합니다.
  1. 조회 성공
  2. 기준 지표가 최신 (`analysis.stale_after_seconds` 이내)
  3. 대상 필터가 있으면 그 대상의 기준 지표 시계열이 실제로 존재
     (대상 이름 오타 등으로 빈 결과가 되는 것 방지)
- 재시작·OOM은 분석 구간 전체의 증가량으로 판정합니다(상태 질문이면 기본 구간).
- Pending 사유, 종료 사유(OOMKilled 등), 이벤트는 수집 위치가 확인되지 않아 조회하지 않습니다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from infra_agent.agents.base import context_targets
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
    Finding,
    FindingKind,
    JudgementBasis,
    Severity,
    TargetKind,
    TargetRef,
    ToolResult,
    ToolStatus,
)
from infra_agent.tools import CatalogQueryTool, QueryMode, value_rows

SCOPE_NOTE = (
    "Kubernetes 분석은 Prometheus의 k8s_cluster·cAdvisor 지표 기준입니다. Pending 사유, "
    "컨테이너 종료 사유(OOMKilled 등), Kubernetes 이벤트는 수집 위치가 확인되지 않아 조회하지 "
    "않았습니다(Kubernetes API 읽기 계정 연동 후 확인 가능)."
)

POD_PHASES: Mapping[int, str] = {
    1: "Pending",
    2: "Running",
    3: "Succeeded",
    4: "Failed",
    5: "Unknown",
}
"""OTel k8s_cluster 수신기의 `k8s.pod.phase` 값 정의.
개발 서버 값 검토 전 가정입니다(카탈로그 caveats)."""

PHASE_NOTE = (
    "Pod phase 값은 OTel k8s_cluster 수신기 정의(1=Pending, 2=Running, 3=Succeeded, "
    "4=Failed, 5=Unknown)로 해석했습니다(개발 서버 값 검토 전)."
)

_WORKLOAD_LABELS = (
    "k8s_deployment_name",
    "k8s_statefulset_name",
    "k8s_daemonset_name",
    "k8s_job_name",
    "k8s_hpa_name",
)


def _count(value: float) -> str:
    return f"{value:g}"


def _phase_text(value: float) -> str:
    return POD_PHASES.get(int(value), f"알 수 없는 값 {value:g}")


def _phase_severity(value: float) -> Severity:
    return Severity.CRITICAL if int(value) == 4 else Severity.WARNING


@dataclass(frozen=True)
class StateCheck:
    """조건 조회 하나의 판정 규칙. 결과 행 하나가 문제 대상 하나입니다."""

    key: str
    label: str
    """사실 문장 앞부분 (예: "재시작한 컨테이너")."""
    severity: Severity
    describe: Callable[[float], str]
    """결과 값 설명 (예: 2 → "2회")."""
    mode: QueryMode = QueryMode.CURRENT
    severity_of: Callable[[float], Severity] | None = None


CHECKS: tuple[StateCheck, ...] = (
    StateCheck(
        "k8s.node_not_ready",
        "Ready 상태가 아닌 노드",
        Severity.CRITICAL,
        lambda v: f"Ready 조건 값 {v:g}",
    ),
    StateCheck(
        "k8s.node_pressure",
        "메모리·디스크·PID 압박 조건이 참인 노드",
        Severity.WARNING,
        lambda v: "압박 조건 참",
    ),
    StateCheck(
        "k8s.pod_phase",
        "Running·Succeeded가 아닌 Pod",
        Severity.WARNING,
        _phase_text,
        severity_of=_phase_severity,
    ),
    StateCheck(
        "k8s.container_not_ready",
        "준비(ready)되지 않은 컨테이너",
        Severity.WARNING,
        lambda v: "not ready",
    ),
    StateCheck(
        "k8s.container_restarts_increase",
        "재시작한 컨테이너",
        Severity.WARNING,
        lambda v: f"{_count(v)}회",
        mode=QueryMode.WINDOW,
    ),
    StateCheck(
        "k8s.container_oom_events",
        "OOM 이벤트가 발생한 컨테이너",
        Severity.CRITICAL,
        lambda v: f"{_count(v)}회",
        mode=QueryMode.WINDOW,
    ),
    StateCheck(
        "k8s.deployment_unavailable",
        "사용 가능 복제 수가 부족한 Deployment",
        Severity.WARNING,
        lambda v: f"{_count(v)}개 부족",
    ),
    StateCheck(
        "k8s.statefulset_unready",
        "준비된 Pod 수가 부족한 StatefulSet",
        Severity.WARNING,
        lambda v: f"{_count(v)}개 부족",
    ),
    StateCheck(
        "k8s.daemonset_unready",
        "준비된 노드 수가 부족한 DaemonSet",
        Severity.WARNING,
        lambda v: f"{_count(v)}개 부족",
    ),
    StateCheck(
        "k8s.job_failed_pods",
        "실패한 Pod가 있는 Job",
        Severity.WARNING,
        lambda v: f"실패 Pod {_count(v)}개",
    ),
    StateCheck(
        "k8s.hpa_at_max",
        "최대 복제 수에 도달한 HPA",
        Severity.WARNING,
        lambda v: "최대 도달",
    ),
)


def k8s_entity(labels: Mapping[str, str]) -> TargetRef:
    """결과 라벨로 가장 구체적인 대상을 만듭니다 (container > pod > workload > node > namespace)."""
    namespace = labels.get("k8s_namespace_name")
    workload_label = next((w for w in _WORKLOAD_LABELS if w in labels), None)
    pod = labels.get("k8s_pod_name")
    container = labels.get("k8s_container_name")
    node = labels.get("k8s_node_name")
    if container and pod:
        kind, parts = TargetKind.CONTAINER, [namespace, pod, container]
    elif pod:
        kind, parts = TargetKind.POD, [namespace, pod]
    elif workload_label:
        kind, parts = TargetKind.WORKLOAD, [namespace, labels[workload_label]]
    elif node:
        kind, parts = TargetKind.NODE, [node]
    elif namespace:
        kind, parts = TargetKind.NAMESPACE, [namespace]
    else:
        kind, parts = TargetKind.HOST, [str(dict(labels))]
    used_names = (
        "k8s_namespace_name",
        "k8s_pod_name",
        "k8s_container_name",
        "k8s_node_name",
        *_WORKLOAD_LABELS,
    )
    used = {k: labels[k] for k in used_names if k in labels}
    return TargetRef(kind=kind, name="/".join(p for p in parts if p), labels=used)


def _window_text(ctx: AnalysisContext) -> str:
    minutes = int(ctx.time_range.duration.total_seconds() // 60)
    if minutes >= 120 and minutes % 60 == 0:
        return f"최근 {minutes // 60}시간"
    return f"최근 {minutes}분"


class KubernetesAgent:
    name = AgentName.KUBERNETES

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
        """선행 작업이 없는 독립 분석이므로 `upstream`은 사용하지 않습니다."""
        targets = context_targets(ctx)
        col = Collector()
        col.limit(SCOPE_NOTE)
        found: dict[str, dict[str, float]] = {}
        for check in CHECKS:
            found[check.key] = await self._check(check, ctx, targets, col)
        self._suggest(found, col)
        result = finish(task, self.name, col)
        return await with_explanation(result, self._explainer, ctx)

    async def _check(
        self,
        check: StateCheck,
        ctx: AnalysisContext,
        targets: Mapping[TargetKind, str],
        col: Collector,
    ) -> dict[str, float]:
        """판정하고, 문제 대상 이름 → 값을 돌려줍니다."""
        result = await fetch_evidence(
            self._tool, self._cfg, check.key, ctx, check.mode, targets, col
        )
        if result is None:
            return {}
        scope = _window_text(ctx) if check.mode is QueryMode.WINDOW else "현재"
        rows = sorted(value_rows(result), key=lambda r: -r[1])
        if result.status is ToolStatus.EMPTY or not rows:
            await self._empty(check, result, ctx, targets, scope, col)
            return {}
        problems: dict[str, float] = {}
        for labels, value in rows:
            target = k8s_entity(labels)
            problems.setdefault(target.name, value)
        if check.key == "k8s.pod_phase":
            col.limit(PHASE_NOTE)
        shown = rows[: self._cfg.top_n]
        for labels, value in shown:
            target = k8s_entity(labels)
            severity = check.severity_of(value) if check.severity_of else check.severity
            col.findings.append(
                Finding(
                    kind=FindingKind.FACT,
                    statement=f"{check.label} ({scope}): {target.name} {check.describe(value)}",
                    severity=severity,
                    targets=(target,),
                    evidence_ids=(result.evidence_id,),
                    basis=JudgementBasis.STATE,
                )
            )
        if len(rows) > len(shown):
            col.limit(f"{check.key}: 해당 대상 {len(rows)}개 중 상위 {len(shown)}개만 표시")
        return problems

    async def _empty(
        self,
        check: StateCheck,
        result: ToolResult,
        ctx: AnalysisContext,
        targets: Mapping[TargetKind, str],
        scope: str,
        col: Collector,
    ) -> None:
        """빈 결과는 최신성과 대상 존재를 확인한 경우에만 "해당 없음"으로 판정합니다."""
        if not is_fresh(result, self._cfg):
            col.limit(f"{check.key}: 결과가 없지만 데이터 최신성을 확인하지 못해 판단하지 않음")
            return
        coverage = await self._tool.coverage(check.key, targets, ctx.time_range.end)
        if targets and coverage is None:
            col.limit(f"{check.key}: 요청 대상의 데이터 존재를 확인하지 못해 판단하지 않음")
            return
        if coverage == 0:
            names = ", ".join(f"{k.value}={v}" for k, v in targets.items())
            col.limit(f"{check.key}: 요청 대상({names})에 해당하는 시계열이 없어 판단하지 않음")
            return
        col.findings.append(
            Finding(
                kind=FindingKind.FACT,
                statement=f"{check.label} ({scope}): 해당 대상 없음",
                severity=Severity.INFO,
                evidence_ids=(result.evidence_id,),
                basis=JudgementBasis.STATE,
            )
        )

    def _suggest(self, found: Mapping[str, Mapping[str, float]], col: Collector) -> None:
        oom = found.get("k8s.container_oom_events", {})
        restarts = found.get("k8s.container_restarts_increase", {})
        both = sorted(set(oom) & set(restarts))
        if both:
            col.suggest(
                "OOM 이벤트와 재시작이 같은 구간에 함께 확인된 컨테이너("
                + ", ".join(both[: self._cfg.top_n])
                + "): 종료 사유가 OOMKilled인지는 Kubernetes API 연동 후 확인 가능. "
                "메모리 limit 대비 사용률은 Server Agent로 확인"
            )
        elif oom or restarts:
            col.suggest(
                "재시작·OOM 컨테이너의 종료 사유와 이벤트 확인 (Kubernetes API 연동 후 가능)"
            )
        if found.get("k8s.pod_phase"):
            col.suggest(
                "Pending·Failed Pod의 사유(스케줄링 실패 등) 확인 (Kubernetes API 연동 후 가능)"
            )
