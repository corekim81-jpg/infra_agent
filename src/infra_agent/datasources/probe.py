"""데이터 소스 연결 점검 (architecture.md 4.3절 '연결 가능 여부').

활성화된 HTTP 데이터 소스(Prometheus, Loki, Tempo)의 준비 상태와 버전을 확인합니다.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict

from infra_agent.config.settings import HttpDatasourceConfig, Settings
from infra_agent.datasources.errors import ConnectFailedError, DataSourceError
from infra_agent.datasources.loki import LokiClient
from infra_agent.datasources.prometheus import PrometheusClient
from infra_agent.datasources.tempo import TempoClient

TransportFactory = Callable[[str], httpx.AsyncBaseTransport | None]


class _ProbeClient(Protocol):
    async def ready(self) -> bool: ...
    async def buildinfo(self) -> dict[str, Any]: ...
    async def aclose(self) -> None: ...


class SourceStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    enabled: bool
    url: str | None = None
    reachable: bool = False
    ready: bool = False
    version: str | None = None
    latency_ms: int | None = None
    error_code: str | None = None
    error: str | None = None
    hint: str | None = None


def _hint(url: str | None, exc: DataSourceError) -> str | None:
    if not isinstance(exc, ConnectFailedError) or url is None:
        return None
    host = urlparse(url).hostname or ""
    if host in ("127.0.0.1", "localhost", "::1"):
        return (
            "로컬 주소에 연결하지 못했습니다. SSH 터널이 실행 중인지, "
            "WSL·Docker에서 실행 중이라면 접속 경로가 다른지 확인하세요."
        )
    return "주소·포트·방화벽과 서비스 실행 여부를 확인하세요."


async def _probe_one(
    name: str,
    config: HttpDatasourceConfig,
    factory: Callable[[], _ProbeClient],
) -> SourceStatus:
    if not config.enabled:
        return SourceStatus(name=name, enabled=False, url=config.url)
    started = time.perf_counter()
    client: _ProbeClient | None = None
    try:
        client = factory()
        ready = await client.ready()
        version: str | None = None
        try:
            info = await client.buildinfo()
            version = str(info["version"]) if info.get("version") else None
        except DataSourceError:
            version = None  # buildinfo를 제공하지 않는 버전·설정은 연결 성공으로 봅니다
        return SourceStatus(
            name=name,
            enabled=True,
            url=config.url,
            reachable=True,
            ready=ready,
            version=version,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
    except DataSourceError as exc:
        return SourceStatus(
            name=name,
            enabled=True,
            url=config.url,
            error_code=exc.code,
            error=exc.message,
            hint=_hint(config.url, exc),
        )
    finally:
        if client is not None:
            await client.aclose()


async def check_sources(
    settings: Settings,
    *,
    environ: Mapping[str, str] | None = None,
    transport_factory: TransportFactory | None = None,
) -> list[SourceStatus]:
    """활성화된 데이터 소스를 병렬로 점검합니다. 점검 중 재시도는 하지 않습니다."""
    ds = settings.datasources

    def transport(name: str) -> httpx.AsyncBaseTransport | None:
        return transport_factory(name) if transport_factory else None

    def prom() -> _ProbeClient:
        return PrometheusClient.from_config(
            ds.prometheus, max_retries=0, environ=environ, transport=transport("prometheus")
        )

    def loki() -> _ProbeClient:
        return LokiClient.from_config(
            ds.loki, max_retries=0, environ=environ, transport=transport("loki")
        )

    def tempo() -> _ProbeClient:
        return TempoClient.from_config(
            ds.tempo, max_retries=0, environ=environ, transport=transport("tempo")
        )

    probes: list[Awaitable[SourceStatus]] = [
        _probe_one("prometheus", ds.prometheus, prom),
        _probe_one("loki", ds.loki, loki),
        _probe_one("tempo", ds.tempo, tempo),
    ]
    return list(await asyncio.gather(*probes))
