"""읽기 전용 로그 조회 도구 (Loki, 카탈로그 기반).

- 에이전트는 카탈로그에서 자기 분야로 지정된 **Loki 항목만** 실행할 수 있습니다.
- 로그 본문은 외부 데이터입니다. 제어문자를 지우고 길이를 제한한 뒤 비밀값을 마스킹해
  근거(`ToolResult.data`)에 담으며, 어떤 경우에도 실행 지시로 취급하지 않습니다.
- 모든 호출은 요청 단위 조회 예산(`ToolBudget`)과 호출별 제한 시간을 따릅니다.
- Loki 결과에는 Prometheus처럼 "최신 샘플 시각"이 없으므로, 빈 결과를 "해당 없음"으로 판단하려면
  호출자가 같은 대상·구간의 전체 로그 수(`log.lines_total`)로 로그 존재를 확인해야 합니다.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from typing import Any

from infra_agent.catalog import Catalog, CatalogItem
from infra_agent.datasources.errors import DataSourceError
from infra_agent.datasources.loki import LokiClient
from infra_agent.schemas import (
    AgentName,
    DataSourceKind,
    TargetKind,
    TimeRange,
    ToolResult,
    ToolStatus,
)
from infra_agent.security import redact
from infra_agent.timeutil import utc_now
from infra_agent.tools.catalog_query import QueryOutcome, ToolBudget, ToolPermissionError

MAX_LINE_CHARS = 200
SAMPLE_LABELS = (
    "service_name",
    "k8s_namespace_name",
    "k8s_pod_name",
    "severity_text",
    "detected_level",
    "level",
)
"""샘플에 남길 라벨. 나머지 라벨은 버립니다(불필요한 내부 정보 노출 방지)."""
TRACE_ID_LABELS = ("trace_id", "traceid", "traceID", "trace.id")

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_SPACE_RE = re.compile(r"\s{2,}")
_TRACE_IN_LINE_RE = re.compile(
    r"(?i)trace[_.-]?id[\"'\s]*[:=][\"'\s]*([0-9a-f]{32}|[0-9a-f]{16})\b"
)
_TRACE_ID_RE = re.compile(r"^(?:[0-9a-f]{32}|[0-9a-f]{16})$")
_HEX_ID_RE = re.compile(r"^[0-9a-f]{1,32}$")


def normalize_trace_id(value: str) -> str | None:
    """trace_id를 비교·표시용 표준 형태(소문자 16진수 32자리)로 맞춥니다.

    Tempo 검색 응답은 앞자리 0을 뺀 16진수(예: 31자리)를 돌려주고, 로그에는 32자리(또는 64비트
    trace의 16자리)로 남아 문자열 그대로는 같은 트레이스를 연결하지 못합니다. 앞을 0으로 채워
    128비트 표현으로 통일합니다. 16진수가 아니거나 전부 0이면 None입니다.
    """
    text = value.strip().lower()
    if not _HEX_ID_RE.match(text) or set(text) == {"0"}:
        return None
    return text.zfill(32)


def clean_text(text: str, limit: int = MAX_LINE_CHARS) -> str:
    """외부 텍스트를 표시·전달 가능한 형태로 정리합니다.

    제어문자 제거, 공백 정리, 비밀값 마스킹, 길이 제한 순서로 적용합니다.
    """
    flat = _SPACE_RE.sub(" ", _CONTROL_RE.sub(" ", text)).strip()
    flat = redact(flat)
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def trace_id_of(labels: Mapping[str, str], line: str) -> str | None:
    """구조화 메타데이터·라벨에서 먼저, 없으면 본문에서 trace_id(16진수 16·32자리)를 찾습니다.

    찾은 값은 `normalize_trace_id`로 32자리 표준 형태로 돌려줍니다.
    """
    for key in TRACE_ID_LABELS:
        value = labels.get(key, "").strip().lower()
        if _TRACE_ID_RE.match(value):
            normalized = normalize_trace_id(value)
            if normalized:
                return normalized
    match = _TRACE_IN_LINE_RE.search(line)
    return normalize_trace_id(match.group(1)) if match else None


def _seconds(value: float) -> str:
    return f"{max(1, int(value))}s"


class LogQueryTool:
    def __init__(
        self,
        catalog: Catalog,
        loki: LokiClient,
        *,
        agent: AgentName,
        budget: ToolBudget,
        timeout_seconds: float,
    ) -> None:
        self._catalog = catalog
        self._loki = loki
        self._agent = agent
        self._budget = budget
        self._timeout = timeout_seconds

    def item(self, key: str) -> CatalogItem:
        item = self._catalog.items.get(key)
        if item is None:
            raise ToolPermissionError(f"카탈로그에 없는 항목입니다: {key}")
        if item.agent is not self._agent:
            raise ToolPermissionError(
                f"{self._agent.value} 에이전트는 "
                f"{item.agent.value} 항목({key})을 실행할 수 없습니다"
            )
        if item.source is not DataSourceKind.LOKI:
            raise ToolPermissionError(f"Loki 항목이 아닙니다: {key}")
        return item

    def selector(
        self, item: CatalogItem, targets: Mapping[TargetKind, str]
    ) -> tuple[str, list[TargetKind]]:
        selector, unsupported = item.selector_for(targets)
        if not selector:
            # Loki는 빈 스트림 매처를 허용하지 않으므로 서비스 라벨의 "값 있음" 매처를 씁니다.
            label = item.target_labels.get(TargetKind.SERVICE) or next(
                iter(item.target_labels.values()), None
            )
            if label is None:
                raise ToolPermissionError("Loki 항목에는 target_labels가 필요합니다")
            selector = f'{label}=~".+"'
        return selector, unsupported

    def _result(
        self,
        evidence_id: str,
        expr: str,
        window: TimeRange,
        status: ToolStatus,
        *,
        data: Any = None,
        error: str | None = None,
        unit: str | None = None,
    ) -> ToolResult:
        return ToolResult(
            evidence_id=evidence_id,
            source=DataSourceKind.LOKI,
            query=expr,
            time_range=window,
            status=status,
            data=data,
            fetched_at=utc_now(),
            error=error,
            unit=unit,
        )

    async def _call(self, coro: Any, evidence_id: str, expr: str, window: TimeRange) -> Any:
        """예산·제한 시간·오류 처리. 실패하면 ToolResult(오류)를 반환합니다."""
        if not self._budget.take():
            coro.close()
            return self._result(
                evidence_id,
                expr,
                window,
                ToolStatus.ERROR,
                error="요청당 조회 호출 상한(analysis.max_tool_calls)에 도달해 조회하지 않음",
            )
        try:
            return await asyncio.wait_for(coro, timeout=self._timeout)
        except TimeoutError:
            return self._result(
                evidence_id,
                expr,
                window,
                ToolStatus.TIMEOUT,
                error=f"조회 제한 시간({self._timeout:.0f}초) 초과",
            )
        except DataSourceError as exc:
            status = ToolStatus.TIMEOUT if exc.code == "timeout" else ToolStatus.ERROR
            return self._result(
                evidence_id, expr, window, status, error=f"{exc.code}: {exc.message}"
            )

    def _skipped(
        self, evidence_id: str, window: TimeRange, unsupported: list[TargetKind]
    ) -> QueryOutcome:
        return QueryOutcome(
            result=self._result(
                evidence_id,
                "",
                window,
                ToolStatus.ERROR,
                error="대상 필터를 적용할 수 없어 조회하지 않음: "
                + ", ".join(k.value for k in unsupported),
            ),
            unsupported_targets=unsupported,
            skipped=True,
        )

    async def count(
        self,
        key: str,
        window: TimeRange,
        targets: Mapping[TargetKind, str],
        evidence_id: str,
    ) -> QueryOutcome:
        """구간 전체의 로그 줄 수(지표형 LogQL)를 대상별로 조회합니다."""
        item = self.item(key)
        selector, unsupported = self.selector(item, targets)
        if unsupported:
            return self._skipped(evidence_id, window, unsupported)
        expr = item.render(selector, range=_seconds(window.duration.total_seconds()))
        outcome = await self._call(self._loki.query(expr, window.end), evidence_id, expr, window)
        if isinstance(outcome, ToolResult):
            return QueryOutcome(result=outcome)
        rows = [{"labels": s.labels, "value": s.value} for s in outcome]
        status = ToolStatus.OK if rows else ToolStatus.EMPTY
        return QueryOutcome(
            result=self._result(evidence_id, expr, window, status, data=rows, unit=item.unit)
        )

    async def samples(
        self,
        key: str,
        window: TimeRange,
        targets: Mapping[TargetKind, str],
        evidence_id: str,
        limit: int,
    ) -> QueryOutcome:
        """로그 샘플(최신순)을 조회해 정리된 본문·trace_id와 함께 돌려줍니다."""
        item = self.item(key)
        if item.uses_range:
            raise ToolPermissionError(f"샘플 조회 항목에는 {{range}}를 쓰지 않습니다: {key}")
        selector, unsupported = self.selector(item, targets)
        if unsupported:
            return self._skipped(evidence_id, window, unsupported)
        expr = item.render(selector)
        outcome = await self._call(
            self._loki.query_logs(expr, window.start, window.end, limit), evidence_id, expr, window
        )
        if isinstance(outcome, ToolResult):
            return QueryOutcome(result=outcome)
        rows = [
            {
                "time": entry.time.isoformat(),
                "labels": {
                    k: clean_text(v, 80) for k, v in entry.labels.items() if k in SAMPLE_LABELS
                },
                "line": clean_text(entry.line),
                "trace_id": trace_id_of(entry.labels, entry.line),
            }
            for entry in outcome
        ]
        status = ToolStatus.OK if rows else ToolStatus.EMPTY
        return QueryOutcome(
            result=self._result(evidence_id, expr, window, status, data=rows, unit=item.unit)
        )
