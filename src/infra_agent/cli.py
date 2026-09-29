"""명령행 인터페이스.

현재는 설정 확인(`config`)만 제공합니다. 질문 응답 명령은 이후 단계에서 추가합니다.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from infra_agent import __version__
from infra_agent.config import ConfigError, load_settings
from infra_agent.security import redact

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="infra-agent",
        description="멀티 에이전트 인프라 운영 분석 시스템 (읽기 전용)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    config_cmd = sub.add_parser("config", help="적용될 설정을 검증하고 출력합니다")
    config_cmd.add_argument(
        "--config",
        dest="config_path",
        default=None,
        help="설정 파일 경로 (기본: 환경 변수 INFRA_AGENT_CONFIG)",
    )
    return parser


def _cmd_config(config_path: str | None) -> int:
    try:
        settings = load_settings(config_path)
    except ConfigError as exc:
        print(redact(str(exc)), file=sys.stderr)
        return EXIT_CONFIG_ERROR
    # 설정에는 비밀값이 없지만, 출력 전 마스킹을 한 번 더 적용합니다.
    text = json.dumps(settings.model_dump(mode="json"), ensure_ascii=False, indent=2)
    print(redact(text))
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "config":
        return _cmd_config(args.config_path)
    return EXIT_CONFIG_ERROR  # pragma: no cover - argparse가 먼저 차단


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
