"""질문 처리 서비스 — HTTP API와 MCP 서버가 함께 쓰는 입력 검증·제한·오류 처리.

- 분석 흐름은 CLI `ask`와 같습니다(조회만 수행).
- 같은 프로세스에서 HTTP API와 MCP를 함께 제공하면 동시 처리 제한을 함께 셉니다.
- 내부 오류 내용(주소·조회식 등)은 호출자에게 넘기지 않고, 로그에서 찾을 수 있는 ID만 줍니다.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from infra_agent.answer.payload import answer_payload
from infra_agent.answer.render import render_text
from infra_agent.catalog import Catalog
from infra_agent.config.settings import Settings
from infra_agent.datasources.probe import SourceStatus, check_sources
from infra_agent.orchestration.runner import AnswerBundle, answer_question
from infra_agent.schemas import AgentStatus, TargetKind
from infra_agent.security import redact, redact_values
from infra_agent.timeutil import parse_duration

logger = logging.getLogger(__name__)

AnswerFn = Callable[..., Awaitable[AnswerBundle]]
CheckFn = Callable[[Settings], Awaitable[list[SourceStatus]]]

_TARGET_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,251}[A-Za-z0-9])?$")
REQUEST_MARGIN_SECONDS = 5.0
"""요청 제한 시간(`execution.request_timeout_seconds`)에 더하는 여유 (종합·응답 작성)."""


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)
    range: str | None = None
    """분석 구간 (예: 30m, 1h). 없으면 질문·기본값에서 정합니다."""
    namespace: str | None = None
    node: str | None = None
    pod: str | None = None
    use_llm: bool = True
    """설정에서 모델을 쓰도록 했을 때만 의미가 있습니다. false면 규칙·코드 판정만 합니다."""
    include_text: bool = True
    """사람이 읽는 답변 텍스트(`text`)를 함께 돌려줄지."""

    @field_validator("range")
    @classmethod
    def _check_range(cls, value: str | None) -> str | None:
        if value is not None:
            parse_duration(value)
        return value

    @field_validator("namespace", "node", "pod")
    @classmethod
    def _check_target(cls, value: str | None) -> str | None:
        if value is not None and not _TARGET_RE.match(value):
            raise ValueError("대상 이름 형식이 올바르지 않습니다")
        return value


class ServiceError(Exception):
    """호출자에게 그대로 보여도 되는 서비스 오류."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class QuestionTooLongError(ServiceError):
    pass


class BusyError(ServiceError):
    retry_after_seconds = 5


class AnalysisTimeoutError(ServiceError):
    pass


class InternalError(ServiceError):
    def __init__(self, error_id: str) -> None:
        self.error_id = error_id
        super().__init__(f"내부 오류가 발생했습니다 (error_id={error_id})")


def overall_status(bundle: AnswerBundle) -> str:
    statuses = {r.status for r in bundle.results}
    if not statuses or statuses <= {AgentStatus.FAILED, AgentStatus.SKIPPED}:
        return "failed"
    return "ok" if statuses == {AgentStatus.SUCCESS} else "partial"


class AskService:
    def __init__(
        self,
        settings: Settings,
        catalog: Catalog,
        *,
        answer: AnswerFn = answer_question,
        check: CheckFn = check_sources,
    ) -> None:
        self.settings = settings
        self._catalog = catalog
        self._answer = answer
        self._check = check
        self._active = 0

    async def ask(self, req: AskRequest) -> AnswerBundle:
        """질문을 분석합니다. 제한을 넘거나 실패하면 `ServiceError` 계열 예외."""
        cfg = self.settings.api
        if len(req.question) > cfg.max_question_chars:
            raise QuestionTooLongError(f"질문이 너무 깁니다 (최대 {cfg.max_question_chars}자)")
        if self._active >= cfg.max_concurrent_requests:
            raise BusyError("처리 중인 질문이 많습니다. 잠시 후 다시 시도하세요")
        overrides = {
            kind: value
            for kind, value in (
                (TargetKind.NAMESPACE, req.namespace),
                (TargetKind.NODE, req.node),
                (TargetKind.POD, req.pod),
            )
            if value
        }
        self._active += 1
        try:
            return await asyncio.wait_for(
                self._answer(
                    req.question,
                    self.settings,
                    self._catalog,
                    range_override=req.range,
                    target_overrides=overrides,
                    use_llm=req.use_llm,
                ),
                timeout=self.settings.execution.request_timeout_seconds + REQUEST_MARGIN_SECONDS,
            )
        except TimeoutError:
            raise AnalysisTimeoutError("분석 제한 시간을 넘었습니다") from None
        except Exception:
            error_id = uuid.uuid4().hex[:12]
            logger.exception("ask 처리 실패 (error_id=%s)", error_id)
            raise InternalError(error_id) from None
        finally:
            self._active -= 1

    def payload(self, bundle: AnswerBundle, *, include_text: bool = True) -> dict[str, Any]:
        """마스킹한 답변 JSON (CLI `ask --json` 구조 + status, text)."""
        payload: dict[str, Any] = {"status": overall_status(bundle), **answer_payload(bundle)}
        if include_text:
            payload["text"] = self.text(bundle)
        masked: dict[str, Any] = redact_values(payload)
        return masked

    @staticmethod
    def text(bundle: AnswerBundle) -> str:
        return redact(render_text(bundle))

    async def check(self) -> dict[str, Any]:
        statuses = await self._check(self.settings)
        enabled = [s for s in statuses if s.enabled]
        body = {
            "ok": bool(enabled) and all(s.reachable and s.ready for s in enabled),
            "profile": self.settings.profile.value,
            "sources": [s.model_dump(mode="json") for s in statuses],
        }
        masked: dict[str, Any] = redact_values(body)
        return masked
