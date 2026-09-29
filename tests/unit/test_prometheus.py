"""Prometheus·Loki·Tempo 클라이언트 테스트 (가상 응답)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from infra_agent.config.settings import HttpDatasourceConfig
from infra_agent.datasources import (
    LokiClient,
    PrometheusClient,
    QueryError,
    ResponseFormatError,
    TempoClient,
    build_selector,
)
from infra_agent.datasources.prometheus import escape_label_value

BASE = "http://backend.synthetic.test"
CFG = HttpDatasourceConfig(enabled=True, url=BASE)
NOW = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)


def _ok(data: object) -> httpx.Response:
    return httpx.Response(200, json={"status": "success", "data": data})


# ---------------------------------------------------------------- selector


def test_build_selector_escapes() -> None:
    sel = build_selector({"k8s_namespace_name": "otel-demo", "x": 'a"b\\c'})
    assert sel == 'k8s_namespace_name="otel-demo",x="a\\"b\\\\c"'


def test_build_selector_rejects_bad_label() -> None:
    with pytest.raises(ValueError):
        build_selector({"bad-label": "x"})
    with pytest.raises(ValueError):
        build_selector({'x"}) or vector(1': "x"})


def test_escape_newline() -> None:
    assert escape_label_value("a\nb") == "a\\nb"


# ---------------------------------------------------------------- Prometheus


@respx.mock
async def test_query_vector() -> None:
    route = respx.get(f"{BASE}/api/v1/query").mock(
        return_value=_ok(
            {
                "resultType": "vector",
                "result": [
                    {"metric": {"k8s_node_name": "n1"}, "value": [1.5, "0.25"]},
                    {"metric": {"k8s_node_name": "n2"}, "value": [1.5, "NaN"]},
                ],
            }
        )
    )
    async with PrometheusClient.from_config(CFG) as prom:
        res = await prom.query("k8s_node_cpu_usage", NOW)
    assert res.result_type == "vector"
    assert res.samples[0].labels == {"k8s_node_name": "n1"}
    assert res.samples[0].value == 0.25
    assert math.isnan(res.samples[1].value)
    sent = route.calls.last.request.url.params
    assert sent["time"] == f"{NOW.timestamp():.3f}"


@respx.mock
async def test_query_scalar_and_scalar_or_none() -> None:
    respx.get(f"{BASE}/api/v1/query").mock(
        side_effect=[
            _ok({"resultType": "scalar", "result": [1.0, "3"]}),
            _ok({"resultType": "vector", "result": []}),
            _ok({"resultType": "vector", "result": [{"metric": {}, "value": [1, "NaN"]}]}),
        ]
    )
    async with PrometheusClient.from_config(CFG) as prom:
        assert await prom.scalar_or_none("scalar(1)") == 3.0
        assert await prom.scalar_or_none("absent_thing") is None
        assert await prom.scalar_or_none("nan_thing") is None


@respx.mock
async def test_query_error_status() -> None:
    respx.get(f"{BASE}/api/v1/query").mock(
        return_value=httpx.Response(200, json={"status": "error", "error": "boom"})
    )
    async with PrometheusClient.from_config(CFG) as prom:
        with pytest.raises(QueryError, match="boom"):
            await prom.query("x")


@respx.mock
async def test_query_unsupported_result_type() -> None:
    respx.get(f"{BASE}/api/v1/query").mock(
        return_value=_ok({"resultType": "string", "result": [1, "x"]})
    )
    async with PrometheusClient.from_config(CFG) as prom:
        with pytest.raises(ResponseFormatError):
            await prom.query("x")


@respx.mock
async def test_query_range() -> None:
    route = respx.get(f"{BASE}/api/v1/query_range").mock(
        return_value=_ok(
            {
                "resultType": "matrix",
                "result": [{"metric": {"pod": "p"}, "values": [[1, "1"], [2, "2.5"]]}],
            }
        )
    )
    async with PrometheusClient.from_config(CFG) as prom:
        series = await prom.query_range("x", NOW - timedelta(minutes=30), NOW, 60)
        with pytest.raises(ValueError):
            await prom.query_range("x", NOW - timedelta(minutes=30), NOW, 0)
    assert series[0].labels == {"pod": "p"}
    assert series[0].points == [(1.0, 1.0), (2.0, 2.5)]
    assert route.calls.last.request.url.params["step"] == "60"


@respx.mock
async def test_metric_names_labels_metadata() -> None:
    respx.get(f"{BASE}/api/v1/label/__name__/values").mock(return_value=_ok(["b", "a"]))
    labels_route = respx.get(f"{BASE}/api/v1/labels").mock(return_value=_ok(["pod", "__name__"]))
    respx.get(f"{BASE}/api/v1/metadata").mock(
        return_value=_ok({"a": [{"type": "gauge", "help": "h", "unit": ""}], "bad": "x"})
    )
    async with PrometheusClient.from_config(CFG) as prom:
        assert await prom.metric_names() == ["a", "b"]
        assert await prom.label_names(match=['{__name__="a"}'], start=NOW, end=NOW) == [
            "__name__",
            "pod",
        ]
        meta = await prom.metadata()
    assert meta == {"a": [{"type": "gauge", "help": "h", "unit": ""}]}
    assert labels_route.calls.last.request.url.params.get_list("match[]") == ['{__name__="a"}']


@respx.mock
async def test_ready_and_buildinfo() -> None:
    respx.get(f"{BASE}/-/ready").mock(return_value=httpx.Response(200, text="ready"))
    respx.get(f"{BASE}/api/v1/status/buildinfo").mock(return_value=_ok({"version": "3.0.0"}))
    async with PrometheusClient.from_config(CFG) as prom:
        assert await prom.ready()
        assert (await prom.buildinfo())["version"] == "3.0.0"


@respx.mock
async def test_base_url_with_path_prefix() -> None:
    cfg = HttpDatasourceConfig(enabled=True, url=f"{BASE}/prometheus")
    route = respx.get(f"{BASE}/prometheus/api/v1/label/__name__/values").mock(
        return_value=_ok(["a"])
    )
    async with PrometheusClient.from_config(cfg) as prom:
        assert await prom.metric_names() == ["a"]
    assert route.called


# ---------------------------------------------------------------- Loki / Tempo


@respx.mock
async def test_loki_labels() -> None:
    names = respx.get(f"{BASE}/loki/api/v1/labels").mock(return_value=_ok(["service_name"]))
    respx.get(f"{BASE}/loki/api/v1/label/service_name/values").mock(
        return_value=_ok(["cart", "checkout"])
    )
    async with LokiClient.from_config(CFG) as loki:
        assert await loki.label_names(NOW - timedelta(hours=1), NOW) == ["service_name"]
        assert await loki.label_values("service_name", NOW - timedelta(hours=1), NOW) == [
            "cart",
            "checkout",
        ]
        with pytest.raises(ValueError):
            await loki.label_values("../../x", NOW - timedelta(hours=1), NOW)
    assert names.calls.last.request.url.params["end"] == str(int(NOW.timestamp() * 1e9))


@respx.mock
async def test_tempo_tags_v2() -> None:
    respx.get(f"{BASE}/api/v2/search/tags").mock(
        return_value=httpx.Response(
            200, json={"scopes": [{"name": "span", "tags": ["http.method", "db.system"]}]}
        )
    )
    async with TempoClient.from_config(CFG) as tempo:
        assert await tempo.tag_names() == {"span": ["db.system", "http.method"]}


@respx.mock
async def test_tempo_tags_v1_fallback() -> None:
    respx.get(f"{BASE}/api/v2/search/tags").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE}/api/search/tags").mock(
        return_value=httpx.Response(200, json={"tagNames": ["service.name"]})
    )
    async with TempoClient.from_config(CFG) as tempo:
        assert await tempo.tag_names() == {"all": ["service.name"]}
