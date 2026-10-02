"""비밀값 마스킹.

로그, 오류 메시지, 사용자 출력에 인증정보가 남지 않도록 문자열을 마스킹합니다.
패턴 기반 마스킹은 완전하지 않으므로, 프로그램이 알고 있는 비밀값은
`Redactor.register()`로 등록해 값 그대로도 마스킹합니다.
"""

from __future__ import annotations

import re
import threading
from typing import Any

MASK = "***"

_KEY_NAMES = (
    r"password|passwd|pwd|secret|client[_-]?secret|token|access[_-]?token|refresh[_-]?token"
    r"|api[_-]?key|x-api-key|access[_-]?key|secret[_-]?key|private[_-]?key"
)

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Authorization 헤더 값 (Bearer/Basic/Token 등)
    (
        re.compile(
            r"(?i)\b(authorization[\"']?\s*[:=]\s*[\"']?)(bearer|basic|token)\s+[^\s\"',;]+"
        ),
        rf"\1\2 {MASK}",
    ),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"), rf"\1 {MASK}"),
    # JSON/YAML/쿼리 문자열의 key=value, "key": "value"
    (
        re.compile(rf"(?i)([\"']?(?:{_KEY_NAMES})[\"']?\s*[:=]\s*[\"']?)([^\s\"',;&}}]+)"),
        rf"\1{MASK}",
    ),
    # URL 사용자 정보 scheme://user:pass@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@"), rf"\1{MASK}@"),
    # 잘 알려진 키 형식
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}"), MASK),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"), MASK),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), MASK),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), MASK),
    # JWT
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"), MASK),
)

_MIN_LITERAL_LENGTH = 4


class Redactor:
    """패턴 기반 마스킹과 등록된 비밀값 마스킹을 수행합니다."""

    def __init__(self) -> None:
        self._literals: set[str] = set()
        self._lock = threading.Lock()

    def register(self, secret: str | None) -> None:
        """비밀값을 등록합니다. 너무 짧은 값은 오탐을 막기 위해 등록하지 않습니다."""
        if secret and len(secret) >= _MIN_LITERAL_LENGTH:
            with self._lock:
                self._literals.add(secret)

    def redact(self, text: str) -> str:
        with self._lock:
            literals = sorted(self._literals, key=len, reverse=True)
        for literal in literals:
            text = text.replace(literal, MASK)
        for pattern, replacement in _PATTERNS:
            text = pattern.sub(replacement, text)
        return text


default_redactor = Redactor()


def redact(text: str) -> str:
    """기본 Redactor로 문자열을 마스킹합니다."""
    return default_redactor.redact(text)


def redact_values(value: Any) -> Any:
    """JSON으로 내보낼 구조의 문자열 값마다 마스킹합니다.

    직렬화한 뒤 전체를 마스킹하면 `token=***\"`처럼 이스케이프가 지워져 JSON이 깨질 수 있으므로,
    직렬화 전에 값 단위로 마스킹합니다.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_values(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact_values(v) for v in value]
    return value
