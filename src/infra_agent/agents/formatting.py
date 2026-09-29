"""값·대상 표시용 공통 함수."""

from __future__ import annotations

from collections.abc import Mapping

from infra_agent.schemas import TargetKind, TargetRef


def fmt_value(value: float, unit: str | None) -> str:
    if unit == "ratio":
        return f"{value * 100:.1f}%"
    if unit == "bytes":
        size = float(value)
        for suffix in ("B", "KiB", "MiB", "GiB", "TiB"):
            if abs(size) < 1024 or suffix == "TiB":
                return f"{size:.0f}{suffix}" if suffix == "B" else f"{size:.1f}{suffix}"
            size /= 1024
    if unit == "cores":
        return f"{value:.3f} cores"
    return f"{value:.3g}{(' ' + unit) if unit else ''}"


def fmt_delta(value: float, unit: str | None) -> str:
    sign = "+" if value >= 0 else "-"
    return sign + fmt_value(abs(value), unit)


_ENTITY_LABELS: dict[str, tuple[TargetKind, tuple[str, ...]]] = {
    "node": (TargetKind.NODE, ("k8s_node_name",)),
    "pod": (TargetKind.POD, ("k8s_namespace_name", "k8s_pod_name")),
    "container": (
        TargetKind.CONTAINER,
        ("k8s_namespace_name", "k8s_pod_name", "k8s_container_name"),
    ),
}


def entity_of(item_key: str, labels: Mapping[str, str]) -> TargetRef:
    """카탈로그 항목 종류(node/pod/container)와 결과 라벨로 대상 식별자를 만듭니다."""
    domain = item_key.split(".", 1)[0]
    kind, names = _ENTITY_LABELS.get(domain, (TargetKind.HOST, ()))
    parts = [labels.get(n, "?") for n in names] if names else [str(dict(labels))]
    used = {n: labels[n] for n in names if n in labels}
    return TargetRef(kind=kind, name="/".join(parts), labels=used)


def row_key(labels: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((k, v) for k, v in labels.items() if k != "__name__"))
