"""분야 간 교차 확인 테스트 (가상 데이터, 에이전트 결과를 직접 구성)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from infra_agent.agents.upstream import has_core_judgement, service_db_calls, service_issues
from infra_agent.answer.cross import CROSS_NOTE, cross_check
from infra_agent.answer.synthesis import synthesize
from infra_agent.orchestration.rules import interpret
from infra_agent.schemas import (
    AgentName,
    AgentResult,
    AgentStatus,
    Confidence,
    DataSourceKind,
    ErrorInfo,
    Finding,
    FindingKind,
    JudgementBasis,
    Severity,
    TargetKind,
    TargetRef,
    ToolResult,
    ToolStatus,
)

NOW = datetime(2026, 9, 30, 8, 0, tzinfo=UTC)


def _ev(evidence_id: str, rows: list[dict[str, object]] | None = None) -> ToolResult:
    return ToolResult(
        evidence_id=evidence_id,
        source=DataSourceKind.PROMETHEUS,
        query="q",
        status=ToolStatus.OK,
        data=rows or [],
        fetched_at=NOW,
        freshness_seconds=5.0,
        synthetic=True,
    )


def _fact(
    statement: str,
    evidence_id: str,
    target: TargetRef | None = None,
    severity: Severity = Severity.WARNING,
) -> Finding:
    return Finding(
        kind=FindingKind.FACT,
        statement=statement,
        severity=severity,
        targets=(target,) if target else (),
        evidence_ids=(evidence_id,),
        basis=JudgementBasis.THRESHOLD,
    )


def _result(
    agent: AgentName,
    findings: tuple[Finding, ...],
    evidence: tuple[ToolResult, ...],
    status: AgentStatus = AgentStatus.SUCCESS,
) -> AgentResult:
    return AgentResult(
        task_id=f"{agent.value}-1",
        agent=agent,
        status=status,
        findings=findings,
        evidence=evidence,
        limitations=("x",) if status is not AgentStatus.SUCCESS else (),
    )


DB_SPANS = _ev(
    "service.db_span_latency_p95@current",
    [
        {"labels": {"service": "accounting", "db_system_name": "PostgreSQL"}, "value": 0.01},
        {"labels": {"service": "cart", "db_system_name": "redis"}, "value": 0.002},
    ],
)
SERVICE = _result(
    AgentName.SERVICE,
    (
        _fact(
            "서비스 응답 지연 p95 증가: accounting 평균 0.1초 → 0.5초 (+400%)",
            "service.latency_p95@window_avg",
            TargetRef(kind=TargetKind.SERVICE, name="accounting"),
        ),
        _fact(
            "서비스 간 호출 지연 p95(서버 측)(현재): ad → flagd 12.8초",
            "service.dependency_latency_p95@current",
            TargetRef(
                kind=TargetKind.SERVICE,
                name="ad → flagd",
                labels={"client": "ad", "server": "flagd"},
            ),
        ),
        _fact(
            "서비스 오류율: 모두 기준 미만", "service.error_ratio@current", severity=Severity.INFO
        ),
    ),
    (
        _ev("service.latency_p95@window_avg"),
        _ev("service.dependency_latency_p95@current"),
        _ev("service.error_ratio@current"),
        DB_SPANS,
    ),
)
DB = _result(
    AgentName.DB,
    (
        _fact(
            "DB 작업 지연 p95 증가: accounting 평균 0.003초 → 0.216초",
            "db.client_operation_latency_p95@window_avg",
            TargetRef(
                kind=TargetKind.SERVICE,
                name="accounting insert",
                labels={"service_name": "accounting", "db_operation_name": "insert"},
            ),
        ),
        _fact(
            "PostgreSQL 데드락 (최근 30분): otel 1회",
            "db.pg_deadlocks_increase@window",
            TargetRef(
                kind=TargetKind.DATABASE, name="otel", labels={"postgresql_database_name": "otel"}
            ),
        ),
        _fact(
            "Valkey 연결 거부 (최근 30분): valkey-cart 3회",
            "cache.valkey_rejected_increase@window",
            TargetRef(
                kind=TargetKind.SERVICE, name="valkey-cart", labels={"service_name": "valkey-cart"}
            ),
        ),
    ),
    (
        _ev("db.client_operation_latency_p95@window_avg"),
        _ev("db.pg_deadlocks_increase@window"),
        _ev("cache.valkey_rejected_increase@window"),
    ),
)
NETWORK_OK = _result(
    AgentName.NETWORK,
    (
        _fact(
            "컨테이너 패킷 드롭: 발생 없음", "network.drops_increase@window", severity=Severity.INFO
        ),
    ),
    (_ev("network.drops_increase@window"),),
)


def test_service_issues_and_db_calls() -> None:
    issues = service_issues([SERVICE])
    assert list(issues) == ["accounting", "flagd"]
    assert issues["accounting"].aspects == ("응답 지연",)
    assert issues["flagd"].label == "flagd(호출받는 지연)"
    assert issues["flagd"].evidence_ids == ("service.dependency_latency_p95@current",)
    assert service_db_calls([SERVICE], 300) == {
        "accounting": frozenset({"postgresql"}),
        "cart": frozenset({"redis"}),
    }
    # DB 호출 span을 조회하지 못했으면 "호출 없음"이 아니라 모름(None)
    no_spans = _result(AgentName.SERVICE, SERVICE.findings, SERVICE.evidence[:3])
    assert service_db_calls([no_spans], 300) is None
    # Service가 조회하지 못해도 DB Agent의 DB 호출 span 결과(같은 라벨)를 씀
    db_spans = _result(
        AgentName.DB,
        (),
        (
            _ev(
                "db.span_latency_p95@window_avg",
                [{"labels": {"service": "quote", "db_system_name": "sqlite"}, "value": 0.1}],
            ),
        ),
    )
    assert service_db_calls([no_spans, db_spans], 300) == {"quote": frozenset({"sqlite"})}
    failed = AgentResult(
        task_id="s",
        agent=AgentName.SERVICE,
        status=AgentStatus.FAILED,
        errors=(ErrorInfo(code="timeout", message="t"),),
    )
    assert service_issues([failed]) == {}
    # 빈 결과·오래된 결과로는 "DB 호출 없음"이라고 하지 않음 (모름 = None)
    stale = _result(
        AgentName.SERVICE,
        (),
        (DB_SPANS.model_copy(update={"freshness_seconds": 900.0}),),
    )
    assert service_db_calls([stale], 300) is None
    empty = _result(
        AgentName.SERVICE,
        (),
        (DB_SPANS.model_copy(update={"status": ToolStatus.EMPTY, "data": []}),),
    )
    assert service_db_calls([empty], 300) is None


def test_core_judgement_requires_threshold_or_baseline() -> None:
    """판정 기준 미적용(STATE) 사실은 오류율 근거를 써도 "이상 대상 없음"의 근거가 아님."""
    state_only = _result(
        AgentName.SERVICE,
        (
            Finding(
                kind=FindingKind.FACT,
                statement="SERVER 외 span의 오류율(현재, 판정 기준 미적용): ad(CLIENT) 100.0%",
                evidence_ids=("service.error_ratio@current",),
                basis=JudgementBasis.STATE,
            ),
        ),
        (_ev("service.error_ratio@current"),),
    )
    assert not has_core_judgement([state_only])
    assert cross_check([state_only, DB]).lines == (
        "서비스: 오류율·응답 지연 판정 결과가 없어 이상 대상을 정하지 못함",
    )
    assert has_core_judgement([SERVICE])


def test_issues_are_ordered_critical_first() -> None:
    critical = SERVICE.findings[1].model_copy(update={"severity": Severity.CRITICAL})
    service = SERVICE.model_copy(update={"findings": (SERVICE.findings[0], critical)})
    assert list(service_issues([service])) == ["flagd", "accounting"]


def test_links_direct_indirect_and_unlinked() -> None:
    cross = cross_check([SERVICE, NETWORK_OK, DB])
    assert cross.lines[0] == "서비스 이상 대상: accounting(응답 지연), flagd(호출받는 지연)"
    assert "네트워크: 확인한 항목에서 기준을 넘는 이상 없음" in cross.lines
    db_line = next(x for x in cross.lines if x.startswith("DB·캐시:"))
    # accounting DB 작업 지연(같은 대상)·데드락(postgresql 호출) 연결, Valkey는 미연결
    assert db_line.startswith("DB·캐시: 이상 3건, 그중 서비스 이상 대상과 연결 2건")
    assert "연결되지 않은 이상: Valkey 연결 거부" in db_line
    # 서비스별 판정: 네트워크는 서비스 단위 결과가 없어 "이상 없음"이라고 말하지 않음
    assert (
        "accounting(응답 지연): 연결된 이상 — DB·캐시; "
        "이 서비스와 연결할 수 있는 결과가 없어 판단하지 못함 — 네트워크"
    ) in cross.lines
    assert (
        "flagd(호출받는 지연): DB 호출 span에 이 서비스의 DB 호출이 없어 DB 이상과 연결하지 않음 — "
        "DB·캐시; 이 서비스와 연결할 수 있는 결과가 없어 판단하지 못함 — 네트워크. "
        "이 결과만으로는 원인 분야를 가리지 못함"
    ) in cross.lines
    [corr] = cross.correlations
    assert corr.kind is FindingKind.HYPOTHESIS and corr.basis is JudgementBasis.CORRELATION
    assert corr.confidence is Confidence.MEDIUM  # 같은 대상 연결이 있음
    assert corr.severity is Severity.WARNING
    assert "accounting에서 postgresql 호출, DB 호출 span 기준" in corr.statement
    assert "인과는 확인되지 않음" in corr.statement
    assert corr.evidence_ids == (
        "service.latency_p95@window_avg",
        "db.client_operation_latency_p95@window_avg",
        "db.pg_deadlocks_increase@window",
    )
    # 네트워크는 서비스 단위 결과가 없어 "연결 0개"로 세지 않고 판단 불가로 따로 적음
    assert cross.summary == (
        "분야 간 교차 확인: 서비스 이상 대상 2개 중 DB·캐시 이상과 연결된 대상 1개 "
        "(같은 구간 동시 발생 기준, 인과 미확인). 네트워크 분야는 서비스 단위로 연결할 수 있는 "
        "결과가 없어 연결 여부를 판단하지 못했습니다."
    )
    assert CROSS_NOTE in cross.scope_notes and CROSS_NOTE not in cross.limitations


def test_indirect_only_is_low_confidence_and_issue_severity_counts() -> None:
    db = _result(AgentName.DB, DB.findings[1:2], DB.evidence[1:2])
    [corr] = cross_check([SERVICE, db]).correlations
    assert corr.confidence is Confidence.LOW
    critical = SERVICE.findings[0].model_copy(update={"severity": Severity.CRITICAL})
    service = SERVICE.model_copy(update={"findings": (critical, *SERVICE.findings[1:])})
    [corr2] = cross_check([service, db]).correlations
    assert corr2.severity is Severity.CRITICAL  # 서비스 이상의 심각도도 반영


def test_network_focus_fact_links_and_clears() -> None:
    def focus(severity: Severity) -> AgentResult:
        return _result(
            AgentName.NETWORK,
            (
                _fact(
                    "Service 이상 대상 flagd(호출받는 지연)의 네트워크: ...",
                    "network.workload_ingress_by_verdict@current",
                    TargetRef(kind=TargetKind.SERVICE, name="flagd", labels={"service": "flagd"}),
                    severity=severity,
                ),
            ),
            (_ev("network.workload_ingress_by_verdict@current"),),
        )

    cross = cross_check([SERVICE, focus(Severity.WARNING)])
    assert [c.targets[0].name for c in cross.correlations] == ["flagd"]
    assert "네트워크 이상과 연결된 대상 1개" in cross.summary
    clear = cross_check([SERVICE, focus(Severity.INFO)])
    assert (
        "flagd(호출받는 지연): 이 서비스 관련 결과에서 기준을 넘는 이상 없음 — 네트워크. "
        "이 결과만으로는 원인 분야를 가리지 못함"
    ) in clear.lines


def test_summary_separates_unlinkable_domains() -> None:
    server = _result(
        AgentName.SERVER,
        (
            _fact(
                "노드 CPU 사용률: k3d-a-0 95%",
                "node.cpu_utilization@current",
                TargetRef(
                    kind=TargetKind.NODE, name="k3d-a-0", labels={"k8s_node_name": "k3d-a-0"}
                ),
            ),
        ),
        (_ev("node.cpu_utilization@current"),),
    )
    cross = cross_check([SERVICE, server, DB])
    assert "서버 자원 이상과 연결된 대상" not in cross.summary
    assert cross.summary.endswith(
        "서버 자원 분야는 서비스 단위로 연결할 수 있는 결과가 없어 연결 여부를 판단하지 못했습니다."
    )
    assert "서버 자원: 이상 1건, 그중 서비스 이상 대상과 연결 0건" in next(
        x for x in cross.lines if x.startswith("서버 자원:")
    )


def test_pod_level_anomaly_is_not_linked_or_cleared() -> None:
    """Pod 단위 이상은 서비스 이름으로 연결할 수 없으므로 '이상 없음'이라고 하지 않음."""
    k8s = _result(
        AgentName.KUBERNETES,
        (
            _fact(
                "Pending Pod: otel-demo/accounting-7d9f-abc",
                "k8s.pod_phase@current",
                TargetRef(
                    kind=TargetKind.POD,
                    name="otel-demo/accounting-7d9f-abc",
                    labels={"k8s_namespace_name": "otel-demo", "k8s_pod_name": "accounting-7d9f"},
                ),
            ),
        ),
        (_ev("k8s.pod_phase@current"),),
    )
    cross = cross_check([SERVICE, k8s])
    assert cross.correlations == ()
    assert any(
        x.startswith(
            "accounting(응답 지연): 이 서비스와 연결할 수 있는 결과가 없어 판단하지 못함 "
            "— Kubernetes"
        )
        for x in cross.lines
    )
    # 컨테이너 이름이 같으면 같은 대상으로 연결
    container = _result(
        AgentName.KUBERNETES,
        (
            _fact(
                "컨테이너 재시작: otel-demo/accounting-x/accounting 2회",
                "k8s.container_restarts_increase@window",
                TargetRef(
                    kind=TargetKind.CONTAINER,
                    name="otel-demo/accounting-x/accounting",
                    labels={"k8s_pod_name": "accounting-x", "k8s_container_name": "accounting"},
                ),
            ),
        ),
        (_ev("k8s.container_restarts_increase@window"),),
    )
    [corr] = cross_check([SERVICE, container]).correlations
    assert corr.confidence is Confidence.MEDIUM and "Kubernetes 이상" in corr.statement


def test_no_issues_failed_and_empty_domains() -> None:
    quiet = _result(AgentName.SERVICE, SERVICE.findings[2:], SERVICE.evidence)
    failed_db = AgentResult(
        task_id="db-1",
        agent=AgentName.DB,
        status=AgentStatus.FAILED,
        errors=(ErrorInfo(code="timeout", message="t"),),
    )
    empty_net = _result(AgentName.NETWORK, (), (), AgentStatus.PARTIAL)
    cross = cross_check([quiet, empty_net, failed_db])
    assert cross.lines == (
        "서비스 이상 대상: 없음 (Service Agent 오류율·응답 지연 판정 기준)",
        "네트워크: 판단에 사용할 결과가 없어 확인하지 못함 (일부 조회를 완료하지 못함)",
        "DB·캐시: 분석을 완료하지 못해 확인하지 못함",
    )
    assert cross.correlations == ()
    assert cross.summary == (
        "분야 간 교차 확인: 서비스 오류율·응답 지연 이상이 확인되지 않아 원인 분야를 가리지 "
        "않았습니다. 네트워크, DB·캐시 분야는 확인하지 못했습니다."
    )

    issues_only = cross_check([SERVICE, failed_db])
    assert issues_only.summary.endswith("DB·캐시 분야는 확인하지 못했습니다.")
    assert "연결을 판단한 분야 없음" in issues_only.summary
    # 확인한 분야가 없으면 서비스별 판정 줄을 만들지 않음
    assert not any(x.startswith(("accounting(", "flagd(")) for x in issues_only.lines)


def test_partial_domain_is_tagged() -> None:
    partial_db = _result(AgentName.DB, DB.findings, DB.evidence, AgentStatus.PARTIAL)
    cross = cross_check([SERVICE, partial_db])
    assert any(
        x.startswith("accounting(응답 지연): 연결된 이상 — DB·캐시(일부 조회 미완료)")
        for x in cross.lines
    )


def test_service_failure_no_core_judgement_and_single_domain() -> None:
    failed = AgentResult(
        task_id="service-1",
        agent=AgentName.SERVICE,
        status=AgentStatus.FAILED,
        errors=(ErrorInfo(code="timeout", message="t"),),
    )
    cross = cross_check([failed, DB])
    assert cross.lines == ("서비스: 분석을 완료하지 못해 이상 대상을 정하지 못함",)
    assert cross.correlations == ()
    assert cross_check([DB]).lines == ()  # Service 없이 단일 분야면 교차 확인하지 않음
    assert cross_check([SERVICE]).lines == ()
    # 오류율·응답 지연 판정이 없으면(다른 사실만 있어도) "이상 대상 없음"이 아니라 판단 불가
    no_core = _result(
        AgentName.SERVICE,
        (_fact("서비스 요청량: 15개", "service.request_rate@current", severity=Severity.INFO),),
        (_ev("service.request_rate@current"),),
    )
    expected = ("서비스: 오류율·응답 지연 판정 결과가 없어 이상 대상을 정하지 못함",)
    assert cross_check([no_core, DB]).lines == expected
    assert cross_check([_result(AgentName.SERVICE, (), ()), DB]).lines == expected
    partial = _result(
        AgentName.SERVICE, SERVICE.findings[2:], SERVICE.evidence, AgentStatus.PARTIAL
    )
    assert cross_check([partial, DB]).lines[0] == (
        "서비스 이상 대상: 없음 (Service Agent 오류율·응답 지연 판정 기준) — 서비스 분석 일부를 "
        "완료하지 못해 빠진 대상이 있을 수 있음"
    )


def test_synthesis_includes_cross_check() -> None:
    interp = interpret(
        "서비스 응답이 느려진 이유가 네트워크인지 DB인지 분석해 줘", NOW, timedelta(minutes=30)
    )
    answer = synthesize("r1", interp, [SERVICE, NETWORK_OK, DB])
    assert "분야 간 교차 확인: 서비스 이상 대상 2개 중" in answer.summary
    assert answer.cross_checks[0].startswith("서비스 이상 대상:")
    assert len(answer.correlations) == 1 and answer.hypotheses == ()
    assert CROSS_NOTE in answer.scope_notes
    single = synthesize("r2", interp, [DB])
    assert single.cross_checks == () and CROSS_NOTE not in single.scope_notes


def test_live_like_service_without_db_calls() -> None:
    """live(2026-10-01) 형태: checkout 지연 증가, 네트워크 정상, checkout은 DB 호출 없음."""
    service = _result(
        AgentName.SERVICE,
        (
            _fact(
                "서비스 응답 지연 p95 증가: checkout 평균 0.031초 → 1.62초",
                "service.latency_p95@window_avg",
                TargetRef(kind=TargetKind.SERVICE, name="checkout"),
            ),
        ),
        (_ev("service.latency_p95@window_avg"), DB_SPANS),
    )
    network = _result(
        AgentName.NETWORK,
        (
            _fact(
                "Service 이상 대상 checkout(응답 지연)의 네트워크: 드롭·오류 판정 0.0%",
                "network.workload_ingress_by_verdict@current:checkout",
                TargetRef(kind=TargetKind.SERVICE, name="checkout", labels={"service": "checkout"}),
                severity=Severity.INFO,
            ),
        ),
        (_ev("network.workload_ingress_by_verdict@current:checkout"),),
    )
    db = _result(
        AgentName.DB,
        (
            _fact(
                "PostgreSQL 데드락 (최근 30분): 발생 없음",
                "db.pg_deadlocks_increase@window",
                severity=Severity.INFO,
            ),
        ),
        (_ev("db.pg_deadlocks_increase@window"),),
    )
    cross = cross_check([service, network, db])
    # "DB 호출 없음"도 서비스 단위 판단이므로 판단 불가로 분류하지 않음
    assert cross.summary == (
        "분야 간 교차 확인: 서비스 이상 대상 1개 중 네트워크 이상과 연결된 대상 0개, "
        "DB·캐시 이상과 연결된 대상 0개 (같은 구간 동시 발생 기준, 인과 미확인)."
    )
    assert cross.lines[-1] == (
        "checkout(응답 지연): 이 서비스 관련 결과에서 기준을 넘는 이상 없음 — 네트워크; "
        "DB 호출 span에 이 서비스의 DB 호출이 없어 DB 이상과 연결하지 않음 — DB·캐시. "
        "이 결과만으로는 원인 분야를 가리지 못함"
    )


def test_summary_names_domains_without_results() -> None:
    """한 분야에만 판단 결과가 있으면, 결과가 없는 분야를 요약과 확인하지 못한 영역에 밝힘."""
    interp = interpret(
        "서비스 응답이 느려진 이유가 네트워크인지 DB인지 분석해 줘", NOW, timedelta(minutes=30)
    )
    empty_db = _result(AgentName.DB, (), ())
    answer = synthesize("r3", interp, [SERVICE, NETWORK_OK, empty_db])
    assert "(DB·캐시 분야는 판단에 사용할 결과가 없어 확인하지 못했습니다.)" in answer.summary
    assert (
        "DB·캐시: 판단에 사용할 결과가 없어 확인하지 못함 (사유는 한계 참고)"
        in answer.unverified_areas
    )


def test_no_issue_cross_summary_answers_cause_questions_only() -> None:
    """live 평가(2026-10-01): 원인 질문에는 직접 답하고, 단순 비교 질문 요약은 채우지 않음."""
    quiet = _result(AgentName.SERVICE, SERVICE.findings[2:], SERVICE.evidence)
    interp = interpret(
        "서비스 응답이 느려진 원인이 네트워크인지 DB인지 분석해 줘.", NOW, timedelta(minutes=30)
    )
    cause = synthesize("r4", interp, [quiet, NETWORK_OK, DB], question="원인이 네트워크인지 DB인지")
    assert (
        "분야 간 교차 확인: 서비스 오류율·응답 지연 이상이 확인되지 않아 원인 분야를 가리지 "
        "않았습니다. 네트워크도 확인한 항목에서 기준을 넘는 이상이 없습니다. "
        "다른 분야 이상(DB·캐시 3건)은 서비스 영향과 연결하지 않았습니다."
    ) in cause.summary
    compare = synthesize("r5", interp, [quiet, NETWORK_OK, DB], question="직전 30분과 비교해서")
    assert "분야 간 교차 확인" not in compare.summary
    assert compare.cross_checks  # 교차 확인 줄은 그대로 답변에 있음
    issues = synthesize("r6", interp, [SERVICE, NETWORK_OK, DB], question="직전 30분과 비교해서")
    assert "분야 간 교차 확인: 서비스 이상 대상 2개 중" in issues.summary  # 이상 대상이 있으면 붙임
