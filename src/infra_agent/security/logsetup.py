"""비밀값을 마스킹하는 로깅 설정."""

from __future__ import annotations

import logging
import sys
from typing import TextIO

from infra_agent.security.redaction import Redactor, default_redactor

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class RedactingFormatter(logging.Formatter):
    """메시지, 인자, 예외 추적 정보를 포함한 최종 로그 문자열을 마스킹합니다."""

    def __init__(self, fmt: str = _FORMAT, redactor: Redactor | None = None) -> None:
        super().__init__(fmt)
        self._redactor = redactor or default_redactor

    def format(self, record: logging.LogRecord) -> str:
        return self._redactor.redact(super().format(record))


def configure_logging(level: int | str = logging.INFO, stream: TextIO | None = None) -> None:
    """루트 로거에 마스킹 포매터를 사용하는 핸들러를 설정합니다."""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(RedactingFormatter())
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
