"""Kubernetes Agent — 클러스터 리소스 상태·재시작·OOM 분석 (모델 없이 결정적 판정).

- 사용 데이터: 카탈로그에서 agent=kubernetes로 지정된 항목만 (조회 도구가 강제).
  Prometheus의 k8s_cluster 수집 지표와 cAdvisor OOM 이벤트로 판정합니다.
- Kubernetes API(전용 읽기 계정, 설정 시)는 Pending 사유, 컨테이너 대기 사유, 구간 내 비정상 종료
  사유(OOMKilled 등), Warning 이벤트를 더합니다. 지표 판정이 이미 경고한 대상은 사유만 덧붙이는
  정보(INFO)로, 지표 판정에 없던 대상은 경고로 표시합니다(같은 문제를 두 번 세지 않음).
  Warning 이벤트는 정보로만 표시합니다.
  권한 점검에서 쓰기·Pod 실행·프록시·secrets 읽기 권한이 확인되면 API를 조회하지 않습니다.
- 대부분의 항목은 **조건 조회**(문제가 있는 대상만 결과로 나옴)입니다. 빈 결과를 "해당 없음"으로
  판단하려면 다음이 모두 필요합니다. 하나라도 확인하지 못하면 "확인하지 못함"으로 표시합니다.
  1. 조회 성공
  2. 기준 지표가 최신 (`analysis.stale_after_seconds` 이내)
  3. 대상 필터가 있으면 그 대상의 기준 지표 시계열이 실제로 존재
     (대상 이름 오타 등으로 빈 결과가 되는 것 방지)
- 재시작·OOM은 분석 구간 전체의 증가량으로 판정합니다(상태 질문이면 기본 구간).
- Kubernetes API의 Pod 상태는 "지금"의 상태이므로, 분석 구간 끝이 현재가 아니면 사용하지 않습니다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

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
    ErrorInfo,
    Finding,
    FindingKind,
    JudgementBasis,
    Severity,
    TargetKind,
    TargetRef,
    ToolResult,
    ToolStatus,
)
from infra_agent.timeutil import utc_now
from infra_agent.tools import CatalogQueryTool, QueryMode, QueryOutcome, rows_of, value_rows
from infra_agent.tools.k8s_query import KubernetesQueryTool
from infra_agent.units import fmt_time

SCOPE_NOTE = (
    "Kubernetes 분석은 Prometheus의 k8s_cluster·cAdvisor 지표 기준입니다. Kubernetes API를 "
    "사용하지 않아 Pending 사유, 컨테이너 종료 사유(OOMKilled 등), Kubernetes 이벤트는 조회하지 "
    "않았습니다(전용 읽기 계정 설정 후 확인 가능, docs/environment.md 3.5절)."
)
API_SCOPE_NOTE = (
    "Kubernetes 분석은 Prometheus의 k8s_cluster·cAdvisor 지표 판정에 Kubernetes API(전용 읽기 "
    "계정)의 Pending 사유, 컨테이너 대기·종료 사유, Warning 이벤트를 더한 것입니다. 지표 판정에 "
    "없던 문제만 API 결과로 경고하고, 이벤트는 정보로만 표시합니다."
)
EVENT_RETENTION_NOTE = (
    "Kubernetes 이벤트는 API 서버 보관 기간(기본 1시간)이 지나면 사라지므로, 분석 구간 앞부분의 "
    "이벤트는 확인하지 못했을 수 있음"
)
EVENT_RETENTION_SECONDS = 3600
"""kube-apiserver `--event-ttl` 기본값(1시간)."""
EVENT_COUNT_NOTE = (
    "Warning 이벤트 횟수는 이벤트 객체의 누적 횟수로, 분석 구간 이전 발생분이 포함될 수 있음"
)
BENIGN_WAITING = frozenset({"ContainerCreating", "PodInitializing"})
"""정상 시작 과정의 대기 사유 (상세 사실로 표시하지 않음)."""
_EVENT_WORKLOAD_KINDS = frozenset(
    {"Deployment", "ReplicaSet", "StatefulSet", "DaemonSet", "Job", "CronJob"}
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
        *,
        api: KubernetesQueryTool | None = None,
        api_note: str | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """`api`는 Kubernetes API 조회 도구(설정 시), `api_note`는 API를 쓰지 못한 이유입니다.

        `clock`은 분석 구간 끝이 현재인지 판단할 때 씁니다(테스트용으로 바꿀 수 있음)."""
        self._tool = tool
        self._cfg = analysis
        self._explainer = explainer
        self._api = api
        self._api_note = api_note
        self._clock = clock

    async def run(
        self,
        task: AgentTask,
        ctx: AnalysisContext,
        upstream: Mapping[str, AgentResult] | None = None,
    ) -> AgentResult:
        """선행 작업이 없는 독립 분석이므로 `upstream`은 사용하지 않습니다."""
        targets = context_targets(ctx)
        col = Collector()
        col.limit(API_SCOPE_NOTE if self._api is not None else SCOPE_NOTE)
        if self._api_note:
            col.limit(self._api_note)
        found: dict[str, dict[str, float]] = {}
        for check in CHECKS:
            found[check.key] = await self._check(check, ctx, targets, col)
        details = (
            await self._api_details(self._api, ctx, targets, col, found) if self._api else None
        )
        self._suggest(found, col, details)
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

    # --- Kubernetes API 상세 정보

    async def _api_details(
        self,
        api: KubernetesQueryTool,
        ctx: AnalysisContext,
        targets: Mapping[TargetKind, str],
        col: Collector,
        found: Mapping[str, Mapping[str, float]],
    ) -> ApiDetails | None:
        """권한 점검 뒤 Pod 상태·Warning 이벤트를 상세 사실로 더합니다. 조회하지 못하면 None.

        `found`는 지표 판정에서 문제로 확인된 대상(검사 키 → 대상 이름)입니다."""
        perm = await api.permissions(targets)
        col.queries += 1
        col.add_evidence(perm.result)
        if perm.report is None:
            col.failed_queries += 1
            col.errors.append(
                ErrorInfo(
                    code=perm.result.status.value,
                    message=f"k8s_api.permissions: {perm.result.error}",
                )
            )
            col.limit(f"Kubernetes API 권한 점검 실패로 API 조회를 하지 않음 ({perm.result.error})")
            return None
        for note in perm.report.notes:
            col.limit(f"Kubernetes API: {note}")
        if not perm.report.ok:
            col.limit(
                "Kubernetes API: 계정에 "
                + ", ".join(perm.report.problems)
                + " — 전용 읽기 계정이 아니므로 API 조회를 하지 않음 "
                "(deploy/rbac/infra-agent-reader.yaml의 읽기 계정 사용)"
            )
            return None
        details = ApiDetails()
        lag = (self._clock() - ctx.time_range.end).total_seconds()
        pod_names: frozenset[str] | None = None
        if lag > self._cfg.stale_after_seconds:
            col.limit(
                "k8s_api.pods: 분석 구간 끝이 현재가 아니어서 Kubernetes API의 현재 Pod 상태는 "
                "사용하지 않음"
            )
        else:
            pods = self._record(await api.pods(ctx, targets), "k8s_api.pods", col)
            if pods is not None:
                details.pods_seen = self._pod_facts(pods, ctx, targets, col, details, found)
                if pods.status is not ToolStatus.TRUNCATED:
                    pod_names = frozenset(str(r.get("pod", "")) for r in rows_of(pods))
        events = self._record(
            await api.warning_events(ctx, targets, pod_names), "k8s_api.events", col
        )
        if events is not None:
            self._event_facts(events, ctx, targets, col, details)
        return details

    def _record(self, outcome: QueryOutcome, label: str, col: Collector) -> ToolResult | None:
        result = outcome.result
        if outcome.skipped:
            if outcome.unsupported_targets:
                kinds = ", ".join(k.value for k in outcome.unsupported_targets)
                col.limit(f"{label}: 요청한 대상({kinds})으로 거를 수 없어 조회하지 않음")
            else:
                col.limit(f"{label}: 조회하지 않음 ({result.error})")
            return None
        col.queries += 1
        col.add_evidence(result)
        if result.status in (ToolStatus.ERROR, ToolStatus.TIMEOUT):
            col.failed_queries += 1
            col.errors.append(
                ErrorInfo(code=result.status.value, message=f"{label}: {result.error}")
            )
            col.limit(f"{label}: 조회 실패로 확인하지 못함 ({result.error})")
            return None
        if result.status is ToolStatus.TRUNCATED:
            col.limit(f"{label}: 객체가 많아 일부(datasources.kubernetes.max_items)만 확인함")
        return result

    def _pod_facts(
        self,
        result: ToolResult,
        ctx: AnalysisContext,
        targets: Mapping[TargetKind, str],
        col: Collector,
        details: ApiDetails,
        found: Mapping[str, Mapping[str, float]],
    ) -> int:
        """Pending 사유·대기 사유·구간 내 비정상 종료를 사실로 만들고 확인한 Pod 수를 돌려줍니다.

        지표 판정이 이미 경고한 대상이면 사유를 덧붙이는 정보(INFO)로, 지표 판정에 없던 대상이면
        경고(OOMKilled는 심각)로 표시합니다. 같은 문제를 두 번 세지 않고, 지표 수집 지연·누락으로
        놓친 문제도 드러내기 위함입니다."""

        def flagged(name: str, *keys: str) -> bool:
            return any(name in found.get(k, {}) for k in keys)

        rows = rows_of(result)
        if not rows:
            if result.status is ToolStatus.TRUNCATED:
                return 0
            what = "요청 대상에 해당하는 Pod가 없어" if targets else "Pod 목록이 비어 있어"
            col.limit(f"k8s_api.pods: {what} Pod 상세 상태를 판단하지 않음")
            return 0
        window = _window_text(ctx)
        container_target = targets.get(TargetKind.CONTAINER)
        pending: list[tuple[str, TargetRef, Severity]] = []
        waiting: list[tuple[str, TargetRef, Severity]] = []
        ended: list[tuple[datetime, str, TargetRef, Severity]] = []
        merged_ends = False
        for row in rows:
            ns, pod = str(row.get("namespace", "")), str(row.get("pod", ""))
            pod_ref = TargetRef(
                kind=TargetKind.POD,
                name=f"{ns}/{pod}",
                labels={"k8s_namespace_name": ns, "k8s_pod_name": pod},
            )
            containers = [
                c
                for c in _dicts(row.get("containers"))
                if container_target is None or c.get("name") == container_target
            ]
            reason_container: str | None = None
            if row.get("phase") == "Pending":
                severity = (
                    Severity.INFO if flagged(pod_ref.name, "k8s.pod_phase") else Severity.WARNING
                )
                reason, reason_container = _pending_reason(row, containers)
                pending.append((f"{pod_ref.name} — {reason}", pod_ref, severity))
            for c in containers:
                name = str(c.get("name", ""))
                ref = TargetRef(
                    kind=TargetKind.CONTAINER,
                    name=f"{ns}/{pod}/{name}",
                    labels={**pod_ref.labels, "k8s_container_name": name},
                )
                ends = self._container_ends(c, ctx, ref, details, flagged)
                wait = _dict_or_none(c.get("waiting"))
                if name == reason_container:
                    wait = None  # Pending 사유로 이미 표시 (같은 문제를 두 번 세지 않음)
                if wait and wait.get("reason") not in BENIGN_WAITING:
                    # 대기 중인 컨테이너의 구간 내 종료는 같은 문제이므로 한 사실로 묶습니다.
                    text = f"{ref.name} — {wait.get('reason') or '사유 미기록'}"
                    text += f" (재시작 {c.get('restart_count', 0)}회)"
                    if wait.get("message"):
                        text += f": {wait['message']}"
                    known = flagged(
                        ref.name, "k8s.container_not_ready", "k8s.container_restarts_increase"
                    )
                    severities = [Severity.INFO if known else Severity.WARNING]
                    if ends:
                        merged_ends = True
                        text += "; 구간 내 종료 " + ", ".join(t for _, t, _ in ends)
                        severities += [sev for _, _, sev in ends]
                    waiting.append((text, ref, max(severities, key=_SEVERITY_ORDER.__getitem__)))
                else:
                    ended.extend(
                        (finished, f"{ref.name} — {text}", ref, sev) for finished, text, sev in ends
                    )
        latest_first = sorted(ended, key=lambda e: e[0], reverse=True)
        groups: list[tuple[str, str, list[tuple[str, TargetRef, Severity]]]] = [
            ("Pending Pod 사유", "현재", pending),
            ("대기 중인 컨테이너", "현재", waiting),
            ("비정상 종료된 컨테이너", window, [(t, r, sev) for _, t, r, sev in latest_first]),
        ]
        none: list[str] = []
        for label, scope, items in groups:
            if not items:
                if not (label == "비정상 종료된 컨테이너" and merged_ends):
                    none.append(f"{label.replace(' 사유', '')}({scope})")
                continue
            items.sort(key=lambda item: -_SEVERITY_ORDER[item[2]])
            for text, ref, severity in items[: self._cfg.top_n]:
                col.findings.append(
                    _api_fact(
                        f"{label} ({scope}, Kubernetes API): {text}", (ref,), result, severity
                    )
                )
            if len(items) > self._cfg.top_n:
                col.limit(
                    f"k8s_api.pods: {label} {len(items)}개 중 상위 {self._cfg.top_n}개만 표시"
                )
        # 목록 일부만 봤으면 "해당 대상 없음"을 말하지 않음 (데이터 부족을 정상으로 보지 않음)
        if none and result.status is not ToolStatus.TRUNCATED:
            col.findings.append(
                _api_fact(
                    f"Kubernetes API 상세 확인 (Pod {len(rows)}개): "
                    + ", ".join(none)
                    + " 해당 대상 없음",
                    (),
                    result,
                )
            )
        return len(rows)

    @staticmethod
    def _container_ends(
        c: Mapping[str, Any],
        ctx: AnalysisContext,
        ref: TargetRef,
        details: ApiDetails,
        flagged: Callable[..., bool],
    ) -> list[tuple[datetime, str, Severity]]:
        """컨테이너의 분석 구간 내 비정상 종료 (시각, 설명, 심각도)."""
        out: list[tuple[datetime, str, Severity]] = []
        seen: set[str] = set()
        for term in (c.get("last_terminated"), c.get("terminated")):
            info = _abnormal_end(term, ctx)
            if info is None or info[1] in seen:
                continue
            seen.add(info[1])
            finished, _key, reason, code = info
            code_text = f"종료 코드 {code}, " if code is not None else ""
            if reason == "OOMKilled":
                details.oom_killed.add(ref.name)
                known = flagged(ref.name, "k8s.container_oom_events")
                severity = Severity.INFO if known else Severity.CRITICAL
            else:
                known = flagged(ref.name, "k8s.container_restarts_increase")
                severity = Severity.INFO if known else Severity.WARNING
            out.append((finished, f"{reason} ({code_text}{fmt_time(finished)})", severity))
        return out

    def _event_facts(
        self,
        result: ToolResult,
        ctx: AnalysisContext,
        targets: Mapping[TargetKind, str],
        col: Collector,
        details: ApiDetails,
    ) -> None:
        window = _window_text(ctx)
        # 이벤트 보관 기간보다 오래된 시각이 구간에 포함되면 그 부분은 확인할 수 없음
        expired = (self._clock() - ctx.time_range.start).total_seconds() > EVENT_RETENTION_SECONDS
        if expired:
            col.limit(EVENT_RETENTION_NOTE)
        grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for row in rows_of(result):
            key = (
                str(row.get("namespace", "")),
                str(row.get("kind", "")),
                str(row.get("name", "")),
                str(row.get("reason", "")),
            )
            count = row.get("count")
            entry = grouped.setdefault(key, {"count": 0, "last_seen": None, "message": None})
            entry["count"] += count if isinstance(count, int) else 1
            last = _parse_iso(row.get("last_seen"))
            if last is not None and (entry["last_seen"] is None or last >= entry["last_seen"]):
                entry["last_seen"] = last
                entry["message"] = row.get("message")
        if not grouped:
            if result.status is ToolStatus.TRUNCATED:
                return
            if targets and not details.pods_seen:
                col.limit(
                    "k8s_api.events: 요청 대상의 존재를 확인하지 못해 Warning 이벤트 없음으로 "
                    "판단하지 않음"
                )
                return
            if expired:
                col.limit(
                    "k8s_api.events: 분석 구간 일부가 이벤트 보관 기간 밖이어서 Warning 이벤트 "
                    "없음으로 판단하지 않음"
                )
                return
            col.findings.append(
                _api_fact(f"Warning 이벤트 ({window}, Kubernetes API): 없음", (), result)
            )
            return
        col.limit(EVENT_COUNT_NOTE)
        ordered = sorted(grouped.items(), key=lambda kv: (-kv[1]["count"], kv[0]))
        for (ns, kind, name, reason), entry in ordered[: self._cfg.top_n]:
            seen = entry["last_seen"]
            when = f", 마지막 {fmt_time(seen)}" if seen else ""
            obj = "/".join(p for p in (ns, kind, name) if p)
            text = f"{obj} — {reason} 누적 {entry['count']}회{when}"
            if entry["message"]:
                text += f": {entry['message']}"
            col.findings.append(
                _api_fact(
                    f"Warning 이벤트 ({window}, Kubernetes API): {text}",
                    _event_targets(ns, kind, name),
                    result,
                )
            )
        if len(ordered) > self._cfg.top_n:
            col.limit(
                f"k8s_api.events: Warning 이벤트 {len(ordered)}종 중 "
                f"상위 {self._cfg.top_n}종만 표시"
            )

    def _suggest(
        self,
        found: Mapping[str, Mapping[str, float]],
        col: Collector,
        details: ApiDetails | None,
    ) -> None:
        oom = found.get("k8s.container_oom_events", {})
        restarts = found.get("k8s.container_restarts_increase", {})
        both = sorted(set(oom) & set(restarts))
        if both:
            names = ", ".join(both[: self._cfg.top_n])
            if details is not None:
                col.suggest(
                    f"OOM 이벤트와 재시작이 같은 구간에 함께 확인된 컨테이너({names}): 종료 사유는 "
                    "Kubernetes API 상세(비정상 종료된 컨테이너) 참고. 메모리 limit 대비 사용률은 "
                    "Server Agent로 확인"
                )
            else:
                col.suggest(
                    f"OOM 이벤트와 재시작이 같은 구간에 함께 확인된 컨테이너({names}): 종료 사유가 "
                    "OOMKilled인지는 Kubernetes API 연동 후 확인 가능. "
                    "메모리 limit 대비 사용률은 Server Agent로 확인"
                )
        elif (oom or restarts) and details is None:
            col.suggest(
                "재시작·OOM 컨테이너의 종료 사유와 이벤트 확인 (Kubernetes API 연동 후 가능)"
            )
        if found.get("k8s.pod_phase") and details is None:
            col.suggest(
                "Pending·Failed Pod의 사유(스케줄링 실패 등) 확인 (Kubernetes API 연동 후 가능)"
            )


@dataclass
class ApiDetails:
    """Kubernetes API 상세 확인 결과 (추가 확인 제안에 사용)."""

    pods_seen: int = 0
    oom_killed: set[str] = field(default_factory=set)


def _dicts(value: object) -> list[dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _dict_or_none(value: object) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _pending_reason(
    row: Mapping[str, Any], containers: list[dict[str, Any]]
) -> tuple[str, str | None]:
    """Pending 사유와, 사유가 컨테이너 대기이면 그 컨테이너 이름.

    우선순위: 스케줄링 실패(PodScheduled=False) > 컨테이너 대기 사유 > Pod 사유."""
    sched = _dict_or_none(row.get("scheduled"))
    if sched and sched.get("status") == "False":
        reason = sched.get("reason") or "스케줄링 안 됨"
        text = f"{reason}: {sched['message']}" if sched.get("message") else str(reason)
        return text, None
    for c in containers:
        wait = _dict_or_none(c.get("waiting"))
        if wait and wait.get("reason"):
            text = f"컨테이너 {c.get('name')} 대기 {wait['reason']}"
            if wait.get("message"):
                text += f": {wait['message']}"
            return text, str(c.get("name"))
    return str(row.get("reason") or "사유 미기록"), None


def _abnormal_end(
    term: object, ctx: AnalysisContext
) -> tuple[datetime, str, str, int | None] | None:
    """분석 구간 안의 비정상 종료 (시각, 구분 키, 사유, 종료 코드). 정상 완료는 None."""
    info = _dict_or_none(term)
    if info is None:
        return None
    finished = _parse_iso(info.get("finished_at"))
    if finished is None or not (ctx.time_range.start <= finished <= ctx.time_range.end):
        return None
    reason = str(info.get("reason") or "사유 미기록")
    code = info.get("exit_code")
    code = code if isinstance(code, int) else None
    if reason == "Completed" and code in (0, None):
        return None
    return finished, f"{finished.isoformat()}|{reason}|{code}", reason, code


def _event_targets(ns: str, kind: str, name: str) -> tuple[TargetRef, ...]:
    if kind == "Pod":
        labels = {"k8s_namespace_name": ns, "k8s_pod_name": name}
        return (TargetRef(kind=TargetKind.POD, name=f"{ns}/{name}", labels=labels),)
    if kind == "Node":
        return (TargetRef(kind=TargetKind.NODE, name=name, labels={"k8s_node_name": name}),)
    if kind in _EVENT_WORKLOAD_KINDS:
        return (TargetRef(kind=TargetKind.WORKLOAD, name=f"{ns}/{name}"),)
    return ()


_SEVERITY_ORDER: Mapping[Severity, int] = {
    Severity.INFO: 0,
    Severity.WARNING: 1,
    Severity.CRITICAL: 2,
}


def _api_fact(
    statement: str,
    targets: tuple[TargetRef, ...],
    result: ToolResult,
    severity: Severity = Severity.INFO,
) -> Finding:
    """Kubernetes API 사실 (기본은 정보, 지표 판정에 없던 문제만 경고·심각)."""
    return Finding(
        kind=FindingKind.FACT,
        statement=statement,
        severity=severity,
        targets=targets,
        evidence_ids=(result.evidence_id,),
        basis=JudgementBasis.STATE,
    )
