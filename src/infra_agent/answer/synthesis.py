"""에이전트 결과 종합 (모델 없이).

- 근거 ID가 실제 조회 결과를 가리키는 Finding만 사용합니다(스키마에서도 강제).
- 실패·부분 실패 에이전트는 확인하지 못한 영역으로 표시합니다.
"""

from __future__ import annotations

from collections.abc import Sequence

from infra_agent.agents.server import LIMIT_NEAR_CHECK
from infra_agent.orchestration.rules import Interpretation
from infra_agent.schemas import (
    AgentName,
    AgentResult,
    AgentStatus,
    EvidenceSummary,
    FinalAnswer,
    Finding,
    FindingKind,
    Intent,
    JudgementBasis,
    Severity,
    ToolStatus,
)

DOMAIN_NAMES = {
    "server": "서버 자원",
    "kubernetes": "Kubernetes 상태·이벤트",
    "network": "네트워크",
    "db": "DB·캐시",
    "service": "서비스·로그·트레이스",
}

_SEVERITY_ORDER = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}


def _severity_counts(findings: Sequence[Finding]) -> str:
    """경고·심각 건수 문구. 없으면 빈 문자열."""
    critical = sum(1 for f in findings if f.severity is Severity.CRITICAL)
    warning = sum(1 for f in findings if f.severity is Severity.WARNING)
    if not critical and not warning:
        return ""
    parts = []
    if critical:
        parts.append(f"심각 {critical}건")
    if warning:
        parts.append(f"경고 {warning}건")
    return f"{critical + warning}건({', '.join(parts)})"


def _compares(interp: Interpretation, results: Sequence[AgentResult]) -> bool:
    """직전 구간 비교 결과를 먼저 답할지. 비교는 현재 Server Agent만 수행합니다."""
    return interp.intent in (Intent.COMPARE, Intent.ANOMALY) and any(
        r.agent is AgentName.SERVER for r in results
    )


def _summary(
    facts: Sequence[Finding], results: Sequence[AgentResult], interp: Interpretation
) -> str:
    if results and all(r.status in (AgentStatus.FAILED, AgentStatus.SKIPPED) for r in results):
        return "조회에 실패해 상태를 판단하지 못했습니다. 한계와 오류 내용을 확인하세요."
    if not results:
        return "요청한 분야를 분석할 수 있는 에이전트가 아직 없어 답할 수 없습니다."
    partial = any(r.status is not AgentStatus.SUCCESS for r in results)
    if _compares(interp, results):
        # 비교·증가 질문에는 증가 판정 결과를 먼저 답합니다.
        changes = [f for f in facts if f.basis is JudgementBasis.BASELINE]
        increased = [f for f in changes if f.severity is not Severity.INFO]
        if increased:
            text = (
                f"직전 같은 길이 구간 대비 기준 이상 증가한 대상 {len(increased)}건을 확인했습니다."
            )
        elif changes:
            text = "직전 같은 길이 구간 대비 기준 이상 증가한 대상은 없습니다."
        else:
            text = "직전 구간과 비교할 수 있는 결과가 없어 증가 여부를 판단하지 못했습니다."
        others = [f for f in facts if f.basis is not JudgementBasis.BASELINE]
        counts = _severity_counts(others)
        if counts:
            text += f" 별도로 현재 값이 기준을 넘는 항목 {counts}이 있습니다."
    else:
        counts = _severity_counts(facts)
        if counts:
            text = f"기준을 넘는 이상 징후 {counts}을 확인했습니다."
        elif any(f.severity is Severity.INFO for f in facts):
            text = "확인한 항목에서는 기준을 넘는 이상 징후가 없습니다."
        else:
            text = "판단에 사용할 수 있는 결과가 없어 이상 여부를 판단하지 못했습니다."
    if partial:
        text += " 일부 조회를 완료하지 못해 결과가 불완전합니다."
    if interp.unsupported_domains:
        names = ", ".join(DOMAIN_NAMES.get(d, d) for d in sorted(interp.unsupported_domains))
        text += f" ({names} 분야는 아직 분석하지 않았습니다.)"
    return text


def synthesize(
    request_id: str, interp: Interpretation, results: Sequence[AgentResult]
) -> FinalAnswer:
    change_first = _compares(interp, results)
    facts = sorted(
        (f for r in results for f in r.findings if f.kind is FindingKind.FACT),
        key=lambda f: (
            0 if change_first and f.basis is JudgementBasis.BASELINE else 1,
            _SEVERITY_ORDER[f.severity],
        ),
    )
    hypotheses = [f for r in results for f in r.findings if f.kind is FindingKind.HYPOTHESIS]
    evidence = []
    for r in results:
        for e in r.evidence:
            key_values: dict[str, object] = {
                "id": e.evidence_id,
                "status": e.status.value,
                "series": len(e.data) if isinstance(e.data, list) else 0,
            }
            if e.freshness_seconds is not None:
                key_values["freshness_seconds"] = round(e.freshness_seconds, 1)
            if e.status in (ToolStatus.ERROR, ToolStatus.TIMEOUT) and e.error:
                key_values["error"] = e.error
            evidence.append(
                EvidenceSummary(
                    source=e.source, query=e.query, time_range=e.time_range, key_values=key_values
                )
            )
    limitations = list(dict.fromkeys(x for r in results for x in r.limitations))
    unverified = [
        f"{DOMAIN_NAMES.get(d, d)}: 해당 분야 에이전트가 아직 구현되지 않아 확인하지 않음"
        for d in sorted(interp.unsupported_domains)
    ]
    for r in results:
        reason = f" ({r.errors[0].message})" if r.errors else ""
        if r.status is AgentStatus.FAILED:
            unverified.append(f"{r.agent.value} 에이전트: 실패로 확인하지 못함{reason}")
        elif r.status is AgentStatus.SKIPPED:
            unverified.append(f"{r.agent.value} 에이전트: 실행하지 않음{reason}")
    next_checks = list(dict.fromkeys(x for r in results for x in r.next_checks))
    if any(r.agent is AgentName.KUBERNETES and r.status is AgentStatus.SUCCESS for r in results):
        # 같은 요청에서 Kubernetes Agent가 이미 확인했으므로 Server의 확인 제안은 뺍니다.
        next_checks = [x for x in next_checks if x != LIMIT_NEAR_CHECK]
    return FinalAnswer(
        request_id=request_id,
        summary=_summary(facts, results, interp),
        time_range=interp.time_range,
        facts=tuple(facts),
        hypotheses=tuple(hypotheses),
        evidence=tuple(evidence),
        limitations=tuple(limitations),
        unverified_areas=tuple(unverified),
        next_checks=tuple(next_checks),
    )
