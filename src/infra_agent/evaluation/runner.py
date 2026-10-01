"""평가 세트 실행: 질문마다 실제 질문 처리 흐름(`answer_question`)을 돌리고 평가 기준을 적용합니다.

질문은 차례로 실행합니다(데이터 소스 부하와 모델 호출을 한 번에 몰지 않기 위함). 한 질문이 예외로
실패해도 나머지 질문은 계속 평가하고, 실패한 질문은 오류로 기록합니다.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import httpx

from infra_agent.catalog import Catalog
from infra_agent.config.settings import Settings
from infra_agent.evaluation.checks import CheckResult, CheckStatus, evaluate
from infra_agent.evaluation.models import EvalQuestion, EvalSet
from infra_agent.llm import LLMClient
from infra_agent.orchestration.runner import AnswerBundle, answer_question
from infra_agent.security import redact


@dataclass(frozen=True)
class QuestionResult:
    question: EvalQuestion
    checks: tuple[CheckResult, ...]
    elapsed_seconds: float
    bundle: AnswerBundle | None = None
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(c.status is not CheckStatus.FAIL for c in self.checks)

    def check(self, name: str) -> CheckResult | None:
        return next((c for c in self.checks if c.name == name), None)


def select(eval_set: EvalSet, only: Sequence[str] = ()) -> tuple[EvalQuestion, ...]:
    """`only`(질문 id, 또는 `_` 앞 번호 부분: q3)에 맞는 질문만 고릅니다. 비어 있으면 전체.

    `q1`은 `q1_…`에만 맞고 `q10_…`에는 맞지 않습니다.
    """
    if not only:
        return eval_set.questions
    chosen = tuple(
        q for q in eval_set.questions if any(q.id == o or q.id.startswith(f"{o}_") for o in only)
    )
    if not chosen:
        known = ", ".join(q.id for q in eval_set.questions)
        raise ValueError(f"평가 세트에 맞는 질문이 없습니다: {', '.join(only)} (질문 id: {known})")
    return chosen


async def run_eval(
    questions: Sequence[EvalQuestion],
    settings: Settings,
    catalog: Catalog,
    *,
    use_llm: bool = True,
    now: datetime | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    llm: LLMClient | None = None,
) -> tuple[QuestionResult, ...]:
    results: list[QuestionResult] = []
    for q in questions:
        started = time.monotonic()
        try:
            bundle = await answer_question(
                q.question,
                settings,
                catalog,
                now=now,
                transport=transport,
                llm=llm,
                use_llm=use_llm,
            )
        except Exception as exc:  # 한 질문의 실패가 평가 전체를 멈추지 않게 함
            results.append(
                QuestionResult(
                    q,
                    (),
                    time.monotonic() - started,
                    error=redact(f"{type(exc).__name__}: {exc}"),
                )
            )
            continue
        results.append(
            QuestionResult(q, evaluate(q, bundle, settings), time.monotonic() - started, bundle)
        )
    return tuple(results)
