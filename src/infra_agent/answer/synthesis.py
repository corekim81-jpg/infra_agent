"""에이전트 결과 종합 (모델 없이).

- 근거 ID가 실제 조회 결과를 가리키는 Finding만 사용합니다(스키마에서도 강제).
- 실패·부분 실패 에이전트는 확인하지 못한 영역으로 표시합니다.
"""

from __future__ import annotations

from collections.abc import Sequence

from infra_agent.orchestration.rules import Interpretation
from infra_agent.schemas import (
    AgentResult,
    AgentStatus,
    EvidenceSummary,
    FinalAnswer,
    Finding,
    FindingKind,
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


def _summary(
    facts: Sequence[Finding], results: Sequence[AgentResult], interp: Interpretation
) -> str:
    if results and all(r.status is AgentStatus.FAILED for r in results):
        return "조회에 실패해 상태를 판단하지 못했습니다. 한계와 오류 내용을 확인하세요."
    if not results:
        return "요청한 분야를 분석할 수 있는 에이전트가 아직 없어 답할 수 없습니다."
    critical = sum(1 for f in facts if f.severity is Severity.CRITICAL)
    warning = sum(1 for f in facts if f.severity is Severity.WARNING)
    partial = any(r.status is not AgentStatus.SUCCESS for r in results)
    if critical or warning:
        parts = []
        if critical:
            parts.append(f"심각 {critical}건")
        if warning:
            parts.append(f"경고 {warning}건")
        text = f"기준을 넘는 이상 징후 {critical + warning}건({', '.join(parts)})을 확인했습니다."
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
    facts = sorted(
        (f for r in results for f in r.findings if f.kind is FindingKind.FACT),
        key=lambda f: _SEVERITY_ORDER[f.severity],
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
        if r.status is AgentStatus.FAILED:
            unverified.append(f"{r.agent.value} 에이전트: 조회 실패로 확인하지 못함")
    next_checks = list(dict.fromkeys(x for r in results for x in r.next_checks))
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
