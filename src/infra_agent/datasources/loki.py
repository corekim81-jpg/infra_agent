"""Loki HTTP API 읽기 전용 클라이언트.

- 탐색: 라벨 이름·값
- 조회(9단계): 지표형 LogQL 순간 조회(`query`), 로그 조회(`query_logs`, 건수 제한)
- 로그 본문은 외부 데이터입니다. 이 계층은 형식만 검증하고, 마스킹·길이 제한·지시 무시는
  도구·에이전트 계층에서 적용합니다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
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


MAX_LOG_LIMIT = 100
"""로그 조회 건수 상한 (요청 인자와 무관하게 적용)."""


def _ns(value: datetime) -> str:
    return str(int(ensure_utc(value).timestamp() * 1_000_000_000))


@dataclass(frozen=True)
class LogSample:
    labels: dict[str, str]
    value: float


@dataclass(frozen=True)
class LogEntry:
    time: datetime
    labels: dict[str, str]
    """스트림 라벨과 구조화 메타데이터(trace_id 등)를 합친 값."""
    line: str


def _str_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ResponseFormatError(SOURCE, "라벨 형식이 올바르지 않습니다")
    return {str(k): str(v) for k, v in value.items()}


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

    # ------------------------------------------------------------ 조회

    async def _data(self, path: str, params: QueryParams) -> dict[str, Any]:
        body = await self._http.get_json(path, params)
        if not isinstance(body, dict):
            raise ResponseFormatError(SOURCE, "응답 최상위가 객체가 아닙니다")
        if body.get("status") != "success":
            raise QueryError(SOURCE, str(body.get("error") or "status가 success가 아닙니다"))
        data = body.get("data")
        if not isinstance(data, dict):
            raise ResponseFormatError(SOURCE, "data 형식이 올바르지 않습니다")
        return data

    async def query(self, expr: str, at: datetime) -> list[LogSample]:
        """지표형 LogQL(`count_over_time` 등)의 순간 조회. 결과는 vector여야 합니다."""
        data = await self._data("/loki/api/v1/query", [("query", expr), ("time", _ns(at))])
        if data.get("resultType") != "vector" or not isinstance(data.get("result"), list):
            raise ResponseFormatError(SOURCE, "지표형 조회 결과가 vector가 아닙니다")
        samples = []
        for item in data["result"]:
            value = item.get("value") if isinstance(item, dict) else None
            if not isinstance(value, list) or len(value) != 2:
                raise ResponseFormatError(SOURCE, "값 형식이 올바르지 않습니다")
            try:
                number = float(value[1])
            except (TypeError, ValueError) as exc:
                raise ResponseFormatError(SOURCE, "숫자로 변환할 수 없는 값입니다") from exc
            samples.append(LogSample(labels=_str_map(item.get("metric", {})), value=number))
        return samples

    async def query_logs(
        self, expr: str, start: datetime, end: datetime, limit: int
    ) -> list[LogEntry]:
        """로그 조회 (최신순). 건수는 `MAX_LOG_LIMIT`을 넘지 않습니다."""
        limit = max(1, min(limit, MAX_LOG_LIMIT))
        params = [
            ("query", expr),
            ("start", _ns(start)),
            ("end", _ns(end)),
            ("limit", str(limit)),
            ("direction", "backward"),
        ]
        data = await self._data("/loki/api/v1/query_range", params)
        if data.get("resultType") != "streams" or not isinstance(data.get("result"), list):
            raise ResponseFormatError(SOURCE, "로그 조회 결과가 streams가 아닙니다")
        entries: list[LogEntry] = []
        for stream in data["result"]:
            if not isinstance(stream, dict):
                raise ResponseFormatError(SOURCE, "스트림 형식이 올바르지 않습니다")
            labels = _str_map(stream.get("stream", {}))
            for value in stream.get("values") or []:
                if not isinstance(value, list) or len(value) < 2:
                    raise ResponseFormatError(SOURCE, "로그 값 형식이 올바르지 않습니다")
                merged = dict(labels)
                if len(value) >= 3 and isinstance(value[2], dict):
                    # categorize-labels 응답: 구조화 메타데이터가 세 번째 원소에 있음
                    for group in value[2].values():
                        if isinstance(group, dict):
                            merged.update({str(k): str(v) for k, v in group.items()})
                try:
                    ts = datetime.fromtimestamp(int(value[0]) / 1_000_000_000, tz=UTC)
                except (TypeError, ValueError) as exc:
                    raise ResponseFormatError(SOURCE, "로그 시각 형식이 올바르지 않습니다") from exc
                entries.append(LogEntry(time=ts, labels=merged, line=str(value[1])))
        entries.sort(key=lambda e: e.time, reverse=True)
        return entries[:limit]
