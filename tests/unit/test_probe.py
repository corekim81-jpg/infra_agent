from __future__ import annotations

from datetime import UTC, datetime

import httpx

from fakes import FakeBackends
from infra_agent.config import load_settings
from infra_agent.datasources.probe import check_sources

NOW = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)

ENV_ALL = {
    "INFRA_AGENT_PROFILE": "dev-tunnel",
    "INFRA_AGENT__DATASOURCES__PROMETHEUS__ENABLED": "true",
    "INFRA_AGENT__DATASOURCES__PROMETHEUS__URL": "http://127.0.0.1:19090",
    "INFRA_AGENT__DATASOURCES__LOKI__ENABLED": "true",
    "INFRA_AGENT__DATASOURCES__LOKI__URL": "http://127.0.0.1:13100",
    "INFRA_AGENT__DATASOURCES__TEMPO__ENABLED": "true",
    "INFRA_AGENT__DATASOURCES__TEMPO__URL": "http://127.0.0.1:13200",
}


async def test_all_sources_ready() -> None:
    fake = FakeBackends(NOW)
    statuses = await check_sources(
        load_settings(environ=ENV_ALL), environ={}, transport_factory=fake.transport_factory
    )
    by_name = {s.name: s for s in statuses}
    assert all(s.reachable and s.ready for s in statuses)
    assert by_name["prometheus"].version == "3.0.0-synthetic"
    assert by_name["loki"].version == "3.4.0-synthetic"
    assert by_name["tempo"].version == "2.7.0-synthetic"


async def test_disabled_sources_not_contacted() -> None:
    fake = FakeBackends(NOW)
    statuses = await check_sources(
        load_settings(environ={}), environ={}, transport_factory=fake.transport_factory
    )
    assert all(not s.enabled for s in statuses)
    assert fake.requests == []


async def test_connect_failure_gives_tunnel_hint() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    statuses = await check_sources(
        load_settings(environ=ENV_ALL),
        environ={},
        transport_factory=lambda name: httpx.MockTransport(refuse),
    )
    prom = next(s for s in statuses if s.name == "prometheus")
    assert not prom.reachable
    assert prom.error_code == "connect_error"
    assert prom.hint is not None and "SSH 터널" in prom.hint


async def test_buildinfo_failure_still_reachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/-/ready":
            return httpx.Response(200, text="ready")
        return httpx.Response(404)

    env = {k: v for k, v in ENV_ALL.items() if "LOKI" not in k and "TEMPO" not in k}
    statuses = await check_sources(
        load_settings(environ=env),
        environ={},
        transport_factory=lambda name: httpx.MockTransport(handler),
    )
    prom = next(s for s in statuses if s.name == "prometheus")
    assert prom.reachable and prom.ready and prom.version is None


async def test_missing_token_reported() -> None:
    env = dict(ENV_ALL, INFRA_AGENT__DATASOURCES__PROMETHEUS__TOKEN_ENV="SYN_TOKEN")
    fake = FakeBackends(NOW)
    statuses = await check_sources(
        load_settings(environ=env), environ={}, transport_factory=fake.transport_factory
    )
    prom = next(s for s in statuses if s.name == "prometheus")
    assert prom.error_code == "missing_credential"
