"""설정 모델.

비밀값(토큰, 비밀번호, kubeconfig 내용)은 이 모델에 저장하지 않습니다.
비밀값이 필요한 항목은 값을 담은 **환경 변수 이름**만 설정합니다.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Self
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from infra_agent.timeutil import parse_duration

_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _validate_env_name(value: str | None) -> str | None:
    if value is not None and not _ENV_NAME_RE.match(value):
        raise ValueError(
            f"환경 변수 이름 형식이 아닙니다: {value!r}. "
            "비밀값 자체가 아니라 변수 이름을 지정하세요."
        )
    return value


class Profile(StrEnum):
    CI = "ci"
    DEV_TUNNEL = "dev-tunnel"
    IN_CLUSTER = "in-cluster"


class HttpDatasourceConfig(_Strict):
    """HTTP API 기반 데이터 소스(Prometheus, Loki, Tempo) 연결 설정."""

    enabled: bool = False
    url: str | None = None
    timeout_seconds: float = Field(default=15.0, gt=0, le=300)
    token_env: str | None = None
    """인증 토큰을 담은 환경 변수 이름 (토큰 값이 아님)."""

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"http(s) URL이어야 합니다: {value!r}")
        if parsed.username or parsed.password:
            raise ValueError("URL에 인증정보를 넣지 마세요. token_env를 사용하세요.")
        return value.rstrip("/")

    @field_validator("token_env")
    @classmethod
    def _check_token_env(cls, value: str | None) -> str | None:
        return _validate_env_name(value)

    @model_validator(mode="after")
    def _require_url_when_enabled(self) -> Self:
        if self.enabled and self.url is None:
            raise ValueError("enabled=true인 데이터 소스에는 url이 필요합니다")
        return self


class KubernetesConfig(_Strict):
    enabled: bool = False
    kubeconfig_env: str = "INFRA_AGENT_KUBECONFIG"
    """전용 읽기 계정 kubeconfig 파일 **경로**를 담은 환경 변수 이름.

    ServiceAccount 토큰(`token`, `tokenFile`) 계정만 허용합니다. 클라이언트 인증서·exec 플러그인
    등 관리자 kubeconfig 형식은 거부합니다."""
    context: str | None = None
    """사용할 kubeconfig 컨텍스트. None이면 current-context."""
    timeout_seconds: float = Field(default=15.0, gt=0, le=300)
    max_items: int = Field(default=2000, ge=1, le=20000)
    """목록 조회 1회에서 가져올 최대 객체 수 (넘으면 일부만 보았다고 표시)."""
    allow_insecure_tls: bool = False
    """kubeconfig의 insecure-skip-tls-verify를 허용할지 (기본 거부, 개발 환경 전용)."""

    @field_validator("kubeconfig_env")
    @classmethod
    def _check_env(cls, value: str) -> str:
        _validate_env_name(value)
        return value


class HubbleConfig(_Strict):
    enabled: bool = False


class DatasourcesConfig(_Strict):
    prometheus: HttpDatasourceConfig = HttpDatasourceConfig()
    loki: HttpDatasourceConfig = HttpDatasourceConfig(timeout_seconds=20.0)
    tempo: HttpDatasourceConfig = HttpDatasourceConfig(timeout_seconds=20.0)
    kubernetes: KubernetesConfig = KubernetesConfig()
    hubble: HubbleConfig = HubbleConfig()


class CatalogConfig(_Strict):
    path: str | None = None


class LLMProvider(StrEnum):
    FAKE = "fake"
    CLAUDE_AGENT_SDK = "claude_agent_sdk"


class DataPolicy(StrEnum):
    """모델에 전달할 수 있는 조회 데이터 범위 (architecture.md 10.2절)."""

    NONE = "none"
    AGGREGATED = "aggregated"
    FULL = "full"


class LLMConfig(_Strict):
    provider: LLMProvider = LLMProvider.FAKE
    model: str | None = None
    data_policy: DataPolicy = DataPolicy.NONE
    max_calls_per_request: int = Field(default=8, ge=0, le=100)
    max_turns_per_agent: int = Field(default=6, ge=1, le=50)
    max_budget_usd_per_request: float | None = Field(default=None, gt=0)


class ExecutionConfig(_Strict):
    max_concurrency: int = Field(default=4, ge=1, le=32)
    request_timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    agent_timeout_seconds: float = Field(default=60.0, gt=0, le=3600)
    tool_timeout_seconds: float = Field(default=20.0, gt=0, le=600)
    max_retries: int = Field(default=2, ge=0, le=5)
    default_time_range: str = "30m"

    @field_validator("default_time_range")
    @classmethod
    def _check_range(cls, value: str) -> str:
        parse_duration(value)
        return value.strip()

    @model_validator(mode="after")
    def _check_timeouts(self) -> Self:
        if not (
            self.tool_timeout_seconds <= self.agent_timeout_seconds <= self.request_timeout_seconds
        ):
            raise ValueError(
                "제한 시간은 tool_timeout_seconds <= agent_timeout_seconds "
                "<= request_timeout_seconds 이어야 합니다"
            )
        return self


class AnalysisConfig(_Strict):
    """결정적 판정 기준. 값은 개발 환경용 제안 기본값이며 설정으로 바꿀 수 있습니다."""

    utilization_warning: float = Field(default=0.8, gt=0, le=10)
    """사용률(사용량/할당 가능량 또는 limit) 경고 기준."""
    utilization_critical: float = Field(default=0.9, gt=0, le=10)
    throttling_warning: float = Field(default=0.25, gt=0, le=1)
    """CPU 스로틀링 비율(throttled periods / periods) 경고 기준."""
    increase_ratio: float = Field(default=0.5, gt=0, le=100)
    """직전 구간 평균 대비 증가 비율 기준 (0.5 = 50% 증가)."""
    min_cpu_increase_cores: float = Field(default=0.05, ge=0)
    """증가로 판단할 최소 CPU 증가량(cores). 작은 값의 비율 변동을 무시하기 위함."""
    min_memory_increase_bytes: int = Field(default=100 * 1024 * 1024, ge=0)
    stale_after_seconds: int = Field(default=300, ge=10, le=86400)
    """최신 샘플이 이 시간보다 오래되면 데이터 지연으로 표시."""
    top_n: int = Field(default=5, ge=1, le=50)
    """답변에 표시할 대상 수 상한."""
    max_tool_calls: int = Field(default=100, ge=1, le=1000)
    """요청당 도구(조회) 호출 상한."""
    # --- Service Agent (#23)
    error_ratio_warning: float = Field(default=0.05, gt=0, le=1)
    """서비스·호출 경로 오류율 경고 기준 (오류 span / 전체 span)."""
    error_ratio_critical: float = Field(default=0.2, gt=0, le=1)
    latency_p95_warning_seconds: float = Field(default=1.0, gt=0)
    """p95 지연 경고 기준(초)."""
    min_request_rate: float = Field(default=0.01, ge=0)
    """오류율을 판정할 최소 요청률(건/초). 요청이 너무 적은 서비스의 비율 변동을 무시하기 위함."""
    min_error_ratio_increase: float = Field(default=0.02, ge=0, le=1)
    """직전 구간 대비 오류율 증가로 판단할 최소 증가폭 (0.02 = 2%p)."""
    min_latency_increase_seconds: float = Field(default=0.1, ge=0)
    detail_services: int = Field(default=3, ge=0, le=10)
    """로그·트레이스를 자세히 볼 오류 서비스 수 상한."""
    peak_window_seconds: int = Field(default=600, ge=60, le=86400)
    """오류율 최고 시점 전후로 로그·트레이스를 볼 구간 길이."""
    log_sample_limit: int = Field(default=3, ge=0, le=20)
    trace_sample_limit: int = Field(default=3, ge=0, le=20)
    streaming_services: tuple[str, ...] = ()
    """서비스 간 호출 지연을 판정하지 않을 스트리밍 서비스(호출받는 쪽, 대소문자 무시).

    오래 열린 스트림(예: 기능 플래그 이벤트 스트림)은 p95가 지연이 아니라 연결 유지 시간이므로
    운영자가 지정합니다. 값은 판정 기준 미적용 정보로 표시하고, 호출 실패율은 계속 판정합니다."""
    # --- DB Agent (#25). 연결·풀 사용률은 utilization_*, DB 지연은 latency_p95_warning_seconds 사용
    rollback_ratio_warning: float = Field(default=0.05, gt=0, le=1)
    """PostgreSQL 롤백 비율(rollbacks / (commits + rollbacks)) 경고 기준."""
    cache_hit_ratio_warning: float = Field(default=0.9, gt=0, le=1)
    """PostgreSQL 버퍼 캐시 적중률이 이 값보다 낮으면 경고."""
    # --- Network Agent (#27). 흐름이 min_request_rate보다 적은 네임스페이스 쌍은 판정하지 않음
    flow_drop_ratio_warning: float = Field(default=0.05, gt=0, le=1)
    """Hubble 흐름 중 DROPPED·ERROR 판정 비율 경고 기준."""
    benign_drop_reasons: tuple[str, ...] = ("UNSUPPORTED_L3_PROTOCOL",)
    """경고 대신 정보로 표시할 Hubble 드롭 사유(대소문자 무시). 기본값은 IPv4·IPv6가 아닌
    L3 패킷(ARP 등)을 Cilium이 처리하지 않아 생기는 드롭으로, 일반적으로 장애가 아닙니다."""

    @model_validator(mode="after")
    def _order(self) -> Self:
        if self.utilization_warning >= self.utilization_critical:
            raise ValueError("utilization_warning은 utilization_critical보다 작아야 합니다")
        if self.error_ratio_warning >= self.error_ratio_critical:
            raise ValueError("error_ratio_warning은 error_ratio_critical보다 작아야 합니다")
        return self


class Settings(_Strict):
    profile: Profile = Profile.CI
    datasources: DatasourcesConfig = DatasourcesConfig()
    catalog: CatalogConfig = CatalogConfig()
    llm: LLMConfig = LLMConfig()
    execution: ExecutionConfig = ExecutionConfig()
    analysis: AnalysisConfig = AnalysisConfig()

    @model_validator(mode="after")
    def _ci_profile_guard(self) -> Self:
        if self.profile is Profile.CI and self.llm.provider is not LLMProvider.FAKE:
            raise ValueError("ci 프로필에서는 llm.provider=fake만 사용할 수 있습니다")
        return self
