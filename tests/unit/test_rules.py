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
    assert not r.assumptions  # 상태 조회는 현재 값 기준이라 구간 가정을 표시하지 않음
    r2 = interpret("증가한 서버 있어?", NOW, D30)
    assert any("30분" in a for a in r2.assumptions)


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


def test_all_domains_implemented() -> None:
    r = interpret("서비스 응답이 느려진 이유가 네트워크인지 DB인지 분석해 줘", NOW, D30)
    assert {"service", "network", "db"} <= r.domains
    assert r.unsupported_domains == frozenset()  # Service(#23)·DB(#25)·Network(#27) 구현됨
    assert r.intent is Intent.ANOMALY  # "느려진" → 직전 구간 대비 비교
    r2 = interpret("오류가 증가한 시간대의 로그와 트레이스를 연결해서 원인 후보를 알려줘", NOW, D30)
    assert r2.domains == {"service"} and r2.intent is Intent.ANOMALY


def test_db_questions_do_not_pull_in_service() -> None:
    r = interpret("DB 커넥션 풀이 부족하거나 쿼리가 느려진 징후가 있어?", NOW, D30)
    assert r.domains == {"db"}  # "느려진"만으로는 서비스 분석을 붙이지 않음
    assert r.intent is Intent.ANOMALY and not r.unsupported_domains
    assert interpret("valkey 캐시 상태는?", NOW, D30).domains == {"db"}
    both = interpret("서비스 응답 지연이 DB 때문이야?", NOW, D30)
    assert both.domains == {"service", "db"}  # 서비스 키워드가 있으면 함께
    # DB 내부 단어 없이 "느린 이유가 DB인지"는 서비스 지연 질문이므로 서비스와 DB 모두
    assert interpret("checkout 느린 이유가 DB야?", NOW, D30).domains == {"service", "db"}
    assert interpret("checkout 지연이 캐시 때문인지", NOW, D30).domains == {"service", "db"}


def test_keywords_do_not_match_inside_other_words() -> None:
    # "block"의 lock, "catalog"의 log는 키워드가 아님 (영문 키워드는 앞 경계 확인)
    assert interpret("block I/O가 높은 노드", NOW, D30).domains == {"server"}
    assert interpret("catalog 서비스 상태", NOW, D30).domains == {"service"}
    # 쿠버네티스 롤백, 노드 풀, 노드 메모리 캐시는 DB 질문이 아님
    assert interpret("Deployment 롤백 이후 재시작한 Pod 있어?", NOW, D30).domains == {"kubernetes"}
    assert interpret("node pool 노드 상태 알려줘", NOW, D30).domains == {"server"}
    assert interpret("노드 메모리 캐시 사용량이 높아?", NOW, D30).domains == {"server"}
    assert interpret("DB 잠금이나 데드락 있어?", NOW, D30).domains == {"db"}


def test_kubernetes_questions_do_not_pull_in_server() -> None:
    r = interpret("Kubernetes에서 재시작하거나 Pending 상태인 Pod를 확인해 줘", NOW, D30)
    assert r.domains == {"kubernetes"}  # "pod"만으로는 서버 자원 분석을 붙이지 않음
    assert r.unsupported_domains == frozenset()
    assert interpret("노드 NotReady 있어?", NOW, D30).domains == {"kubernetes"}
    both = interpret("메모리 많이 쓰는 Pod가 재시작했어?", NOW, D30)
    assert both.domains == {"server", "kubernetes"}  # 자원 키워드(메모리)가 있으면 함께
    assert interpret("현재 서버 상태가 어때?", NOW, D30).domains == {"server"}


def test_range_clamped_to_retention() -> None:
    r = interpret("지난 30일 서버 상태", NOW, D30)
    assert r.time_range.duration == timedelta(days=7)
    r2 = interpret("최근 5일과 비교해 증가한 서버", NOW, D30)
    assert r2.intent is Intent.STATUS and r2.baseline_range is None
