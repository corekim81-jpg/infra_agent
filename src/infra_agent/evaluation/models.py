"""평가 세트 형식 (`config/eval/questions.yaml`).

질문마다 답변이 지켜야 할 기대값을 둡니다. 기대값은 결정적으로 확인할 수 있는 것만 적습니다
(에이전트 선택, 의도, 시간 범위, 데이터 소스 등). 답변 문장의 품질은 사람이 검토합니다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from infra_agent.schemas import AgentName, DataSourceKind, Intent

PRODUCIBLE_INTENTS = frozenset({Intent.STATUS, Intent.COMPARE, Intent.ANOMALY})
"""질문 해석(규칙 기반·모델)이 실제로 만드는 의도. 평가 세트는 이 안에서만 허용합니다."""


class EvalSetError(ValueError):
    """평가 세트 파일을 읽거나 검증하지 못했습니다."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Expectation(_Strict):
    agents: frozenset[AgentName] = Field(min_length=1)
    """실행되어야 하는 에이전트 (정확히 이 집합. 단순 질문을 넓은 분석으로 확장하지 않는지 확인)."""
    after: dict[AgentName, AgentName] = Field(default_factory=dict)
    """선행 관계: {에이전트: 먼저 실행되어야 하는 에이전트}."""
    intents: frozenset[Intent] = Field(min_length=1)
    """허용하는 의도 (규칙 기반·모델 해석이 다르게 볼 수 있는 경우 여러 개)."""
    range_minutes: int | None = Field(default=None, ge=1)
    """분석 구간 길이(분). 질문에 시간이 있거나 기본값을 확인할 때."""
    baseline: bool | None = None
    """직전 구간(기준 구간) 비교가 있어야 하는지."""
    sources: frozenset[DataSourceKind] = frozenset()
    """근거에 있어야 하는 데이터 소스."""
    cross_check: bool = False
    """분야 간 교차 확인이 있어야 하는지."""

    @model_validator(mode="after")
    def _after_agents(self) -> Self:
        unknown = self.intents - PRODUCIBLE_INTENTS
        if unknown:
            raise ValueError(
                "질문 해석이 만들지 않는 의도입니다: " + ", ".join(sorted(i.value for i in unknown))
            )
        for agent, before in self.after.items():
            if agent not in self.agents or before not in self.agents:
                raise ValueError(f"after의 에이전트는 agents에 있어야 합니다: {agent} ← {before}")
        return self


class EvalQuestion(_Strict):
    id: str = Field(pattern=r"^[a-z0-9_-]{1,40}$")
    question: str = Field(min_length=1)
    expect: Expectation
    note: str | None = None


class EvalSet(_Strict):
    version: int = Field(ge=1, le=1)
    questions: tuple[EvalQuestion, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        ids = [q.id for q in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError("질문 id가 중복되었습니다")
        return self


def load_eval_set(path: str | Path) -> EvalSet:
    file = Path(path)
    if not file.is_file():
        raise EvalSetError(f"평가 세트 파일을 찾을 수 없습니다: {file}")
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise EvalSetError(f"평가 세트 YAML 구문 오류: {file}: {exc}") from exc
    try:
        return EvalSet.model_validate(data)
    except ValidationError as exc:
        lines = [
            f"  - {'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}"
            for e in exc.errors()
        ]
        raise EvalSetError(f"평가 세트 형식 오류: {file}\n" + "\n".join(lines)) from exc
