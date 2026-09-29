"""Tempo HTTP API 읽기 전용 최소 클라이언트 (탐색용).

트레이스 검색·조회는 Service Agent 단계(9단계)에서 추가합니다.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from infra_agent.config.settings import HttpDatasourceConfig
from infra_agent.datasources.errors import HttpStatusError, ResponseFormatError
from infra_agent.datasources.http import HttpDataSource

SOURCE = "tempo"
ALLOWED_PATHS = ("/ready", "/api/")


class TempoClient:
    def __init__(self, http: HttpDataSource) -> None:
        self._http = http

    @classmethod
    def from_config(
        cls,
        config: HttpDatasourceConfig,
        *,
        max_retries: int = 2,
        environ: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> TempoClient:
        return cls(
            HttpDataSource(
                SOURCE,
                config,
                allowed_path_prefixes=ALLOWED_PATHS,
                max_retries=max_retries,
                environ=environ,
                transport=transport,
            )
        )

    async def __aenter__(self) -> TempoClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._http.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def ready(self) -> bool:
        return (await self._http.get_raw("/ready")).status_code == 200

    async def buildinfo(self) -> dict[str, Any]:
        body = await self._http.get_json("/api/status/buildinfo")
        return body if isinstance(body, dict) else {}

    async def tag_names(self) -> dict[str, list[str]]:
        """범위(scope)별 태그 이름. v2 API가 없으면 v1 API로 대체하고 범위를 `all`로 둡니다."""
        try:
            body = await self._http.get_json("/api/v2/search/tags")
        except HttpStatusError as exc:
            if exc.status_code != 404:
                raise
            body = None
        if isinstance(body, dict) and isinstance(body.get("scopes"), list):
            result: dict[str, list[str]] = {}
            for scope in body["scopes"]:
                if isinstance(scope, dict):
                    tags = scope.get("tags") or []
                    result[str(scope.get("name", "unknown"))] = sorted(str(t) for t in tags)
            return result
        body = await self._http.get_json("/api/search/tags")
        if not isinstance(body, dict) or not isinstance(body.get("tagNames"), list):
            raise ResponseFormatError(SOURCE, "태그 이름 응답 형식이 올바르지 않습니다")
        return {"all": sorted(str(t) for t in body["tagNames"])}
