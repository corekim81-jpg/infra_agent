"""조회식·평가 시각별로 가상 결과를 돌려주는 Prometheus (httpx.MockTransport).

테스트가 실제 카탈로그로 조회식을 렌더링해 응답을 등록하므로,
에이전트가 보내는 조회식 그대로를 검증합니다.
등록되지 않은 조회는 빈 결과를 돌려주고 `unmatched`에 기록합니다. 모든 값은 가상입니다.
"""

from __future__ import annotations

from datetime import datetime

import httpx

Rows = list[tuple[dict[str, str], float]]


def ts(value: datetime) -> str:
    return f"{value.timestamp():.3f}"


class ExprProm:
    def __init__(self, *, freshness_seconds: float = 15.0) -> None:
        self.responses: dict[tuple[str, str | None], Rows] = {}
        self.errors: dict[str, str] = {}
        self.freshness_seconds = freshness_seconds
        self.queries: list[tuple[str, str | None]] = []
        self.unmatched: list[str] = []

    def add(self, expr: str, rows: Rows, at: datetime | None = None) -> None:
        self.responses[(expr, ts(at) if at else None)] = rows

    def fail(self, expr: str, message: str = "synthetic failure") -> None:
        self.errors[expr] = message

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        if request.url.path != "/api/v1/query":
            return httpx.Response(404)
        expr = request.url.params["query"]
        at = request.url.params.get("time")
        self.queries.append((expr, at))
        if expr in self.errors:
            return httpx.Response(400, json={"status": "error", "error": self.errors[expr]})
        if expr.startswith("time() - max(timestamp("):
            result = [{"metric": {}, "value": [0, str(self.freshness_seconds)]}]
        else:
            rows = self.responses.get((expr, at), self.responses.get((expr, None)))
            if rows is None:
                self.unmatched.append(expr)
                rows = []
            result = [{"metric": labels, "value": [0, str(v)]} for labels, v in rows]
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "vector", "result": result}}
        )

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
