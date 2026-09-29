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
    """전용 읽기 계정 kubeconfig 파일 **경로**를 담은 환경 변수 이름."""
    context: str | None = None

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


class Settings(_Strict):
    profile: Profile = Profile.CI
    datasources: DatasourcesConfig = DatasourcesConfig()
    catalog: CatalogConfig = CatalogConfig()
    llm: LLMConfig = LLMConfig()
    execution: ExecutionConfig = ExecutionConfig()

    @model_validator(mode="after")
    def _ci_profile_guard(self) -> Self:
        if self.profile is Profile.CI and self.llm.provider is not LLMProvider.FAKE:
            raise ValueError("ci 프로필에서는 llm.provider=fake만 사용할 수 있습니다")
        return self
