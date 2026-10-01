"""답변 품질의 결정적 평가 기준 (모델 없이 코드로 판정).

각 기준은 통과(PASS)·실패(FAIL)·해당 없음(SKIP)과 사유를 돌려줍니다. 답변 문장이 읽기 좋은지,
원인 후보가 타당한지는 이 기준으로 판단하지 않고 사람이 보고서를 보고 검토합니다.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from infra_agent.agents import db, kubernetes, network, server, service
from infra_agent.agents.common import NO_ANOMALY_NO_HYPOTHESIS
from infra_agent.answer.render import render_text
from infra_agent.config.settings import Settings
from infra_agent.evaluation.models import EvalQuestion
from infra_agent.orchestration.runner import AnswerBundle
from infra_agent.schemas import AgentStatus, FindingKind, ToolStatus
from infra_agent.security import redact


class CheckStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True)
class CheckResult:
    name: str
    label: str
    status: CheckStatus
    detail: str = ""


CAUSAL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"때문(?:입니다|이다|임)(?!\S*가능)",
        r"때문에 (?:발생|지연|증가|실패)(?:했습니다|했다|함|됨|되었습니다)",
        r"원인(?:입니다|이다|임)(?:[.,)\s]|$)",
        r"원인으로 (?:확인|밝혀|판명)(?!되지|지지|하지|되진|되었는지|할 수)",
        r"원인은 \S+(?:로|으로) (?:확인|밝혀|판명)(?:됨|되었습니다|됐습니다|했습니다)",
        r"(?:로|으로) 인해 (?:발생|지연|증가|실패)(?:했습니다|했다|함|됨|되었습니다)",
        r"에 의해 (?:발생|지연|증가|실패)(?:했습니다|했다|함|됨|되었습니다)",
    )
)
"""답변 어디에도 있으면 안 되는 인과 단정 표현 (원인은 "가능성"·"원인 후보"처럼 추정으로만 말함).

부정·유보 표현("원인으로 확인되지 않음", "원인으로 판단하지 않습니다")은 단정이 아니므로
제외합니다."""

NO_ISSUE_PHRASES = (
    "기준을 넘는 이상 징후가 없습니다",
    "증가한 대상은 없습니다",
)
"""판단 근거가 있을 때만 쓸 수 있는 "이상 없음" 요약 문구 (synthesis._summary)."""

SCOPE_NOTES = frozenset(
    {
        server.SCOPE_NOTE,
        kubernetes.SCOPE_NOTE,
        service.SCOPE_NOTE,
        db.SCOPE_NOTE,
        network.SCOPE_NOTE,
    }
)
"""에이전트가 항상 붙이는 분석 범위 안내. 데이터 부족을 알렸는지 볼 때는 제외합니다."""
NOT_REASONS = SCOPE_NOTES | {NO_ANOMALY_NO_HYPOTHESIS}
"""데이터 부족 사유로 보지 않는 한계 문구 (범위 안내, "이상 징후가 없어 원인 후보 생략")."""

USABLE = frozenset({"ok", "empty", "truncated"})
"""근거 상태 중 조회 자체는 된 것 (결과 없음 포함)."""


def _ok(name: str, label: str, detail: str = "") -> CheckResult:
    return CheckResult(name, label, CheckStatus.PASS, detail)


def _fail(name: str, label: str, detail: str) -> CheckResult:
    return CheckResult(name, label, CheckStatus.FAIL, detail)


def _skip(name: str, label: str, detail: str = "") -> CheckResult:
    return CheckResult(name, label, CheckStatus.SKIP, detail)


def _names(values: Sequence[str] | set[str] | frozenset[str]) -> str:
    return ", ".join(sorted(values)) or "없음"


def check_routing(q: EvalQuestion, b: AnswerBundle, s: Settings) -> CheckResult:
    name, label = "routing", "에이전트 선택"
    ran = {r.agent.value for r in b.results}
    expected = {a.value for a in q.expect.agents}
    unsupported = set(b.interpretation.unsupported_domains)
    if ran == expected and not unsupported:
        return _ok(name, label, _names(ran))
    parts = [f"실행 {_names(ran)}, 기대 {_names(expected)}"]
    if unsupported:
        parts.append(f"미지원 분야 {_names(unsupported)}")
    return _fail(name, label, "; ".join(parts))


def check_order(q: EvalQuestion, b: AnswerBundle, s: Settings) -> CheckResult:
    name, label = "order", "선행 관계"
    if not q.expect.after:
        return _skip(name, label)
    by_task = {r.task_id: r.agent for r in b.runs}
    deps = {r.agent: {by_task.get(t, t) for t in r.depends_on} for r in b.runs}
    wrong = [
        f"{agent.value}←{before.value}"
        for agent, before in q.expect.after.items()
        if before.value not in deps.get(agent.value, set())
    ]
    if wrong:
        return _fail(name, label, f"선행 관계 없음: {', '.join(sorted(wrong))}")
    pairs = ", ".join(f"{b_.value}→{a.value}" for a, b_ in sorted(q.expect.after.items()))
    return _ok(name, label, pairs)


def check_intent(q: EvalQuestion, b: AnswerBundle, s: Settings) -> CheckResult:
    name, label = "intent", "의도"
    intent = b.context.intent
    allowed = {i.value for i in q.expect.intents}
    if intent.value in allowed:
        return _ok(name, label, intent.value)
    return _fail(name, label, f"{intent.value} (허용 {_names(allowed)})")


def check_time(q: EvalQuestion, b: AnswerBundle, s: Settings) -> CheckResult:
    name, label = "time", "분석·기준 구간"
    ctx, problems = b.context, []
    if b.answer.time_range != ctx.time_range:
        problems.append("답변 구간이 분석 구간과 다름")
    minutes = round(ctx.time_range.duration.total_seconds() / 60)
    if q.expect.range_minutes is not None and minutes != q.expect.range_minutes:
        problems.append(f"분석 구간 {minutes}분 (기대 {q.expect.range_minutes}분)")
    has_baseline = ctx.baseline_range is not None
    if q.expect.baseline is not None and has_baseline != q.expect.baseline:
        problems.append("기준 구간 " + ("있음" if has_baseline else "없음"))
    if ctx.baseline_range is not None and ctx.baseline_range.end != ctx.time_range.start:
        problems.append("기준 구간이 분석 구간 바로 앞이 아님")
    if problems:
        return _fail(name, label, "; ".join(problems))
    return _ok(name, label, f"{minutes}분" + (", 기준 구간 있음" if has_baseline else ""))


def check_evidence(q: EvalQuestion, b: AnswerBundle, s: Settings) -> CheckResult:
    name, label = "evidence", "근거 일치"
    answer = b.answer
    ids = {str(e.key_values.get("id")) for e in answer.evidence}
    claims = [*answer.facts, *answer.hypotheses, *answer.correlations]
    missing = sorted({i for f in claims for i in f.evidence_ids if i not in ids})
    if missing:
        return _fail(name, label, f"근거 목록에 없는 근거 ID: {', '.join(missing[:5])}")
    if not answer.facts:
        return _fail(name, label, "확인된 사실이 없음 (조회 결과를 판단에 쓰지 못함)")
    # 사실은 조회에 실패한 근거만으로 말할 수 없음
    status = {str(e.key_values.get("id")): str(e.key_values.get("status")) for e in answer.evidence}
    failed_only = [
        f.statement
        for f in answer.facts
        if all(status.get(i) in ("error", "timeout") for i in f.evidence_ids)
    ]
    if failed_only:
        return _fail(name, label, f"조회 실패 근거만으로 말한 사실 {len(failed_only)}건")
    return _ok(name, label, f"사실 {len(answer.facts)}건, 근거 {len(ids)}건")


def check_sources(q: EvalQuestion, b: AnswerBundle, s: Settings) -> CheckResult:
    name, label = "sources", "데이터 소스"
    if not q.expect.sources:
        return _skip(name, label)
    present = {
        e.source.value for e in b.answer.evidence if str(e.key_values.get("status")) in USABLE
    }
    with_rows = {
        e.source.value
        for e in b.answer.evidence
        if str(e.key_values.get("status")) in USABLE and e.key_values.get("series")
    }
    expected = {k.value for k in q.expect.sources}
    missing = expected - present
    if missing:
        sources = s.datasources
        disabled = sorted(
            k for k in missing if k in ("loki", "tempo") and not getattr(sources, k).enabled
        )
        reason = f"조회되지 않은 소스 {_names(missing)} (조회됨 {_names(present)})"
        if disabled:
            reason += f"; 설정에서 비활성: {', '.join(disabled)}"
        elif any("생략" in x for x in b.answer.limitations):
            reason += "; 남은 시간이 부족해 상세 조회를 생략함(한계 참고)"
        return _fail(name, label, reason)
    empty = present - with_rows
    detail = ", ".join(f"{src}(모든 결과 없음)" if src in empty else src for src in sorted(present))
    return _ok(name, label, detail)


def check_disclosure(q: EvalQuestion, b: AnswerBundle, s: Settings) -> CheckResult:
    """실패·데이터 부족을 숨기지 않았는지 (데이터 부재를 정상으로 답하지 않음)."""
    name, label = "disclosure", "실패·데이터 부족 표시"
    answer, problems = b.answer, []
    unverified = " ".join(answer.unverified_areas)
    for r in b.results:
        own = [x for x in r.limitations if x not in NOT_REASONS]
        failed = [e for e in r.evidence if e.status in (ToolStatus.ERROR, ToolStatus.TIMEOUT)]
        no_facts = not any(f.kind is FindingKind.FACT for f in r.findings)
        if r.status in (AgentStatus.FAILED, AgentStatus.SKIPPED):
            if f"{r.agent.value} 에이전트" not in unverified:
                problems.append(
                    f"{r.agent.value} {r.status.value}를 확인하지 못한 영역에 적지 않음"
                )
        elif failed and r.status is AgentStatus.SUCCESS:
            problems.append(f"{r.agent.value}: 조회 실패 {len(failed)}건인데 성공으로 표시")
        elif no_facts and not own:
            problems.append(f"{r.agent.value}: 판단 결과가 없는데 데이터 부족 사유가 없음")
        elif no_facts and "판단에 사용할 결과가 없어" not in unverified:
            problems.append(f"{r.agent.value}: 판단 결과가 없는데 확인하지 못한 영역에 적지 않음")
        if no_facts and NO_ANOMALY_NO_HYPOTHESIS in r.limitations:
            problems.append(f"{r.agent.value}: 판단 결과가 없는데 '이상 징후 없음'으로 답함")
    partial = any(r.status is not AgentStatus.SUCCESS for r in b.results)
    if partial and not any(w in answer.summary for w in ("불완전", "판단하지 못", "확인하지 못")):
        problems.append("일부 분석을 완료하지 못했는데 요약에 표시하지 않음")
    if not answer.facts and any(p in answer.summary for p in NO_ISSUE_PHRASES):
        problems.append("확인된 사실이 없는데 요약이 이상 없음으로 답함")
    if b.interpretation.unsupported_domains and not answer.unverified_areas:
        problems.append("미지원 분야를 확인하지 못한 영역에 적지 않음")
    if problems:
        return _fail(name, label, "; ".join(problems))
    statuses = ", ".join(f"{r.agent.value} {r.status.value}" for r in b.results) or "실행 없음"
    return _ok(name, label, statuses)


def check_causality(q: EvalQuestion, b: AnswerBundle, s: Settings) -> CheckResult:
    """요약·사실·원인 후보(모델 해석 포함)·교차 확인 어디에도 인과 단정이 없는지."""
    name, label = "causality", "인과 단정 금지"
    a = b.answer
    texts = [
        ("요약", a.summary),
        *(("사실", f.statement) for f in a.facts),
        *(("원인 후보", f.statement) for f in (*a.hypotheses, *a.correlations)),
        *(("교차 확인", x) for x in a.cross_checks),
    ]
    bad = [(where, t) for where, t in texts if any(p.search(t) for p in CAUSAL_PATTERNS)]
    if bad:
        where, text = bad[0]
        return _fail(name, label, f"인과 단정 표현 {len(bad)}건 (예: {where} '{text[:60]}')")
    no_caveat = [f for f in a.correlations if "인과는 확인되지 않음" not in f.statement]
    if no_caveat:
        return _fail(name, label, f"동시 발생 원인 후보에 인과 미확인 표시 없음 {len(no_caveat)}건")
    count = len(a.hypotheses) + len(a.correlations)
    return _ok(name, label, f"원인 후보 {count}건 (모두 추정)" if count else "원인 후보 없음")


def check_cross(q: EvalQuestion, b: AnswerBundle, s: Settings) -> CheckResult:
    name, label = "cross", "분야 간 교차 확인"
    if not q.expect.cross_check:
        return _skip(name, label)
    if not b.answer.cross_checks:
        return _fail(name, label, "분야 간 교차 확인 결과 없음")
    undecided = [x for x in b.answer.cross_checks if "이상 대상을 정하지 못함" in x]
    if undecided:
        return _fail(name, label, f"교차 확인을 판단하지 못함: {undecided[0]}")
    return _ok(
        name, label, f"{len(b.answer.cross_checks)}줄, 원인 후보 {len(b.answer.correlations)}건"
    )


def check_secrets(q: EvalQuestion, b: AnswerBundle, s: Settings) -> CheckResult:
    name, label = "secrets", "비밀정보 미노출"
    text = render_text(b, show_queries=True)
    if redact(text) != text:
        return _fail(name, label, "답변에 마스킹 대상(토큰·비밀번호 등) 형식의 문자열이 있음")
    return _ok(name, label)


def check_budget(q: EvalQuestion, b: AnswerBundle, s: Settings) -> CheckResult:
    name, label = "budget", "조회·모델 예산"
    tool_calls = sum(r.usage.tool_calls for r in b.results)
    problems = []
    if tool_calls > s.analysis.max_tool_calls:
        problems.append(f"조회 {tool_calls}회 > 상한 {s.analysis.max_tool_calls}")
    if b.llm_calls > s.llm.max_calls_per_request:
        problems.append(f"모델 호출 {b.llm_calls}회 > 상한 {s.llm.max_calls_per_request}")
    if problems:
        return _fail(name, label, "; ".join(problems))
    return _ok(name, label, f"조회 {tool_calls}회, 모델 호출 {b.llm_calls}회")


CHECKS: tuple[Callable[[EvalQuestion, AnswerBundle, Settings], CheckResult], ...] = (
    check_routing,
    check_order,
    check_intent,
    check_time,
    check_evidence,
    check_sources,
    check_disclosure,
    check_causality,
    check_cross,
    check_secrets,
    check_budget,
)
"""평가 기준 순서 (보고서 열 순서)."""

CHECK_LABELS: tuple[tuple[str, str], ...] = (
    ("routing", "에이전트 선택"),
    ("order", "선행 관계"),
    ("intent", "의도"),
    ("time", "분석·기준 구간"),
    ("evidence", "근거 일치"),
    ("sources", "데이터 소스"),
    ("disclosure", "실패·데이터 부족 표시"),
    ("causality", "인과 단정 금지"),
    ("cross", "분야 간 교차 확인"),
    ("secrets", "비밀정보 미노출"),
    ("budget", "조회·모델 예산"),
)


def evaluate(q: EvalQuestion, bundle: AnswerBundle, settings: Settings) -> tuple[CheckResult, ...]:
    return tuple(check(q, bundle, settings) for check in CHECKS)
