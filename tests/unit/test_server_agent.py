"""Server Agent와 질문 처리 흐름 테스트. 모든 조회 결과는 가상 데이터입니다."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from expr_prom import ExprProm, Rows
from infra_agent.agents.server import ServerAgent
from infra_agent.answer.render import render_text
from infra_agent.catalog import load_catalog
from infra_agent.config import load_settings
from infra_agent.config.settings import AnalysisConfig, HttpDatasourceConfig
from infra_agent.datasources import PrometheusClient
from infra_agent.orchestration.runner import answer_question
from infra_agent.schemas import (
    AgentName,
    AgentStatus,
    AgentTask,
    AnalysisContext,
    Budget,
    FindingKind,
    Intent,
    JudgementBasis,
    Severity,
    TargetKind,
    TargetRef,
    TimeRange,
)
from infra_agent.tools import CatalogQueryTool, QueryMode, ToolBudget

ROOT = Path(__file__).resolve().parents[2]
CATALOG = load_catalog(ROOT / "config/catalog/otel-demo.yaml")
NOW = datetime(2026, 9, 29, 3, 0, tzinfo=UTC)
CFG = HttpDatasourceConfig(enabled=True, url="http://prom.synthetic.test")
TASK = AgentTask(task_id="t1", agent=AgentName.SERVER, objective="test")
NODES = [{"k8s_node_name": f"k3d-syn-{i}"} for i in range(3)]


def _ctx(intent: Intent) -> AnalysisContext:
    tr = TimeRange.last(timedelta(minutes=30), NOW)
    return AnalysisContext(
        request_id="r1",
        question="q",
        intent=intent,
        time_range=tr,
        baseline_range=tr.previous() if intent is not Intent.STATUS else None,
        budget=Budget(max_llm_calls=0, max_tool_calls=60, deadline=NOW),
    )


def _expr(key: str, mode: QueryMode, ctx: AnalysisContext, selector: str = "") -> str:
    tool = CatalogQueryTool(
        CATALOG,
        None,
        agent=AgentName.SERVER,
        budget=ToolBudget(1),
        timeout_seconds=1,  # type: ignore[arg-type]
    )
    return tool.build_expr(tool.item(key), selector, mode, ctx)


def _nodes(*values: float) -> Rows:
    return [(NODES[i], v) for i, v in enumerate(values)]


def _status_prom(ctx: AnalysisContext, fake: ExprProm) -> None:
    fake.add(_expr("node.cpu_utilization", QueryMode.CURRENT, ctx), _nodes(0.3, 0.85, 0.95))
    fake.add(_expr("node.memory_utilization", QueryMode.CURRENT, ctx), _nodes(0.4, 0.5, 0.6))
    fake.add(_expr("node.filesystem_utilization", QueryMode.CURRENT, ctx), _nodes(0.1, 0.2, 0.3))
    fake.add(
        _expr("container.memory_limit_utilization", QueryMode.CURRENT, ctx),
        [
            (
                {
                    "k8s_namespace_name": "otel-demo",
                    "k8s_pod_name": "cart-x",
                    "k8s_container_name": "cart",
                },
                0.5,
            )
        ],
    )
    # container.cpu_limit_utilization, container.cpu_throttled_ratio: 등록하지 않음 → 빈 결과
    fake.add(_expr("node.cpu_usage", QueryMode.CURRENT, ctx), _nodes(0.5, 1.2, 2.0))
    fake.add(
        _expr("node.memory_working_set", QueryMode.CURRENT, ctx),
        _nodes(2 * 2**30, 3 * 2**30, 4 * 2**30),
    )


async def _run(ctx: AnalysisContext, fake: ExprProm, targets: dict[TargetKind, str] | None = None):  # type: ignore[no-untyped-def]
    async with PrometheusClient.from_config(CFG, transport=fake.transport()) as prom:
        tool = CatalogQueryTool(
            CATALOG, prom, agent=AgentName.SERVER, budget=ToolBudget(60), timeout_seconds=5
        )
        if targets:
            ctx = ctx.model_copy(
                update={"targets": tuple(TargetRef(kind=k, name=v) for k, v in targets.items())}
            )
        return await ServerAgent(tool, AnalysisConfig()).run(TASK, ctx, {})


async def test_status_thresholds() -> None:
    ctx = _ctx(Intent.STATUS)
    fake = ExprProm()
    _status_prom(ctx, fake)
    result = await _run(ctx, fake)
    assert result.status is AgentStatus.SUCCESS
    by_sev = {s: [f.statement for f in result.findings if f.severity is s] for s in Severity}
    assert len(by_sev[Severity.CRITICAL]) == 1 and "k3d-syn-2 95.0%" in by_sev[Severity.CRITICAL][0]
    assert len(by_sev[Severity.WARNING]) == 1 and "k3d-syn-1 85.0%" in by_sev[Severity.WARNING][0]
    infos = "\n".join(by_sev[Severity.INFO])
    assert "노드 메모리 사용률" in infos and "모두 기준(80.0%) 미만" in infos
    assert "노드 CPU 사용량 현재 값" in infos and "k3d-syn-2 2.000 cores" in infos
    assert "4.0GiB" in infos
    # 빈 결과는 "정상"이 아니라 한계로 표시
    assert any("CPU limit이 있는 컨테이너 결과가 없어" in x for x in result.limitations)
    assert any("스로틀링 결과가 없어" in x for x in result.limitations)
    assert any("물리 서버" in x for x in result.limitations)
    # 근거 ID는 모두 실제 evidence에 존재 (스키마가 강제)
    assert all(f.evidence_ids for f in result.findings)
    assert (
        fake.unmatched.count(_expr("container.cpu_limit_utilization", QueryMode.CURRENT, ctx)) == 1
    )


async def test_stale_data_is_not_reported_as_normal() -> None:
    ctx = _ctx(Intent.STATUS)
    fake = ExprProm(freshness_seconds=3600)
    _status_prom(ctx, fake)
    result = await _run(ctx, fake)
    statements = [f.statement for f in result.findings]
    assert not any("미만" in s for s in statements)  # 오래된 데이터로 "기준 미만" 판정 금지
    assert any(f.severity is Severity.CRITICAL for f in result.findings)  # 관측값 초과는 보고
    assert any("오래되어" in x for x in result.limitations)


async def test_increase_detection() -> None:
    ctx = _ctx(Intent.ANOMALY)
    assert ctx.baseline_range is not None
    fake = ExprProm()
    win = _expr("node.cpu_usage", QueryMode.WINDOW_AVG, ctx)
    fake.add(win, _nodes(1.0, 0.52, 0.02), at=ctx.time_range.end)
    fake.add(win, _nodes(0.5, 0.50, 0.01), at=ctx.baseline_range.end)
    mem = _expr("pod.memory_working_set", QueryMode.WINDOW_AVG, ctx)
    pod_a = {"k8s_namespace_name": "otel-demo", "k8s_pod_name": "cart-a"}
    pod_new = {"k8s_namespace_name": "otel-demo", "k8s_pod_name": "cart-new"}
    fake.add(mem, [(pod_a, 400 * 2**20), (pod_new, 50 * 2**20)], at=ctx.time_range.end)
    fake.add(mem, [(pod_a, 200 * 2**20)], at=ctx.baseline_range.end)
    result = await _run(ctx, fake)
    increases = [
        f
        for f in result.findings
        if f.basis is JudgementBasis.BASELINE and f.severity is Severity.WARNING
    ]
    texts = [f.statement for f in increases]
    assert len(increases) == 2, texts
    assert any("k3d-syn-0" in t and "+100%" in t for t in texts)  # 0.5 → 1.0
    assert not any("k3d-syn-2" in t for t in texts)  # +100%지만 0.01 cores로 최소 증가량 미만
    assert any("otel-demo/cart-a" in t and "200.0MiB → 400.0MiB" in t for t in texts)
    assert all(len(f.evidence_ids) == 2 for f in increases)
    assert all(f.kind is FindingKind.FACT for f in increases)
    assert any("기준 구간에 없던 대상 1개" in x for x in result.limitations)
    assert any("비교할 수 없음" in x for x in result.limitations)  # node.memory 등 미등록 → 빈 결과
    assert result.next_checks


async def test_target_filter_and_unsupported() -> None:
    ctx = _ctx(Intent.STATUS)
    fake = ExprProm()
    result = await _run(ctx, fake, {TargetKind.NAMESPACE: "otel-demo"})
    # 노드 항목은 namespace로 필터링할 수 없어 조회하지 않음
    assert any("node.cpu_utilization: 요청한 대상(namespace)" in x for x in result.limitations)
    sent = [q for q, _ in fake.queries if not q.startswith("time()")]
    assert sent and all('k8s_namespace_name="otel-demo"' in q for q in sent)


async def test_all_queries_failing_marks_failed() -> None:
    ctx = _ctx(Intent.STATUS)
    fake = ExprProm()
    for key in (
        "node.cpu_utilization",
        "node.memory_utilization",
        "node.filesystem_utilization",
        "container.cpu_limit_utilization",
        "container.memory_limit_utilization",
        "container.cpu_throttled_ratio",
        "node.cpu_usage",
        "node.memory_working_set",
    ):
        fake.fail(_expr(key, QueryMode.CURRENT, ctx))
    result = await _run(ctx, fake)
    assert result.status is AgentStatus.FAILED
    assert not result.findings and result.errors


async def test_answer_question_end_to_end() -> None:
    settings = load_settings(
        environ={
            "INFRA_AGENT_PROFILE": "dev-tunnel",
            "INFRA_AGENT__DATASOURCES__PROMETHEUS__ENABLED": "true",
            "INFRA_AGENT__DATASOURCES__PROMETHEUS__URL": "http://prom.synthetic.test",
        }
    )
    ctx = _ctx(Intent.STATUS)
    fake = ExprProm()
    _status_prom(ctx, fake)
    bundle = await answer_question(
        "현재 서버 상태가 어때?", settings, CATALOG, now=NOW, transport=fake.transport()
    )
    assert bundle.context.intent is Intent.STATUS
    assert bundle.context.budget.max_llm_calls == 0
    text = render_text(bundle, show_queries=True)
    for section in ("[질문 해석]", "[요약]", "[이상 징후]", "[확인된 사실]", "[근거]", "[한계]"):
        assert section in text
    assert "기준을 넘는 이상 징후 2건(심각 1건, 경고 1건)" in text
    assert "k8s_node_cpu_usage" in text  # --show-queries
    assert "모델 호출 없음" in text
    assert bundle.plan is not None and [t.task_id for t in bundle.plan.tasks] == ["server-1"]
    assert [(r.task_id, r.status) for r in bundle.runs] == [("server-1", AgentStatus.SUCCESS)]
    assert "(에이전트 실행: server 성공 " in text

    other = await answer_question(
        "서비스 응답이 느려진 이유가 네트워크인지 DB인지 분석해 줘",
        settings,
        CATALOG,
        now=NOW,
        transport=fake.transport(),
    )
    assert other.results == ()  # 서버 분야가 아니므로 실행하지 않음
    assert "답할 수 없습니다" in other.answer.summary
    assert len(other.answer.unverified_areas) == 3
    assert "- 실행한 조회 없음" in render_text(other)
    assert other.runs == () and "에이전트 실행" not in render_text(other)


async def test_agent_failure_is_isolated_in_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """에이전트가 예외를 던져도 요청 전체가 실패하지 않고 확인하지 못한 영역으로 답합니다."""
    import infra_agent.orchestration.runner as runner_mod

    class Broken:
        name = AgentName.SERVER

        async def run(self, task: AgentTask, ctx: AnalysisContext, upstream: object) -> object:
            raise RuntimeError("내부 오류 token=abcd1234secret")

    monkeypatch.setitem(runner_mod.AGENT_BUILDERS, AgentName.SERVER, lambda deps: Broken())  # type: ignore[attr-defined]
    settings = load_settings(
        environ={
            "INFRA_AGENT_PROFILE": "dev-tunnel",
            "INFRA_AGENT__DATASOURCES__PROMETHEUS__ENABLED": "true",
            "INFRA_AGENT__DATASOURCES__PROMETHEUS__URL": "http://prom.synthetic.test",
        }
    )
    bundle = await answer_question(
        "현재 서버 상태가 어때?", settings, CATALOG, now=NOW, transport=ExprProm().transport()
    )
    assert bundle.results[0].status is AgentStatus.FAILED
    assert "판단하지 못했습니다" in bundle.answer.summary
    text = render_text(bundle)
    assert "server 에이전트: 실패로 확인하지 못함 (에이전트 오류(RuntimeError)" in text
    assert "abcd1234secret" not in text
    assert "(에이전트 실행: server 실패 " in text


async def test_anomaly_summary_answers_increase_first() -> None:
    settings = load_settings(
        environ={
            "INFRA_AGENT_PROFILE": "dev-tunnel",
            "INFRA_AGENT__DATASOURCES__PROMETHEUS__ENABLED": "true",
            "INFRA_AGENT__DATASOURCES__PROMETHEUS__URL": "http://prom.synthetic.test",
        }
    )
    question = "최근 30분 동안 CPU나 메모리가 비정상적으로 증가한 서버가 있어?"
    ctx = _ctx(Intent.ANOMALY)
    assert ctx.baseline_range is not None
    fake = ExprProm()
    _status_prom(ctx, fake)  # 현재 값 기준 심각 1·경고 1
    win = _expr("node.cpu_usage", QueryMode.WINDOW_AVG, ctx)
    fake.add(win, _nodes(0.5, 0.5, 0.5), at=ctx.time_range.end)
    fake.add(win, _nodes(0.5, 0.5, 0.5), at=ctx.baseline_range.end)
    bundle = await answer_question(question, settings, CATALOG, now=NOW, transport=fake.transport())
    summary = bundle.answer.summary
    assert summary.startswith("직전 같은 길이 구간 대비 기준 이상 증가한 대상은 없습니다.")
    assert "별도로 현재 값이 기준을 넘는 항목 2건(심각 1건, 경고 1건)" in summary
    assert bundle.answer.facts[0].basis is JudgementBasis.BASELINE  # 증가 판정이 먼저

    fake.add(win, _nodes(1.0, 0.5, 0.5), at=ctx.time_range.end)
    bundle2 = await answer_question(
        question, settings, CATALOG, now=NOW, transport=fake.transport()
    )
    assert bundle2.answer.summary.startswith("직전 같은 길이 구간 대비 기준 이상 증가한 대상 1건")
    text = render_text(bundle2)
    assert "- 분석 구간:" in text and "- 기준 구간:" in text

    status = await answer_question(
        "현재 서버 상태가 어때?", settings, CATALOG, now=NOW, transport=fake.transport()
    )
    status_text = render_text(status)
    assert "- 조회 시각:" in status_text and "현재 값 기준" in status_text
    assert "- 분석 구간:" not in status_text


async def test_answer_with_model_full_policy(monkeypatch: object) -> None:
    from infra_agent.llm import LLMUnavailableError
    from infra_agent.llm.fake import FakeLLM
    from infra_agent.orchestration.llm_interpret import PURPOSE

    env = {
        "INFRA_AGENT_PROFILE": "dev-tunnel",
        "INFRA_AGENT__DATASOURCES__PROMETHEUS__ENABLED": "true",
        "INFRA_AGENT__DATASOURCES__PROMETHEUS__URL": "http://prom.synthetic.test",
        "INFRA_AGENT__LLM__PROVIDER": "claude_agent_sdk",
        "INFRA_AGENT__LLM__DATA_POLICY": "full",
    }
    settings = load_settings(environ=env)
    ctx = _ctx(Intent.STATUS)
    fake_prom = ExprProm()
    _status_prom(ctx, fake_prom)
    llm = FakeLLM(
        {
            PURPOSE: {
                "intent": "status",
                "duration_minutes": None,
                "namespace": None,
                "node": None,
                "pod": None,
                "domains": ["server"],
            },
            "explain_server": {
                "hypotheses": [
                    {
                        "statement": (
                            "k3d-syn-2 노드 CPU 사용률 95.0%는 해당 노드 Pod 부하 증가 가능성"
                        ),
                        "evidence_ids": ["node.cpu_utilization@current"],
                        "confidence": "low",
                    },
                    {
                        "statement": "노드 CPU가 417분째 증가 중",
                        "evidence_ids": ["node.cpu_utilization@current"],
                        "confidence": "low",
                    },
                ],
                "next_checks": [],
            },
        }
    )
    bundle = await answer_question(
        "현재 서버 상태가 어때?",
        settings,
        CATALOG,
        now=NOW,
        transport=fake_prom.transport(),
        llm=llm,
    )
    assert bundle.llm_calls == 2 and bundle.data_policy == "full"
    assert [r.purpose for r in llm.requests] == [PURPOSE, "explain_server"]
    assert bundle.context.budget.max_llm_calls == settings.llm.max_calls_per_request
    assert len(bundle.answer.hypotheses) == 1
    # 코드 판정(사실)은 모델과 무관하게 유지
    assert "기준을 넘는 이상 징후 2건(심각 1건, 경고 1건)" in bundle.answer.summary
    text = render_text(bundle)
    assert "[원인 후보 (추정, 모델 해석)]" in text
    assert "모델 호출 2회(fake, data_policy=full)" in text
    assert "- 해석 방식: 모델" in text and "가정: 질문 해석" not in text
    # 제외된 원인 후보: 본문에는 없고 진단 출력(--show-queries)에만 원문·이유 표시
    assert "417분째" not in text and "[제외된 원인 후보" not in text
    diag = render_text(bundle, show_queries=True)
    assert "[제외된 원인 후보 (검증 실패, 진단용)]" in diag
    assert "- (server) 노드 CPU가 417분째 증가 중" in diag
    assert "제외 이유: 관측 데이터에 없는 수치: 417" in diag
    # 조회 데이터가 모델 입력에 포함됨(full)
    assert "k8s_node_cpu_usage" in llm.requests[1].prompt
    assert '"display": "95.0%"' in llm.requests[1].prompt  # 카탈로그 단위로 만든 표시값

    # --no-llm: 모델을 호출하지 않음
    off = await answer_question(
        "현재 서버 상태가 어때?",
        settings,
        CATALOG,
        now=NOW,
        transport=fake_prom.transport(),
        llm=llm,
        use_llm=False,
    )
    assert off.llm_calls == 0 and not off.answer.hypotheses
    assert "모델 호출 없음" in render_text(off)
    assert off.interpretation.method == "rules" and "- 해석 방식: 규칙 기반\n" in render_text(off)

    # data_policy=none: 질문 해석만 모델, 조회 데이터는 보내지 않음
    none_settings = load_settings(environ={**env, "INFRA_AGENT__LLM__DATA_POLICY": "none"})
    llm.requests.clear()
    only_q = await answer_question(
        "현재 서버 상태가 어때?",
        none_settings,
        CATALOG,
        now=NOW,
        transport=fake_prom.transport(),
        llm=llm,
    )
    assert [r.purpose for r in llm.requests] == [PURPOSE] and only_q.llm_calls == 1

    # SDK를 쓸 수 없으면 규칙 기반으로 계속하고 해석 방식에 이유를 표시
    import infra_agent.orchestration.runner as runner_mod

    def unavailable(_: object) -> None:
        raise LLMUnavailableError("claude-agent-sdk가 설치되지 않았습니다")

    monkeypatch.setattr(runner_mod, "make_llm", unavailable)  # type: ignore[attr-defined]
    fallback = await answer_question(
        "현재 서버 상태가 어때?", settings, CATALOG, now=NOW, transport=fake_prom.transport()
    )
    assert fallback.llm_calls == 0
    assert fallback.interpretation.method == "rules"
    assert fallback.interpretation.method_note == (
        "모델 사용 불가: claude-agent-sdk가 설치되지 않았습니다"
    )
    assert "- 해석 방식: 규칙 기반 (모델 사용 불가: claude-agent-sdk" in render_text(fallback)
