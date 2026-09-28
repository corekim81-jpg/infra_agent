"""시간 범위 유틸리티.

모든 분석 시간 범위는 UTC 기준의 timezone-aware datetime으로 다룹니다.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value: str) -> timedelta:
    """`30s`, `30m`, `1h`, `7d` 형식의 기간 문자열을 timedelta로 변환합니다."""
    match = _DURATION_RE.match(value)
    if match is None:
        raise ValueError(f"기간 형식이 올바르지 않습니다: {value!r} (예: 30s, 30m, 1h, 7d)")
    amount, unit = int(match.group(1)), match.group(2)
    if amount <= 0:
        raise ValueError(f"기간은 0보다 커야 합니다: {value!r}")
    return timedelta(seconds=amount * _UNIT_SECONDS[unit])


def ensure_utc(value: datetime) -> datetime:
    """timezone-aware datetime을 UTC로 변환합니다. naive datetime은 거부합니다."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("시간대 정보가 없는 datetime은 사용할 수 없습니다 (UTC 기준 필요)")
    return value.astimezone(UTC)


def utc_now() -> datetime:
    return datetime.now(UTC)
