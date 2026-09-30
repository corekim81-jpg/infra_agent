"""값·대상 표시용 공통 함수."""

from __future__ import annotations

from collections.abc import Mapping

from infra_agent.schemas import AnalysisContext, TargetKind, TargetRef
from infra_agent.units import fmt_delta, fmt_value

__all__ = ["count_text", "entity_of", "fmt_delta", "fmt_value", "row_key", "window_text"]


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


def count_text(value: float) -> str:
    """increase() 추정값 표시 (정수에 가까우면 정수, 아니면 '약 N.N')."""
    rounded = round(value)
    if abs(value - rounded) < 0.05:
        return f"{rounded}회"
    return f"약 {value:.1f}회"


def window_text(ctx: AnalysisContext) -> str:
    """분석 구간 길이 표시 (예: 최근 30분, 최근 2시간)."""
    minutes = int(ctx.time_range.duration.total_seconds() // 60)
    if minutes >= 120 and minutes % 60 == 0:
        return f"최근 {minutes // 60}시간"
    return f"최근 {minutes}분"
