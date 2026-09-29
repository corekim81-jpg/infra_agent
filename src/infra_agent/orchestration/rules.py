"""규칙 기반 질문 해석 (모델 없이 동작하는 임시 Coordinator 해석부).

키워드와 정규식으로 의도·시간 범위·대상·분야를 판별합니다.
- 판별하지 못한 부분은 추측으로 채우지 않고 `assumptions`에 기본값 사용 사실을 남깁니다.
- 아직 구현되지 않은 분야는 `unsupported_domains`로 돌려주어
  답변의 "확인하지 못한 영역"에 표시합니다.
모델 기반 해석(`llm_interpret`)도 `finalize()`로 같은 검증·보정을 거치며,
모델 해석이 실패하면 이 모듈로 대체합니다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from infra_agent.schemas import Intent, TargetKind, TimeRange

InterpretMethod = Literal["rules", "model"]

MAX_RANGE = timedelta(days=7)
"""개발 환경 Prometheus 보존 기간(1w) 기준 최대 조회 구간."""

IMPLEMENTED_DOMAINS = frozenset({"server"})

DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "server": (
        "서버",
        "노드",
        "node",
        "cpu",
        "씨피유",
        "메모리",
        "memory",
        "디스크",
        "disk",
        "파일시스템",
        "filesystem",
        "자원",
        "리소스",
        "resource",
        "사용률",
        "사용량",
        "스로틀",
        "throttl",
        "파드",
        "pod",
        "컨테이너",
        "container",
        "부하",
        "load",
    ),
    "kubernetes": (
        "재시작",
        "restart",
        "pending",
        "펜딩",
        "oom",
        "스케줄",
        "schedul",
        "deployment",
        "디플로이먼트",
        "이벤트",
        "event",
        "crashloop",
        "replica",
        "레플리카",
        "notready",
    ),
    "network": (
        "네트워크",
        "network",
        "dns",
        "패킷",
        "packet",
        "드롭",
        "drop",
        "통신",
        "hubble",
        "연결 오류",
        "tcp",
    ),
    "db": (
        "db",
        "데이터베이스",
        "database",
        "커넥션",
        "connection",
        "쿼리",
        "query",
        "postgres",
        "잠금",
        "lock",
        "데드락",
        "deadlock",
        "valkey",
        "redis",
        "캐시",
        "cache",
    ),
    "service": (
        "서비스",
        "service",
        "응답",
        "latency",
        "지연",
        "오류율",
        "에러",
        "error",
        "로그",
        "log",
        "트레이스",
        "trace",
        "요청",
        "request",
    ),
}

COMPARE_KEYWORDS = ("비교", "직전", "달라", "변화", "전보다", "대비", "compare", "차이")
ANOMALY_KEYWORDS = ("증가", "급증", "늘어", "늘었", "비정상", "이상 징후", "튀", "spike", "올라")

# 한글 조사("1시간과")가 붙어도 인식하도록 \b 대신 영문자가 이어지지 않는 조건만 둡니다.
_DURATION_RE = re.compile(r"(\d+)\s*(초|분|시간|일|s|m|h|d)(?![a-z])", re.IGNORECASE)
_UNIT = {"초": 1, "s": 1, "분": 60, "m": 60, "시간": 3600, "h": 3600, "일": 86400, "d": 86400}

# 대상 이름은 하이픈이나 숫자를 포함한 소문자 이름만 인정합니다.
# ("node cpu"의 cpu를 대상으로 오인하지 않기 위함)
_NAME = r"((?=[a-z0-9.-]*[-0-9])[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)(?![a-z0-9.-])"
_TARGET_PATTERNS: dict[TargetKind, re.Pattern[str]] = {
    TargetKind.NAMESPACE: re.compile(r"(?:네임스페이스|namespace|ns)\s*[:=]?\s*" + _NAME),
    TargetKind.NODE: re.compile(r"(?:노드|node)\s*[:=]?\s*" + _NAME),
    TargetKind.POD: re.compile(r"(?:파드|pod)\s*[:=]?\s*" + _NAME),
}


@dataclass(frozen=True)
class Interpretation:
    intent: Intent
    time_range: TimeRange
    baseline_range: TimeRange | None
    targets: dict[TargetKind, str]
    domains: frozenset[str]
    unsupported_domains: frozenset[str]
    assumptions: tuple[str, ...] = field(default_factory=tuple)
    method: InterpretMethod = "rules"
    """질문 해석 방식. 가정(assumptions)과 구분해 표시합니다."""
    method_note: str | None = None
    """규칙 기반으로 처리한 이유 (모델 사용 불가, 모델 해석 실패 등)."""


def _duration(text: str) -> timedelta | None:
    match = _DURATION_RE.search(text)
    if match is None:
        return None
    amount = int(match.group(1))
    unit = _UNIT[match.group(2).lower()]
    return timedelta(seconds=amount * unit) if amount > 0 else None


KNOWN_DOMAINS = frozenset(DOMAIN_KEYWORDS)
TARGET_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")


def finalize(
    *,
    intent: Intent,
    duration: timedelta | None,
    targets: Mapping[TargetKind, str],
    domains: set[str],
    now: datetime,
    default_range: timedelta,
    range_override: timedelta | None = None,
    target_overrides: Mapping[TargetKind, str] | None = None,
    assumptions: list[str] | None = None,
    method: InterpretMethod = "rules",
) -> Interpretation:
    """해석 결과를 공통 규칙(기본 구간, 보존 기간 제한, 옵션 우선)으로 확정합니다.

    규칙 기반 해석과 모델 기반 해석이 같은 검증·보정 경로를 사용합니다.
    """
    notes = list(assumptions or [])
    duration = range_override or duration
    if duration is None:
        duration = default_range
        if intent is not Intent.STATUS:  # 상태 조회는 현재 값만 쓰므로 구간 가정을 표시하지 않음
            minutes = int(duration.total_seconds() // 60)
            notes.append(f"시간 범위가 없어 최근 {minutes}분으로 해석")
    if duration > MAX_RANGE:
        duration = MAX_RANGE
        notes.append("데이터 보존 기간(7일)을 넘는 범위는 최근 7일로 줄여 해석")
    time_range = TimeRange.last(duration, now)
    baseline: TimeRange | None = None
    if intent in (Intent.COMPARE, Intent.ANOMALY):
        baseline = time_range.previous()
        if now - baseline.start > MAX_RANGE:
            baseline = None
            intent = Intent.STATUS
            notes.append("기준 구간이 보존 기간을 넘어 비교 없이 상태 조회로 해석")

    final_targets = {k: v for k, v in targets.items() if TARGET_NAME_RE.match(v)}
    if target_overrides:
        final_targets.update(target_overrides)

    final_domains = {d for d in domains if d in KNOWN_DOMAINS}
    if not final_domains:
        final_domains = {"server"}
        notes.append("질문 분야를 특정하지 못해 서버(노드·Pod·컨테이너) 자원 상태로 해석")
    return Interpretation(
        intent=intent,
        time_range=time_range,
        baseline_range=baseline,
        targets=final_targets,
        domains=frozenset(final_domains),
        unsupported_domains=frozenset(final_domains - IMPLEMENTED_DOMAINS),
        assumptions=tuple(notes),
        method=method,
    )


def interpret(
    question: str,
    now: datetime,
    default_range: timedelta,
    *,
    range_override: timedelta | None = None,
    target_overrides: Mapping[TargetKind, str] | None = None,
) -> Interpretation:
    """키워드·정규식 기반 해석."""
    text = question.lower()
    if any(k in text for k in COMPARE_KEYWORDS):
        intent = Intent.COMPARE
    elif any(k in text for k in ANOMALY_KEYWORDS):
        intent = Intent.ANOMALY
    else:
        intent = Intent.STATUS
    targets: dict[TargetKind, str] = {}
    for kind, pattern in _TARGET_PATTERNS.items():
        match = pattern.search(text)
        if match:
            targets[kind] = match.group(1)
    domains = {d for d, words in DOMAIN_KEYWORDS.items() if any(w in text for w in words)}
    return finalize(
        intent=intent,
        duration=_duration(text),
        targets=targets,
        domains=domains,
        now=now,
        default_range=default_range,
        range_override=range_override,
        target_overrides=target_overrides,
    )
