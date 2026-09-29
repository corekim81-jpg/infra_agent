"""읽기 전용 HTTP 데이터 소스 기반.

- GET 요청만 보냅니다. 상태를 바꾸는 요청을 보낼 수 있는 메서드는 제공하지 않습니다.
- 클라이언트마다 허용된 경로 접두어만 호출할 수 있습니다.
- 일시 오류(연결 실패, 타임아웃, 429, 5xx)만 제한 횟수 안에서 재시도합니다.
- 인증 토큰은 설정의 `token_env`가 가리키는 환경 변수에서 읽고, 마스킹 대상으로 등록합니다.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from infra_agent import __version__
from infra_agent.config.settings import HttpDatasourceConfig
from infra_agent.datasources.errors import (
    ConnectFailedError,
    DataSourceError,
    DataSourceTimeoutError,
    HttpStatusError,
    MissingCredentialError,
    QueryError,
    ResponseFormatError,
)
from infra_agent.security import Redactor, default_redactor

QueryParams = Sequence[tuple[str, str]]
SleepFn = Callable[[float], Awaitable[None]]

_MAX_ERROR_BODY = 300


@dataclass(frozen=True)
class RawResponse:
    status_code: int
    text: str
    elapsed_ms: int


class HttpDataSource:
    """허용 경로에 대해 GET만 수행하는 비동기 HTTP 클라이언트."""

    def __init__(
        self,
        name: str,
        config: HttpDatasourceConfig,
        *,
        allowed_path_prefixes: Sequence[str],
        max_retries: int = 2,
        backoff_seconds: float = 0.5,
        environ: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        redactor: Redactor | None = None,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        if config.url is None:
            raise ValueError(f"{name}: url이 설정되지 않았습니다")
        self.name = name
        self.base_url = config.url
        self._allowed = tuple(allowed_path_prefixes)
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._sleep = sleep
        self._redactor = redactor or default_redactor

        headers = {"User-Agent": f"infra-agent/{__version__}", "Accept": "application/json"}
        if config.token_env:
            env = os.environ if environ is None else environ
            token = env.get(config.token_env)
            if not token:
                raise MissingCredentialError(
                    name, f"토큰 환경 변수 {config.token_env}가 설정되지 않았습니다"
                )
            self._redactor.register(token)
            headers["Authorization"] = f"Bearer {token}"

        self._client = httpx.AsyncClient(
            base_url=config.url,
            headers=headers,
            timeout=httpx.Timeout(config.timeout_seconds),
            transport=transport,
            follow_redirects=False,
        )

    async def __aenter__(self) -> HttpDataSource:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _check_path(self, path: str) -> None:
        if not path.startswith("/") or ".." in path:
            raise ValueError(f"허용되지 않는 경로 형식입니다: {path!r}")
        if not any(path.startswith(prefix) for prefix in self._allowed):
            raise ValueError(f"{self.name}: 허용되지 않은 경로입니다: {path!r}")

    async def get_raw(self, path: str, params: QueryParams = ()) -> RawResponse:
        """GET 요청을 보내고 상태 코드와 본문을 반환합니다. 일시 오류는 재시도합니다."""
        self._check_path(path)
        attempt = 0
        while True:
            try:
                return await self._get_once(path, params)
            except DataSourceError as exc:
                if not exc.retryable or attempt >= self._max_retries:
                    raise
                await self._sleep(self._backoff * (2**attempt))
                attempt += 1

    async def _get_once(self, path: str, params: QueryParams) -> RawResponse:
        started = time.perf_counter()
        try:
            response = await self._client.get(path, params=list(params))
        except httpx.TimeoutException as exc:
            raise DataSourceTimeoutError(self.name, f"요청 시간 초과: {path}") from exc
        except httpx.TransportError as exc:
            raise ConnectFailedError(
                self.name, f"연결 실패: {self.base_url} ({type(exc).__name__})"
            ) from exc
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code == 429 or response.status_code >= 500:
            raise HttpStatusError(self.name, response.status_code, _short(response.text))
        return RawResponse(response.status_code, response.text, elapsed_ms)

    async def get_json(self, path: str, params: QueryParams = ()) -> Any:
        """GET 요청의 JSON 본문을 반환합니다. 4xx는 조회 오류로 변환합니다."""
        raw = await self.get_raw(path, params)
        if raw.status_code >= 400:
            message = _error_message(raw.text)
            if raw.status_code in (400, 422) and message:
                raise QueryError(self.name, message)
            raise HttpStatusError(self.name, raw.status_code, message or _short(raw.text))
        try:
            return json.loads(raw.text)
        except ValueError as exc:
            raise ResponseFormatError(self.name, f"JSON이 아닌 응답입니다: {path}") from exc


def _short(text: str) -> str:
    text = text.strip().replace("\n", " ")
    return text[:_MAX_ERROR_BODY]


def _error_message(text: str) -> str:
    try:
        body = json.loads(text)
    except ValueError:
        return _short(text)
    if isinstance(body, dict):
        for key in ("error", "message"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return _short(value)
    return _short(text)
