"""Loki·Tempo 조회 API 파싱과 로그 텍스트 정리 테스트 (가상 응답)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from infra_agent.config.settings import HttpDatasourceConfig
from infra_agent.datasources import LokiClient, ResponseFormatError, TempoClient
from infra_agent.datasources.loki import MAX_LOG_LIMIT
from infra_agent.tools import clean_text, normalize_trace_id, trace_id_of

NOW = datetime(2026, 9, 29, 3, 0, tzinfo=UTC)
CFG = HttpDatasourceConfig(enabled=True, url="http://synthetic.test")
TID = "4bf92f3577b34da6a3ce929d0e0e4736"


def _transport(body: object, seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


async def test_loki_logs_merge_structured_metadata_and_cap_limit() -> None:
    ns = str(int(NOW.timestamp() * 1e9))
    body = {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"service_name": "cart"},
                    "values": [
                        [ns, "boom", {"structuredMetadata": {"trace_id": TID}, "parsed": {}}],
                        [str(int((NOW - timedelta(seconds=1)).timestamp() * 1e9)), "older"],
                    ],
                }
            ],
        },
    }
    seen: list[httpx.Request] = []
    async with LokiClient.from_config(CFG, transport=_transport(body, seen)) as loki:
        entries = await loki.query_logs(
            '{service_name="cart"}', NOW - timedelta(minutes=5), NOW, 999
        )
    assert [e.line for e in entries] == ["boom", "older"]  # 최신순
    assert entries[0].labels == {"service_name": "cart", "trace_id": TID}
    assert entries[0].time == NOW
    assert seen[0].url.path == "/loki/api/v1/query_range"
    assert seen[0].url.params["limit"] == str(MAX_LOG_LIMIT)
    assert seen[0].url.params["direction"] == "backward"


async def test_loki_metric_query_requires_vector() -> None:
    seen: list[httpx.Request] = []
    bad = {"status": "success", "data": {"resultType": "streams", "result": []}}
    async with LokiClient.from_config(CFG, transport=_transport(bad, seen)) as loki:
        with pytest.raises(ResponseFormatError):
            await loki.query('count_over_time({a="b"}[5m])', NOW)
    ok = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": {"service_name": "cart"}, "value": [0, "12"]}],
        },
    }
    async with LokiClient.from_config(CFG, transport=_transport(ok, seen)) as loki:
        rows = await loki.query('sum(count_over_time({a="b"}[5m]))', NOW)
    assert rows[0].labels == {"service_name": "cart"} and rows[0].value == 12


async def test_tempo_search_parses_and_caps() -> None:
    body = {
        "traces": [
            {
                "traceID": TID,
                "rootServiceName": "frontend",
                "rootTraceName": "GET /",
                "startTimeUnixNano": str(int(NOW.timestamp() * 1e9)),
                "durationMs": 12,
            },
            {"traceID": "ab" * 16},
        ]
    }
    seen: list[httpx.Request] = []
    async with TempoClient.from_config(CFG, transport=_transport(body, seen)) as tempo:
        traces = await tempo.search("{ status = error }", NOW - timedelta(minutes=5), NOW, 1)
    assert len(traces) == 1 and traces[0].trace_id == TID and traces[0].start == NOW
    assert seen[0].url.path == "/api/search" and seen[0].url.params["limit"] == "1"
    async with TempoClient.from_config(CFG, transport=_transport({"traces": [{}]}, seen)) as tempo:
        with pytest.raises(ResponseFormatError):
            await tempo.search("{ }", NOW - timedelta(minutes=5), NOW, 5)


def test_clean_text_and_trace_id() -> None:
    text = clean_text("a\x1b[31m\nb   c token=abcdef123456 " + "x" * 300)
    assert "\x1b" not in text and "\n" not in text and "abcdef123456" not in text
    assert len(text) == 200 and text.endswith("…")
    assert trace_id_of({"trace_id": TID.upper()}, "") == TID
    assert trace_id_of({}, f'msg traceId="{TID}"') == TID
    assert trace_id_of({"trace_id": "0" * 32}, "") is None  # 빈(0) trace_id는 무시
    assert trace_id_of({}, "no id here") is None
    # 64비트(16자리) trace_id는 Tempo와 같은 128비트 표현(앞을 0으로 채움)으로 맞춤
    assert trace_id_of({"trace_id": "a3ce929d0e0e4736"}, "") == "0" * 16 + "a3ce929d0e0e4736"


def test_normalize_trace_id() -> None:
    # Tempo 검색 응답은 앞자리 0을 뺀 값(31자리 등)을 돌려줌 → 로그의 32자리와 같게 맞춤
    padded = "0d37e1fad09d9d91be74cef3c6187859"
    assert normalize_trace_id(padded[1:]) == padded
    assert normalize_trace_id(f" {padded.upper()} ") == padded
    assert normalize_trace_id("0" * 32) is None
    assert normalize_trace_id("not-hex") is None
    assert normalize_trace_id("a" * 33) is None
