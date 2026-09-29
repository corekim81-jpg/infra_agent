"""Prometheus HTTP API 읽기 전용 클라이언트.

지원 API: `/-/ready`, `/api/v1/status/{buildinfo,runtimeinfo}`, `/api/v1/label/__name__/values`,
`/api/v1/labels`, `/api/v1/metadata`, `/api/v1/query`, `/api/v1/query_range`.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from infra_agent.config.settings import HttpDatasourceConfig
from infra_agent.datasources.errors import QueryError, ResponseFormatError
from infra_agent.datasources.http import HttpDataSource, QueryParams
from infra_agent.timeutil import ensure_utc

SOURCE = "prometheus"
ALLOWED_PATHS = ("/-/ready", "/api/v1/")

_LABEL_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_METRIC_NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")


def is_valid_label_name(name: str) -> bool:
    return bool(_LABEL_NAME_RE.match(name))


def is_valid_metric_name(name: str) -> bool:
    return bool(_METRIC_NAME_RE.match(name))


def escape_label_value(value: str) -> str:
    """PromQL 문자열 리터럴용 이스케이프 (역슬래시, 큰따옴표, 줄바꿈)."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def build_selector(matchers: Mapping[str, str]) -> str:
    """`{"a": "x"}` → `a="x"` 형태의 등호 매처 목록을 만듭니다 (중괄호 제외)."""
    parts = []
    for name, value in matchers.items():
        if not is_valid_label_name(name):
            raise ValueError(f"라벨 이름 형식이 올바르지 않습니다: {name!r}")
        parts.append(f'{name}="{escape_label_value(value)}"')
    return ",".join(parts)


def _ts(value: datetime) -> str:
    return f"{ensure_utc(value).timestamp():.3f}"


@dataclass(frozen=True)
class Sample:
    labels: dict[str, str]
    value: float
    timestamp: float


@dataclass(frozen=True)
class RangeSeries:
    labels: dict[str, str]
    points: list[tuple[float, float]] = field(default_factory=list)


@dataclass(frozen=True)
class InstantResult:
    result_type: str
    samples: list[Sample]
    """vector는 시계열별 샘플, scalar는 라벨이 없는 샘플 1개."""


def _parse_value(pair: Any) -> tuple[float, float]:
    if not isinstance(pair, list) or len(pair) != 2:
        raise ResponseFormatError(SOURCE, "값 형식이 올바르지 않습니다")
    ts, raw = pair
    try:
        return float(ts), float(raw)
    except (TypeError, ValueError) as exc:
        raise ResponseFormatError(SOURCE, "숫자로 변환할 수 없는 값입니다") from exc


def _labels(item: Mapping[str, Any]) -> dict[str, str]:
    metric = item.get("metric", {})
    if not isinstance(metric, dict):
        raise ResponseFormatError(SOURCE, "metric 필드 형식이 올바르지 않습니다")
    return {str(k): str(v) for k, v in metric.items()}


class PrometheusClient:
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
    ) -> PrometheusClient:
        http = HttpDataSource(
            SOURCE,
            config,
            allowed_path_prefixes=ALLOWED_PATHS,
            max_retries=max_retries,
            environ=environ,
            transport=transport,
        )
        return cls(http)

    @property
    def base_url(self) -> str:
        return self._http.base_url

    async def __aenter__(self) -> PrometheusClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _data(self, path: str, params: QueryParams = ()) -> Any:
        body = await self._http.get_json(path, params)
        if not isinstance(body, dict):
            raise ResponseFormatError(SOURCE, "응답 최상위가 객체가 아닙니다")
        if body.get("status") != "success":
            raise QueryError(SOURCE, str(body.get("error") or "status가 success가 아닙니다"))
        return body.get("data")

    # ------------------------------------------------------------ 상태

    async def ready(self) -> bool:
        raw = await self._http.get_raw("/-/ready")
        return raw.status_code == 200

    async def buildinfo(self) -> dict[str, Any]:
        data = await self._data("/api/v1/status/buildinfo")
        return data if isinstance(data, dict) else {}

    async def runtimeinfo(self) -> dict[str, Any]:
        data = await self._data("/api/v1/status/runtimeinfo")
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------ 탐색

    async def metric_names(
        self, start: datetime | None = None, end: datetime | None = None
    ) -> list[str]:
        params: list[tuple[str, str]] = []
        if start is not None:
            params.append(("start", _ts(start)))
        if end is not None:
            params.append(("end", _ts(end)))
        data = await self._data("/api/v1/label/__name__/values", params)
        if not isinstance(data, list):
            raise ResponseFormatError(SOURCE, "지표 이름 목록 형식이 올바르지 않습니다")
        return sorted(str(x) for x in data)

    async def label_names(
        self,
        match: Sequence[str] = (),
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[str]:
        params: list[tuple[str, str]] = [("match[]", m) for m in match]
        if start is not None:
            params.append(("start", _ts(start)))
        if end is not None:
            params.append(("end", _ts(end)))
        data = await self._data("/api/v1/labels", params)
        if not isinstance(data, list):
            raise ResponseFormatError(SOURCE, "라벨 이름 목록 형식이 올바르지 않습니다")
        return sorted(str(x) for x in data)

    async def metadata(self, metric: str | None = None) -> dict[str, list[dict[str, str]]]:
        params: list[tuple[str, str]] = []
        if metric is not None:
            params.append(("metric", metric))
        data = await self._data("/api/v1/metadata", params)
        if not isinstance(data, dict):
            raise ResponseFormatError(SOURCE, "메타데이터 형식이 올바르지 않습니다")
        return {
            str(k): [dict(e) for e in v if isinstance(e, dict)]
            for k, v in data.items()
            if isinstance(v, list)
        }

    # ------------------------------------------------------------ 조회

    async def query(self, expr: str, time: datetime | None = None) -> InstantResult:
        params: list[tuple[str, str]] = [("query", expr)]
        if time is not None:
            params.append(("time", _ts(time)))
        data = await self._data("/api/v1/query", params)
        if not isinstance(data, dict):
            raise ResponseFormatError(SOURCE, "query 응답 형식이 올바르지 않습니다")
        result_type = str(data.get("resultType"))
        result = data.get("result")
        if result_type == "vector" and isinstance(result, list):
            samples = []
            for item in result:
                ts, val = _parse_value(item.get("value"))
                samples.append(Sample(labels=_labels(item), value=val, timestamp=ts))
            return InstantResult("vector", samples)
        if result_type == "scalar":
            ts, val = _parse_value(result)
            return InstantResult("scalar", [Sample(labels={}, value=val, timestamp=ts)])
        raise ResponseFormatError(SOURCE, f"지원하지 않는 결과 형식입니다: {result_type}")

    async def query_range(
        self, expr: str, start: datetime, end: datetime, step_seconds: int
    ) -> list[RangeSeries]:
        if step_seconds <= 0:
            raise ValueError("step_seconds는 0보다 커야 합니다")
        params = [
            ("query", expr),
            ("start", _ts(start)),
            ("end", _ts(end)),
            ("step", str(step_seconds)),
        ]
        data = await self._data("/api/v1/query_range", params)
        if not isinstance(data, dict) or data.get("resultType") != "matrix":
            raise ResponseFormatError(SOURCE, "query_range 응답 형식이 올바르지 않습니다")
        series = []
        for item in data.get("result", []):
            points = [_parse_value(v) for v in item.get("values", [])]
            series.append(RangeSeries(labels=_labels(item), points=points))
        return series

    async def scalar_or_none(self, expr: str, time: datetime | None = None) -> float | None:
        """결과가 없으면 None, 있으면 첫 샘플 값을 반환합니다 (NaN은 None)."""
        result = await self.query(expr, time)
        if not result.samples:
            return None
        value = result.samples[0].value
        return None if math.isnan(value) else value
