"""조회 카탈로그 로더 테스트. 카탈로그 내용은 가상 예시입니다."""

from __future__ import annotations

from pathlib import Path

import pytest

from infra_agent.catalog import CatalogError, EvidenceStatus, load_catalog
from infra_agent.schemas import AgentName, TargetKind

VALID = """
version: 1
environment: synthetic-test
items:
  node.cpu_usage:
    agent: server
    source: prometheus
    description: 노드 CPU 사용량 (가상 예시)
    unit: cores
    query: 'sum by ({node_label}) (k8s_node_cpu_usage{{{selector}}})'
    requires_metrics: [k8s_node_cpu_usage]
    labels: {node_label: k8s_node_name}
    target_labels: {node: k8s_node_name, namespace: k8s_namespace_name}
    evidence: {status: discovered, checked_at: 2026-09-29}
    caveats: [k3d 노드 값이며 물리 서버 전체 자원이 아님]
  network.drop_rate:
    agent: network
    source: prometheus
    description: 드롭 증가율 (가상 예시)
    query: 'sum by (reason) (rate(hubble_drop_total{{{selector}}}[{range}]))'
    requires_metrics: [hubble_drop_total]
    evidence: {status: user_confirmed}
  db.pool_usage:
    agent: db
    source: prometheus
    description: 커넥션 풀 사용률 (가상 예시)
    query: 'db_client_connection_count{{{selector}}} / db_client_connection_max{{{selector}}}'
    requires_metrics: [db_client_connection_count, db_client_connection_max]
    evidence: {status: verified}
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "catalog.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_and_render(tmp_path: Path) -> None:
    cat = load_catalog(_write(tmp_path, VALID))
    item = cat.items["node.cpu_usage"]
    assert item.agent is AgentName.SERVER
    assert item.render('k8s_node_name="n1"') == (
        'sum by (k8s_node_name) (k8s_node_cpu_usage{k8s_node_name="n1"})'
    )
    assert item.render() == "sum by (k8s_node_name) (k8s_node_cpu_usage{})"


def test_range_placeholder(tmp_path: Path) -> None:
    item = load_catalog(_write(tmp_path, VALID)).items["network.drop_rate"]
    assert item.uses_range
    assert item.render("", range="5m").endswith("[5m]))")
    with pytest.raises(CatalogError, match="range"):
        item.render("")
    with pytest.raises(ValueError):
        item.render("", range="5 minutes")


def test_selector_braces_rejected(tmp_path: Path) -> None:
    item = load_catalog(_write(tmp_path, VALID)).items["node.cpu_usage"]
    with pytest.raises(CatalogError):
        item.render('}) or vector(1) or foo{x="')


def test_usable_filters_status_and_metrics(tmp_path: Path) -> None:
    cat = load_catalog(_write(tmp_path, VALID))
    available = {"k8s_node_cpu_usage", "hubble_drop_total", "db_client_connection_count"}
    usable = cat.usable(available)
    assert set(usable) == {"node.cpu_usage"}  # drop_rate: user_confirmed, pool: 지표 부족
    assert set(cat.usable(available, EvidenceStatus.USER_CONFIRMED)) == {
        "node.cpu_usage",
        "network.drop_rate",
    }
    assert cat.items["db.pool_usage"].missing_metrics(available) == ["db_client_connection_max"]
    assert set(cat.for_agent(AgentName.DB)) == {"db.pool_usage"}


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        (VALID.replace("{node_label}) (k8s", "{nodelabel}) (k8s"), "자리표시자"),
        (VALID.replace("agent: server", "agent: coordinator"), "전문 에이전트"),
        (VALID.replace("node.cpu_usage:", "cpu:"), "domain.name"),
        (VALID.replace("{node_label: k8s_node_name}", "{node_label: 'k8s-node'}"), "라벨 이름"),
        (VALID.replace("[k8s_node_cpu_usage]", "[]"), "requires_metrics"),
        (VALID.replace("status: verified", "status: maybe"), "status"),
        (VALID.replace("version: 1", "version: 2"), "version"),
        (VALID.replace("    unit: cores\n", "    unit: cores\n    extra: 1\n"), "extra"),
    ],
)
def test_invalid_catalog(tmp_path: Path, bad: str, message: str) -> None:
    with pytest.raises(CatalogError, match=message):
        load_catalog(_write(tmp_path, bad))


def test_missing_catalog_file(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="찾을 수 없습니다"):
        load_catalog(tmp_path / "none.yaml")


def test_selector_for(tmp_path: Path) -> None:
    item = load_catalog(_write(tmp_path, VALID)).items["node.cpu_usage"]
    selector, unsupported = item.selector_for(
        {TargetKind.NODE: 'k3d-"x"', TargetKind.SERVICE: "cart"}
    )
    assert selector == 'k8s_node_name="k3d-\\"x\\""'
    assert unsupported == [TargetKind.SERVICE]
    assert item.render(selector).endswith('{k8s_node_name="k3d-\\"x\\""})')


def test_bad_target_label_rejected(tmp_path: Path) -> None:
    bad = VALID.replace("target_labels: {node: k8s_node_name,", "target_labels: {node: 'k8s-node',")
    with pytest.raises(CatalogError, match="target_labels"):
        load_catalog(_write(tmp_path, bad))
    worse = VALID.replace("target_labels: {node: k8s_node_name,", "target_labels: {rack: x,")
    with pytest.raises(CatalogError):
        load_catalog(_write(tmp_path, worse))
