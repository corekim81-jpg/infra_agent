"""가상 데이터 소스 (httpx.MockTransport).

`tests/fixtures/synthetic/otel_demo_backends.json`의 가상 데이터로
Prometheus·Loki·Tempo HTTP API의 읽기 전용 응답을 흉내 냅니다.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic" / "otel_demo_backends.json"
NAME_RE = re.compile(r'__name__="([^"]+)"')


def load_fixture() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["synthetic"] is True
    return data


def _ok(data: Any) -> httpx.Response:
    return httpx.Response(200, json={"status": "success", "data": data})


class FakeBackends:
    """요청 기록과 함께 가상 응답을 돌려주는 백엔드 모음."""

    def __init__(self, now: datetime, *, history_seconds: float = 2 * 86400) -> None:
        self.data = load_fixture()
        self.now = now
        self.history_seconds = history_seconds
        self.requests: list[httpx.Request] = []

    # ------------------------------------------------------------ Prometheus
    def prometheus(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.method == "GET"
        prom = self.data["prometheus"]
        metrics: dict[str, Any] = prom["metrics"]
        path = request.url.path
        params = request.url.params
        if path == "/-/ready":
            return httpx.Response(200, text="Prometheus Server is Ready.")
        if path == "/api/v1/status/buildinfo":
            return _ok({"version": prom["version"]})
        if path == "/api/v1/status/runtimeinfo":
            return _ok({"storageRetention": prom["storage_retention"]})
        if path == "/api/v1/label/__name__/values":
            return _ok(sorted(metrics))
        if path == "/api/v1/metadata":
            return _ok(
                {
                    name: [
                        {"type": m["type"], "unit": m.get("unit", ""), "help": m.get("help", "")}
                    ]
                    for name, m in metrics.items()
                }
            )
        if path == "/api/v1/labels":
            match = NAME_RE.search(params.get("match[]", ""))
            if match is None:
                return _ok(sorted({lbl for m in metrics.values() for lbl in m["labels"]}))
            return _ok(sorted(metrics.get(match.group(1), {}).get("labels", [])))
        if path == "/api/v1/query":
            return self._prom_query(params.get("query", ""), params.get("time"), metrics)
        return httpx.Response(404, text="not found")

    def _prom_query(self, query: str, time: str | None, metrics: dict[str, Any]) -> httpx.Response:
        if query == "bad(":
            return httpx.Response(
                400,
                json={"status": "error", "errorType": "bad_data", "error": "parse error"},
            )
        ts = float(time) if time else self.now.timestamp()
        match = NAME_RE.search(query)
        metric = metrics.get(match.group(1)) if match else None
        too_old = self.now.timestamp() - ts > self.history_seconds
        if metric is None or metric["series"] == 0 or too_old:
            return _ok({"resultType": "vector", "result": []})
        if query.startswith("count("):
            return _ok(
                {
                    "resultType": "vector",
                    "result": [{"metric": {}, "value": [ts, str(metric["series"])]}],
                }
            )
        if query.startswith("max(timestamp("):
            return _ok(
                {"resultType": "vector", "result": [{"metric": {}, "value": [ts, str(ts - 30)]}]}
            )
        if query.startswith("topk("):
            name = match.group(1) if match else ""
            result = [
                {"metric": {"__name__": name, **labels}, "value": [ts, "1"]}
                for labels in metric["sample"]
            ]
            return _ok({"resultType": "vector", "result": result})
        return _ok({"resultType": "vector", "result": []})

    # ------------------------------------------------------------ Loki
    def loki(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.method == "GET"
        loki = self.data["loki"]
        path = request.url.path
        if path == "/ready":
            return httpx.Response(200, text="ready")
        if path == "/loki/api/v1/status/buildinfo":
            return httpx.Response(200, json={"version": loki["version"]})
        if path == "/loki/api/v1/labels":
            return _ok(sorted(loki["labels"]))
        m = re.fullmatch(r"/loki/api/v1/label/([^/]+)/values", path)
        if m:
            return _ok(loki["labels"].get(m.group(1), []))
        return httpx.Response(404, text="not found")

    # ------------------------------------------------------------ Tempo
    def tempo(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.method == "GET"
        tempo = self.data["tempo"]
        path = request.url.path
        if path == "/ready":
            return httpx.Response(200, text="ready")
        if path == "/api/status/buildinfo":
            return httpx.Response(200, json={"version": tempo["version"]})
        if path == "/api/v2/search/tags":
            return httpx.Response(
                200,
                json={"scopes": [{"name": k, "tags": v} for k, v in tempo["scopes"].items()]},
            )
        return httpx.Response(404, text="not found")

    def transport_factory(self, name: str) -> httpx.AsyncBaseTransport:
        handler = {"prometheus": self.prometheus, "loki": self.loki, "tempo": self.tempo}[name]
        return httpx.MockTransport(handler)
