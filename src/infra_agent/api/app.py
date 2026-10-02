"""읽기 전용 HTTP API.

- `POST /v1/ask`: 질문 한 건을 분석해 답변을 돌려줍니다 (CLI `ask`와 같은 흐름, 조회만 수행).
- `GET /v1/check`: 데이터 소스 연결 상태 (CLI `check`와 같음).
- `GET /healthz`, `GET /readyz`: 프로세스 상태 확인용 (인증 없음, 내부 정보 없음).

보안:
- `/v1/*`는 Bearer 토큰이 필요합니다. 토큰은 환경 변수(`api.token_env`)로만 받고, 없으면
  `api.allow_anonymous`를 명시하지 않는 한 시작하지 않습니다.
- 동시 처리 수(`api.max_concurrent_requests`)와 질문 길이(`api.max_question_chars`)를 제한합니다.
- 응답은 값 단위로 마스킹하고, 내부 오류 내용은 응답에 넣지 않습니다(요청 ID만 돌려줌).
- API 문서·스키마 엔드포인트(/docs, /openapi.json)는 열지 않습니다.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from infra_agent import __version__
from infra_agent.answer.payload import answer_payload
from infra_agent.answer.render import render_text
from infra_agent.catalog import Catalog
from infra_agent.config.settings import Settings
from infra_agent.datasources.probe import SourceStatus, check_sources
from infra_agent.orchestration.runner import AnswerBundle, answer_question
from infra_agent.schemas import AgentStatus, TargetKind
from infra_agent.security import default_redactor, redact, redact_values
from infra_agent.timeutil import parse_duration

logger = logging.getLogger(__name__)

AnswerFn = Callable[..., Awaitable[AnswerBundle]]
CheckFn = Callable[[Settings], Awaitable[list[SourceStatus]]]

_TARGET_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,251}[A-Za-z0-9])?$")
REQUEST_MARGIN_SECONDS = 5.0
"""요청 제한 시간(`execution.request_timeout_seconds`)에 더하는 여유 (종합·응답 작성)."""


class ApiConfigError(Exception):
    """API를 시작할 수 없는 설정 (토큰 없음, Prometheus 비활성 등)."""


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


def _overall_status(bundle: AnswerBundle) -> str:
    statuses = {r.status for r in bundle.results}
    if not statuses or statuses <= {AgentStatus.FAILED, AgentStatus.SKIPPED}:
        return "failed"
    return "ok" if statuses == {AgentStatus.SUCCESS} else "partial"


def create_app(
    settings: Settings,
    catalog: Catalog,
    *,
    environ: Mapping[str, str] | None = None,
    answer: AnswerFn = answer_question,
    check: CheckFn = check_sources,
) -> FastAPI:
    """설정과 카탈로그로 API 앱을 만듭니다. 시작할 수 없는 설정이면 ApiConfigError."""
    cfg = settings.api
    env = os.environ if environ is None else environ
    token = env.get(cfg.token_env) or None
    if token is None and not cfg.allow_anonymous:
        raise ApiConfigError(
            f"API 토큰 환경 변수 {cfg.token_env}가 설정되지 않았습니다. 토큰 없이 질문을 받으려면 "
            "api.allow_anonymous: true를 명시하세요(로컬 개발 전용)."
        )
    if not settings.datasources.prometheus.enabled:
        raise ApiConfigError("Prometheus가 비활성화되어 있어 질문에 답할 수 없습니다")
    if token is not None:
        default_redactor.register(token)

    app = FastAPI(
        title="infra-agent",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    active = 0

    def require_token(authorization: str | None = Header(default=None)) -> None:
        if token is None:
            return
        scheme, _, given = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            given.strip().encode(), token.encode()
        ):
            raise HTTPException(
                status_code=401,
                detail="인증 토큰이 필요합니다",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        # 데이터 소스 일부가 내려가도 확인한 범위로 답해야 하므로, 준비 상태는 앱 초기화만 봅니다.
        return {"status": "ready", "version": __version__}

    @app.get("/v1/check", dependencies=[Depends(require_token)])
    async def v1_check() -> JSONResponse:
        statuses = await check(settings)
        enabled = [s for s in statuses if s.enabled]
        ok = bool(enabled) and all(s.reachable and s.ready for s in enabled)
        body = {
            "ok": ok,
            "profile": settings.profile.value,
            "sources": [s.model_dump(mode="json") for s in statuses],
        }
        return JSONResponse(redact_values(body))

    @app.post("/v1/ask", dependencies=[Depends(require_token)])
    async def v1_ask(req: AskRequest) -> JSONResponse:
        nonlocal active
        if len(req.question) > cfg.max_question_chars:
            raise HTTPException(
                status_code=413, detail=f"질문이 너무 깁니다 (최대 {cfg.max_question_chars}자)"
            )
        if active >= cfg.max_concurrent_requests:
            raise HTTPException(
                status_code=429,
                detail="처리 중인 질문이 많습니다. 잠시 후 다시 시도하세요",
                headers={"Retry-After": "5"},
            )
        overrides = {
            kind: value
            for kind, value in (
                (TargetKind.NAMESPACE, req.namespace),
                (TargetKind.NODE, req.node),
                (TargetKind.POD, req.pod),
            )
            if value
        }
        active += 1
        try:
            bundle = await asyncio.wait_for(
                answer(
                    req.question,
                    settings,
                    catalog,
                    range_override=req.range,
                    target_overrides=overrides,
                    use_llm=req.use_llm,
                ),
                timeout=settings.execution.request_timeout_seconds + REQUEST_MARGIN_SECONDS,
            )
        except TimeoutError:
            raise HTTPException(status_code=504, detail="분석 제한 시간을 넘었습니다") from None
        except Exception:
            # 내부 오류 내용(주소·조회식 등)은 응답에 넣지 않고, 로그에서 찾을 수 있는 ID만 줍니다.
            error_id = uuid.uuid4().hex[:12]
            logger.exception("ask 처리 실패 (error_id=%s)", error_id)
            return JSONResponse({"error": "internal_error", "error_id": error_id}, status_code=500)
        finally:
            active -= 1
        payload: dict[str, Any] = {"status": _overall_status(bundle), **answer_payload(bundle)}
        if req.include_text:
            payload["text"] = redact(render_text(bundle))
        return JSONResponse(redact_values(payload))

    return app
