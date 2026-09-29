"""지표·라벨·최신성 탐색 (environment.md 4.3절).

읽기 전용 조회만 사용합니다. 조회 부하를 제한하기 위해 동시 요청 수(`execution.max_concurrency`)와
상세 탐색 지표 수(`max_metrics`)를 제한합니다.
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

from infra_agent import __version__
from infra_agent.config.settings import Settings
from infra_agent.datasources.errors import DataSourceError
from infra_agent.datasources.loki import LokiClient
from infra_agent.datasources.probe import SourceStatus, check_sources
from infra_agent.datasources.prometheus import PrometheusClient, escape_label_value
from infra_agent.datasources.tempo import TempoClient
from infra_agent.discovery.expectations import (
    AVAILABILITY_OFFSETS,
    CONFIRMED_METRICS,
    HISTOGRAM_COMPANION_SUFFIXES,
    INTEREST_FIRST_TOKENS,
    MAX_LOKI_LABELS,
    MAX_SAMPLE_VALUES,
    SAMPLE_SERIES,
)
from infra_agent.discovery.report import (
    DiscoveryReport,
    LabelValues,
    LokiDiscovery,
    MetricDetail,
    PrometheusDiscovery,
    TempoDiscovery,
)
from infra_agent.security import redact
from infra_agent.timeutil import parse_duration, utc_now

_IPV4_RE = re.compile(r"\b(?!127\.)(?:\d{1,3}\.){3}\d{1,3}\b")

TransportFactory = Callable[[str], httpx.AsyncBaseTransport | None]


@dataclass(frozen=True)
class DiscoveryOptions:
    lookback: timedelta = timedelta(hours=1)
    max_metrics: int = 400
    mask_ips: bool = True
    include_values: bool = True
    extra_prefixes: frozenset[str] = field(default_factory=frozenset)


def mask_value(value: str, mask_ips: bool) -> str:
    value = redact(value)
    return _IPV4_RE.sub("<ip>", value) if mask_ips else value


def name_selector(name: str) -> str:
    """지표 이름 형식과 무관하게 안전한 `{__name__="..."}` 선택자."""
    return '{__name__="' + escape_label_value(name) + '"}'


def family_of(name: str) -> str:
    return name.split("_", 1)[0] if "_" in name else name


def select_interest(
    names: list[str], extra_prefixes: frozenset[str], max_metrics: int
) -> tuple[list[str], int, int]:
    """상세 탐색 대상을 고릅니다. 반환: (대상, 생략 수, 히스토그램 동반 지표 생략 수)."""
    available = set(names)
    tokens = INTEREST_FIRST_TOKENS | extra_prefixes
    companions = 0
    picked: list[str] = []
    for name in names:
        if name not in CONFIRMED_METRICS and family_of(name) not in tokens:
            continue
        base = next(
            (name[: -len(s)] for s in HISTOGRAM_COMPANION_SUFFIXES if name.endswith(s)), None
        )
        if base is not None and f"{base}_bucket" in available and name not in CONFIRMED_METRICS:
            companions += 1
            continue
        picked.append(name)
    # 사용자 확인 지표를 먼저 두어 상한에 걸려도 빠지지 않게 합니다.
    picked.sort(key=lambda n: (n not in CONFIRMED_METRICS, n))
    omitted = max(0, len(picked) - max_metrics)
    return picked[:max_metrics], omitted, companions


def _metadata_for(name: str, metadata: Mapping[str, list[dict[str, str]]]) -> dict[str, str]:
    candidates = [name]
    for suffix in ("_bucket", "_sum", "_count", "_total"):
        if name.endswith(suffix):
            candidates.append(name[: -len(suffix)])
    for candidate in candidates:
        entries = metadata.get(candidate)
        if entries:
            return entries[0]
    return {}


async def _metric_detail(
    prom: PrometheusClient,
    name: str,
    now: datetime,
    options: DiscoveryOptions,
    metadata: Mapping[str, list[dict[str, str]]],
    sem: asyncio.Semaphore,
) -> MetricDetail:
    sel = name_selector(name)
    meta = _metadata_for(name, metadata)
    detail = MetricDetail(
        name=name,
        confirmed=name in CONFIRMED_METRICS,
        type=meta.get("type") or None,
        unit=meta.get("unit") or None,
        help=(meta.get("help") or None),
    )
    try:
        async with sem:
            detail.label_names = await prom.label_names(
                match=[sel], start=now - options.lookback, end=now
            )
        async with sem:
            count = await prom.scalar_or_none(f"count({sel})", now)
        detail.series_count = int(count) if count is not None else 0
        if detail.series_count:
            async with sem:
                latest = await prom.scalar_or_none(f"max(timestamp({sel}))", now)
            if latest is not None:
                detail.latest_age_seconds = max(0.0, now.timestamp() - latest)
            if options.include_values:
                async with sem:
                    top = await prom.query(f"topk({SAMPLE_SERIES}, {sel})", now)
                detail.sample_labels = [
                    {
                        k: mask_value(v, options.mask_ips)
                        for k, v in s.labels.items()
                        if k != "__name__"
                    }
                    for s in top.samples
                ]
    except DataSourceError as exc:
        detail.error = f"{exc.code}: {exc.message}"
    return detail


async def _discover_prometheus(
    settings: Settings,
    status: SourceStatus,
    now: datetime,
    options: DiscoveryOptions,
    environ: Mapping[str, str] | None,
    transport: httpx.AsyncBaseTransport | None,
    warnings: list[str],
) -> PrometheusDiscovery:
    result = PrometheusDiscovery(status=status)
    if not status.reachable:
        return result
    sem = asyncio.Semaphore(settings.execution.max_concurrency)
    async with PrometheusClient.from_config(
        settings.datasources.prometheus,
        max_retries=settings.execution.max_retries,
        environ=environ,
        transport=transport,
    ) as prom:
        try:
            runtime = await prom.runtimeinfo()
            retention = runtime.get("storageRetention")
            result.storage_retention = str(retention) if retention else None
        except DataSourceError as exc:
            warnings.append(f"prometheus runtimeinfo 조회 실패: {exc.message}")

        try:
            names = await prom.metric_names()
        except DataSourceError as exc:
            warnings.append(f"prometheus 지표 목록 조회 실패: {exc.message}")
            return result
        available = set(names)
        result.total_metrics = len(names)
        result.families = dict(Counter(family_of(n) for n in names))
        result.confirmed_present = {n: n in available for n in CONFIRMED_METRICS}

        try:
            metadata = await prom.metadata()
        except DataSourceError as exc:
            warnings.append(f"prometheus 메타데이터 조회 실패: {exc.message}")
            metadata = {}

        targets, omitted, companions = select_interest(
            names, options.extra_prefixes, options.max_metrics
        )
        result.detailed_count = len(targets)
        result.omitted_count = omitted
        result.histogram_companions_skipped = companions
        if omitted:
            warnings.append(
                f"상세 탐색 대상 {omitted}개를 생략했습니다 (--max-metrics로 조정 가능)"
            )
        result.metrics = list(
            await asyncio.gather(
                *(_metric_detail(prom, n, now, options, metadata, sem) for n in targets)
            )
        )

        reference = next((n for n in CONFIRMED_METRICS if n in available), None)
        if reference is None and targets:
            reference = targets[0]
        result.reference_metric = reference
        if reference is not None:
            for offset in AVAILABILITY_OFFSETS:
                try:
                    value = await prom.scalar_or_none(
                        f"count({name_selector(reference)})", now - parse_duration(offset)
                    )
                    result.availability[offset] = value is not None and value > 0
                except DataSourceError:
                    result.availability[offset] = None
    return result


async def _discover_loki(
    settings: Settings,
    status: SourceStatus,
    now: datetime,
    options: DiscoveryOptions,
    environ: Mapping[str, str] | None,
    transport: httpx.AsyncBaseTransport | None,
) -> LokiDiscovery:
    result = LokiDiscovery(status=status)
    if not status.reachable:
        return result
    start = now - options.lookback
    async with LokiClient.from_config(
        settings.datasources.loki,
        max_retries=settings.execution.max_retries,
        environ=environ,
        transport=transport,
    ) as loki:
        try:
            result.label_names = await loki.label_names(start, now)
            for name in result.label_names[:MAX_LOKI_LABELS]:
                values = await loki.label_values(name, start, now)
                samples = (
                    [mask_value(v, options.mask_ips) for v in values[:MAX_SAMPLE_VALUES]]
                    if options.include_values
                    else []
                )
                result.label_values[name] = LabelValues(count=len(values), samples=samples)
        except DataSourceError as exc:
            result.error = f"{exc.code}: {exc.message}"
    return result


async def _discover_tempo(
    settings: Settings,
    status: SourceStatus,
    environ: Mapping[str, str] | None,
    transport: httpx.AsyncBaseTransport | None,
) -> TempoDiscovery:
    result = TempoDiscovery(status=status)
    if not status.reachable:
        return result
    async with TempoClient.from_config(
        settings.datasources.tempo,
        max_retries=settings.execution.max_retries,
        environ=environ,
        transport=transport,
    ) as tempo:
        try:
            result.tag_names = await tempo.tag_names()
        except DataSourceError as exc:
            result.error = f"{exc.code}: {exc.message}"
    return result


async def discover(
    settings: Settings,
    options: DiscoveryOptions | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    transport_factory: TransportFactory | None = None,
    now: datetime | None = None,
) -> DiscoveryReport:
    """활성화된 데이터 소스를 탐색해 보고서를 만듭니다. 파일 저장은 호출자가 합니다."""
    opts = options or DiscoveryOptions()
    current = now or utc_now()
    warnings: list[str] = []

    def transport(name: str) -> httpx.AsyncBaseTransport | None:
        return transport_factory(name) if transport_factory else None

    statuses = {
        s.name: s
        for s in await check_sources(settings, environ=environ, transport_factory=transport_factory)
    }
    report = DiscoveryReport(
        generated_at=current,
        tool_version=__version__,
        profile=settings.profile.value,
        lookback_seconds=int(opts.lookback.total_seconds()),
        mask_ips=opts.mask_ips,
        include_values=opts.include_values,
    )
    if statuses["prometheus"].enabled:
        report.prometheus = await _discover_prometheus(
            settings,
            statuses["prometheus"],
            current,
            opts,
            environ,
            transport("prometheus"),
            warnings,
        )
    if statuses["loki"].enabled:
        report.loki = await _discover_loki(
            settings, statuses["loki"], current, opts, environ, transport("loki")
        )
    if statuses["tempo"].enabled:
        report.tempo = await _discover_tempo(
            settings, statuses["tempo"], environ, transport("tempo")
        )
    if not any(s.enabled for s in statuses.values()):
        warnings.append("활성화된 데이터 소스가 없습니다. 설정 파일의 datasources를 확인하세요.")
    report.warnings = warnings
    return report
