"""에이전트별 모델 해석 (원인 후보·추가 확인 제안).

- `data_policy`가 none이면 호출하지 않습니다(조회 결과를 모델에 보내지 않음).
- 모델 출력은 검증을 통과한 것만 사용합니다.
  - 원인 후보의 evidence id는 이 에이전트가 실제로 조회한 근거여야 함
  - 문장 속 수치는 모델에 제공한 관측 데이터에 그대로 있어야 함 (지어낸 수치 차단)
  - confidence는 low/medium만 허용 (모델 추정을 high로 표시하지 않음)
- 사실(Finding kind=fact)은 코드 판정만 사용하며 모델이 바꾸지 않습니다.
- 추가 확인 제안은 코드가 이미 낸 항목과 겹치면 제외합니다(정규화 후 동일·포함 관계).
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from infra_agent.agents.base import remaining_seconds
from infra_agent.config.settings import DataPolicy
from infra_agent.llm.base import LLMClient, LLMError, LLMRequest
from infra_agent.llm.policy import DATA_GUARD, MAX_ROWS, build_observations, truncated_evidence
from infra_agent.schemas import (
    AgentResult,
    AnalysisContext,
    Confidence,
    Finding,
    FindingKind,
    JudgementBasis,
    RejectedHypothesis,
    Severity,
)
from infra_agent.security import redact
from infra_agent.units import fmt_time

MIN_EXPLAIN_SECONDS = 5.0
"""에이전트 제한 시간 안에 남은 시간이 이보다 적으면 모델 해석을 생략합니다."""
RESERVE_SECONDS = 2.0
"""모델 해석 뒤 결과를 정리할 여유 시간."""

MAX_HYPOTHESES = 5
MAX_NEXT_CHECKS = 3
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9.])\d+(?:\.\d+)?")
"""문장 속 수치. 영문자에 붙은 숫자(k3d, p99)는 이름의 일부로 보고 검사하지 않습니다."""
_OBSERVED_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9.])\d+(?:\.\d+)?(?![0-9])")
"""관측 데이터 속 수치. 영문자 뒤의 숫자(k3d의 3)는 이름의 일부로 보고 인정하지 않으며,
단위가 붙은 값(11.5GiB, 0.473 cores)은 인정합니다."""
_NORMALIZE_RE = re.compile(r"[\s\W_]+")
MODEL_CHECK_PREFIX = "(모델 제안) "

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "hypotheses": {
            "type": "array",
            "maxItems": MAX_HYPOTHESES,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "statement": {"type": "string", "maxLength": 300},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "confidence": {"type": "string", "enum": ["low", "medium"]},
                },
                "required": ["statement", "evidence_ids", "confidence"],
            },
        },
        "next_checks": {
            "type": "array",
            "maxItems": MAX_NEXT_CHECKS,
            "items": {"type": "string", "maxLength": 200},
        },
    },
    "required": ["hypotheses", "next_checks"],
}


class _Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    statement: str = Field(min_length=1, max_length=300)
    evidence_ids: list[str] = Field(min_length=1)
    confidence: Literal["low", "medium"]


class _Explanation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hypotheses: list[_Hypothesis] = Field(default_factory=list, max_length=MAX_HYPOTHESES)
    next_checks: list[str] = Field(default_factory=list)  # 초과분은 중복 제거 후 잘라냄


@dataclass
class Explanation:
    hypotheses: list[Finding] = field(default_factory=list)
    next_checks: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    rejected: list[RejectedHypothesis] = field(default_factory=list)
    llm_calls: int = 0


def _normalize(text: str) -> str:
    return _NORMALIZE_RE.sub("", text).lower()


def is_duplicate_check(candidate: str, existing: list[str]) -> bool:
    """공백·기호를 무시하고 같거나 한쪽이 다른 쪽을 포함하면 중복으로 봅니다."""
    key = _normalize(candidate)
    if not key:
        return True
    for other in existing:
        other_key = _normalize(other.removeprefix(MODEL_CHECK_PREFIX))
        if other_key and (key == other_key or key in other_key or other_key in key):
            return True
    return False


def ungrounded_numbers(statement: str, observed: str) -> list[str]:
    """관측 데이터에 같은 값으로 없는 수치 목록 (순서 유지, 중복 제거).

    값으로 비교하므로 95와 95.0은 같게 봅니다. 단위 환산·계산한 값은 인정하지 않습니다.
    """
    known = {float(n) for n in _OBSERVED_NUMBER_RE.findall(observed)}
    return [n for n in dict.fromkeys(_NUMBER_RE.findall(statement)) if float(n) not in known]


def numbers_grounded(statement: str, observed: str) -> bool:
    """문장 속 모든 수치가 관측 데이터 문자열에 그대로 있는지 확인합니다."""
    return not ungrounded_numbers(statement, observed)


class AgentExplainer:
    def __init__(
        self, llm: LLMClient, *, purpose: str, system_prompt: str, policy: DataPolicy
    ) -> None:
        self._llm = llm
        self._purpose = purpose
        self._system = system_prompt
        self._policy = policy

    def needed(self, result: AgentResult) -> bool:
        """이상 징후(경고·심각)가 있을 때만 해석을 요청합니다."""
        return any(f.severity is not Severity.INFO for f in result.findings)

    async def explain(self, result: AgentResult, ctx: AnalysisContext) -> Explanation:
        out = Explanation()
        observed = build_observations([result], self._policy)
        if observed is None:
            return out  # data_policy=none: 조회 결과를 모델에 보내지 않음
        window = (
            f"{fmt_time(ctx.time_range.start)} ~ {fmt_time(ctx.time_range.end)} "
            f"(UTC {ctx.time_range.start.isoformat()} ~ {ctx.time_range.end.isoformat()})"
        )
        existing_checks = "\n".join(f"- {c}" for c in result.next_checks) or "- (없음)"
        prompt = (
            f"질문: {ctx.question}\n"
            f"분석 구간: {window}\n"
            f"{DATA_GUARD}\n\n{observed}\n\n"
            f"이미 답변에 포함된 추가 확인 사항 (같은 내용은 다시 제안하지 마세요):\n"
            f"{existing_checks}\n\n"
            "위 관측 데이터의 이상 징후에 대해 원인 후보(hypotheses)와 "
            "추가 확인 사항(next_checks)을 JSON으로 제시하세요."
        )
        remaining = remaining_seconds()
        budget = None if remaining is None else remaining - RESERVE_SECONDS
        if budget is not None and budget < MIN_EXPLAIN_SECONDS:
            out.limitations.append("모델 해석 생략: 에이전트 제한 시간 안에 남은 시간이 부족함")
            return out
        out.llm_calls = 1
        request = LLMRequest(
            purpose=self._purpose, system=self._system, prompt=prompt, schema=SCHEMA
        )
        try:
            response = await asyncio.wait_for(self._llm.complete(request), timeout=budget)
            parsed = _Explanation.model_validate(response.data or {})
        except TimeoutError:
            waited = f"({budget:.0f}초)" if budget is not None else ""
            out.limitations.append(
                f"모델 해석 생략: 에이전트 제한 시간 안에 응답이 없음{waited}. "
                "코드 판정 결과는 그대로 사용"
            )
            return out
        except LLMError as exc:
            out.limitations.append(f"모델 해석 생략: {exc.message}")
            return out
        except ValidationError:
            out.limitations.append("모델 해석 생략: 모델 출력 형식 오류")
            return out

        known = {e.evidence_id for e in result.evidence}
        truncated = truncated_evidence([result])
        for h in parsed.hypotheses:
            ids = tuple(dict.fromkeys(h.evidence_ids))
            reasons: list[str] = []
            unknown = [i for i in ids if i not in known]
            if not ids or unknown:
                reasons.append("근거 ID 불일치: " + (", ".join(unknown) or "(없음)"))
            missing = ungrounded_numbers(h.statement, observed)
            if missing:
                reason = "관측 데이터에 없는 수치: " + ", ".join(missing)
                cut = [i for i in ids if i in truncated]
                if cut:
                    reason += (
                        f" (근거 {', '.join(cut)}는 결과 행 상위 {MAX_ROWS}개만 전달됨. "
                        "전달되지 않은 대상의 값일 수 있음)"
                    )
                reasons.append(reason)
            if reasons:
                out.rejected.append(
                    RejectedHypothesis(
                        statement=redact(h.statement.strip()),
                        evidence_ids=tuple(redact(i) for i in ids),
                        reasons=tuple(redact(r) for r in reasons),
                    )
                )
                continue
            out.hypotheses.append(
                Finding(
                    kind=FindingKind.HYPOTHESIS,
                    statement=h.statement.strip(),
                    severity=Severity.INFO,
                    evidence_ids=ids,
                    basis=JudgementBasis.CORRELATION,
                    confidence=Confidence(h.confidence),
                )
            )
        if out.rejected:
            by_id = sum(any(r.startswith("근거 ID") for r in x.reasons) for x in out.rejected)
            by_num = sum(any(r.startswith("관측 데이터") for r in x.reasons) for x in out.rejected)
            out.limitations.append(
                f"모델이 제시한 원인 후보 {len(out.rejected)}건은 검증에 실패해 제외함 "
                f"(근거 ID 불일치 {by_id}건, 관측 데이터에 없는 수치 {by_num}건; "
                "원문은 --show-queries 또는 --json에서 확인)"
            )
        seen = list(result.next_checks)
        for check in parsed.next_checks:
            text = check.strip()
            if not text or not numbers_grounded(text, observed) or is_duplicate_check(text, seen):
                continue
            seen.append(text)
            out.next_checks.append(f"{MODEL_CHECK_PREFIX}{text}")
            if len(out.next_checks) >= MAX_NEXT_CHECKS:
                break
        return out
