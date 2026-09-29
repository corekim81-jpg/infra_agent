"""카탈로그 실행 점검 테스트 (가상 Prometheus 응답)."""

from __future__ import annotations

from pathlib import Path

import httpx

from infra_agent.catalog import load_catalog
from infra_agent.catalog.check import CheckStatus, check_catalog
from infra_agent.config.settings import HttpDatasourceConfig
from infra_agent.datasources import PrometheusClient

CATALOG = """
version: 1
environment: synthetic-test
items:
  a.ok:
    agent: server
    source: prometheus
    description: 결과가 있는 항목
    query: 'metric_ok{{{selector}}}'
    requires_metrics: [metric_ok]
    evidence: {status: discovered}
  a.empty:
    agent: server
    source: prometheus
    description: 결과가 없는 항목
    query: 'rate(metric_empty{{{selector}}}[{range}])'
    requires_metrics: [metric_empty]
    evidence: {status: discovered}
  a.missing:
    agent: db
    source: prometheus
    description: 지표가 없는 항목
    query: 'metric_absent{{{selector}}}'
    requires_metrics: [metric_absent]
    evidence: {status: discovered}
  a.error:
    agent: network
    source: prometheus
    description: 조회 오류 항목
    query: 'metric_bad{{{selector}}}'
    requires_metrics: [metric_bad]
    evidence: {status: discovered}
"""


def _handler(request: httpx.Request) -> httpx.Response:
    assert request.method == "GET"
    if request.url.path == "/api/v1/label/__name__/values":
        return httpx.Response(
            200, json={"status": "success", "data": ["metric_ok", "metric_empty", "metric_bad"]}
        )
    query = request.url.params["query"]
    if query.startswith("metric_bad"):
        return httpx.Response(400, json={"status": "error", "error": "synthetic parse error"})
    if query.startswith("metric_ok"):
        result = [
            {"metric": {"a": "1"}, "value": [1, "1"]},
            {"metric": {"a": "2"}, "value": [1, "2"]},
        ]
    else:
        assert "[7m]" in query  # range 값 전달 확인
        result = []
    return httpx.Response(
        200, json={"status": "success", "data": {"resultType": "vector", "result": result}}
    )


async def test_check_catalog(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(CATALOG, encoding="utf-8")
    catalog = load_catalog(path)
    cfg = HttpDatasourceConfig(enabled=True, url="http://prom.synthetic.test")
    async with PrometheusClient.from_config(cfg, transport=httpx.MockTransport(_handler)) as prom:
        results = {r.key: r for r in await check_catalog(catalog, prom, range_="7m")}
    assert results["a.ok"].status is CheckStatus.OK and results["a.ok"].series == 2
    assert results["a.empty"].status is CheckStatus.EMPTY
    assert results["a.missing"].status is CheckStatus.MISSING_METRICS
    assert results["a.missing"].detail == "metric_absent"
    assert results["a.error"].status is CheckStatus.ERROR
    assert "synthetic parse error" in (results["a.error"].detail or "")
