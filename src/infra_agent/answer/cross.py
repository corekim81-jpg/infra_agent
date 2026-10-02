"""분야 간 교차 확인 (Coordinator, 모델 없이).

Service Agent가 이상으로 판정한 서비스를 기준으로 다른 분야(Network·DB·Kubernetes·Server)의
경고·심각 사실을 연결합니다.

연결 기준
- 같은 대상: 이상 사실의 대상 라벨(서비스·워크로드·컨테이너 이름)이 이상 서비스 이름과 같음
  → 원인 후보 신뢰도 medium
- 간접 연결: 이상 사실이 DB·캐시 전체 지표(PostgreSQL·Valkey)이고, 이상 서비스가 그 DB 종류를
  호출함(Service Agent의 DB 호출 span 기준) → 신뢰도 low

연결은 같은 분석 구간 안에서 함께 관측됐다는 뜻일 뿐이므로 `basis=correlation`인 원인
후보(추정)로만 표시하고 인과를 단정하지 않습니다. 확인하지 못한 분야와 이상이 없던 분야를
구분해 적습니다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from infra_agent.agents.upstream import (
    ServiceIssue,
    has_core_judgement,
    item_key,
    service_db_calls,
    service_issues,
)
from infra_agent.schemas import (
    AgentName,
    AgentResult,
    AgentStatus,
    Confidence,
    Finding,
    FindingKind,
    JudgementBasis,
    Severity,
    TargetKind,
    TargetRef,
)

DOMAIN_LABELS = {
    AgentName.SERVER: "서버 자원",
    AgentName.KUBERNETES: "Kubernetes",
    AgentName.NETWORK: "네트워크",
    AgentName.DB: "DB·캐시",
}
_ORDER = (AgentName.NETWORK, AgentName.DB, AgentName.KUBERNETES, AgentName.SERVER)

NAME_LABELS = (
    "service",
    "service_name",
    "server",
    "source_workload",
    "destination_workload",
    "k8s_container_name",
    "k8s_deployment_name",
    "k8s_statefulset_name",
    "k8s_daemonset_name",
)
"""이상 서비스 이름과 비교할 대상 라벨 (서비스 이름 = 워크로드·컨테이너 이름으로 가정)."""

DB_SYSTEM_PREFIXES: tuple[tuple[str, frozenset[str]], ...] = (
    ("db.pg_", frozenset({"postgresql"})),
    ("cache.valkey_", frozenset({"redis", "valkey"})),
)
"""DB·캐시 전체 지표(카탈로그 키 접두어) → DB 호출 span의 `db_system_name` 값."""

CROSS_NOTE = (
    "분야 간 교차 확인은 같은 분석 구간 안에서 함께 관측된 이상을 이름 일치(서비스·워크로드·"
    "컨테이너 이름)와 DB 호출 관계(DB 호출 span의 DB 종류)로 연결한 것이며, 인과를 확인한 것이 "
    "아닙니다. 시간 기준은 분야마다 다릅니다: Network 집중 확인은 서비스 이상 최고 시점, "
    "DB·Kubernetes·서버는 현재 값 또는 분석 구간 집계."
)
DEFAULT_STALE_SECONDS = 300.0
"""DB 호출 span 결과 최신성 기준 기본값 (`analysis.stale_after_seconds` 기본값과 같음)."""
MAX_STATEMENT = 120
MAX_SHOWN = 2


@dataclass(frozen=True)
class CrossCheck:
    summary: str = ""
    lines: tuple[str, ...] = ()
    correlations: tuple[Finding, ...] = ()
    limitations: tuple[str, ...] = ()
    scope_notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Link:
    finding: Finding
    relation: str
    direct: bool


def _short(text: str) -> str:
    return text if len(text) <= MAX_STATEMENT else text[: MAX_STATEMENT - 1] + "…"


def _names(finding: Finding) -> set[str]:
    names: set[str] = set()
    for target in finding.targets:
        names.update(target.labels[k] for k in NAME_LABELS if target.labels.get(k))
        if target.kind is TargetKind.SERVICE and not target.labels:
            names.add(target.name)
    return names


def _systems(finding: Finding) -> frozenset[str]:
    out: set[str] = set()
    for evidence_id in finding.evidence_ids:
        key = item_key(evidence_id)
        for prefix, systems in DB_SYSTEM_PREFIXES:
            if key.startswith(prefix):
                out.update(systems)
    return frozenset(out)


def _links(
    finding: Finding,
    issue: ServiceIssue,
    calls: dict[str, frozenset[str]] | None,
) -> _Link | None:
    if issue.service in _names(finding):
        return _Link(finding, "같은 대상", direct=True)
    systems = _systems(finding)
    called = (calls or {}).get(issue.service, frozenset()) & systems
    if called:
        return _Link(
            finding,
            f"{issue.service}에서 {', '.join(sorted(called))} 호출, DB 호출 span 기준",
            direct=False,
        )
    return None


def _correlation(issue: ServiceIssue, domain: str, links: Sequence[_Link]) -> Finding:
    shown = "; ".join(f"{_short(x.finding.statement)} ({x.relation})" for x in links[:MAX_SHOWN])
    more = f" 외 {len(links) - MAX_SHOWN}건" if len(links) > MAX_SHOWN else ""
    ids: list[str] = list(issue.evidence_ids)
    for x in links:
        ids.extend(i for i in x.finding.evidence_ids if i not in ids)
    severity = (
        Severity.CRITICAL
        if issue.severity is Severity.CRITICAL
        or any(x.finding.severity is Severity.CRITICAL for x in links)
        else Severity.WARNING
    )
    return Finding(
        kind=FindingKind.HYPOTHESIS,
        statement=(
            f"서비스 이상 {issue.label}: 같은 분석 구간에 {domain} 이상이 함께 관측됨 — "
            f"{shown}{more}. 시간상 함께 나타난 것으로 인과는 확인되지 않음"
        ),
        severity=severity,
        targets=(
            TargetRef(
                kind=TargetKind.SERVICE, name=issue.service, labels={"service": issue.service}
            ),
        ),
        evidence_ids=tuple(ids),
        basis=JudgementBasis.CORRELATION,
        confidence=Confidence.MEDIUM if any(x.direct for x in links) else Confidence.LOW,
    )


def _addresses(
    finding: Finding, issue: ServiceIssue, calls: dict[str, frozenset[str]] | None
) -> bool:
    """이 사실(정상 포함)이 이상 서비스를 대상으로 하거나 그 서비스가 호출하는 DB를 다루는지."""
    if issue.service in _names(finding):
        return True
    return bool((calls or {}).get(issue.service, frozenset()) & _systems(finding))


def _issue_line(issue: ServiceIssue, groups: dict[str, list[str]]) -> str:
    parts = [f"{title} — {', '.join(domains)}" for title, domains in groups.items() if domains]
    text = f"{issue.label}: " + "; ".join(parts)
    if not groups[LINKED]:
        text += ". 이 결과만으로는 원인 분야를 가리지 못함"
    return text


LINKED = "연결된 이상"
CLEAR = "이 서비스 관련 결과에서 기준을 넘는 이상 없음"
NO_DB_CALL = "DB 호출 span에 이 서비스의 DB 호출이 없어 DB 이상과 연결하지 않음"
UNADDRESSED = "이 서비스와 연결할 수 있는 결과가 없어 판단하지 못함"


def cross_check(
    results: Sequence[AgentResult], stale_after_seconds: float = DEFAULT_STALE_SECONDS
) -> CrossCheck:
    """Service 결과와 다른 분야 결과가 함께 있을 때만 교차 확인합니다.

    `stale_after_seconds`: DB 호출 span 결과를 최신으로 볼 기준(`analysis.stale_after_seconds`).
    """
    service = [r for r in results if r.agent is AgentName.SERVICE]
    others = sorted(
        (r for r in results if r.agent in DOMAIN_LABELS), key=lambda r: _ORDER.index(r.agent)
    )
    if not service or not others:
        return CrossCheck()
    if all(r.status in (AgentStatus.FAILED, AgentStatus.SKIPPED) for r in service):
        return CrossCheck(
            summary=(
                "분야 간 교차 확인: 서비스 분석을 완료하지 못해 분야 간 연결을 판단하지 않았습니다."
            ),
            lines=("서비스: 분석을 완료하지 못해 이상 대상을 정하지 못함",),
        )
    issues = service_issues(service)
    if not issues and not has_core_judgement(service):
        # 오류율·응답 지연 판정 결과가 없으면 "이상 대상 없음"이 아니라 판단 불가입니다.
        return CrossCheck(
            summary=(
                "분야 간 교차 확인: 서비스 오류율·응답 지연 판정 결과가 없어 분야 간 연결을 "
                "판단하지 않았습니다."
            ),
            lines=("서비스: 오류율·응답 지연 판정 결과가 없어 이상 대상을 정하지 못함",),
        )
    calls = service_db_calls(results, stale_after_seconds)
    lines: list[str] = []
    limitations: list[str] = []
    if issues:
        lines.append("서비스 이상 대상: " + ", ".join(i.label for i in issues.values()))
    else:
        lines.append("서비스 이상 대상: 없음 (Service Agent 오류율·응답 지연 판정 기준)")
    if any(r.status is AgentStatus.PARTIAL for r in service):
        lines[-1] += " — 서비스 분석 일부를 완료하지 못해 빠진 대상이 있을 수 있음"
    if calls is None and issues:
        limitations.append(
            "최신 DB 호출 span 결과가 없어 DB·캐시 전체 지표 이상과 서비스의 간접 연결은 확인하지 "
            "않음"
        )

    correlations: list[Finding] = []
    groups: dict[str, dict[str, list[str]]] = {
        name: {LINKED: [], CLEAR: [], NO_DB_CALL: [], UNADDRESSED: []} for name in issues
    }
    checked: list[str] = []
    unchecked: list[str] = []
    clear_domains: list[str] = []
    anomaly_counts: dict[str, int] = {}
    for result in others:
        domain = DOMAIN_LABELS[result.agent]
        is_partial = result.status is AgentStatus.PARTIAL
        partial = " (일부 조회를 완료하지 못함)" if is_partial else ""
        tag = f"{domain}(일부 조회 미완료)" if is_partial else domain
        if result.status in (AgentStatus.FAILED, AgentStatus.SKIPPED):
            unchecked.append(domain)
            lines.append(f"{domain}: 분석을 완료하지 못해 확인하지 못함")
            continue
        facts = [f for f in result.findings if f.kind is FindingKind.FACT]
        anomalies = [f for f in facts if f.severity is not Severity.INFO]
        if not facts:
            unchecked.append(domain)
            lines.append(f"{domain}: 판단에 사용할 결과가 없어 확인하지 못함{partial}")
            continue
        checked.append(domain)
        used: set[int] = set()
        for name, issue in issues.items():
            links = [x for f in anomalies if (x := _links(f, issue, calls)) is not None]
            if links:
                groups[name][LINKED].append(tag)
                used.update(id(x.finding) for x in links)
                correlations.append(_correlation(issue, domain, links))
            elif any(_addresses(f, issue, calls) for f in facts):
                groups[name][CLEAR].append(tag)
            elif result.agent is AgentName.DB and calls is not None and name not in calls:
                groups[name][NO_DB_CALL].append(tag)
            else:
                groups[name][UNADDRESSED].append(tag)
        if not anomalies:
            clear_domains.append(tag)
            lines.append(f"{domain}: 확인한 항목에서 기준을 넘는 이상 없음{partial}")
            continue
        anomaly_counts[domain] = len(anomalies)
        text = f"{domain}: 이상 {len(anomalies)}건"
        if issues:
            unlinked = [f for f in anomalies if id(f) not in used]
            text += f", 그중 서비스 이상 대상과 연결 {len(anomalies) - len(unlinked)}건"
            if unlinked:
                shown = "; ".join(_short(f.statement) for f in unlinked[:MAX_SHOWN])
                more = f" 외 {len(unlinked) - MAX_SHOWN}건" if len(unlinked) > MAX_SHOWN else ""
                text += f" (연결되지 않은 이상: {shown}{more})"
        lines.append(text + partial)

    if checked:
        lines.extend(_issue_line(issue, groups[name]) for name, issue in issues.items())

    if not issues:
        # 원인을 묻는 질문에 직접 답하도록, 전제(서비스 이상)가 없다는 것과 분야별 결과를 함께 적음
        summary = (
            "분야 간 교차 확인: 서비스 오류율·응답 지연 이상이 확인되지 않아 원인 분야를 가리지 "
            "않았습니다."
        )
        if clear_domains:
            summary += f" {', '.join(clear_domains)}도 확인한 항목에서 기준을 넘는 이상이 없습니다."
        if anomaly_counts:
            found = ", ".join(f"{d} {n}건" for d, n in anomaly_counts.items())
            summary += f" 다른 분야 이상({found})은 서비스 영향과 연결하지 않았습니다."
        if unchecked:
            summary += f" {', '.join(unchecked)} 분야는 확인하지 못했습니다."
    else:

        def has(domain: str, kind: str) -> int:
            return sum(1 for g in groups.values() if any(t.startswith(domain) for t in g[kind]))

        # 서비스 단위로 연결 여부를 판단할 수 있었던 분야만 연결 수를 셉니다.
        judged = [d for d in checked if has(d, LINKED) or has(d, CLEAR) or has(d, NO_DB_CALL)]
        counts = [f"{d} 이상과 연결된 대상 {has(d, LINKED)}개" for d in judged]
        summary = f"분야 간 교차 확인: 서비스 이상 대상 {len(issues)}개 중 " + (
            ", ".join(counts) if counts else "연결을 판단한 분야 없음"
        )
        summary += " (같은 구간 동시 발생 기준, 인과 미확인)."
        undecided = [d for d in checked if d not in judged]
        if undecided:
            summary += (
                f" {', '.join(undecided)} 분야는 서비스 단위로 연결할 수 있는 결과가 없어 연결 "
                "여부를 판단하지 못했습니다."
            )
        if unchecked:
            summary += f" {', '.join(unchecked)} 분야는 확인하지 못했습니다."
    return CrossCheck(
        summary=summary,
        lines=tuple(lines),
        correlations=tuple(correlations),
        limitations=tuple(limitations),
        scope_notes=(CROSS_NOTE,),
    )
