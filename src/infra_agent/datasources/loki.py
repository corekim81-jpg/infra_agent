"""Loki HTTP API 읽기 전용 최소 클라이언트 (탐색용).

로그 조회(query_range)는 Service Agent 단계(9단계)에서 추가합니다.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from infra_agent.config.settings import HttpDatasourceConfig
from infra_agent.datasources.errors import QueryError, ResponseFormatError
from infra_agent.datasources.http import HttpDataSource, QueryParams
from infra_agent.datasources.prometheus import is_valid_label_name
from infra_agent.timeutil import ensure_utc

SOURCE = "loki"
ALLOWED_PATHS = ("/ready", "/loki/api/v1/")


def _ns(value: datetime) -> str:
    return str(int(ensure_utc(value).timestamp() * 1_000_000_000))


class LokiClient:
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
    ) -> LokiClient:
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

    async def __aenter__(self) -> LokiClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._http.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def ready(self) -> bool:
        return (await self._http.get_raw("/ready")).status_code == 200

    async def buildinfo(self) -> dict[str, Any]:
        body = await self._http.get_json("/loki/api/v1/status/buildinfo")
        return body if isinstance(body, dict) else {}

    async def _list(self, path: str, params: QueryParams) -> list[str]:
        body = await self._http.get_json(path, params)
        if not isinstance(body, dict):
            raise ResponseFormatError(SOURCE, "응답 최상위가 객체가 아닙니다")
        if body.get("status") != "success":
            raise QueryError(SOURCE, str(body.get("error") or "status가 success가 아닙니다"))
        data = body.get("data") or []
        if not isinstance(data, list):
            raise ResponseFormatError(SOURCE, "목록 형식이 올바르지 않습니다")
        return sorted(str(x) for x in data)

    async def label_names(self, start: datetime, end: datetime) -> list[str]:
        return await self._list("/loki/api/v1/labels", [("start", _ns(start)), ("end", _ns(end))])

    async def label_values(self, name: str, start: datetime, end: datetime) -> list[str]:
        if not is_valid_label_name(name):
            raise ValueError(f"라벨 이름 형식이 올바르지 않습니다: {name!r}")
        return await self._list(
            f"/loki/api/v1/label/{quote(name)}/values",
            [("start", _ns(start)), ("end", _ns(end))],
        )
