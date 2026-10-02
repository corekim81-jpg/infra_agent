"""에이전트 공통 입출력 형식 (architecture.md 5절).

- 모든 시각은 UTC timezone-aware datetime입니다.
- Finding은 반드시 실제 조회 결과(ToolResult)의 evidence_id를 근거로 가져야 합니다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from infra_agent.timeutil import ensure_utc


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------- 공통 값


class Intent(StrEnum):
    STATUS = "status"
    COMPARE = "compare"
    ANOMALY = "anomaly"
    IMPACT = "impact"
    ROOT_CAUSE = "root_cause"


class AgentName(StrEnum):
    COORDINATOR = "coordinator"
    SERVER = "server"
    NETWORK = "network"
    DB = "db"
    SERVICE = "service"
    KUBERNETES = "kubernetes"


class DataSourceKind(StrEnum):
    PROMETHEUS = "prometheus"
    LOKI = "loki"
    TEMPO = "tempo"
    KUBERNETES = "kubernetes"
    HUBBLE = "hubble"


class TargetKind(StrEnum):
    HOST = "host"
    NODE = "node"
    NAMESPACE = "namespace"
    WORKLOAD = "workload"
    POD = "pod"
    CONTAINER = "container"
    SERVICE = "service"
    DATABASE = "database"


class TimeRange(_Model):
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _order(self) -> Self:
        if self.start >= self.end:
            raise ValueError("시간 범위의 start는 end보다 이전이어야 합니다")
        return self

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    @classmethod
    def last(cls, duration: timedelta, now: datetime) -> TimeRange:
        """`now`에서 끝나는 최근 `duration` 구간."""
        return cls(start=now - duration, end=now)

    def previous(self) -> TimeRange:
        """같은 길이의 직전 구간 (기준 구간 비교용)."""
        return TimeRange(start=self.start - self.duration, end=self.start)


class TargetRef(_Model):
    kind: TargetKind
    name: str = Field(min_length=1)
    labels: dict[str, str] = Field(default_factory=dict)
    """실제 조회에 사용한 라벨 매칭 기준."""


class Budget(_Model):
    max_llm_calls: int = Field(ge=0)
    max_tool_calls: int = Field(ge=0)
    deadline: datetime

    @field_validator("deadline")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


# ---------------------------------------------------------------- 요청·작업


class AnalysisContext(_Model):
    """요청 단위 컨텍스트. Coordinator가 만들고 모든 에이전트가 읽기 전용으로 공유합니다."""

    request_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    intent: Intent
    time_range: TimeRange
    step_seconds: int = Field(default=60, ge=1)
    baseline_range: TimeRange | None = None
    targets: tuple[TargetRef, ...] = ()
    budget: Budget

    @model_validator(mode="after")
    def _baseline(self) -> Self:
        if self.intent is Intent.COMPARE and self.baseline_range is None:
            raise ValueError("compare 의도에는 baseline_range가 필요합니다")
        return self


class AgentTask(_Model):
    task_id: str = Field(min_length=1)
    agent: AgentName
    objective: str = Field(min_length=1)
    depends_on: tuple[str, ...] = ()
    inputs: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _no_self_dependency(self) -> Self:
        if self.task_id in self.depends_on:
            raise ValueError("작업이 자기 자신에 의존할 수 없습니다")
        return self


# ---------------------------------------------------------------- 조회 결과·분석 결과


class ToolStatus(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    ERROR = "error"
    TIMEOUT = "timeout"
    TRUNCATED = "truncated"


class ToolResult(_Model):
    """모든 조회 결과. 근거(evidence)의 원천입니다."""

    evidence_id: str = Field(min_length=1)
    source: DataSourceKind
    query: str
    """실행한 조회식 (비밀값 제외)."""
    time_range: TimeRange | None = None
    status: ToolStatus
    data: Any = None
    freshness_seconds: float | None = Field(default=None, ge=0)
    fetched_at: datetime
    error: str | None = None
    synthetic: bool = False
    """테스트용 가상 데이터 여부."""
    unit: str | None = None
    """결과 값의 단위 (카탈로그 항목 기준: ratio, bytes, cores 등). 표시값 생성에 사용."""

    @field_validator("fetched_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class FindingKind(StrEnum):
    FACT = "fact"
    HYPOTHESIS = "hypothesis"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class JudgementBasis(StrEnum):
    THRESHOLD = "threshold"
    BASELINE = "baseline"
    STATE = "state"
    CORRELATION = "correlation"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Finding(_Model):
    kind: FindingKind
    statement: str = Field(min_length=1)
    severity: Severity = Severity.INFO
    targets: tuple[TargetRef, ...] = ()
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    basis: JudgementBasis
    confidence: Confidence | None = None
    observed_at: datetime | None = None
    """시점이 있는 사실(예: 오류율·지연 최고 시점)의 관측 시각. 분야 간 확인의 시간 기준."""

    @field_validator("observed_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @model_validator(mode="after")
    def _rules(self) -> Self:
        if self.kind is FindingKind.FACT and self.basis is JudgementBasis.CORRELATION:
            raise ValueError("시간상 상관관계(correlation)만으로는 사실(fact)로 판정할 수 없습니다")
        if self.kind is FindingKind.HYPOTHESIS and self.confidence is None:
            raise ValueError("원인 후보(hypothesis)에는 confidence가 필요합니다")
        if self.kind is FindingKind.FACT and self.confidence is not None:
            raise ValueError("사실(fact)에는 confidence를 지정하지 않습니다")
        return self


class AgentStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


class ErrorInfo(_Model):
    code: str
    message: str
    """비밀값을 제거한 메시지."""


class Usage(_Model):
    llm_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    elapsed_ms: int = Field(default=0, ge=0)


class RejectedHypothesis(_Model):
    """검증에 실패해 답변에서 제외한 모델 원인 후보 (진단용, 사실·추정으로 쓰지 않음)."""

    statement: str
    evidence_ids: tuple[str, ...] = ()
    reasons: tuple[str, ...] = Field(min_length=1)


class AgentResult(_Model):
    task_id: str
    agent: AgentName
    status: AgentStatus
    findings: tuple[Finding, ...] = ()
    evidence: tuple[ToolResult, ...] = ()
    limitations: tuple[str, ...] = ()
    scope_notes: tuple[str, ...] = ()
    """분석 범위·해석 기준 안내 (조회 결과와 관계없이 같은 문구). 이번 조회에서 확인하지 못한
    내용은 `limitations`에 둡니다."""
    next_checks: tuple[str, ...] = ()
    errors: tuple[ErrorInfo, ...] = ()
    usage: Usage = Usage()
    rejected_hypotheses: tuple[RejectedHypothesis, ...] = ()
    """검증 실패로 제외한 모델 원인 후보. 답변 본문에는 넣지 않고 진단 출력에만 사용."""

    @model_validator(mode="after")
    def _evidence_refs(self) -> Self:
        known = {e.evidence_id for e in self.evidence}
        if len(known) != len(self.evidence):
            raise ValueError("evidence_id가 중복되었습니다")
        missing = {ref for f in self.findings for ref in f.evidence_ids if ref not in known}
        if missing:
            raise ValueError(f"근거가 없는 evidence_id를 참조합니다: {sorted(missing)}")
        if self.status is AgentStatus.FAILED and self.findings:
            raise ValueError("failed 상태 결과에는 findings를 포함할 수 없습니다")
        if self.status in (AgentStatus.FAILED, AgentStatus.PARTIAL) and not (
            self.errors or self.limitations
        ):
            raise ValueError("failed/partial 상태에는 errors 또는 limitations가 필요합니다")
        return self


class EvidenceSummary(_Model):
    source: DataSourceKind
    query: str
    time_range: TimeRange | None = None
    key_values: dict[str, Any] = Field(default_factory=dict)


class FinalAnswer(_Model):
    request_id: str
    summary: str
    time_range: TimeRange
    facts: tuple[Finding, ...] = ()
    hypotheses: tuple[Finding, ...] = ()
    evidence: tuple[EvidenceSummary, ...] = ()
    limitations: tuple[str, ...] = ()
    scope_notes: tuple[str, ...] = ()
    """분석 범위·해석 기준 안내 (조회 결과와 관계없이 같은 문구). 이번 조회에서 확인하지 못한
    내용은 `limitations`에 둡니다."""
    unverified_areas: tuple[str, ...] = ()
    next_checks: tuple[str, ...] = ()
    cross_checks: tuple[str, ...] = ()
    """분야 간 교차 확인 (Coordinator 코드 판정).

    연결된 이상, 연결되지 않은 이상, 확인하지 못한 분야를 줄 단위로 적습니다."""
    correlations: tuple[Finding, ...] = ()
    """분야 간 동시 발생으로 연결한 원인 후보 (hypothesis, basis=correlation)."""

    @model_validator(mode="after")
    def _kinds(self) -> Self:
        if any(f.kind is not FindingKind.FACT for f in self.facts):
            raise ValueError("facts에는 fact만 포함할 수 있습니다")
        if any(f.kind is not FindingKind.HYPOTHESIS for f in self.hypotheses):
            raise ValueError("hypotheses에는 hypothesis만 포함할 수 있습니다")
        if any(
            f.kind is not FindingKind.HYPOTHESIS or f.basis is not JudgementBasis.CORRELATION
            for f in self.correlations
        ):
            raise ValueError("correlations에는 basis=correlation인 hypothesis만 포함할 수 있습니다")
        return self
