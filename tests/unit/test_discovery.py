"""지표 탐색 테스트 (가상 백엔드)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from fakes import FakeBackends
from infra_agent.config import load_settings
from infra_agent.discovery import DiscoveryOptions, discover, render_markdown, write_report
from infra_agent.discovery.runner import family_of, name_selector, select_interest

NOW = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)
ENV = {
    "INFRA_AGENT_PROFILE": "dev-tunnel",
    "INFRA_AGENT__DATASOURCES__PROMETHEUS__ENABLED": "true",
    "INFRA_AGENT__DATASOURCES__PROMETHEUS__URL": "http://127.0.0.1:19090",
    "INFRA_AGENT__DATASOURCES__LOKI__ENABLED": "true",
    "INFRA_AGENT__DATASOURCES__LOKI__URL": "http://127.0.0.1:13100",
    "INFRA_AGENT__DATASOURCES__TEMPO__ENABLED": "true",
    "INFRA_AGENT__DATASOURCES__TEMPO__URL": "http://127.0.0.1:13200",
}


async def _run(options: DiscoveryOptions | None = None, env: dict[str, str] = ENV):  # type: ignore[no-untyped-def]
    fake = FakeBackends(NOW)
    report = await discover(
        load_settings(environ=env),
        options,
        environ={},
        transport_factory=fake.transport_factory,
        now=NOW,
    )
    return report, fake


def test_helpers() -> None:
    assert family_of("k8s_node_cpu_usage") == "k8s"
    assert family_of("up") == "up"
    assert name_selector('a"b') == '{__name__="a\\"b"}'
    names = ["a_b_bucket", "a_b_sum", "a_b_count", "k8s_x", "zzz_unrelated", "k8s_node_cpu_usage"]
    picked, omitted, companions = select_interest(names, frozenset({"a"}), max_metrics=2)
    assert picked == ["k8s_node_cpu_usage", "a_b_bucket"]  # 확인 지표 우선
    assert omitted == 1
    assert companions == 2


async def test_discover_prometheus_section() -> None:
    report, fake = await _run()
    prom = report.prometheus
    assert prom is not None and prom.status.ready
    assert all(r.method == "GET" for r in fake.requests)
    assert prom.storage_retention == "15d"
    assert prom.total_metrics == 12
    assert prom.confirmed_present["k8s_node_cpu_usage"] is True
    assert prom.confirmed_present["postgresql_deadlocks_total"] is False
    assert prom.families["k8s"] == 3
    names = [m.name for m in prom.metrics]
    assert "app_frontend_requests_total" not in names  # 관심 계열 아님
    assert "db_client_operation_duration_seconds_sum" not in names  # 히스토그램 동반 지표 생략
    assert prom.histogram_companions_skipped == 2

    cpu = next(m for m in prom.metrics if m.name == "k8s_node_cpu_usage")
    assert cpu.confirmed and cpu.type == "gauge"
    assert cpu.series_count == 3
    assert cpu.latest_age_seconds == 30
    assert "k8s_node_name" in cpu.label_names

    stale = next(m for m in prom.metrics if m.name == "system_cpu_time_seconds_total")
    assert stale.unit == "seconds"

    assert prom.reference_metric == "k8s_node_cpu_usage"
    assert prom.availability == {"1h": True, "6h": True, "24h": True, "7d": False, "30d": False}


async def test_ips_masked_by_default() -> None:
    report, _ = await _run()
    assert report.prometheus is not None
    mem = next(
        m for m in report.prometheus.metrics if m.name == "container_memory_working_set_bytes"
    )
    assert mem.sample_labels[0]["instance"] == "<ip>:10250"
    assert report.loki is not None
    assert report.loki.label_values["host_ip"].samples == ["<ip>"]

    shown, _ = await _run(DiscoveryOptions(mask_ips=False))
    assert shown.prometheus is not None
    mem2 = next(
        m for m in shown.prometheus.metrics if m.name == "container_memory_working_set_bytes"
    )
    assert mem2.sample_labels[0]["instance"] == "10.42.0.7:10250"


async def test_no_values_option() -> None:
    report, fake = await _run(DiscoveryOptions(include_values=False))
    assert report.prometheus is not None
    assert all(m.sample_labels == [] for m in report.prometheus.metrics)
    assert not any("topk" in r.url.params.get("query", "") for r in fake.requests)
    assert report.loki is not None
    assert report.loki.label_values["service_name"].count == 3
    assert report.loki.label_values["service_name"].samples == []


async def test_max_metrics_warning() -> None:
    report, _ = await _run(DiscoveryOptions(max_metrics=2))
    assert report.prometheus is not None
    assert report.prometheus.detailed_count == 2
    assert all(m.confirmed for m in report.prometheus.metrics)
    assert any("생략" in w for w in report.warnings)


async def test_loki_and_tempo_sections() -> None:
    report, _ = await _run()
    assert report.loki is not None and report.loki.status.ready
    assert report.loki.label_names == ["host_ip", "k8s_namespace_name", "service_name"]
    assert report.tempo is not None
    assert report.tempo.tag_names["span"] == ["db.system", "http.response.status_code"]


async def test_no_sources_enabled() -> None:
    report, fake = await _run(env={})
    assert report.prometheus is None and report.loki is None and report.tempo is None
    assert report.warnings
    assert fake.requests == []


async def test_write_report(tmp_path: Path) -> None:
    report, _ = await _run()
    json_path, md_path = write_report(report, tmp_path / "out")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["synthetic"] is False  # 실제 환경 보고서 형식 (테스트 데이터는 가상)
    md = md_path.read_text(encoding="utf-8")
    assert "커밋하지 마세요" in md
    assert "`k8s_node_cpu_usage`" in md
    assert "Loki 라벨" in md and "Tempo 태그" in md
    assert render_markdown(report) == md
