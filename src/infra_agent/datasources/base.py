"""조회 도구가 의존하는 데이터 접근 인터페이스.

조회 도구(`tools/`)는 구체 클라이언트가 아니라 이 인터페이스만 사용합니다. 직접 API 클라이언트
(`PrometheusClient`, `LokiClient`, `TempoClient`)가 기본 구현이며, 같은 메서드를 가진 다른 구현
(예: MCP 서버를 통해 조회하는 클라이언트)으로 바꿀 수 있습니다.

구현이 지켜야 할 것:
- 조회만 수행합니다 (쓰기·변경 없음).
- 실패는 `datasources.errors.DataSourceError` 계열 예외로 알립니다. 도구 계층이 이를 오류 상태의
  근거로 바꾸므로, 빈 결과로 바꿔 돌려주면 안 됩니다(데이터 부재와 조회 실패를 구분).
- 결과 형식(`InstantResult`, `RangeSeries`, `LogEntry`, `TraceSummary`)을 그대로 지킵니다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from infra_agent.datasources.loki import LogEntry, LogSample
from infra_agent.datasources.prometheus import InstantResult, RangeSeries
from infra_agent.datasources.tempo import TraceSummary


class MetricsSource(Protocol):
    """PromQL 지표 조회."""

    async def query(self, expr: str, time: datetime | None = None) -> InstantResult: ...

    async def query_range(
        self, expr: str, start: datetime, end: datetime, step_seconds: int
    ) -> list[RangeSeries]: ...

    async def scalar_or_none(self, expr: str, time: datetime | None = None) -> float | None: ...


class LogsSource(Protocol):
    """LogQL 조회: 지표형 순간 조회와 로그 줄 조회(최신순)."""

    async def query(self, expr: str, at: datetime) -> list[LogSample]: ...

    async def query_logs(
        self, expr: str, start: datetime, end: datetime, limit: int
    ) -> list[LogEntry]: ...


class TracesSource(Protocol):
    """TraceQL 트레이스 검색."""

    async def search(
        self, traceql: str, start: datetime, end: datetime, limit: int
    ) -> list[TraceSummary]: ...
