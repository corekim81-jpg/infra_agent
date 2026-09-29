"""에이전트별 모델 해석 (원인 후보·추가 확인 제안).

- `data_policy`가 none이면 호출하지 않습니다(조회 결과를 모델에 보내지 않음).
- 모델 출력은 검증을 통과한 것만 사용합니다.
  - 원인 후보의 evidence id는 이 에이전트가 실제로 조회한 근거여야 함
  - 문장 속 수치는 모델에 제공한 관측 데이터에 그대로 있어야 함 (지어낸 수치 차단)
  - confidence는 low/medium만 허용 (모델 추정을 high로 표시하지 않음)
- 사실(Finding kind=fact)은 코드 판정만 사용하며 모델이 바꾸지 않습니다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from infra_agent.config.settings import DataPolicy
from infra_agent.llm.base import LLMClient, LLMError, LLMRequest
from infra_agent.llm.policy import DATA_GUARD, build_observations
from infra_agent.schemas import (
    AgentResult,
    AnalysisContext,
    Confidence,
    Finding,
    FindingKind,
    JudgementBasis,
    Severity,
)

MAX_HYPOTHESES = 5
MAX_NEXT_CHECKS = 5
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")

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
    next_checks: list[str] = Field(default_factory=list, max_length=MAX_NEXT_CHECKS)


@dataclass
class Explanation:
    hypotheses: list[Finding] = field(default_factory=list)
    next_checks: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    llm_calls: int = 0


def numbers_grounded(statement: str, observed: str) -> bool:
    """문장 속 모든 수치가 관측 데이터 문자열에 그대로 있는지 확인합니다."""
    return all(num in observed for num in _NUMBER_RE.findall(statement))


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
        window = f"{ctx.time_range.start.isoformat()} ~ {ctx.time_range.end.isoformat()}"
        prompt = (
            f"질문: {ctx.question}\n"
            f"분석 구간(UTC): {window}\n"
            f"{DATA_GUARD}\n\n{observed}\n\n"
            "위 관측 데이터의 이상 징후에 대해 원인 후보(hypotheses)와 "
            "추가 확인 사항(next_checks)을 JSON으로 제시하세요."
        )
        out.llm_calls = 1
        try:
            response = await self._llm.complete(
                LLMRequest(purpose=self._purpose, system=self._system, prompt=prompt, schema=SCHEMA)
            )
            parsed = _Explanation.model_validate(response.data or {})
        except LLMError as exc:
            out.limitations.append(f"모델 해석 생략: {exc.message}")
            return out
        except ValidationError:
            out.limitations.append("모델 해석 생략: 모델 출력 형식 오류")
            return out

        known = {e.evidence_id for e in result.evidence}
        rejected = 0
        for h in parsed.hypotheses:
            ids = tuple(dict.fromkeys(h.evidence_ids))
            if not ids or not set(ids) <= known or not numbers_grounded(h.statement, observed):
                rejected += 1
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
        if rejected:
            out.limitations.append(
                f"모델이 제시한 원인 후보 {rejected}건은 근거 ID 또는 수치 검증에 실패해 제외함"
            )
        for check in parsed.next_checks:
            text = check.strip()
            if text and numbers_grounded(text, observed):
                out.next_checks.append(f"(모델 제안) {text}")
        return out
