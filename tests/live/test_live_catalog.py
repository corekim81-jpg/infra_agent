"""조회 카탈로그의 개발 서버 실행 점검 (실제 데이터, 읽기 전용).

실행 조건은 test_live_sources.py와 같습니다. 특정 값을 기대하지 않고
조회식 오류가 없는지, 이름에 단위가 없는 CPU 지표가 cores 단위인지 교차 검증합니다.
"""

from __future__ import annotations

import os

import pytest

from infra_agent.catalog import load_catalog
from infra_agent.catalog.check import CheckStatus, check_catalog
from infra_agent.config import Settings, load_settings
from infra_agent.datasources import PrometheusClient

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def settings() -> Settings:
    if not os.environ.get("INFRA_AGENT_CONFIG"):
        pytest.skip("INFRA_AGENT_CONFIG가 설정되지 않았습니다")
    s = load_settings()
    if not s.datasources.prometheus.enabled or not s.catalog.path:
        pytest.skip("Prometheus 또는 catalog.path가 설정되지 않았습니다")
    return s


async def test_all_catalog_queries_execute(settings: Settings) -> None:
    assert settings.catalog.path is not None
    catalog = load_catalog(settings.catalog.path)
    async with PrometheusClient.from_config(settings.datasources.prometheus) as prom:
        results = await check_catalog(catalog, prom, range_="5m")
    problems = [
        f"{r.key}: {r.status.value} {r.detail}"
        for r in results
        if r.status in (CheckStatus.ERROR, CheckStatus.MISSING_METRICS)
    ]
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize(
    ("usage", "cpu_time"),
    [
        ("k8s_node_cpu_usage", "k8s_node_cpu_time_seconds_total"),
        ("container_cpu_usage", "container_cpu_time_seconds_total"),
    ],
)
async def test_cpu_usage_unit_is_cores(settings: Settings, usage: str, cpu_time: str) -> None:
    """CPU 사용량 합계가 CPU 시간 증가율(초/초 = cores) 합계와 같은 규모인지 확인합니다."""
    async with PrometheusClient.from_config(settings.datasources.prometheus) as prom:
        a = await prom.scalar_or_none(f"sum({usage})")
        b = await prom.scalar_or_none(f"sum(rate({cpu_time}[5m]))")
    assert a is not None and b is not None, "비교할 데이터가 없습니다"
    assert b > 0
    ratio = a / b
    assert 0.5 <= ratio <= 2.0, (
        f"{usage} / rate({cpu_time}) = {ratio:.3f} (cores 단위가 아닐 수 있음)"
    )
