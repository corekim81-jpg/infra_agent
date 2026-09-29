"""카탈로그 실행 점검.

각 Prometheus 항목에 대해 필요한 지표가 있는지 확인하고,
selector 없이(전체 대상) 조회를 한 번 실행해 조회식 오류·빈 결과를 확인합니다.
읽기 전용 instant query만 사용합니다.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from infra_agent.catalog.models import Catalog
from infra_agent.datasources.errors import DataSourceError
from infra_agent.datasources.prometheus import PrometheusClient
from infra_agent.schemas import DataSourceKind


class CheckStatus(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    MISSING_METRICS = "missing_metrics"
    ERROR = "error"
    SKIPPED = "skipped"


class ItemCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    agent: str
    status: CheckStatus
    series: int = 0
    detail: str | None = None


async def _check_item(
    key: str,
    catalog: Catalog,
    prom: PrometheusClient,
    available: set[str],
    range_: str,
    now: datetime | None,
    sem: asyncio.Semaphore,
) -> ItemCheck:
    item = catalog.items[key]
    agent = item.agent.value
    if item.source is not DataSourceKind.PROMETHEUS:
        return ItemCheck(key=key, agent=agent, status=CheckStatus.SKIPPED, detail=item.source.value)
    missing = item.missing_metrics(available)
    if missing:
        return ItemCheck(
            key=key, agent=agent, status=CheckStatus.MISSING_METRICS, detail=", ".join(missing)
        )
    expr = item.render("", range=range_ if item.uses_range else None)
    try:
        async with sem:
            result = await prom.query(expr, now)
    except DataSourceError as exc:
        return ItemCheck(
            key=key, agent=agent, status=CheckStatus.ERROR, detail=f"{exc.code}: {exc.message}"
        )
    count = len(result.samples)
    return ItemCheck(
        key=key,
        agent=agent,
        status=CheckStatus.OK if count else CheckStatus.EMPTY,
        series=count,
    )


async def check_catalog(
    catalog: Catalog,
    prom: PrometheusClient,
    *,
    range_: str = "5m",
    concurrency: int = 4,
    now: datetime | None = None,
) -> list[ItemCheck]:
    """모든 항목을 점검합니다. 지표 목록 조회 실패는 예외로 전달합니다."""
    available = set(await prom.metric_names())
    sem = asyncio.Semaphore(concurrency)
    return list(
        await asyncio.gather(
            *(_check_item(k, catalog, prom, available, range_, now, sem) for k in catalog.items)
        )
    )
