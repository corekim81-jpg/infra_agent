"""단위 표시 형식. 답변과 모델 관측 데이터가 같은 표시값을 쓰도록 계층 중립 위치에 둡니다."""

from __future__ import annotations

from datetime import datetime

RATE_UNITS = frozenset({"calls/s", "requests/s"})


def fmt_value(value: float, unit: str | None) -> str:
    if unit == "ratio":
        return f"{value * 100:.1f}%"
    if unit == "bytes":
        size = float(value)
        for suffix in ("B", "KiB", "MiB", "GiB", "TiB"):
            if abs(size) < 1024 or suffix == "TiB":
                return f"{size:.0f}{suffix}" if suffix == "B" else f"{size:.1f}{suffix}"
            size /= 1024
    if unit == "cores":
        return f"{value:.3f} cores"
    if unit == "seconds":
        return f"{value:.3g}초"
    if unit in RATE_UNITS:
        return f"{value:.3g}건/초"
    return f"{value:.3g}{(' ' + unit) if unit else ''}"


def fmt_delta(value: float, unit: str | None) -> str:
    sign = "+" if value >= 0 else "-"
    return sign + fmt_value(abs(value), unit)


def fmt_time(value: datetime, *, seconds: bool = True) -> str:
    """답변 표시용 로컬 시각 (예: 2026-09-29 16:57:44 KST)."""
    local = value.astimezone()
    text = f"{local:%Y-%m-%d %H:%M:%S}" if seconds else f"{local:%Y-%m-%d %H:%M}"
    return f"{text} {local.tzname() or ''}".strip()


def fmt_points(delta_ratio: float) -> str:
    """비율 차이를 %p로 표시 (예: 0.072 → +7.2%p)."""
    return f"{'+' if delta_ratio >= 0 else '-'}{abs(delta_ratio) * 100:.1f}%p"
