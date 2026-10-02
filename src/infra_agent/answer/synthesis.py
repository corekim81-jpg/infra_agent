"""에이전트 결과 종합 (모델 없이).

- 근거 ID가 실제 조회 결과를 가리키는 Finding만 사용합니다(스키마에서도 강제).
- 실패·부분 실패 에이전트는 확인하지 못한 영역으로 표시합니다.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from infra_agent.agents.common import asks_cause
from infra_agent.agents.server import LIMIT_NEAR_CHECK
from infra_agent.answer.cross import DEFAULT_STALE_SECONDS, cross_check
from infra_agent.llm.policy import sample_rows
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
    ToolResult,
    ToolStatus,
)
from infra_agent.units import fmt_time

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


COMPARING_AGENTS = (AgentName.SERVER, AgentName.SERVICE, AgentName.DB)
"""직전 구간 대비 증가를 판정하는 에이전트."""


def _compares(interp: Interpretation, results: Sequence[AgentResult]) -> bool:
    """직전 구간 비교 결과를 먼저 답할지. 비교는 Server·Service·DB Agent가 수행합니다."""
    return interp.intent in (Intent.COMPARE, Intent.ANOMALY) and any(
        r.agent in COMPARING_AGENTS for r in results
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
    blank = _no_fact_domains(results)
    if blank and facts:
        # 다른 분야의 판정만으로 "이상 없음"처럼 읽히지 않게, 판단 결과가 없는 분야를 밝힙니다.
        text += f" ({', '.join(blank)} 분야는 판단에 사용할 결과가 없어 확인하지 못했습니다.)"
    if interp.unsupported_domains:
        names = ", ".join(DOMAIN_NAMES.get(d, d) for d in sorted(interp.unsupported_domains))
        text += f" ({names} 분야는 아직 분석하지 않았습니다.)"
    return text


def _no_fact_domains(results: Sequence[AgentResult]) -> list[str]:
    """실행은 됐지만(성공·부분 성공) 판단 결과(사실)가 하나도 없는 분야."""
    return [
        DOMAIN_NAMES.get(r.agent.value, r.agent.value)
        for r in results
        if r.status in (AgentStatus.SUCCESS, AgentStatus.PARTIAL)
        and not any(f.kind is FindingKind.FACT for f in r.findings)
    ]


MAX_SHOWN_SAMPLES = 3


def sample_texts(e: ToolResult) -> list[str]:
    """근거의 로그·트레이스 발췌를 답변 표시용 문장으로 만듭니다 (근거당 최대 3건).

    로그 본문은 도구 계층에서 제어문자 제거·마스킹·길이 제한을 거친 값입니다.
    """
    out: list[str] = []
    for row in sample_rows(e)[:MAX_SHOWN_SAMPLES]:
        when = row.get("time") or row.get("start")
        stamp = fmt_time(datetime.fromisoformat(str(when))) if when else "시각 없음"
        if "line" in row:
            labels = row.get("labels") if isinstance(row.get("labels"), dict) else {}
            service = labels.get("service_name", "?") if isinstance(labels, dict) else "?"
            tid = f" (trace_id {row['trace_id']})" if row.get("trace_id") else ""
            out.append(f"(로그 원문) {stamp} [{service}] {row['line']}{tid}")
        else:
            duration = row.get("duration_ms")
            took = f", {float(duration):.0f}ms" if isinstance(duration, int | float) else ""
            root = " ".join(str(x) for x in (row.get("root_service"), row.get("root_name")) if x)
            out.append(f"(트레이스) {stamp} trace_id {row['trace_id']} {root}{took}".rstrip())
    return out


def synthesize(
    request_id: str,
    interp: Interpretation,
    results: Sequence[AgentResult],
    stale_after_seconds: float = DEFAULT_STALE_SECONDS,
    question: str = "",
) -> FinalAnswer:
    """에이전트 결과를 답변으로 종합합니다.

    분야 간 교차 확인 요약은 서비스 이상 대상이 있거나 질문이 원인을 물을 때만 요약에 붙입니다
    (단순 비교 질문의 요약을 원인 분석 문장으로 채우지 않음). 교차 확인 줄은 항상 답변에 둡니다.
    """
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
            samples = sample_texts(e)
            if samples:
                key_values["samples"] = samples
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
    unverified.extend(
        f"{name}: 판단에 사용할 결과가 없어 확인하지 못함 (사유는 한계 참고)"
        for name in _no_fact_domains(results)
    )
    next_checks = list(dict.fromkeys(x for r in results for x in r.next_checks))
    if any(r.agent is AgentName.KUBERNETES and r.status is AgentStatus.SUCCESS for r in results):
        # 같은 요청에서 Kubernetes Agent가 이미 확인했으므로 Server의 확인 제안은 뺍니다.
        next_checks = [x for x in next_checks if x != LIMIT_NEAR_CHECK]
    cross = cross_check(results, stale_after_seconds)
    limitations.extend(x for x in cross.limitations if x not in limitations)
    scope_notes = list(dict.fromkeys(x for r in results for x in r.scope_notes))
    scope_notes.extend(x for x in cross.scope_notes if x not in scope_notes)
    summary = _summary(facts, results, interp)
    has_issues = bool(cross.lines) and not cross.lines[0].startswith("서비스 이상 대상: 없음")
    if cross.summary and (has_issues or asks_cause(question)):
        summary += " " + cross.summary
    return FinalAnswer(
        request_id=request_id,
        summary=summary,
        time_range=interp.time_range,
        facts=tuple(facts),
        hypotheses=tuple(hypotheses),
        evidence=tuple(evidence),
        limitations=tuple(limitations),
        scope_notes=tuple(scope_notes),
        unverified_areas=tuple(unverified),
        next_checks=tuple(next_checks),
        cross_checks=cross.lines,
        correlations=cross.correlations,
    )
