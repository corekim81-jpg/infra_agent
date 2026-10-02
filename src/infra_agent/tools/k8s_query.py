"""읽기 전용 Kubernetes API 조회 도구 (Kubernetes Agent 전용).

- Kubernetes Agent만 만들 수 있습니다(다른 에이전트는 `ToolPermissionError`).
- 실행할 수 있는 조회는 코드가 정한 세 가지뿐입니다: 권한 점검, Pod 상태 요약, Warning 이벤트.
- **권한 점검을 통과해야** Pod·이벤트를 조회합니다. 쓰기·Pod 실행·프록시·secrets 읽기 권한이
  있는 계정이면 조회하지 않습니다(전용 읽기 계정만 사용).
- 상태 메시지·이벤트 메시지는 외부 데이터입니다. 제어문자 제거, 길이 제한, 비밀값 마스킹 뒤 근거에
  담으며 실행 지시로 취급하지 않습니다. 근거에는 판정에 필요한 필드만 요약해 남깁니다
  (환경 변수·명령·볼륨 등 Pod 명세는 남기지 않음).
- 모든 호출은 요청 단위 조회 예산(`ToolBudget`)과 호출별 제한 시간을 따릅니다.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any, TypeVar

from infra_agent.datasources.errors import DataSourceError
from infra_agent.datasources.kubernetes import (
    KubernetesClient,
    ListResult,
    PermissionReport,
    RulesReview,
    assess_rules,
    is_k8s_name,
    merge_reviews,
    summarize_rules,
)
from infra_agent.schemas import (
    AgentName,
    AnalysisContext,
    DataSourceKind,
    TargetKind,
    TimeRange,
    ToolResult,
    ToolStatus,
)
from infra_agent.timeutil import utc_now
from infra_agent.tools.catalog_query import QueryOutcome, ToolBudget, ToolPermissionError
from infra_agent.tools.log_query import clean_text

MESSAGE_CHARS = 200
SUPPORTED_TARGETS = frozenset(
    {
        TargetKind.NAMESPACE,
        TargetKind.NODE,
        TargetKind.POD,
        TargetKind.CONTAINER,
        TargetKind.WORKLOAD,
    }
)
"""Kubernetes API 결과를 거를 수 있는 대상 종류."""

T = TypeVar("T")


@dataclass
class PermissionOutcome:
    result: ToolResult
    report: PermissionReport | None
    """권한 점검을 하지 못했으면 None."""


def _text(value: object, limit: int = MESSAGE_CHARS) -> str | None:
    return clean_text(value, limit) if isinstance(value, str) and value else None


def _name(value: object) -> str:
    return clean_text(value, 120) if isinstance(value, str) else ""


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _state(state: object) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """컨테이너 상태(state·lastState)에서 대기·종료 정보를 꺼냅니다."""
    if not isinstance(state, dict):
        return None, None
    waiting = state.get("waiting")
    terminated = state.get("terminated")
    w = (
        {"reason": _name(waiting.get("reason")), "message": _text(waiting.get("message"))}
        if isinstance(waiting, dict)
        else None
    )
    t = (
        {
            "reason": _name(terminated.get("reason")),
            "exit_code": terminated.get("exitCode")
            if isinstance(terminated.get("exitCode"), int)
            else None,
            "finished_at": terminated.get("finishedAt")
            if _parse_time(terminated.get("finishedAt"))
            else None,
        }
        if isinstance(terminated, dict)
        else None
    )
    return w, t


def summarize_pod(pod: Mapping[str, Any]) -> dict[str, Any]:
    """Pod 객체에서 판정에 필요한 필드만 남깁니다."""
    meta, spec, status = (
        _dict(pod.get("metadata")),
        _dict(pod.get("spec")),
        _dict(pod.get("status")),
    )
    scheduled = None
    for cond in status.get("conditions") or ():
        if isinstance(cond, dict) and cond.get("type") == "PodScheduled":
            scheduled = {
                "status": _name(cond.get("status")),
                "reason": _name(cond.get("reason")),
                "message": _text(cond.get("message")),
            }
    containers = []
    for key, init in (("initContainerStatuses", True), ("containerStatuses", False)):
        for cs in status.get(key) or ():
            if not isinstance(cs, dict):
                continue
            waiting, terminated = _state(cs.get("state"))
            _, last = _state(cs.get("lastState"))
            restarts = cs.get("restartCount")
            containers.append(
                {
                    "name": _name(cs.get("name")),
                    "init": init,
                    "ready": bool(cs.get("ready")),
                    "restart_count": restarts if isinstance(restarts, int) else 0,
                    "waiting": waiting,
                    "terminated": terminated,
                    "last_terminated": last,
                }
            )
    owner = next(
        (
            {"kind": _name(o.get("kind")), "name": _name(o.get("name"))}
            for o in meta.get("ownerReferences") or ()
            if isinstance(o, dict) and o.get("controller")
        ),
        None,
    )
    return {
        "namespace": _name(meta.get("namespace")),
        "pod": _name(meta.get("name")),
        "owner": owner,
        "node": _name(spec.get("nodeName")) or None,
        "phase": _name(status.get("phase")) or "Unknown",
        "reason": _name(status.get("reason")) or None,
        "scheduled": scheduled,
        "containers": containers,
    }


def summarize_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """core/v1 Event에서 판정에 필요한 필드만 남깁니다."""
    obj, meta = _dict(event.get("involvedObject")), _dict(event.get("metadata"))
    series, source = _dict(event.get("series")), _dict(event.get("source"))
    last_seen = next(
        (
            v
            for v in (
                series.get("lastObservedTime"),
                event.get("lastTimestamp"),
                event.get("eventTime"),
                event.get("firstTimestamp"),
                meta.get("creationTimestamp"),
            )
            if _parse_time(v)
        ),
        None,
    )
    count = series.get("count") if isinstance(series.get("count"), int) else event.get("count")
    return {
        "namespace": _name(obj.get("namespace") or meta.get("namespace")),
        "kind": _name(obj.get("kind")),
        "name": _name(obj.get("name")),
        "field_path": _name(obj.get("fieldPath")) or None,
        "host": _name(source.get("host") or event.get("reportingInstance")) or None,
        "reason": _name(event.get("reason")) or "(사유 없음)",
        "message": _text(event.get("message")),
        "count": count if isinstance(count, int) and count > 0 else 1,
        "last_seen": last_seen,
    }


def owned_by(owner: object, workload: str) -> bool:
    """Pod의 컨트롤러(ownerReferences)가 워크로드 이름에 해당하는지.

    Deployment의 Pod는 ReplicaSet `<이름>-<해시>`가 소유하므로 해시 형식까지 확인합니다
    (이름 접두어만 보면 `frontend`가 `frontend-proxy`의 Pod까지 포함하게 됨). CronJob의 Pod는
    Job `<이름>-<숫자>`가 소유합니다."""
    info = _dict(owner)
    kind, name = info.get("kind"), str(info.get("name") or "")
    if kind in ("StatefulSet", "DaemonSet", "Job") and name == workload:
        return True
    w = re.escape(workload)
    if kind == "ReplicaSet":
        return re.fullmatch(rf"{w}-[a-z0-9]{{1,10}}", name) is not None
    return kind == "Job" and re.fullmatch(rf"{w}-\d+", name) is not None


_POD_OF_WORKLOAD = (
    r"-[a-z0-9]{1,10}-[a-z0-9]{5}",  # Deployment (ReplicaSet 해시 + 임의 접미어)
    r"-\d+",  # StatefulSet
    r"-[a-z0-9]{5}",  # DaemonSet·Job
    r"-\d+-[a-z0-9]{5}",  # CronJob
)


def pod_name_of_workload(pod: str, workload: str) -> bool:
    """Pod 이름만으로 워크로드 소속을 추정합니다 (Pod 목록이 없을 때의 이벤트 대상 확인용)."""
    w = re.escape(workload)
    return any(re.fullmatch(w + suffix, pod) for suffix in _POD_OF_WORKLOAD)


def _pod_matches(row: Mapping[str, Any], targets: Mapping[TargetKind, str]) -> bool:
    pod = str(row.get("pod", ""))
    if TargetKind.POD in targets and pod != targets[TargetKind.POD]:
        return False
    if TargetKind.NODE in targets and row.get("node") != targets[TargetKind.NODE]:
        return False
    workload = targets.get(TargetKind.WORKLOAD)
    if workload is not None and not owned_by(row.get("owner"), workload):
        return False
    container = targets.get(TargetKind.CONTAINER)
    return container is None or any(c.get("name") == container for c in row["containers"])


_WORKLOAD_KINDS = frozenset({"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"})


def _event_matches(
    row: Mapping[str, Any],
    targets: Mapping[TargetKind, str],
    pod_names: frozenset[str] | None,
) -> bool:
    name = str(row.get("name", ""))
    kind = row.get("kind")
    if TargetKind.POD in targets and name != targets[TargetKind.POD]:
        return False
    node = targets.get(TargetKind.NODE)
    if node is not None and not (
        (row.get("kind") == "Node" and name == node) or row.get("host") == node
    ):
        return False
    workload = targets.get(TargetKind.WORKLOAD)
    if workload is not None:
        if kind in _WORKLOAD_KINDS:
            matched = name == workload
        elif kind == "ReplicaSet":
            matched = owned_by({"kind": "ReplicaSet", "name": name}, workload)
        elif kind == "Pod":
            matched = (
                name in pod_names if pod_names is not None else pod_name_of_workload(name, workload)
            )
        else:
            matched = False
        if not matched:
            return False
    container = targets.get(TargetKind.CONTAINER)
    return container is None or f"{{{container}}}" in str(row.get("field_path") or "")


class KubernetesQueryTool:
    def __init__(
        self,
        client: KubernetesClient,
        *,
        agent: AgentName,
        budget: ToolBudget,
        timeout_seconds: float,
        review_namespace: str = "default",
    ) -> None:
        if agent is not AgentName.KUBERNETES:
            raise ToolPermissionError(
                f"{agent.value} 에이전트는 Kubernetes API 조회 도구를 사용할 수 없습니다"
            )
        self._client = client
        self._budget = budget
        self._timeout = timeout_seconds
        self._review_ns = review_namespace
        self._permission: PermissionOutcome | None = None
        self._permission_key: tuple[str, ...] = ()

    def _result(
        self,
        evidence_id: str,
        query: str,
        window: TimeRange | None,
        status: ToolStatus,
        *,
        data: object = None,
        error: str | None = None,
    ) -> ToolResult:
        return ToolResult(
            evidence_id=evidence_id,
            source=DataSourceKind.KUBERNETES,
            query=query,
            time_range=window,
            status=status,
            data=data,
            # API 응답은 조회 시점의 현재 상태입니다.
            freshness_seconds=0.0 if status in (ToolStatus.OK, ToolStatus.EMPTY) else None,
            fetched_at=utc_now(),
            error=error,
        )

    async def _call(
        self,
        evidence_id: str,
        query: str,
        window: TimeRange | None,
        fn: Callable[[], Awaitable[T]],
    ) -> T | ToolResult:
        """예산·제한 시간·오류 처리를 적용해 호출합니다. 실패하면 오류 상태의 근거."""
        if not self._budget.take():
            return self._result(
                evidence_id,
                query,
                window,
                ToolStatus.ERROR,
                error="요청당 조회 호출 상한(analysis.max_tool_calls)에 도달해 조회하지 않음",
            )
        try:
            return await asyncio.wait_for(fn(), timeout=self._timeout)
        except TimeoutError:
            return self._result(
                evidence_id,
                query,
                window,
                ToolStatus.TIMEOUT,
                error=f"조회 제한 시간({self._timeout:.0f}초) 초과",
            )
        except DataSourceError as exc:
            status = ToolStatus.TIMEOUT if exc.code == "timeout" else ToolStatus.ERROR
            return self._result(
                evidence_id, query, window, status, error=f"{exc.code}: {exc.message}"
            )

    async def permissions(
        self, targets: Mapping[TargetKind, str] | None = None
    ) -> PermissionOutcome:
        """계정 권한을 점검합니다 (요청당 한 번).

        기본 네임스페이스와, 대상 네임스페이스가 있으면 그 네임스페이스의 권한을 함께 봅니다
        (ClusterRole 권한은 어느 쪽에서나 보임)."""
        namespaces = [self._review_ns]
        target_ns = (targets or {}).get(TargetKind.NAMESPACE)
        if target_ns and is_k8s_name(target_ns) and target_ns not in namespaces:
            namespaces.append(target_ns)
        key = tuple(namespaces)
        if self._permission is not None and self._permission_key == key:
            return self._permission
        evidence_id = "k8s_api.permissions@current"
        query = f"POST SelfSubjectRulesReview (namespace={', '.join(namespaces)})"
        reviews: list[RulesReview] = []
        for ns in namespaces:
            got = await self._call(evidence_id, query, None, partial(self._client.self_rules, ns))
            if isinstance(got, ToolResult):
                self._permission, self._permission_key = PermissionOutcome(got, None), key
                return self._permission
            reviews.append(got)
        merged = merge_reviews(reviews)
        report = assess_rules(merged)
        # 근거에는 확인한 권한 규칙(그룹·리소스·동사)을 남깁니다. 판정 결과는 `report`로 전달합니다.
        data = summarize_rules(merged.resource_rules)
        self._permission = PermissionOutcome(
            self._result(evidence_id, query, None, ToolStatus.OK, data=data), report
        )
        self._permission_key = key
        return self._permission

    def _unsupported(self, targets: Mapping[TargetKind, str]) -> list[TargetKind]:
        return [k for k in targets if k not in SUPPORTED_TARGETS]

    def _namespace(self, targets: Mapping[TargetKind, str]) -> str | None:
        ns = targets.get(TargetKind.NAMESPACE)
        if ns is not None and not is_k8s_name(ns):
            raise ValueError(f"네임스페이스 이름 형식이 올바르지 않습니다: {ns!r}")
        return ns

    async def _list(
        self,
        evidence_id: str,
        resource: str,
        targets: Mapping[TargetKind, str],
        window: TimeRange | None,
        field_selector: str | None,
    ) -> tuple[ListResult | None, QueryOutcome | None, str]:
        unsupported = self._unsupported(targets)
        try:
            namespace = self._namespace(targets)
            query = "GET " + self._client.list_path(resource, namespace)
        except ValueError as exc:
            result = self._result(evidence_id, "", window, ToolStatus.ERROR, error=str(exc))
            return None, QueryOutcome(result=result, skipped=True), ""
        if field_selector:
            query += f"?fieldSelector={field_selector}"
        if unsupported:
            result = self._result(evidence_id, query, window, ToolStatus.EMPTY)
            return None, QueryOutcome(result, unsupported_targets=unsupported, skipped=True), query
        got = await self._call(
            evidence_id,
            query,
            window,
            lambda: self._client.list(resource, namespace=namespace, field_selector=field_selector),
        )
        if isinstance(got, ToolResult):
            return None, QueryOutcome(result=got), query
        return got, None, query

    async def pods(self, ctx: AnalysisContext, targets: Mapping[TargetKind, str]) -> QueryOutcome:
        """현재 Pod 상태 요약 (대상 필터 적용). 근거 ID `k8s_api.pods@current`."""
        evidence_id = "k8s_api.pods@current"
        listed, early, query = await self._list(evidence_id, "pods", targets, None, None)
        if early is not None:
            return early
        assert listed is not None
        rows = [summarize_pod(p) for p in listed.items]
        rows = [r for r in rows if _pod_matches(r, targets)]
        status = self._status(rows, listed.truncated)
        return QueryOutcome(result=self._result(evidence_id, query, None, status, data=rows))

    async def warning_events(
        self,
        ctx: AnalysisContext,
        targets: Mapping[TargetKind, str],
        pod_names: frozenset[str] | None = None,
    ) -> QueryOutcome:
        """분석 구간에 마지막으로 관측된 Warning 이벤트. 근거 ID `k8s_api.events@window`.

        `pod_names`는 대상에 해당하는 Pod 이름(Pod 목록을 전부 확인한 경우)입니다. 워크로드 대상의
        Pod 이벤트를 고를 때 쓰며, 없으면 Pod 이름 형식으로 추정합니다."""
        evidence_id = "k8s_api.events@window"
        window = ctx.time_range
        listed, early, query = await self._list(
            evidence_id, "events", targets, window, "type=Warning"
        )
        if early is not None:
            return early
        assert listed is not None
        rows = []
        for event in listed.items:
            row = summarize_event(event)
            seen = _parse_time(row["last_seen"])
            if seen is None or not (window.start <= seen <= window.end):
                continue
            if _event_matches(row, targets, pod_names):
                rows.append(row)
        status = self._status(rows, listed.truncated)
        return QueryOutcome(result=self._result(evidence_id, query, window, status, data=rows))

    @staticmethod
    def _status(rows: list[dict[str, Any]], truncated: bool) -> ToolStatus:
        if truncated:
            return ToolStatus.TRUNCATED
        return ToolStatus.OK if rows else ToolStatus.EMPTY
