"""선행 Service Agent 결과에서 분야 간 교차 확인에 쓸 정보를 뽑습니다 (모델 없이).

- 이상 서비스: Service Agent의 경고·심각 사실(Finding)의 대상 서비스와 이상 종류
- 핵심 판정 여부: Service Agent가 오류율·응답 지연을 실제로 판정했는지("이상 없음"을 말할 수 있는지)
- DB 호출 종류: DB 호출 span 지연 결과(Service·DB Agent)의 `service`·`db_system_name` 라벨

Service Agent가 실패했거나 실행되지 않았으면 빈 결과를 돌려줍니다(이상 없음으로 보지 않도록
호출하는 쪽이 상태와 `has_core_judgement`를 함께 확인).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from infra_agent.schemas import (
    AgentName,
    AgentResult,
    AgentStatus,
    Finding,
    FindingKind,
    JudgementBasis,
    Severity,
    TargetKind,
    TargetRef,
    ToolStatus,
)
from infra_agent.tools import value_rows

ASPECTS: dict[str, str] = {
    "service.error_ratio": "오류율",
    "service.latency_p95": "응답 지연",
    "service.dependency_latency_p95": "호출받는 지연",
    "service.dependency_failed_rate": "호출받는 실패",
    "service.db_span_latency_p95": "DB 호출 지연",
}
"""Service 근거 항목(카탈로그 키) → 이상 종류 표시."""

CORE_KEYS = frozenset({"service.error_ratio", "service.latency_p95"})
"""이 항목의 판정 결과가 있어야 "서비스 이상 대상 없음"이라고 말할 수 있습니다."""

DB_SPAN_KEYS = frozenset({"service.db_span_latency_p95", "db.span_latency_p95"})
"""DB 호출 span 지연 항목 (Service Agent와 DB Agent가 같은 라벨로 조회)."""

_SEVERITY_RANK = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}


@dataclass(frozen=True)
class ServiceIssue:
    """Service Agent가 이상으로 판정한 서비스 하나."""

    service: str
    aspects: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    severity: Severity

    @property
    def label(self) -> str:
        return f"{self.service}({', '.join(self.aspects)})"


def item_key(evidence_id: str) -> str:
    """근거 ID(`key@mode`)의 카탈로그 키."""
    return evidence_id.split("@", 1)[0]


def _usable(results: Iterable[AgentResult], agents: frozenset[AgentName]) -> list[AgentResult]:
    return [
        r
        for r in results
        if r.agent in agents and r.status in (AgentStatus.SUCCESS, AgentStatus.PARTIAL)
    ]


_SERVICE_ONLY = frozenset({AgentName.SERVICE})


def issue_service(target: TargetRef) -> str | None:
    """Service 대상에서 이상 서비스 이름을 고릅니다.

    호출 경로(client → server)는 호출받는 쪽(server), DB 호출은 호출한 서비스입니다.
    """
    if target.kind is not TargetKind.SERVICE:
        return None
    labels = target.labels
    name = labels.get("server") or labels.get("service") or labels.get("service_name")
    if name:
        return name
    # 라벨 없이 이름만 있는 서비스 대상 (오류율·지연 판정)
    if "→" in target.name or " " in target.name or target.name in ("?", ""):
        return None
    return target.name


def _aspect(finding: Finding) -> str:
    for evidence_id in finding.evidence_ids:
        aspect = ASPECTS.get(item_key(evidence_id))
        if aspect:
            return aspect
    return "이상"


def has_core_judgement(results: Iterable[AgentResult]) -> bool:
    """Service Agent가 오류율·응답 지연 중 하나라도 판정한 사실이 있는지."""
    return any(
        f.kind is FindingKind.FACT
        and f.basis in (JudgementBasis.THRESHOLD, JudgementBasis.BASELINE)
        and any(item_key(i) in CORE_KEYS for i in f.evidence_ids)
        for r in _usable(results, _SERVICE_ONLY)
        for f in r.findings
    )


def service_issues(results: Iterable[AgentResult]) -> dict[str, ServiceIssue]:
    """Service Agent의 경고·심각 사실에서 이상 서비스를 모읍니다.

    순서는 심각도(심각 먼저) → 이름이며, 상위 N개만 확인하는 쪽이 심각한 대상을 먼저 봅니다.
    """
    aspects: dict[str, list[str]] = {}
    evidence: dict[str, list[str]] = {}
    severity: dict[str, Severity] = {}
    for result in _usable(results, _SERVICE_ONLY):
        for finding in result.findings:
            if finding.kind is not FindingKind.FACT or finding.severity is Severity.INFO:
                continue
            for target in finding.targets:
                name = issue_service(target)
                if name is None:
                    continue
                aspect = _aspect(finding)
                if aspect not in aspects.setdefault(name, []):
                    aspects[name].append(aspect)
                ids = evidence.setdefault(name, [])
                ids.extend(i for i in finding.evidence_ids if i not in ids)
                if finding.severity is Severity.CRITICAL or name not in severity:
                    severity[name] = finding.severity
    order = sorted(aspects, key=lambda n: (_SEVERITY_RANK[severity[n]], n))
    return {
        name: ServiceIssue(
            service=name,
            aspects=tuple(aspects[name]),
            evidence_ids=tuple(evidence[name]),
            severity=severity[name],
        )
        for name in order
    }


def service_db_calls(
    results: Iterable[AgentResult], stale_after_seconds: float
) -> dict[str, frozenset[str]] | None:
    """서비스별 DB 호출 종류(`db_system_name`, 소문자). Service·DB Agent의 DB 호출 span 결과 기준.

    결과가 있고 최신인 DB 호출 span 조회가 없으면 None(호출 여부를 모름)을 돌려줍니다.
    빈 결과나 오래된 결과로 "DB 호출 없음"이라고 판단하지 않습니다.
    """
    calls: dict[str, set[str]] = {}
    found = False
    for result in _usable(results, frozenset({AgentName.SERVICE, AgentName.DB})):
        for e in result.evidence:
            if (
                item_key(e.evidence_id) not in DB_SPAN_KEYS
                or e.status is not ToolStatus.OK
                or e.freshness_seconds is None
                or e.freshness_seconds > stale_after_seconds
            ):
                continue
            found = True
            for labels, _ in value_rows(e):
                service, system = labels.get("service"), labels.get("db_system_name")
                if service and system:
                    calls.setdefault(service, set()).add(system.lower())
    if not found:
        return None
    return {k: frozenset(v) for k, v in calls.items()}
