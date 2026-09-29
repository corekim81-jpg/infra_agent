from __future__ import annotations

from datetime import UTC, datetime, timedelta

from infra_agent.orchestration.rules import interpret
from infra_agent.schemas import Intent, TargetKind

NOW = datetime(2026, 9, 29, 3, 0, tzinfo=UTC)
D30 = timedelta(minutes=30)


def test_status_question_defaults() -> None:
    r = interpret("현재 서버 상태가 어때?", NOW, D30)
    assert r.intent is Intent.STATUS
    assert r.time_range.duration == D30 and r.time_range.end == NOW
    assert r.baseline_range is None
    assert r.domains == {"server"} and not r.unsupported_domains
    assert any("30분" in a for a in r.assumptions)


def test_anomaly_question_with_duration() -> None:
    r = interpret("최근 30분 동안 CPU나 메모리가 비정상적으로 증가한 서버가 있어?", NOW, D30)
    assert r.intent is Intent.ANOMALY
    assert r.baseline_range is not None
    assert r.baseline_range.end == r.time_range.start
    assert r.baseline_range.duration == D30
    assert not r.assumptions


def test_compare_question() -> None:
    r = interpret("직전 1시간과 비교해서 현재 상태가 어떻게 달라졌어?", NOW, D30)
    assert r.intent is Intent.COMPARE
    assert r.time_range.duration == timedelta(hours=1)
    assert r.domains == {"server"}  # 분야 키워드 없음 → 서버로 가정
    assert any("분야" in a for a in r.assumptions)


def test_targets_extracted_only_for_real_names() -> None:
    r = interpret("네임스페이스 otel-demo 의 pod cart-7d9f 메모리 상태", NOW, D30)
    assert r.targets == {TargetKind.NAMESPACE: "otel-demo", TargetKind.POD: "cart-7d9f"}
    r2 = interpret("node cpu 사용률 어때?", NOW, D30)
    assert r2.targets == {}
    r3 = interpret("노드 k3d-infra-agent-0 상태", NOW, D30)
    assert r3.targets == {TargetKind.NODE: "k3d-infra-agent-0"}


def test_overrides_win() -> None:
    r = interpret(
        "네임스페이스 otel-demo 상태",
        NOW,
        D30,
        range_override=timedelta(minutes=15),
        target_overrides={TargetKind.NAMESPACE: "kube-system"},
    )
    assert r.targets[TargetKind.NAMESPACE] == "kube-system"
    assert r.time_range.duration == timedelta(minutes=15)


def test_unsupported_domains_reported() -> None:
    r = interpret("서비스 응답이 느려진 이유가 네트워크인지 DB인지 분석해 줘", NOW, D30)
    assert {"service", "network", "db"} <= r.domains
    assert r.unsupported_domains == r.domains
    r2 = interpret("재시작하거나 Pending 상태인 Pod를 확인해 줘", NOW, D30)
    assert "kubernetes" in r2.unsupported_domains
    assert "server" in r2.domains  # "pod" 키워드


def test_range_clamped_to_retention() -> None:
    r = interpret("지난 30일 서버 상태", NOW, D30)
    assert r.time_range.duration == timedelta(days=7)
    r2 = interpret("최근 5일과 비교해 증가한 서버", NOW, D30)
    assert r2.intent is Intent.STATUS and r2.baseline_range is None
