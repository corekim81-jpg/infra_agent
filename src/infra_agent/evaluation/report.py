"""평가 결과 출력: 터미널 요약, Markdown 보고서, JSON.

Markdown·JSON 보고서에는 실제 환경의 답변 원문(서비스 이름, 수치, 로그 발췌)이 들어가므로
`var/eval/`(Git 제외)에 저장하고 커밋하지 않습니다. 저장소 문서(docs/evaluation.md)에는 집계값만
옮겨 적습니다.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from infra_agent.answer.render import render_text
from infra_agent.evaluation.checks import CHECK_LABELS, CheckStatus
from infra_agent.evaluation.runner import QuestionResult
from infra_agent.security import redact, redact_values
from infra_agent.units import fmt_time

MARK = {CheckStatus.PASS: "O", CheckStatus.FAIL: "X", CheckStatus.SKIP: "-"}


@dataclass(frozen=True)
class RunInfo:
    started_at: datetime
    profile: str
    eval_set: str
    use_llm: bool
    llm_provider: str | None
    data_policy: str


def _cell(r: QuestionResult, name: str) -> str:
    if r.error is not None:
        return "!"
    c = r.check(name)
    return MARK[c.status] if c else "-"


def totals(results: Sequence[QuestionResult]) -> dict[str, int]:
    checks = [c for r in results for c in r.checks]
    return {
        "questions": len(results),
        "passed_questions": sum(1 for r in results if r.passed),
        "errors": sum(1 for r in results if r.error is not None),
        "pass": sum(1 for c in checks if c.status is CheckStatus.PASS),
        "fail": sum(1 for c in checks if c.status is CheckStatus.FAIL),
        "skip": sum(1 for c in checks if c.status is CheckStatus.SKIP),
    }


def render_summary(results: Sequence[QuestionResult]) -> str:
    """터미널 출력용 요약 (질문 × 기준 표와 실패 사유)."""
    names = [n for n, _ in CHECK_LABELS]
    lines = [
        "평가 기준: " + ", ".join(f"{i + 1}={label}" for i, (_, label) in enumerate(CHECK_LABELS))
    ]
    lines.append("표시: O 통과, X 실패, - 해당 없음, ! 실행 오류")
    width = max((len(r.question.id) for r in results), default=4)
    header = " ".join(f"{i + 1:>2}" for i in range(len(names)))
    lines.append(f"{'질문':<{width}}  {header}  결과  시간")
    for r in results:
        cells = " ".join(f"{_cell(r, n):>2}" for n in names)
        verdict = "통과" if r.passed else ("오류" if r.error else "실패")
        lines.append(f"{r.question.id:<{width}}  {cells}  {verdict}  {r.elapsed_seconds:.1f}초")
    t = totals(results)
    lines.append(
        f"질문 {t['questions']}개 중 통과 {t['passed_questions']}개 (기준 통과 {t['pass']}, "
        f"실패 {t['fail']}, 해당 없음 {t['skip']}, 실행 오류 {t['errors']})"
    )
    failures = [
        f"- {r.question.id} [{c.label}] {c.detail}"
        for r in results
        for c in r.checks
        if c.status is CheckStatus.FAIL
    ] + [f"- {r.question.id} [실행 오류] {r.error}" for r in results if r.error]
    if failures:
        lines.append("")
        lines.append("실패 사유:")
        lines.extend(failures)
    return redact("\n".join(lines))


def render_markdown(results: Sequence[QuestionResult], info: RunInfo) -> str:
    names = [n for n, _ in CHECK_LABELS]
    model = (
        f"사용 ({info.llm_provider}, data_policy={info.data_policy})"
        if info.use_llm and info.llm_provider
        else "사용 안 함"
    )
    out = [
        "# 대표 질문 평가 보고서",
        "",
        "> 실제 환경 데이터(서비스 이름, 수치, 로그 발췌)가 포함됩니다. 저장소에 커밋하지 마세요.",
        "",
        f"- 실행 시각: {fmt_time(info.started_at)}",
        f"- 프로필: {info.profile}",
        f"- 평가 세트: {info.eval_set}",
        f"- 모델: {model}",
        "",
        "## 요약",
        "",
        "| 질문 | " + " | ".join(label for _, label in CHECK_LABELS) + " | 결과 | 시간 |",
        "| --- | " + " | ".join("---" for _ in names) + " | --- | --- |",
    ]
    for r in results:
        cells = " | ".join(_cell(r, n) for n in names)
        verdict = "통과" if r.passed else ("오류" if r.error else "실패")
        out.append(f"| {r.question.id} | {cells} | {verdict} | {r.elapsed_seconds:.1f}초 |")
    t = totals(results)
    out += [
        "",
        f"질문 {t['questions']}개 중 통과 {t['passed_questions']}개 · 기준 통과 {t['pass']}, "
        f"실패 {t['fail']}, 해당 없음 {t['skip']}, 실행 오류 {t['errors']}",
        "",
        "표시: O 통과, X 실패, - 해당 없음, ! 실행 오류",
        "",
        "## 질문별 결과",
    ]
    for r in results:
        out += ["", f"### {r.question.id}", "", f"질문: {r.question.question}", ""]
        if r.question.note:
            out += [f"메모: {r.question.note}", ""]
        if r.error:
            out += [f"실행 오류: {r.error}", ""]
            continue
        out.append("| 기준 | 결과 | 내용 |")
        out.append("| --- | --- | --- |")
        for c in r.checks:
            detail = c.detail.replace("|", "\\|")
            out.append(f"| {c.label} | {MARK[c.status]} | {detail} |")
        if r.bundle is not None:
            text = render_text(r.bundle, show_queries=True)
            fence = _fence(text)
            out += ["", "<details><summary>답변 원문</summary>", "", f"{fence}text"]
            out.append(text)
            out += [fence, "", "</details>"]
    out += [
        "",
        "## 사람 검토 메모",
        "",
        "- (답변 문장의 정확성·유용성, 원인 후보의 타당성, 과도하거나 빠진 내용을 적습니다)",
        "",
    ]
    return redact("\n".join(out))


def _fence(text: str) -> str:
    """본문(로그 발췌 등)에 있는 백틱보다 긴 코드 블록 구분자 (보고서 형식이 깨지지 않게)."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def to_json(results: Sequence[QuestionResult], info: RunInfo) -> str:
    payload = {
        "started_at": info.started_at.isoformat(),
        "profile": info.profile,
        "eval_set": info.eval_set,
        "use_llm": info.use_llm,
        "llm_provider": info.llm_provider,
        "data_policy": info.data_policy,
        "totals": totals(results),
        "questions": [
            {
                "id": r.question.id,
                "question": r.question.question,
                "passed": r.passed,
                "elapsed_seconds": round(r.elapsed_seconds, 2),
                "error": r.error,
                "checks": [
                    {
                        "name": c.name,
                        "label": c.label,
                        "status": c.status.value,
                        "detail": c.detail,
                    }
                    for c in r.checks
                ],
                "answer": r.bundle.answer.model_dump(mode="json") if r.bundle else None,
                "agents": [
                    {"agent": run.agent, "status": run.status.value, "elapsed_ms": run.elapsed_ms}
                    for run in (r.bundle.runs if r.bundle else ())
                ],
            }
            for r in results
        ],
    }
    return json.dumps(redact_values(payload), ensure_ascii=False, indent=2)


def write_report(
    results: Sequence[QuestionResult], info: RunInfo, out_dir: Path
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = info.started_at.strftime("%Y%m%dT%H%M%SZ")
    base, n = f"eval-{stamp}", 1
    while (out_dir / f"{base}.md").exists() or (out_dir / f"{base}.json").exists():
        n += 1  # 같은 초에 다시 실행해도 이전 보고서를 덮어쓰지 않음
        base = f"eval-{stamp}-{n}"
    md_path = out_dir / f"{base}.md"
    json_path = out_dir / f"{base}.json"
    md_path.write_text(render_markdown(results, info), encoding="utf-8")
    json_path.write_text(to_json(results, info), encoding="utf-8")
    return md_path, json_path
