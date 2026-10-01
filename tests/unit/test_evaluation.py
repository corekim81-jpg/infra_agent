"""대표 질문 평가 테스트 (가상 데이터).

- 평가 세트 형식과 README 대표 질문과의 일치
- 평가 기준별 통과·실패 판정
- 데이터가 없는 가상 환경에서 7개 질문 전체 평가 (에이전트 선택·의도·데이터 부족 표시 등)
- `infra-agent eval` 명령
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from expr_prom import ExprProm
from infra_agent.catalog import load_catalog
from infra_agent.config import load_settings
from infra_agent.config.settings import Settings
from infra_agent.evaluation import (
    CheckResult,
    CheckStatus,
    EvalSetError,
    QuestionResult,
    RunInfo,
    evaluate,
    load_eval_set,
    render_summary,
    run_eval,
    select,
    write_report,
)
from infra_agent.evaluation.models import EvalSet
from infra_agent.orchestration.runner import AnswerBundle
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
    ToolResult,
    ToolStatus,
)

ROOT = Path(__file__).resolve().parents[2]
EVAL_SET = ROOT / "config/eval/questions.yaml"
CATALOG = load_catalog(ROOT / "config/catalog/otel-demo.yaml")
NOW = datetime(2026, 10, 1, 1, 0, tzinfo=UTC)
EXAMPLE = ROOT / "config/example.yaml"


def _settings() -> Settings:
    return load_settings(
        environ={
            "INFRA_AGENT_PROFILE": "dev-tunnel",
            "INFRA_AGENT__DATASOURCES__PROMETHEUS__ENABLED": "true",
            "INFRA_AGENT__DATASOURCES__PROMETHEUS__URL": "http://prom.synthetic.test",
        }
    )


@pytest.fixture(scope="module")
def offline() -> tuple[QuestionResult, ...]:
    """데이터가 하나도 없는 가상 Prometheus로 7개 질문을 평가 (모델 없음)."""
    import asyncio

    eval_set = load_eval_set(EVAL_SET)
    return asyncio.run(
        run_eval(
            eval_set.questions,
            _settings(),
            CATALOG,
            use_llm=False,
            now=NOW,
            transport=ExprProm().transport(),
        )
    )


def test_eval_set_matches_readme_questions() -> None:
    eval_set = load_eval_set(EVAL_SET)
    assert len(eval_set.questions) == 7
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    listed = re.findall(r"^- “(.+?)”$", readme, flags=re.MULTILINE)
    # README 대표 질문 7개와 평가 세트 질문이 같아야 함 (기능 범위 기준은 README)
    assert [q.question for q in eval_set.questions] == listed[:7]


def test_eval_set_validation(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "version: 1\nquestions:\n"
        "  - {id: a, question: x, expect:\n"
        "      {agents: [db], intents: [status], after: {network: db}}}\n",
        encoding="utf-8",
    )
    with pytest.raises(EvalSetError, match="after의 에이전트"):
        load_eval_set(bad)
    dup = tmp_path / "dup.yaml"
    dup.write_text(
        "version: 1\nquestions:\n"
        "  - {id: a, question: x, expect: {agents: [db], intents: [status]}}\n"
        "  - {id: a, question: y, expect: {agents: [db], intents: [status]}}\n",
        encoding="utf-8",
    )
    with pytest.raises(EvalSetError, match="중복"):
        load_eval_set(dup)
    with pytest.raises(EvalSetError, match="찾을 수 없습니다"):
        load_eval_set(tmp_path / "none.yaml")
    # 질문 해석이 만들지 않는 의도(root_cause)는 기대값으로 쓸 수 없음
    rc = tmp_path / "rc.yaml"
    rc.write_text(
        "version: 1\nquestions:\n"
        "  - {id: a, question: x, expect: {agents: [db], intents: [anomaly, root_cause]}}\n",
        encoding="utf-8",
    )
    with pytest.raises(EvalSetError, match="만들지 않는 의도입니다: root_cause"):
        load_eval_set(rc)
    eval_set = load_eval_set(EVAL_SET)
    assert [q.id for q in select(eval_set, ["q3"])] == ["q3_network_or_db"]
    with pytest.raises(ValueError, match="맞는 질문이 없습니다"):
        select(eval_set, ["zz"])


def test_offline_routing_intent_and_disclosure(offline: tuple[QuestionResult, ...]) -> None:
    """데이터가 없어도 에이전트 선택·의도·구간은 맞고, 데이터 부족은 숨기지 않아야 함."""
    assert len(offline) == 7 and all(r.error is None for r in offline)
    must_pass = (
        "routing",
        "order",
        "intent",
        "time",
        "disclosure",
        "causality",
        "secrets",
        "budget",
    )
    for r in offline:
        for name in must_pass:
            c = r.check(name)
            assert c is not None and c.status is not CheckStatus.FAIL, (r.question.id, c)
    # 데이터가 없으면 확인된 사실이 없으므로 근거 일치 기준은 실패로 드러남 (정상으로 보지 않음).
    # Kubernetes 조건형 조회는 기준 지표가 최신(가상 최신성 15초)이면 "해당 대상 없음"이 사실임.
    for r in offline:
        assert r.bundle is not None
        expected = CheckStatus.PASS if r.bundle.answer.facts else CheckStatus.FAIL
        assert r.check("evidence").status is expected, r.question.id  # type: ignore[union-attr]
    assert [r.question.id for r in offline if r.bundle and r.bundle.answer.facts] == ["q5_k8s_pods"]
    by_id = {r.question.id: r for r in offline}
    q7 = by_id["q7_compare_30m"].bundle
    assert q7 is not None
    assert {x.agent for x in q7.results} == {AgentName.SERVER, AgentName.SERVICE, AgentName.DB}
    # 실행 계획: Server ∥ (Service → DB)
    assert {run.agent: run.depends_on for run in q7.runs} == {
        "server": (),
        "service": (),
        "db": ("service-1",),
    }
    assert any(
        "Kubernetes 상태·네트워크는 확인하지 않음" in a for a in q7.interpretation.assumptions
    )
    q3 = by_id["q3_network_or_db"]
    # 데이터가 없으면 서비스 이상 대상을 정하지 못하므로 교차 확인은 실패로 드러남
    cross = q3.check("cross")
    assert cross is not None and cross.status is CheckStatus.FAIL
    assert "이상 대상을 정하지 못함" in cross.detail
    summary = render_summary(offline)
    assert "질문 7개 중 통과 1개" in summary and "[근거 일치] 확인된 사실이 없음" in summary
    # Loki·Tempo를 켜지 않은 설정에서는 로그·트레이스 소스가 빠진 것이 실패로 드러남
    assert "q6_logs_traces [데이터 소스] 조회되지 않은 소스 loki, tempo" in summary


def _bundle(offline: tuple[QuestionResult, ...], qid: str) -> tuple[QuestionResult, AnswerBundle]:
    r = next(x for x in offline if x.question.id == qid)
    assert r.bundle is not None
    return r, r.bundle


def test_checks_detect_failures(offline: tuple[QuestionResult, ...]) -> None:
    r, bundle = _bundle(offline, "q1_server_status")
    q, settings = r.question, _settings()
    server = bundle.results[0]

    # 에이전트 선택: 기대보다 넓게 실행하면 실패
    extra = AgentResult(task_id="db-1", agent=AgentName.DB, status=AgentStatus.SUCCESS)
    wide = replace(bundle, results=(*bundle.results, extra))
    routing = next(c for c in evaluate(q, wide, settings) if c.name == "routing")
    assert routing.status is CheckStatus.FAIL and "실행 db, server" in routing.detail

    # 근거 일치: 근거 목록에 없는 ID를 가리키는 사실
    ghost = Finding(
        kind=FindingKind.FACT,
        statement="노드 CPU 사용률 높음",
        evidence_ids=("node.ghost@current",),
        basis=JudgementBasis.THRESHOLD,
    )
    claims = replace(bundle, answer=bundle.answer.model_copy(update={"facts": (ghost,)}))
    evidence = next(c for c in evaluate(q, claims, settings) if c.name == "evidence")
    assert evidence.status is CheckStatus.FAIL and "node.ghost@current" in evidence.detail

    # 인과 단정: 사실 문장에 "때문입니다"
    causal = ghost.model_copy(update={"statement": "지연은 DB 때문입니다"})
    told = replace(bundle, answer=bundle.answer.model_copy(update={"facts": (causal,)}))
    causality = next(c for c in evaluate(q, told, settings) if c.name == "causality")
    assert causality.status is CheckStatus.FAIL

    # 실패 표시: 실패한 에이전트를 확인하지 못한 영역에 적지 않으면 실패
    failed = AgentResult(
        task_id=server.task_id,
        agent=server.agent,
        status=AgentStatus.FAILED,
        errors=(ErrorInfo(code="timeout", message="t"),),
    )
    hidden = replace(bundle, results=(failed,))
    disclosure = next(c for c in evaluate(q, hidden, settings) if c.name == "disclosure")
    assert disclosure.status is CheckStatus.FAIL and "server failed" in disclosure.detail

    # 비밀정보: 답변에 비밀번호 형식 문자열
    secret = ghost.model_copy(update={"statement": "password=hunter22 로그 발견"})
    leaky = replace(bundle, answer=bundle.answer.model_copy(update={"facts": (secret,)}))
    secrets = next(c for c in evaluate(q, leaky, settings) if c.name == "secrets")
    assert secrets.status is CheckStatus.FAIL

    # 예산: 조회 상한을 넘으면 실패
    tight = settings.model_copy(
        update={"analysis": settings.analysis.model_copy(update={"max_tool_calls": 1})}
    )
    budget = next(c for c in evaluate(q, bundle, tight) if c.name == "budget")
    assert budget.status is CheckStatus.FAIL


def _check(q: object, bundle: AnswerBundle, name: str) -> CheckResult:
    return next(c for c in evaluate(q, bundle, _settings()) if c.name == name)  # type: ignore[arg-type]


def test_checks_catch_missing_data_reported_as_normal(
    offline: tuple[QuestionResult, ...],
) -> None:
    """데이터 부재를 정상으로 답하거나 조회 실패를 숨기면 실패해야 함."""
    r, bundle = _bundle(offline, "q1_server_status")
    q = r.question
    assert not bundle.answer.facts  # 가상 데이터 없음 → 확인된 사실 없음

    # 확인된 사실이 없는데 요약이 "이상 없음"
    normal = replace(
        bundle,
        answer=bundle.answer.model_copy(
            update={"summary": "확인한 항목에서는 기준을 넘는 이상 징후가 없습니다."}
        ),
    )
    d = _check(q, normal, "disclosure")
    assert d.status is CheckStatus.FAIL and "이상 없음으로 답함" in d.detail

    # 조회 실패가 있는데 에이전트를 성공으로 표시
    server = bundle.results[0]
    broken = ToolResult(
        evidence_id="node.cpu_utilization@current",
        source=DataSourceKind.PROMETHEUS,
        query="q",
        status=ToolStatus.ERROR,
        fetched_at=NOW,
        error="boom",
        synthetic=True,
    )
    ok_but_failed = AgentResult(
        task_id=server.task_id,
        agent=server.agent,
        status=AgentStatus.SUCCESS,
        evidence=(broken,),
        limitations=("node.cpu_utilization: 조회 실패로 확인하지 못함 (boom)",),
    )
    d2 = _check(q, replace(bundle, results=(ok_but_failed,)), "disclosure")
    assert d2.status is CheckStatus.FAIL and "성공으로 표시" in d2.detail

    # 범위 안내만 있고 데이터 부족 사유가 없는 에이전트
    from infra_agent.agents.server import SCOPE_NOTE

    silent = AgentResult(
        task_id=server.task_id,
        agent=server.agent,
        status=AgentStatus.SUCCESS,
        limitations=(SCOPE_NOTE,),
    )
    d3 = _check(q, replace(bundle, results=(silent,)), "disclosure")
    assert d3.status is CheckStatus.FAIL and "데이터 부족 사유가 없음" in d3.detail

    # 조회 실패 근거만으로 말한 사실
    from infra_agent.schemas import EvidenceSummary

    fact = Finding(
        kind=FindingKind.FACT,
        statement="노드 CPU 사용률 정상",
        evidence_ids=("node.cpu_utilization@current",),
        basis=JudgementBasis.THRESHOLD,
    )
    failed_evidence = EvidenceSummary(
        source=DataSourceKind.PROMETHEUS,
        query="q",
        key_values={"id": "node.cpu_utilization@current", "status": "error"},
    )
    claims = replace(
        bundle,
        answer=bundle.answer.model_copy(update={"facts": (fact,), "evidence": (failed_evidence,)}),
    )
    e = _check(q, claims, "evidence")
    assert e.status is CheckStatus.FAIL and "조회 실패 근거만으로" in e.detail


def test_causality_covers_model_hypotheses_and_summary(
    offline: tuple[QuestionResult, ...],
) -> None:
    r, bundle = _bundle(offline, "q3_network_or_db")
    q = r.question
    hypothesis = Finding(
        kind=FindingKind.HYPOTHESIS,
        statement="checkout 지연은 DB 커넥션 부족 때문입니다",
        evidence_ids=("service.latency_p95@current",),
        basis=JudgementBasis.CORRELATION,
        confidence=Confidence.LOW,
    )
    told = replace(bundle, answer=bundle.answer.model_copy(update={"hypotheses": (hypothesis,)}))
    c = _check(q, told, "causality")
    assert c.status is CheckStatus.FAIL and "원인 후보" in c.detail
    hedged = hypothesis.model_copy(
        update={"statement": "DB 커넥션 부족이 원인 후보일 수 있음 (가능성)"}
    )
    ok = replace(bundle, answer=bundle.answer.model_copy(update={"hypotheses": (hedged,)}))
    assert _check(q, ok, "causality").status is CheckStatus.PASS
    summary = replace(
        bundle, answer=bundle.answer.model_copy(update={"summary": "원인은 네트워크로 확인됨"})
    )
    assert _check(q, summary, "causality").status is CheckStatus.FAIL  # 명사형 단정도 잡음
    for text in ("네트워크가 원인입니다.", "DB 때문임", "트래픽 증가로 인해 지연됨"):
        told2 = replace(bundle, answer=bundle.answer.model_copy(update={"summary": text}))
        assert _check(q, told2, "causality").status is CheckStatus.FAIL, text
    # 부정·유보 표현은 단정이 아님
    for text in (
        "네트워크가 원인으로 확인되지 않음",
        "특정 쿼리나 잠금을 원인으로 판단하지 않습니다",
        "원인은 확인되지 않음",
        "인과는 확인되지 않음",
        "DB 때문일 가능성이 있음",
    ):
        hedged2 = replace(bundle, answer=bundle.answer.model_copy(update={"summary": text}))
        assert _check(q, hedged2, "causality").status is CheckStatus.PASS, text


def test_order_check_fails_without_dependency(offline: tuple[QuestionResult, ...]) -> None:
    r, bundle = _bundle(offline, "q3_network_or_db")
    assert _check(r.question, bundle, "order").status is CheckStatus.PASS
    flat = replace(bundle, runs=tuple(replace(run, depends_on=()) for run in bundle.runs))
    o = _check(r.question, flat, "order")
    assert (
        o.status is CheckStatus.FAIL and "db←service" in o.detail and "network←service" in o.detail
    )


def test_report_files(offline: tuple[QuestionResult, ...], tmp_path: Path) -> None:
    info = RunInfo(
        started_at=NOW,
        profile="dev-tunnel",
        eval_set="config/eval/questions.yaml",
        use_llm=False,
        llm_provider=None,
        data_policy="none",
    )
    md, js = write_report(offline, info, tmp_path)
    text = md.read_text(encoding="utf-8")
    assert md.name == "eval-20261001T010000Z.md" and js.suffix == ".json"
    assert "저장소에 커밋하지 마세요" in text and "## 사람 검토 메모" in text
    assert "| q7_compare_30m |" in text and "<summary>답변 원문</summary>" in text
    assert '"passed_questions": 1' in js.read_text(encoding="utf-8")
    # 같은 초에 다시 실행해도 덮어쓰지 않음
    md2, js2 = write_report(offline, info, tmp_path)
    assert md2.name == "eval-20261001T010000Z-2.md" and js2.name == "eval-20261001T010000Z-2.json"
    assert md.exists()


def test_eval_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    import os

    from infra_agent import cli
    from infra_agent.evaluation import runner

    for key in list(os.environ):
        if key.startswith("INFRA_AGENT"):
            monkeypatch.delenv(key, raising=False)
    real = runner.answer_question

    async def offline_answer(question: str, settings: object, catalog: object, **kw: object):  # type: ignore[no-untyped-def]
        kw["transport"] = ExprProm().transport()
        return await real(question, settings, catalog, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(runner, "answer_question", offline_answer)
    out_dir = tmp_path / "eval"
    code = cli.main(
        [
            "eval",
            "--config",
            str(EXAMPLE),
            "--eval-set",
            str(EVAL_SET),
            "--output-dir",
            str(out_dir),
            "--only",
            "q1",
            "--no-llm",
        ]
    )
    captured = capsys.readouterr()
    assert code == cli.EXIT_EVAL_FAILED  # 데이터가 없어 근거 일치 기준 실패 (사용 불가 1과 구분)
    assert "q1_server_status" in captured.out and "보고서 저장:" in captured.out
    assert "경고: 보고서 위치" in captured.err  # tmp 경로는 Git 제외 폴더(var/) 밖
    assert len(list(out_dir.glob("eval-*.md"))) == 1
    args = ["eval", "--config", str(EXAMPLE), "--eval-set", str(EVAL_SET), "--only", "zz"]
    assert cli.main(args) == cli.EXIT_CONFIG_ERROR


def test_eval_set_model_is_frozen() -> None:
    eval_set = load_eval_set(EVAL_SET)
    assert isinstance(eval_set, EvalSet)
    with pytest.raises(Exception):  # noqa: B017 - pydantic frozen 모델
        eval_set.questions[0].expect.agents = frozenset()  # type: ignore[misc]


def test_json_report_stays_valid_with_masked_quotes(
    offline: tuple[QuestionResult, ...], tmp_path: Path
) -> None:
    """마스킹한 값 뒤에 따옴표가 있어도(로그 원문 등) JSON이 깨지지 않음."""
    import json

    first = offline[0]
    detail = 'msg="invalid token=abc123" ``` 끝'
    tricky = replace(
        first,
        checks=(CheckResult("routing", "에이전트 선택", CheckStatus.FAIL, detail),),
    )
    info = RunInfo(NOW, "dev-tunnel", "x.yaml", False, None, "none")
    md, js = write_report((tricky,), info, tmp_path)
    data = json.loads(js.read_text(encoding="utf-8"))
    stored = data["questions"][0]["checks"][0]["detail"]
    assert "abc123" not in stored and "token=" in stored
    # 본문에 ``` 가 있어도 답변 원문 코드 블록이 깨지지 않게 더 긴 구분자를 씀
    from infra_agent.evaluation.report import _fence

    assert _fence("a ``` b") == "````" and _fence("plain") == "```"
    assert "abc123" not in md.read_text(encoding="utf-8")


def test_select_matches_id_or_number_prefix() -> None:
    eval_set = EvalSet.model_validate(
        {
            "version": 1,
            "questions": [
                {
                    "id": f"q{n}_x",
                    "question": "현재 서버 상태가 어때?",
                    "expect": {"agents": ["server"], "intents": ["status"]},
                }
                for n in (1, 10)
            ],
        }
    )
    assert [q.id for q in select(eval_set, ["q1"])] == ["q1_x"]  # q10_x는 고르지 않음
    assert [q.id for q in select(eval_set, ["q10_x"])] == ["q10_x"]


def test_cli_helpers_var_and_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from infra_agent import cli

    monkeypatch.chdir(tmp_path)
    assert cli._inside_var(Path("var/eval"))
    assert not cli._inside_var(Path("other/var/eval"))  # 경로 중간의 var는 Git 제외 폴더가 아님
    assert not cli._inside_var(tmp_path.parent / "var")
    q = load_eval_set(EVAL_SET).questions[0]
    errored = QuestionResult(q, (), 0.1, error="ConnectError: refused")
    assert cli._all_unavailable((errored, errored))
    assert not cli._all_unavailable(())


def test_disclosure_requires_no_result_domains_in_unverified(
    offline: tuple[QuestionResult, ...],
) -> None:
    r, bundle = _bundle(offline, "q3_network_or_db")
    # 데이터 없는 분야는 "확인하지 못한 영역"에 적혀 있으므로 통과
    assert any("판단에 사용할 결과가 없어" in x for x in bundle.answer.unverified_areas)
    assert _check(r.question, bundle, "disclosure").status is CheckStatus.PASS
    hidden = replace(bundle, answer=bundle.answer.model_copy(update={"unverified_areas": ()}))
    c = _check(r.question, hidden, "disclosure")
    assert c.status is CheckStatus.FAIL and "확인하지 못한 영역에 적지 않음" in c.detail
    # 판단 결과가 없는데 "이상 징후가 없어 원인 후보 생략"을 붙이면 실패 (데이터 부재를 정상으로 봄)
    from infra_agent.agents.common import NO_ANOMALY_NO_HYPOTHESIS

    results = tuple(
        res.model_copy(update={"limitations": (*res.limitations, NO_ANOMALY_NO_HYPOTHESIS)})
        if res.agent is AgentName.DB
        else res
        for res in bundle.results
    )
    c2 = _check(r.question, replace(bundle, results=results), "disclosure")
    assert c2.status is CheckStatus.FAIL and "'이상 징후 없음'으로 답함" in c2.detail
