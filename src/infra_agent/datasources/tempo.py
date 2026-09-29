"""Tempo HTTP API 읽기 전용 클라이언트.

- 탐색: 태그 이름
- 조회(9단계): TraceQL 검색(`search`, 건수 제한). 트레이스 전체 조회는 아직 하지 않습니다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from infra_agent.config.settings import HttpDatasourceConfig
from infra_agent.datasources.errors import HttpStatusError, ResponseFormatError
from infra_agent.datasources.http import HttpDataSource
from infra_agent.timeutil import ensure_utc

SOURCE = "tempo"
ALLOWED_PATHS = ("/ready", "/api/")
MAX_SEARCH_LIMIT = 50


@dataclass(frozen=True)
class TraceSummary:
    trace_id: str
    root_service: str | None
    root_name: str | None
    start: datetime | None
    duration_ms: float | None


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

    async def search(
        self, traceql: str, start: datetime, end: datetime, limit: int
    ) -> list[TraceSummary]:
        """TraceQL 검색. 건수는 `MAX_SEARCH_LIMIT`을 넘지 않습니다."""
        limit = max(1, min(limit, MAX_SEARCH_LIMIT))
        params = [
            ("q", traceql),
            ("start", str(int(ensure_utc(start).timestamp()))),
            ("end", str(int(ensure_utc(end).timestamp()))),
            ("limit", str(limit)),
        ]
        body = await self._http.get_json("/api/search", params)
        if not isinstance(body, dict):
            raise ResponseFormatError(SOURCE, "검색 응답 형식이 올바르지 않습니다")
        traces = body.get("traces") or []
        if not isinstance(traces, list):
            raise ResponseFormatError(SOURCE, "traces 형식이 올바르지 않습니다")
        out: list[TraceSummary] = []
        for item in traces[:limit]:
            if not isinstance(item, dict) or not item.get("traceID"):
                raise ResponseFormatError(SOURCE, "트레이스 항목 형식이 올바르지 않습니다")
            started: datetime | None = None
            if item.get("startTimeUnixNano"):
                try:
                    started = datetime.fromtimestamp(
                        int(item["startTimeUnixNano"]) / 1_000_000_000, tz=UTC
                    )
                except (TypeError, ValueError):
                    started = None
            duration = item.get("durationMs")
            out.append(
                TraceSummary(
                    trace_id=str(item["traceID"]),
                    root_service=str(item["rootServiceName"])
                    if item.get("rootServiceName")
                    else None,
                    root_name=str(item["rootTraceName"]) if item.get("rootTraceName") else None,
                    start=started,
                    duration_ms=float(duration) if isinstance(duration, int | float) else None,
                )
            )
        return out
