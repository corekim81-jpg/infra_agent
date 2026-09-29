from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from infra_agent.timeutil import ensure_utc, parse_duration


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("30s", timedelta(seconds=30)),
        ("30m", timedelta(minutes=30)),
        (" 1h ", timedelta(hours=1)),
        ("7d", timedelta(days=7)),
    ],
)
def test_parse_duration(text: str, expected: timedelta) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "30", "m", "0m", "1.5h", "-5m", "30 minutes", "1w"])
def test_parse_duration_invalid(text: str) -> None:
    with pytest.raises(ValueError):
        parse_duration(text)


def test_ensure_utc_converts() -> None:
    kst = timezone(timedelta(hours=9))
    value = ensure_utc(datetime(2026, 9, 28, 18, 0, tzinfo=kst))
    assert value == datetime(2026, 9, 28, 9, 0, tzinfo=UTC)
    assert value.tzinfo is UTC


def test_ensure_utc_rejects_naive() -> None:
    with pytest.raises(ValueError):
        ensure_utc(datetime(2026, 9, 28, 9, 0))
