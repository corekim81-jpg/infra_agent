"""Prometheus·Loki·Tempo를 한 번에 흉내 내는 가상 백엔드 (httpx.MockTransport).

테스트가 실제 카탈로그로 렌더링한 조회식에 응답을 등록하므로, 에이전트가 보내는 조회식 그대로를
검증합니다. 등록되지 않은 조회는 빈 결과를 돌려주고 `unmatched`에 기록합니다. 모든 값은 가상입니다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from expr_prom import ExprProm, Rows

LogRows = list[tuple[dict[str, str], list[tuple[datetime, str]]]]


class FakeBackend(ExprProm):
    def __init__(self, *, freshness_seconds: float = 15.0) -> None:
        super().__init__(freshness_seconds=freshness_seconds)
        self.ranges: dict[str, list[tuple[dict[str, str], list[tuple[datetime, float]]]]] = {}
        self.loki_metrics: dict[str, Rows] = {}
        self.loki_logs: dict[str, LogRows] = {}
        self.loki_fail: set[str] = set()
        self.tempo: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[tuple[str, str]] = []

    def add_range(
        self, expr: str, series: list[tuple[dict[str, str], list[tuple[datetime, float]]]]
    ) -> None:
        self.ranges[expr] = series

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = request.url.params
        if path == "/api/v1/query":
            return super().handler(request)
        if path == "/api/v1/query_range":
            expr = params["query"]
            self.calls.append((path, expr))
            series = self.ranges.get(expr)
            if series is None:
                self.unmatched.append(expr)
                series = []
            result = [
                {"metric": labels, "values": [[ts.timestamp(), str(v)] for ts, v in points]}
                for labels, points in series
            ]
            return _ok({"resultType": "matrix", "result": result})
        if path == "/loki/api/v1/query":
            expr = params["query"]
            self.calls.append((path, expr))
            if expr in self.loki_fail:
                return httpx.Response(400, json={"status": "error", "error": "synthetic failure"})
            rows = self.loki_metrics.get(expr)
            if rows is None:
                self.unmatched.append(expr)
                rows = []
            result = [{"metric": labels, "value": [0, str(v)]} for labels, v in rows]
            return _ok({"resultType": "vector", "result": result})
        if path == "/loki/api/v1/query_range":
            expr = params["query"]
            self.calls.append((path, expr))
            streams = self.loki_logs.get(expr)
            if streams is None:
                self.unmatched.append(expr)
                streams = []
            result = [
                {
                    "stream": labels,
                    "values": [[str(int(ts.timestamp() * 1e9)), line] for ts, line in entries],
                }
                for labels, entries in streams
            ]
            return _ok({"resultType": "streams", "result": result})
        if path == "/api/search":
            q = params["q"]
            self.calls.append((path, q))
            traces = self.tempo.get(q)
            if traces is None:
                self.unmatched.append(q)
                traces = []
            return httpx.Response(200, json={"traces": traces})
        return httpx.Response(404)


def _ok(data: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"status": "success", "data": data})
