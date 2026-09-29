"""한국어 답변 템플릿 (모델 없이)."""

from __future__ import annotations

from datetime import datetime

from infra_agent.orchestration.rules import Interpretation
from infra_agent.orchestration.runner import AnswerBundle
from infra_agent.schemas import Intent, Severity, TimeRange

_INTENT = {
    Intent.STATUS: "현재 상태 조회",
    Intent.COMPARE: "직전 구간과 비교",
    Intent.ANOMALY: "이상 증가 탐지 (직전 구간 대비)",
    Intent.IMPACT: "영향 범위",
    Intent.ROOT_CAUSE: "원인 후보",
}
_SEVERITY = {Severity.CRITICAL: "심각", Severity.WARNING: "경고", Severity.INFO: "정보"}


def _t(value: datetime) -> str:
    local = value.astimezone()
    return f"{local:%Y-%m-%d %H:%M:%S} {local.tzname() or ''}".strip()


def _range(tr: TimeRange) -> str:
    return f"{_t(tr.start)} ~ {_t(tr.end)}"


def _method(interp: Interpretation) -> str:
    if interp.method == "model":
        return "모델"
    return f"규칙 기반 ({interp.method_note})" if interp.method_note else "규칙 기반"


def render_text(bundle: AnswerBundle, *, show_queries: bool = False) -> str:
    interp, ctx, answer = bundle.interpretation, bundle.context, bundle.answer
    lines: list[str] = []
    lines.append(f"질문: {ctx.question}")
    lines.append("")
    lines.append("[질문 해석]")
    lines.append(f"- 의도: {_INTENT[ctx.intent]}")
    if ctx.intent is Intent.STATUS:
        lines.append(f"- 조회 시각: {_t(ctx.time_range.end)} (현재 값 기준)")
    else:
        lines.append(f"- 분석 구간: {_range(ctx.time_range)}")
    if ctx.baseline_range is not None:
        lines.append(f"- 기준 구간: {_range(ctx.baseline_range)}")
    targets = ", ".join(f"{t.kind.value}={t.name}" for t in ctx.targets) or "전체"
    lines.append(f"- 대상: {targets}")
    lines.append(f"- 해석 방식: {_method(interp)}")
    for a in interp.assumptions:
        lines.append(f"- 가정: {a}")
    lines.append("")
    lines.append("[요약]")
    lines.append(answer.summary)

    anomalies = [f for f in answer.facts if f.severity is not Severity.INFO]
    normals = [f for f in answer.facts if f.severity is Severity.INFO]
    if anomalies:
        lines.append("")
        lines.append("[이상 징후]")
        for f in anomalies:
            lines.append(f"- [{_SEVERITY[f.severity]}] {f.statement}")
    if normals:
        lines.append("")
        lines.append("[확인된 사실]")
        for f in normals:
            lines.append(f"- {f.statement}")
    if answer.hypotheses:
        lines.append("")
        lines.append("[원인 후보 (추정, 모델 해석)]")
        for f in answer.hypotheses:
            conf = f.confidence.value if f.confidence else "-"
            refs = ", ".join(f.evidence_ids)
            lines.append(f"- {f.statement} (신뢰도 {conf}, 근거 {refs})")

    lines.append("")
    lines.append("[근거]")
    if not answer.evidence:
        lines.append("- 실행한 조회 없음")
    for e in answer.evidence:
        kv = e.key_values
        window = (
            _range(e.time_range) + " 평균" if e.time_range else f"{_t(ctx.time_range.end)} 시점"
        )
        extra = f", 최신 샘플 {kv['freshness_seconds']}초 전" if "freshness_seconds" in kv else ""
        err = f", 오류: {kv['error']}" if "error" in kv else ""
        lines.append(
            f"- {kv.get('id')}: {e.source.value}, {window}, 결과 {kv.get('series')}개 "
            f"({kv.get('status')}{extra}{err})"
        )
        if show_queries and e.query:
            lines.append(f"    {e.query}")
    if answer.limitations:
        lines.append("")
        lines.append("[한계]")
        lines.extend(f"- {x}" for x in answer.limitations)
    if answer.unverified_areas:
        lines.append("")
        lines.append("[확인하지 못한 영역]")
        lines.extend(f"- {x}" for x in answer.unverified_areas)
    if answer.next_checks:
        lines.append("")
        lines.append("[추가 확인]")
        lines.extend(f"- {x}" for x in answer.next_checks)
    lines.append("")
    calls = sum(r.usage.tool_calls for r in bundle.results)
    if bundle.llm_calls:
        model = (
            f"모델 호출 {bundle.llm_calls}회({bundle.llm_name}, data_policy={bundle.data_policy})"
        )
    else:
        model = "모델 호출 없음"
    lines.append(f"(요청 ID {ctx.request_id}, {model}, 분석 조회 {calls}회)")
    return "\n".join(lines)
