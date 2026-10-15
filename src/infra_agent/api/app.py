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
- `api.mcp_enabled`이면 MCP 서버를 `/mcp`로 함께 제공합니다(같은 토큰 필요, `mcp_server.py`).
"""

from __future__ import annotations

import hmac
import os
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from infra_agent import __version__
from infra_agent.catalog import Catalog
from infra_agent.config.settings import Settings
from infra_agent.datasources.probe import check_sources
from infra_agent.orchestration.runner import answer_question
from infra_agent.security import default_redactor
from infra_agent.service import (
    AnalysisTimeoutError,
    AnswerFn,
    AskRequest,
    AskService,
    BusyError,
    CheckFn,
    InternalError,
    QuestionTooLongError,
    ServiceError,
)


class ApiConfigError(Exception):
    """API를 시작할 수 없는 설정 (토큰 없음, Prometheus 비활성 등)."""


def _http_error(exc: ServiceError) -> HTTPException:
    if isinstance(exc, QuestionTooLongError):
        return HTTPException(status_code=413, detail=exc.message)
    if isinstance(exc, BusyError):
        return HTTPException(
            status_code=429,
            detail=exc.message,
            headers={"Retry-After": str(exc.retry_after_seconds)},
        )
    if isinstance(exc, AnalysisTimeoutError):
        return HTTPException(status_code=504, detail=exc.message)
    return HTTPException(status_code=500, detail=exc.message)


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

    service = AskService(settings, catalog, answer=answer, check=check)
    mcp_app, lifespan = _mcp_mount(service, token) if cfg.mcp_enabled else (None, None)
    app = FastAPI(
        title="infra-agent",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

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
        return JSONResponse(await service.check())

    @app.post("/v1/ask", dependencies=[Depends(require_token)])
    async def v1_ask(req: AskRequest) -> JSONResponse:
        try:
            bundle = await service.ask(req)
        except InternalError as exc:
            return JSONResponse(
                {"error": "internal_error", "error_id": exc.error_id}, status_code=500
            )
        except ServiceError as exc:
            raise _http_error(exc) from None
        return JSONResponse(service.payload(bundle, include_text=req.include_text))

    if mcp_app is not None:
        # 위에서 정의한 경로에 맞지 않는 요청만 여기로 옵니다 (MCP는 /mcp).
        app.mount("/", mcp_app)
    return app


class _BearerGate:
    """마운트한 ASGI 앱 앞에서 Bearer 토큰을 확인합니다 (토큰이 없으면 401)."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        self._token = token.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        header = dict(scope.get("headers") or []).get(b"authorization", b"")
        scheme, _, given = header.partition(b" ")
        if scheme.lower() != b"bearer" or not hmac.compare_digest(given.strip(), self._token):
            response = JSONResponse(
                {"detail": "인증 토큰이 필요합니다"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


def _mcp_mount(
    service: AskService, token: str | None
) -> tuple[ASGIApp, Callable[[FastAPI], AbstractAsyncContextManager[None]]]:
    """MCP 서버(streamable HTTP, `/mcp`)를 만들고 FastAPI 수명 주기에 연결할 함수를 돌려줍니다."""
    try:
        from mcp.server.transport_security import TransportSecuritySettings

        from infra_agent.mcp_server import create_mcp_server
    except ImportError:
        raise ApiConfigError(
            'api.mcp_enabled에는 MCP 의존성이 필요합니다. python -m pip install -e ".[mcp]"'
        ) from None
    server = create_mcp_server(service)
    # 토큰으로 보호할 때는 클러스터 서비스 이름 등 임의의 Host로 접속하므로 Host 검사를 끕니다.
    # 익명 허용(로컬 개발)일 때는 SDK 기본값(로컬 주소만 허용)을 유지합니다.
    security = (
        TransportSecuritySettings(enable_dns_rebinding_protection=False)
        if token is not None
        else None
    )
    inner: ASGIApp = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=security,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with server.session_manager.run():
            yield

    return (_BearerGate(inner, token) if token is not None else inner), lifespan
