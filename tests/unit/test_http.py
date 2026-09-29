"""읽기 전용 HTTP 기반 테스트 (가상 응답)."""

from __future__ import annotations

import httpx
import pytest
import respx

from infra_agent.config.settings import HttpDatasourceConfig
from infra_agent.datasources import (
    ConnectFailedError,
    DataSourceTimeoutError,
    HttpStatusError,
    MissingCredentialError,
    QueryError,
    ResponseFormatError,
)
from infra_agent.datasources.http import HttpDataSource
from infra_agent.security import MASK

BASE = "http://prom.synthetic.test"
CFG = HttpDatasourceConfig(enabled=True, url=BASE, timeout_seconds=1)


class _NoSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _client(max_retries: int = 2, **kwargs: object) -> tuple[HttpDataSource, _NoSleep]:
    sleep = _NoSleep()
    client = HttpDataSource(
        "prometheus",
        kwargs.pop("config", CFG),  # type: ignore[arg-type]
        allowed_path_prefixes=("/api/v1/", "/-/ready"),
        max_retries=max_retries,
        sleep=sleep,
        **kwargs,  # type: ignore[arg-type]
    )
    return client, sleep


@respx.mock
async def test_get_json_success() -> None:
    route = respx.get(f"{BASE}/api/v1/query").mock(return_value=httpx.Response(200, json={"a": 1}))
    client, _ = _client()
    async with client:
        assert await client.get_json("/api/v1/query", [("query", "up")]) == {"a": 1}
    assert route.calls.last.request.url.params["query"] == "up"
    assert route.calls.last.request.method == "GET"


async def test_disallowed_path_rejected() -> None:
    client, _ = _client()
    async with client:
        with pytest.raises(ValueError, match="허용되지 않은"):
            await client.get_raw("/api/v2/admin/tsdb/delete_series")
        with pytest.raises(ValueError):
            await client.get_raw("/api/v1/../admin")
        with pytest.raises(ValueError):
            await client.get_raw("api/v1/query")


def test_no_write_methods_exposed() -> None:
    public = {n for n in dir(HttpDataSource) if not n.startswith("_")}
    assert not public & {"post", "put", "patch", "delete", "request"}


@respx.mock
async def test_retries_transient_then_succeeds() -> None:
    route = respx.get(f"{BASE}/api/v1/query").mock(
        side_effect=[
            httpx.Response(503, text="busy"),
            httpx.Response(429, text="slow down"),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    client, sleep = _client(max_retries=2)
    async with client:
        assert await client.get_json("/api/v1/query") == {"ok": True}
    assert route.call_count == 3
    assert sleep.calls == [0.5, 1.0]


@respx.mock
async def test_retries_exhausted() -> None:
    route = respx.get(f"{BASE}/api/v1/query").mock(return_value=httpx.Response(502, text="bad"))
    client, _ = _client(max_retries=1)
    async with client:
        with pytest.raises(HttpStatusError) as info:
            await client.get_json("/api/v1/query")
    assert info.value.code == "http_502"
    assert route.call_count == 2


@respx.mock
async def test_query_error_not_retried() -> None:
    route = respx.get(f"{BASE}/api/v1/query").mock(
        return_value=httpx.Response(400, json={"status": "error", "error": "parse error at char 5"})
    )
    client, _ = _client()
    async with client:
        with pytest.raises(QueryError, match="parse error"):
            await client.get_json("/api/v1/query")
    assert route.call_count == 1


@respx.mock
async def test_not_found_not_retried() -> None:
    route = respx.get(f"{BASE}/api/v1/x").mock(return_value=httpx.Response(404, text="nope"))
    client, _ = _client()
    async with client:
        with pytest.raises(HttpStatusError) as info:
            await client.get_json("/api/v1/x")
    assert info.value.code == "http_404"
    assert not info.value.retryable
    assert route.call_count == 1


@respx.mock
async def test_connect_error_retried_and_classified() -> None:
    route = respx.get(f"{BASE}/api/v1/query").mock(side_effect=httpx.ConnectError("refused"))
    client, sleep = _client(max_retries=2)
    async with client:
        with pytest.raises(ConnectFailedError):
            await client.get_json("/api/v1/query")
    assert route.call_count == 3
    assert len(sleep.calls) == 2


@respx.mock
async def test_timeout_classified() -> None:
    respx.get(f"{BASE}/api/v1/query").mock(side_effect=httpx.ReadTimeout("slow"))
    client, _ = _client(max_retries=0)
    async with client:
        with pytest.raises(DataSourceTimeoutError):
            await client.get_json("/api/v1/query")


@respx.mock
async def test_invalid_json() -> None:
    respx.get(f"{BASE}/api/v1/query").mock(return_value=httpx.Response(200, text="<html>"))
    client, _ = _client()
    async with client:
        with pytest.raises(ResponseFormatError):
            await client.get_json("/api/v1/query")


@respx.mock
async def test_token_from_env_is_sent_and_masked() -> None:
    token = "synthetic-token-value-123"
    cfg = HttpDatasourceConfig(enabled=True, url=BASE, token_env="SYN_PROM_TOKEN")
    route = respx.get(f"{BASE}/api/v1/query").mock(
        return_value=httpx.Response(500, text=f"echo {token}")
    )
    client, _ = _client(max_retries=0, config=cfg, environ={"SYN_PROM_TOKEN": token})
    async with client:
        with pytest.raises(HttpStatusError) as info:
            await client.get_json("/api/v1/query")
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {token}"
    assert token not in str(info.value)
    assert MASK in str(info.value)


def test_missing_token_env() -> None:
    cfg = HttpDatasourceConfig(enabled=True, url=BASE, token_env="SYN_MISSING_TOKEN")
    with pytest.raises(MissingCredentialError, match="SYN_MISSING_TOKEN"):
        _client(config=cfg, environ={})


def test_url_required() -> None:
    with pytest.raises(ValueError):
        _client(config=HttpDatasourceConfig())
