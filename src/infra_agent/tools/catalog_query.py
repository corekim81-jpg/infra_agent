"""읽기 전용 카탈로그 조회 도구.

- 에이전트는 카탈로그에서 **자기 분야(agent)로 지정된 항목만** 실행할 수 있습니다(코드에서 강제).
- 조회 결과는 항상 `ToolResult`(근거)로 반환하며,
  조회 오류도 예외 대신 오류 상태의 근거로 돌려줍니다.
- 요청당 조회 호출 수(`ToolBudget`)와 호출별 제한 시간을 적용합니다.

조회 방식(`QueryMode`):
- `current`: 분석 구간 끝 시각의 순간값
- `window_avg`: 분석 구간 평균 `avg_over_time((expr)[구간:step])`
- `baseline_avg`: 기준 구간 평균 (같은 방식, 기준 구간 끝 시각에서 실행)
- `window`: `{range}`를 분석 구간 길이로 채워 구간 끝 시각에서 실행 (구간 내 증가량 등)

조건 조회(문제 대상만 결과로 나오는 항목)의 빈 결과는 "해당 없음"일 수도, 데이터가 없는 것일 수도
있습니다. 호출자는 최신성(`freshness_seconds`)과, 대상 필터가 있으면 `coverage()`로 대상 시계열
존재를 확인한 뒤에만 "해당 없음"으로 판단해야 합니다.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from infra_agent.catalog import Catalog, CatalogItem
from infra_agent.datasources.errors import DataSourceError
from infra_agent.datasources.prometheus import PrometheusClient, escape_label_value
from infra_agent.schemas import (
    AgentName,
    AnalysisContext,
    DataSourceKind,
    TargetKind,
    TimeRange,
    ToolResult,
    ToolStatus,
)
from infra_agent.timeutil import utc_now

RATE_RANGE = "5m"
"""rate/increase 계열 항목의 순간값 계산 구간."""


class QueryMode(StrEnum):
    CURRENT = "current"
    WINDOW_AVG = "window_avg"
    BASELINE_AVG = "baseline_avg"
    WINDOW = "window"


class ToolPermissionError(Exception):
    """에이전트가 허용되지 않은 카탈로그 항목을 실행하려 한 경우 (프로그래밍 오류)."""


@dataclass
class ToolBudget:
    """요청 단위 조회 호출 예산. 여러 에이전트가 공유합니다."""

    max_calls: int
    used: int = 0

    def take(self) -> bool:
        if self.used >= self.max_calls:
            return False
        self.used += 1
        return True


@dataclass
class QueryOutcome:
    result: ToolResult
    unsupported_targets: list[TargetKind] = field(default_factory=list)
    skipped: bool = False
    """대상 필터를 적용할 수 없어 조회하지 않은 경우."""


def _seconds(value: float) -> str:
    return f"{max(1, int(value))}s"


def value_rows(result: ToolResult) -> list[tuple[dict[str, str], float]]:
    """ToolResult.data를 (라벨, 값) 목록으로 변환합니다. 형식이 맞지 않는 행은 건너뜁니다."""
    out: list[tuple[dict[str, str], float]] = []
    for row in rows_of(result):
        labels, value = row.get("labels"), row.get("value")
        if isinstance(labels, dict) and isinstance(value, int | float):
            out.append(({str(k): str(v) for k, v in labels.items()}, float(value)))
    return out


def rows_of(result: ToolResult) -> list[dict[str, object]]:
    """ToolResult.data의 행 목록 (labels, value)."""
    data = result.data
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    return []


class CatalogQueryTool:
    def __init__(
        self,
        catalog: Catalog,
        prom: PrometheusClient,
        *,
        agent: AgentName,
        budget: ToolBudget,
        timeout_seconds: float,
    ) -> None:
        self._catalog = catalog
        self._prom = prom
        self._agent = agent
        self._budget = budget
        self._timeout = timeout_seconds
        self._freshness_cache: dict[str, float | None] = {}
        self._coverage_cache: dict[tuple[str, str], int | None] = {}

    def item(self, key: str) -> CatalogItem:
        item = self._catalog.items.get(key)
        if item is None:
            raise ToolPermissionError(f"카탈로그에 없는 항목입니다: {key}")
        if item.agent is not self._agent:
            raise ToolPermissionError(
                f"{self._agent.value} 에이전트는 "
                f"{item.agent.value} 항목({key})을 실행할 수 없습니다"
            )
        if item.source is not DataSourceKind.PROMETHEUS:
            raise ToolPermissionError(f"지원하지 않는 데이터 소스입니다: {item.source.value}")
        return item

    def build_expr(
        self, item: CatalogItem, selector: str, mode: QueryMode, ctx: AnalysisContext
    ) -> str:
        if mode is QueryMode.WINDOW:
            if not item.uses_range:
                raise ValueError("window 조회는 {range}가 있는 항목만 가능합니다")
            return item.render(selector, range=_seconds(ctx.time_range.duration.total_seconds()))
        expr = item.render(selector, range=RATE_RANGE if item.uses_range else None)
        if mode is QueryMode.CURRENT:
            return expr
        window = ctx.time_range if mode is QueryMode.WINDOW_AVG else ctx.baseline_range
        if window is None:
            raise ValueError("baseline_avg 조회에는 baseline_range가 필요합니다")
        duration = _seconds(window.duration.total_seconds())
        step = _seconds(ctx.step_seconds)
        return f"avg_over_time(({expr})[{duration}:{step}])"

    async def query(
        self,
        key: str,
        ctx: AnalysisContext,
        mode: QueryMode,
        targets: Mapping[TargetKind, str] | None = None,
    ) -> QueryOutcome:
        item = self.item(key)
        evidence_id = f"{key}@{mode.value}"
        # current 모드는 순간값이므로 구간 대신 평가 시각(분석 구간 끝)만 의미가 있습니다.
        window: TimeRange | None
        if mode is QueryMode.BASELINE_AVG:
            if ctx.baseline_range is None:
                raise ValueError("baseline_avg 조회에는 baseline_range가 필요합니다")
            window = ctx.baseline_range
        elif mode in (QueryMode.WINDOW_AVG, QueryMode.WINDOW):
            window = ctx.time_range
        else:
            window = None
        at = window.end if window is not None else ctx.time_range.end

        selector, unsupported = item.selector_for(targets or {})
        if unsupported:
            return QueryOutcome(
                result=ToolResult(
                    evidence_id=evidence_id,
                    source=DataSourceKind.PROMETHEUS,
                    query="",
                    time_range=window,
                    status=ToolStatus.ERROR,
                    fetched_at=utc_now(),
                    error="대상 필터를 적용할 수 없어 조회하지 않음: "
                    + ", ".join(k.value for k in unsupported),
                ),
                unsupported_targets=unsupported,
                skipped=True,
            )

        expr = self.build_expr(item, selector, mode, ctx)
        freshness = await self.freshness(item, at)
        if not self._budget.take():
            return QueryOutcome(
                result=ToolResult(
                    evidence_id=evidence_id,
                    source=DataSourceKind.PROMETHEUS,
                    query=expr,
                    time_range=window,
                    status=ToolStatus.ERROR,
                    fetched_at=utc_now(),
                    error="요청당 조회 호출 상한(analysis.max_tool_calls)에 도달해 조회하지 않음",
                )
            )
        try:
            result = await asyncio.wait_for(self._prom.query(expr, at), timeout=self._timeout)
        except TimeoutError:
            return QueryOutcome(
                result=ToolResult(
                    evidence_id=evidence_id,
                    source=DataSourceKind.PROMETHEUS,
                    query=expr,
                    time_range=window,
                    status=ToolStatus.TIMEOUT,
                    fetched_at=utc_now(),
                    freshness_seconds=freshness,
                    error=f"조회 제한 시간({self._timeout:.0f}초) 초과",
                )
            )
        except DataSourceError as exc:
            status = ToolStatus.TIMEOUT if exc.code == "timeout" else ToolStatus.ERROR
            return QueryOutcome(
                result=ToolResult(
                    evidence_id=evidence_id,
                    source=DataSourceKind.PROMETHEUS,
                    query=expr,
                    time_range=window,
                    status=status,
                    fetched_at=utc_now(),
                    freshness_seconds=freshness,
                    error=f"{exc.code}: {exc.message}",
                )
            )
        rows = [
            {"labels": s.labels, "value": s.value}
            for s in result.samples
            if not (math.isnan(s.value) or math.isinf(s.value))
        ]
        return QueryOutcome(
            result=ToolResult(
                evidence_id=evidence_id,
                source=DataSourceKind.PROMETHEUS,
                query=expr,
                time_range=window,
                status=ToolStatus.OK if rows else ToolStatus.EMPTY,
                data=rows,
                fetched_at=utc_now(),
                freshness_seconds=freshness,
                unit=item.unit,
            )
        )

    async def coverage(
        self, key: str, targets: Mapping[TargetKind, str], at: datetime
    ) -> int | None:
        """요청 대상으로 필터링한 기준 지표의 시계열 수. 대상 필터가 없으면 확인하지 않고 None.

        조건 조회의 빈 결과를 "해당 없음"으로 판단하기 전에, 대상 이름이 실제 데이터와 맞는지
        (예: 오타난 namespace) 확인하는 데 씁니다. 확인에 실패하면 None.
        """
        if not targets:
            return None
        item = self.item(key)
        selector, unsupported = item.selector_for(targets)
        if unsupported or not item.requires_metrics:
            return None
        metric = item.requires_metrics[0]
        cache_key = (metric, selector)
        if cache_key not in self._coverage_cache:
            if not self._budget.take():
                return None
            expr = f"count({metric}{{{selector}}})"
            try:
                result = await asyncio.wait_for(self._prom.query(expr, at), timeout=self._timeout)
            except (TimeoutError, DataSourceError):
                self._coverage_cache[cache_key] = None
            else:
                values = [s.value for s in result.samples if not math.isnan(s.value)]
                self._coverage_cache[cache_key] = int(values[0]) if values else 0
        return self._coverage_cache[cache_key]

    async def freshness(self, item: CatalogItem, at: datetime) -> float | None:
        """항목이 필요로 하는 지표 중 가장 오래된 최신 샘플의 경과 시간(초). 확인 실패 시 None."""
        ages: list[float] = []
        for metric in item.requires_metrics:
            if metric not in self._freshness_cache:
                if not self._budget.take():
                    return None
                sel = '{__name__="' + escape_label_value(metric) + '"}'
                try:
                    value = await asyncio.wait_for(
                        self._prom.scalar_or_none(f"time() - max(timestamp({sel}))", at),
                        timeout=self._timeout,
                    )
                except (TimeoutError, DataSourceError):
                    value = None
                self._freshness_cache[metric] = None if value is None else max(0.0, value)
            cached = self._freshness_cache[metric]
            if cached is None:
                return None
            ages.append(cached)
        return max(ages) if ages else None
