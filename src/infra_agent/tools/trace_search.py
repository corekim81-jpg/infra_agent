"""읽기 전용 트레이스 검색 도구 (Tempo TraceQL).

- 검색 조건은 코드가 정한 형태(서비스·오류 상태·최소 지연)만 만들며, 서비스 이름은 형식을 검사한 뒤
  문자열 리터럴로만 넣습니다(임의 TraceQL을 실행하지 않음).
- 속성 이름 `resource.service.name`은 개발 서버 탐색에서 확인한 resource 태그 `service.name`
  기준입니다(environment.md 3.4절).
- 모든 호출은 요청 단위 조회 예산과 호출별 제한 시간을 따릅니다.
"""

from __future__ import annotations

import asyncio
import re

from infra_agent.datasources.errors import DataSourceError
from infra_agent.datasources.tempo import TempoClient
from infra_agent.schemas import AgentName, DataSourceKind, TimeRange, ToolResult, ToolStatus
from infra_agent.timeutil import utc_now
from infra_agent.tools.catalog_query import QueryOutcome, ToolBudget
from infra_agent.tools.log_query import clean_text, normalize_trace_id

SERVICE_ATTR = "resource.service.name"
_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def is_service_name(name: str) -> bool:
    """TraceQL에 넣을 수 있는 서비스 이름 형식인지.

    Tempo가 루트 span 대신 주는 "<root span not yet received>" 같은 표시는 서비스가 아닙니다.
    """
    return bool(_SERVICE_NAME_RE.match(name))


def build_traceql(
    service: str | None, *, errors: bool = False, min_duration_ms: int | None = None
) -> str:
    conditions = []
    if service is not None:
        if not is_service_name(service):
            raise ValueError(f"서비스 이름 형식이 올바르지 않습니다: {service!r}")
        conditions.append(f'{SERVICE_ATTR} = "{service}"')
    if errors:
        conditions.append("status = error")
    if min_duration_ms is not None:
        conditions.append(f"duration > {max(1, int(min_duration_ms))}ms")
    return "{ " + " && ".join(conditions) + " }" if conditions else "{ }"


class TraceSearchTool:
    def __init__(
        self,
        tempo: TempoClient,
        *,
        agent: AgentName,
        budget: ToolBudget,
        timeout_seconds: float,
    ) -> None:
        self._tempo = tempo
        self._agent = agent
        self._budget = budget
        self._timeout = timeout_seconds

    def _result(
        self,
        evidence_id: str,
        query: str,
        window: TimeRange,
        status: ToolStatus,
        *,
        data: object = None,
        error: str | None = None,
    ) -> ToolResult:
        return ToolResult(
            evidence_id=evidence_id,
            source=DataSourceKind.TEMPO,
            query=query,
            time_range=window,
            status=status,
            data=data,
            fetched_at=utc_now(),
            error=error,
        )

    async def search(
        self,
        evidence_id: str,
        window: TimeRange,
        *,
        service: str | None,
        errors: bool = False,
        min_duration_ms: int | None = None,
        limit: int = 3,
    ) -> QueryOutcome:
        try:
            traceql = build_traceql(service, errors=errors, min_duration_ms=min_duration_ms)
        except ValueError as exc:
            return QueryOutcome(
                result=self._result(evidence_id, "", window, ToolStatus.ERROR, error=str(exc)),
                skipped=True,
            )
        if not self._budget.take():
            return QueryOutcome(
                result=self._result(
                    evidence_id,
                    traceql,
                    window,
                    ToolStatus.ERROR,
                    error="요청당 조회 호출 상한(analysis.max_tool_calls)에 도달해 조회하지 않음",
                )
            )
        try:
            traces = await asyncio.wait_for(
                self._tempo.search(traceql, window.start, window.end, limit),
                timeout=self._timeout,
            )
        except TimeoutError:
            return QueryOutcome(
                result=self._result(
                    evidence_id,
                    traceql,
                    window,
                    ToolStatus.TIMEOUT,
                    error=f"조회 제한 시간({self._timeout:.0f}초) 초과",
                )
            )
        except DataSourceError as exc:
            status = ToolStatus.TIMEOUT if exc.code == "timeout" else ToolStatus.ERROR
            return QueryOutcome(
                result=self._result(
                    evidence_id, traceql, window, status, error=f"{exc.code}: {exc.message}"
                )
            )
        # Tempo는 trace_id의 앞자리 0을 빼고 돌려주므로 로그와 같은 32자리 형태로 맞춤.
        # 16진수가 아닌 값(외부 데이터)은 버림.
        rows = [
            {
                "trace_id": trace_id,
                "root_service": clean_text(t.root_service, 80) if t.root_service else None,
                "root_name": clean_text(t.root_name, 120) if t.root_name else None,
                "start": t.start.isoformat() if t.start else None,
                "duration_ms": t.duration_ms,
            }
            for t in traces
            if (trace_id := normalize_trace_id(t.trace_id))
        ]
        status = ToolStatus.OK if rows else ToolStatus.EMPTY
        return QueryOutcome(result=self._result(evidence_id, traceql, window, status, data=rows))
