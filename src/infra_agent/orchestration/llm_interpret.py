"""모델 기반 질문 해석 (Coordinator 해석부).

- 모델에는 질문 문장만 보냅니다(모든 data_policy에서 허용되는 범위).
- 모델 출력은 스키마로 검증한 뒤 규칙 기반 해석과 같은 `finalize()`로 보정합니다.
- 모델 호출·검증이 실패하면 규칙 기반 해석으로 대체하고 그 사실을 `method_note`에 표시합니다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from infra_agent.agents.prompts import COORDINATOR_INTERPRET_PROMPT
from infra_agent.llm.base import LLMClient, LLMError, LLMRequest
from infra_agent.orchestration.rules import (
    KNOWN_DOMAINS,
    TARGET_NAME_RE,
    Interpretation,
    finalize,
    interpret,
)
from infra_agent.schemas import Intent, TargetKind

PURPOSE = "interpret_question"

SYSTEM_PROMPT = COORDINATOR_INTERPRET_PROMPT

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "intent": {"type": "string", "enum": ["status", "compare", "anomaly"]},
        "duration_minutes": {"type": ["integer", "null"], "minimum": 1, "maximum": 43200},
        "namespace": {"type": ["string", "null"]},
        "node": {"type": ["string", "null"]},
        "pod": {"type": ["string", "null"]},
        "domains": {
            "type": "array",
            "items": {"type": "string", "enum": sorted(KNOWN_DOMAINS)},
        },
    },
    "required": ["intent", "duration_minutes", "namespace", "node", "pod", "domains"],
}


class _ModelInterpretation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Literal["status", "compare", "anomaly"]
    duration_minutes: int | None = Field(default=None, ge=1, le=43200)
    namespace: str | None = Field(default=None, max_length=253)
    node: str | None = Field(default=None, max_length=253)
    pod: str | None = Field(default=None, max_length=253)
    domains: list[str] = Field(default_factory=list, max_length=5)


async def interpret_with_model(
    question: str,
    llm: LLMClient,
    now: datetime,
    default_range: timedelta,
    *,
    range_override: timedelta | None = None,
    target_overrides: Mapping[TargetKind, str] | None = None,
) -> Interpretation:
    try:
        response = await llm.complete(
            LLMRequest(purpose=PURPOSE, system=SYSTEM_PROMPT, prompt=question, schema=SCHEMA)
        )
        parsed = _ModelInterpretation.model_validate(response.data or {})
    except (LLMError, ValidationError) as exc:
        reason = exc.message if isinstance(exc, LLMError) else "모델 출력 형식 오류"
        fallback = interpret(
            question,
            now,
            default_range,
            range_override=range_override,
            target_overrides=target_overrides,
        )
        return replace(fallback, method_note=f"모델 해석 실패: {reason}")

    targets: dict[TargetKind, str] = {}
    for kind, value in (
        (TargetKind.NAMESPACE, parsed.namespace),
        (TargetKind.NODE, parsed.node),
        (TargetKind.POD, parsed.pod),
    ):
        if value:
            targets[kind] = value.strip().lower()
    dropped = [v for v in targets.values() if not _looks_like_name(v)]
    notes: list[str] = []
    if dropped:
        notes.append(f"모델이 제시한 대상 중 이름 형식이 아닌 값 제외: {', '.join(dropped)}")
    return finalize(
        intent=Intent(parsed.intent),
        duration=timedelta(minutes=parsed.duration_minutes) if parsed.duration_minutes else None,
        targets=targets,
        domains=set(parsed.domains),
        now=now,
        default_range=default_range,
        range_override=range_override,
        target_overrides=target_overrides,
        assumptions=notes,
        method="model",
    )


def _looks_like_name(value: str) -> bool:
    return bool(TARGET_NAME_RE.match(value))
