"""개발 서버 연동 테스트 (실제 데이터, 읽기 전용).

실행 조건: SSH 터널 실행, `INFRA_AGENT_CONFIG`에 dev-tunnel 설정 파일 경로,
`INFRA_AGENT_LIVE_TESTS=1`. CI에서는 실행하지 않습니다.
특정 값을 기대하지 않고 연결·형식·최신성만 확인합니다.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from infra_agent.config import Settings, load_settings
from infra_agent.datasources import LokiClient, PrometheusClient, TempoClient
from infra_agent.datasources.probe import check_sources
from infra_agent.discovery import DiscoveryOptions, discover, write_report
from infra_agent.discovery.expectations import CONFIRMED_METRICS

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def settings() -> Settings:
    if not os.environ.get("INFRA_AGENT_CONFIG"):
        pytest.skip("INFRA_AGENT_CONFIG가 설정되지 않았습니다 (예: config/local.yaml)")
    s = load_settings()
    if not s.datasources.prometheus.enabled:
        pytest.skip("Prometheus가 비활성화되어 있습니다")
    return s


async def test_enabled_sources_ready(settings: Settings) -> None:
    statuses = await check_sources(settings)
    failed = [f"{s.name}: {s.error_code} {s.error}" for s in statuses if s.enabled and not s.ready]
    assert not failed, f"준비되지 않은 데이터 소스: {failed}"


async def test_prometheus_basic_queries(settings: Settings) -> None:
    async with PrometheusClient.from_config(settings.datasources.prometheus) as prom:
        names = await prom.metric_names()
        assert names, "지표 이름 목록이 비어 있습니다"
        assert await prom.scalar_or_none("vector(1)") == 1.0
        present = [n for n in CONFIRMED_METRICS if n in set(names)]
        assert present, "사용자 확인 지표가 하나도 없습니다. 탐색 보고서로 지표 이름을 확인하세요."


async def test_loki_and_tempo_reachable(settings: Settings) -> None:
    ds = settings.datasources
    if ds.loki.enabled:
        async with LokiClient.from_config(ds.loki) as loki:
            assert await loki.ready()
    if ds.tempo.enabled:
        async with TempoClient.from_config(ds.tempo) as tempo:
            assert await tempo.ready()


async def test_discovery_smoke(settings: Settings, tmp_path: Path) -> None:
    report = await discover(settings, DiscoveryOptions(max_metrics=10, include_values=False))
    assert report.prometheus is not None and report.prometheus.status.ready
    assert report.prometheus.total_metrics > 0
    json_path, md_path = write_report(report, tmp_path)
    assert json_path.is_file() and md_path.is_file()
