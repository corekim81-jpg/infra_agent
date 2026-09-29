"""명령행 인터페이스.

- `config`: 적용될 설정을 검증하고 출력합니다.
- `check`: 활성화된 데이터 소스(Prometheus, Loki, Tempo)의 연결 상태를 점검합니다.
- `discover`: 지표·라벨·최신성을 탐색해 `var/discovery/`에 보고서를 저장합니다.
- `catalog`: 조회 카탈로그 파일을 검증하고, `--execute` 시 각 조회를 Prometheus에 실행해 점검합니다.
- `ask`: 질문에 답합니다. 현재 Server Agent(k3d 노드·Pod·컨테이너 자원)만 동작하며,
  설정에 따라 모델로 질문 해석·원인 후보를 보완합니다(`--no-llm`으로 끌 수 있음).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from infra_agent import __version__
from infra_agent.answer.render import render_text
from infra_agent.catalog import CatalogError, load_catalog
from infra_agent.catalog.check import CheckStatus, ItemCheck, check_catalog
from infra_agent.config import ConfigError, Settings, load_settings
from infra_agent.datasources.errors import DataSourceError
from infra_agent.datasources.probe import SourceStatus, check_sources
from infra_agent.datasources.prometheus import PrometheusClient
from infra_agent.discovery import DiscoveryOptions, discover, write_report
from infra_agent.orchestration.runner import answer_question
from infra_agent.schemas import AgentStatus, TargetKind
from infra_agent.security import configure_logging, redact
from infra_agent.timeutil import parse_duration

EXIT_OK = 0
EXIT_UNAVAILABLE = 1
EXIT_CONFIG_ERROR = 2


def _add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        dest="config_path",
        default=None,
        help="설정 파일 경로 (기본: 환경 변수 INFRA_AGENT_CONFIG)",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="infra-agent",
        description="멀티 에이전트 인프라 운영 분석 시스템 (읽기 전용)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    config_cmd = sub.add_parser("config", help="적용될 설정을 검증하고 출력합니다")
    _add_config_arg(config_cmd)

    check_cmd = sub.add_parser("check", help="데이터 소스 연결 상태를 점검합니다")
    _add_config_arg(check_cmd)
    check_cmd.add_argument("--json", action="store_true", help="JSON으로 출력")

    disc = sub.add_parser("discover", help="지표·라벨·최신성을 탐색해 보고서를 저장합니다")
    _add_config_arg(disc)
    disc.add_argument(
        "--output-dir", default="var/discovery", help="보고서 저장 위치 (기본: var/discovery)"
    )
    disc.add_argument("--lookback", default="1h", help="라벨 조회 구간 (기본: 1h)")
    disc.add_argument(
        "--max-metrics", type=int, default=400, help="상세 탐색할 최대 지표 수 (기본: 400)"
    )
    disc.add_argument(
        "--include-prefix",
        action="append",
        default=[],
        metavar="TOKEN",
        help="추가로 상세 탐색할 지표 이름 첫 토큰 (예: app). 여러 번 지정 가능",
    )
    disc.add_argument("--show-ips", action="store_true", help="라벨 값의 IP 주소를 마스킹하지 않음")
    disc.add_argument("--no-values", action="store_true", help="라벨 값 표본을 보고서에서 제외")

    cat = sub.add_parser("catalog", help="조회 카탈로그를 검증하고 (선택) 실행 점검합니다")
    _add_config_arg(cat)
    cat.add_argument("--catalog", dest="catalog_path", default=None, help="카탈로그 파일 경로")
    cat.add_argument(
        "--execute", action="store_true", help="각 조회를 Prometheus에 실행해 오류·빈 결과 확인"
    )
    cat.add_argument("--range", dest="range_", default="5m", help="범위 구간 값 (기본: 5m)")

    ask = sub.add_parser("ask", help="질문에 답합니다 (현재: 모델 없이 Server Agent)")
    _add_config_arg(ask)
    ask.add_argument("question", help='질문 (예: "현재 서버 상태가 어때?")')
    ask.add_argument("--range", dest="range_", default=None, help="분석 구간 (예: 30m, 1h)")
    ask.add_argument("--namespace", default=None, help="대상 네임스페이스")
    ask.add_argument("--node", default=None, help="대상 노드")
    ask.add_argument("--pod", default=None, help="대상 Pod")
    ask.add_argument("--json", action="store_true", help="답변을 JSON으로 출력")
    ask.add_argument(
        "--show-queries",
        action="store_true",
        help="근거에 실행한 조회식과 제외된 모델 원인 후보(진단용) 표시",
    )
    ask.add_argument(
        "--no-llm", action="store_true", help="모델을 호출하지 않고 규칙·코드 판정만 사용"
    )
    return parser


def _load(config_path: str | None) -> Settings | None:
    try:
        return load_settings(config_path)
    except ConfigError as exc:
        print(redact(str(exc)), file=sys.stderr)
        return None


def _cmd_config(config_path: str | None) -> int:
    settings = _load(config_path)
    if settings is None:
        return EXIT_CONFIG_ERROR
    # 설정에는 비밀값이 없지만, 출력 전 마스킹을 한 번 더 적용합니다.
    text = json.dumps(settings.model_dump(mode="json"), ensure_ascii=False, indent=2)
    print(redact(text))
    return EXIT_OK


def _status_line(st: SourceStatus) -> str:
    if not st.enabled:
        return f"- {st.name}: 비활성 (설정에서 enabled=false)"
    if st.reachable:
        state = "정상" if st.ready else "응답했지만 준비되지 않음"
        return (
            f"- {st.name}: {state} ({st.url}, 버전 {st.version or '확인 불가'}, {st.latency_ms}ms)"
        )
    line = f"- {st.name}: 연결 실패 ({st.url}) [{st.error_code}] {st.error}"
    if st.hint:
        line += f"\n    힌트: {st.hint}"
    return line


def _cmd_check(config_path: str | None, as_json: bool) -> int:
    settings = _load(config_path)
    if settings is None:
        return EXIT_CONFIG_ERROR
    statuses = asyncio.run(check_sources(settings))
    if as_json:
        print(
            json.dumps([s.model_dump(mode="json") for s in statuses], ensure_ascii=False, indent=2)
        )
    else:
        print(f"프로필: {settings.profile.value}")
        for st in statuses:
            print(redact(_status_line(st)))
    enabled = [s for s in statuses if s.enabled]
    if not enabled:
        print("활성화된 데이터 소스가 없습니다.", file=sys.stderr)
        return EXIT_UNAVAILABLE
    return EXIT_OK if all(s.reachable and s.ready for s in enabled) else EXIT_UNAVAILABLE


def _cmd_discover(args: argparse.Namespace) -> int:
    settings = _load(args.config_path)
    if settings is None:
        return EXIT_CONFIG_ERROR
    try:
        lookback = parse_duration(args.lookback)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CONFIG_ERROR
    if args.max_metrics < 1:
        print("--max-metrics는 1 이상이어야 합니다", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    options = DiscoveryOptions(
        lookback=lookback,
        max_metrics=args.max_metrics,
        mask_ips=not args.show_ips,
        include_values=not args.no_values,
        extra_prefixes=frozenset(args.include_prefix),
    )
    print("탐색 중입니다. 지표 수에 따라 수 분이 걸릴 수 있습니다...", file=sys.stderr)
    report = asyncio.run(discover(settings, options))
    json_path, md_path = write_report(report, Path(args.output_dir))
    print(f"보고서 저장: {md_path}")
    print(f"원본 데이터: {json_path}")
    print("주의: 실제 환경 데이터가 포함되어 있으므로 저장소에 커밋하지 마세요.")
    for warning in report.warnings:
        print(f"경고: {redact(warning)}", file=sys.stderr)
    prom = report.prometheus
    return EXIT_OK if prom is not None and prom.status.reachable else EXIT_UNAVAILABLE


_STATUS_LABEL = {
    CheckStatus.OK: "정상",
    CheckStatus.EMPTY: "결과 없음",
    CheckStatus.MISSING_METRICS: "지표 없음",
    CheckStatus.ERROR: "오류",
    CheckStatus.SKIPPED: "건너뜀",
}


def _cmd_catalog(args: argparse.Namespace) -> int:
    settings = _load(args.config_path)
    if settings is None:
        return EXIT_CONFIG_ERROR
    path = args.catalog_path or settings.catalog.path
    if not path:
        print(
            "카탈로그 경로가 없습니다. --catalog 또는 설정 catalog.path를 지정하세요.",
            file=sys.stderr,
        )
        return EXIT_CONFIG_ERROR
    try:
        catalog = load_catalog(path)
        parse_duration(args.range_)
    except (CatalogError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CONFIG_ERROR

    by_agent: dict[str, int] = {}
    for item in catalog.items.values():
        by_agent[item.agent.value] = by_agent.get(item.agent.value, 0) + 1
    print(f"카탈로그: {path} (환경 {catalog.environment}, 항목 {len(catalog.items)}개)")
    print("에이전트별: " + ", ".join(f"{k} {v}" for k, v in sorted(by_agent.items())))
    if not args.execute:
        print("형식 검증 통과. 실제 조회 점검은 --execute를 사용하세요.")
        return EXIT_OK
    if not settings.datasources.prometheus.enabled:
        print("Prometheus가 비활성화되어 있어 실행 점검을 할 수 없습니다.", file=sys.stderr)
        return EXIT_UNAVAILABLE

    async def run() -> list[ItemCheck]:
        async with PrometheusClient.from_config(
            settings.datasources.prometheus, max_retries=settings.execution.max_retries
        ) as prom:
            return await check_catalog(
                catalog, prom, range_=args.range_, concurrency=settings.execution.max_concurrency
            )

    try:
        results = asyncio.run(run())
    except DataSourceError as exc:
        print(f"Prometheus 조회 실패: {exc.message}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    counts: dict[CheckStatus, int] = {}
    for r in sorted(results, key=lambda r: (r.status is CheckStatus.OK, r.key)):
        counts[r.status] = counts.get(r.status, 0) + 1
        line = f"- [{_STATUS_LABEL[r.status]}] {r.key} ({r.agent})"
        if r.status is CheckStatus.OK:
            line += f": 시계열 {r.series}개"
        elif r.detail:
            line += f": {r.detail}"
        print(redact(line))
    print("요약: " + ", ".join(f"{_STATUS_LABEL[k]} {v}" for k, v in counts.items()))
    failed = counts.get(CheckStatus.ERROR, 0) + counts.get(CheckStatus.MISSING_METRICS, 0)
    return EXIT_UNAVAILABLE if failed else EXIT_OK


def _cmd_ask(args: argparse.Namespace) -> int:
    settings = _load(args.config_path)
    if settings is None:
        return EXIT_CONFIG_ERROR
    if not settings.datasources.prometheus.enabled:
        print("Prometheus가 비활성화되어 있어 답할 수 없습니다.", file=sys.stderr)
        return EXIT_UNAVAILABLE
    if not settings.catalog.path:
        print("카탈로그 경로(catalog.path)가 설정되지 않았습니다.", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    try:
        catalog = load_catalog(settings.catalog.path)
        if args.range_:
            parse_duration(args.range_)
    except (CatalogError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CONFIG_ERROR
    overrides: dict[TargetKind, str] = {}
    for kind, value in (
        (TargetKind.NAMESPACE, args.namespace),
        (TargetKind.NODE, args.node),
        (TargetKind.POD, args.pod),
    ):
        if value:
            overrides[kind] = value
    bundle = asyncio.run(
        answer_question(
            args.question,
            settings,
            catalog,
            range_override=args.range_,
            target_overrides=overrides,
            use_llm=not args.no_llm,
        )
    )
    if args.json:
        payload = {
            "request_id": bundle.context.request_id,
            "intent": bundle.context.intent.value,
            "assumptions": list(bundle.interpretation.assumptions),
            "interpretation_method": bundle.interpretation.method,
            "interpretation_note": bundle.interpretation.method_note,
            "llm": {
                "provider": bundle.llm_name,
                "calls": bundle.llm_calls,
                "data_policy": bundle.data_policy,
            },
            "answer": bundle.answer.model_dump(mode="json"),
            "rejected_hypotheses": [
                {"agent": r.agent.value, **x.model_dump(mode="json")}
                for r in bundle.results
                for x in r.rejected_hypotheses
            ],
        }
        print(redact(json.dumps(payload, ensure_ascii=False, indent=2)))
    else:
        print(redact(render_text(bundle, show_queries=args.show_queries)))
    failed = bundle.results and all(r.status is AgentStatus.FAILED for r in bundle.results)
    return EXIT_UNAVAILABLE if failed else EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    configure_logging("WARNING")
    if args.command == "config":
        return _cmd_config(args.config_path)
    if args.command == "check":
        return _cmd_check(args.config_path, args.json)
    if args.command == "discover":
        return _cmd_discover(args)
    if args.command == "catalog":
        return _cmd_catalog(args)
    if args.command == "ask":
        return _cmd_ask(args)
    return EXIT_CONFIG_ERROR  # pragma: no cover - argparse가 먼저 차단


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
