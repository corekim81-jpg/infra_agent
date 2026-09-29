"""단위 표시 형식. 답변과 모델 관측 데이터가 같은 표시값을 쓰도록 계층 중립 위치에 둡니다."""

from __future__ import annotations


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
    return f"{value:.3g}{(' ' + unit) if unit else ''}"


def fmt_delta(value: float, unit: str | None) -> str:
    sign = "+" if value >= 0 else "-"
    return sign + fmt_value(abs(value), unit)
