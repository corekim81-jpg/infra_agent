"""데이터 소스 연결 점검 (architecture.md 4.3절 '연결 가능 여부').

- 활성화된 HTTP 데이터 소스(Prometheus, Loki, Tempo)의 준비 상태와 버전을 확인합니다.
- Kubernetes API는 버전과 계정 권한(SelfSubjectRulesReview)을 확인합니다. 쓰기 권한이나 secrets
  읽기 권한이 있으면 "사용 불가"로 표시합니다(전용 읽기 계정만 사용).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict

from infra_agent.config.settings import HttpDatasourceConfig, KubernetesConfig, Settings
from infra_agent.datasources.errors import ConnectFailedError, DataSourceError
from infra_agent.datasources.kubernetes import (
    KubernetesClient,
    assess_rules,
    rules_scope_note,
)
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
    notes: tuple[str, ...] = ()


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


REVIEW_NAMESPACE = "default"
INSECURE_NOTE = (
    "kubeconfig에 insecure-skip-tls-verify가 설정되어 서버 인증서를 검증하지 않습니다. "
    "certificate-authority-data 사용을 권장합니다"
)


async def _probe_kubernetes(
    config: KubernetesConfig,
    environ: Mapping[str, str] | None,
    transport: Callable[[], httpx.AsyncBaseTransport | None],
) -> SourceStatus:
    """Kubernetes API 버전과 계정 권한을 확인합니다."""
    name = "kubernetes"
    if not config.enabled:
        return SourceStatus(name=name, enabled=False)
    started = time.perf_counter()
    try:
        client = KubernetesClient.from_config(
            config, environ=environ, max_retries=0, transport=transport()
        )
    except DataSourceError as exc:
        return SourceStatus(
            name=name,
            enabled=True,
            error_code=exc.code,
            error=exc.message,
            hint="전용 읽기 계정 kubeconfig 준비 방법은 docs/environment.md 3.5절을 보세요.",
        )
    try:
        info = await client.version()
        version = str(info["gitVersion"]) if info.get("gitVersion") else None
        report = assess_rules(await client.self_rules(REVIEW_NAMESPACE))
        notes = [*report.notes, rules_scope_note(REVIEW_NAMESPACE)]
        if client.insecure:
            notes.append(INSECURE_NOTE)
        latency = int((time.perf_counter() - started) * 1000)
        if not report.ok:
            return SourceStatus(
                name=name,
                enabled=True,
                url=client.server,
                reachable=True,
                ready=False,
                version=version,
                latency_ms=latency,
                error_code="not_read_only",
                error="; ".join(report.problems),
                hint=(
                    "전용 읽기 계정이 아닙니다. 이 상태에서는 Kubernetes API를 조회하지 않습니다. "
                    "deploy/rbac/infra-agent-reader.yaml의 계정 토큰으로 kubeconfig를 만드세요."
                ),
                notes=tuple(notes),
            )
        return SourceStatus(
            name=name,
            enabled=True,
            url=client.server,
            reachable=True,
            ready=True,
            version=version,
            latency_ms=latency,
            notes=tuple(notes),
        )
    except DataSourceError as exc:
        return SourceStatus(
            name=name,
            enabled=True,
            url=client.server,
            error_code=exc.code,
            error=exc.message,
            hint=_hint(client.server, exc),
        )
    finally:
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
        _probe_kubernetes(ds.kubernetes, environ, lambda: transport("kubernetes")),
    ]
    return list(await asyncio.gather(*probes))
