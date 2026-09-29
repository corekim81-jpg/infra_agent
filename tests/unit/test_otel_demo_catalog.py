"""실제 카탈로그 파일(config/catalog/otel-demo.yaml)의 일관성 검사.

실제 서버에 연결하지 않고, 파일 자체의 규칙만 확인합니다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from infra_agent.catalog import Catalog, EvidenceStatus, load_catalog
from infra_agent.schemas import DataSourceKind, TargetKind

CATALOG = Path(__file__).resolve().parents[2] / "config" / "catalog" / "otel-demo.yaml"
METRIC_IN_TEMPLATE = re.compile(r"([a-zA-Z_:][a-zA-Z0-9_:]*)\{\{")


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return load_catalog(CATALOG)


def test_loads_with_all_agents(catalog: Catalog) -> None:
    agents = {item.agent.value for item in catalog.items.values()}
    assert agents == {"server", "kubernetes", "network", "db", "service"}
    assert catalog.environment == "otel-demo-k3d"


def test_template_metrics_match_requires(catalog: Catalog) -> None:
    for key, item in catalog.items.items():
        used = set(METRIC_IN_TEMPLATE.findall(item.query))
        assert used == set(item.requires_metrics), key


def test_no_assumed_or_excluded_metric_families(catalog: Catalog) -> None:
    for key, item in catalog.items.items():
        for metric in item.requires_metrics:
            assert not metric.startswith(("node_", "kube_", "system_")), (key, metric)


def test_evidence_and_targets(catalog: Catalog) -> None:
    for key, item in catalog.items.items():
        assert item.source is DataSourceKind.PROMETHEUS, key
        assert item.evidence.status is EvidenceStatus.DISCOVERED, key
        assert item.evidence.checked_at is not None, key
        assert item.target_labels, key
        assert item.unit, key


def test_rate_like_queries_use_range(catalog: Catalog) -> None:
    for key, item in catalog.items.items():
        needs_range = any(f in item.query for f in ("rate(", "increase("))
        assert needs_range == item.uses_range, key


@pytest.mark.parametrize("selector", ["", 'k8s_namespace_name="otel-demo"'])
def test_all_items_render(catalog: Catalog, selector: str) -> None:
    for key, item in catalog.items.items():
        expr = item.render(selector, range="5m" if item.uses_range else None)
        assert "{selector}" not in expr and "{range}" not in expr, key
        assert expr.count("(") == expr.count(")"), key
        assert expr.count("{") == expr.count("}"), key
        if selector:
            assert selector in expr, key


def test_hubble_targets_do_not_use_collector_pod_labels(catalog: Catalog) -> None:
    for key, item in catalog.items.items():
        if any(m.startswith("hubble_") for m in item.requires_metrics):
            assert item.target_labels.get(TargetKind.NAMESPACE) in (
                None,
                "destination_namespace",
            ), key
            assert TargetKind.POD not in item.target_labels, key


def test_selector_for_reports_unsupported_targets(catalog: Catalog) -> None:
    item = catalog.items["service.request_rate"]
    selector, unsupported = item.selector_for(
        {TargetKind.SERVICE: "checkout", TargetKind.NODE: "k3d-x"}
    )
    assert selector == 'service="checkout"'
    assert unsupported == [TargetKind.NODE]
