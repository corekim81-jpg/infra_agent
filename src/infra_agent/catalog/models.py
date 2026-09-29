"""조회 카탈로그 (environment.md 4절).

에이전트 코드에 조회식과 지표 이름을 고정하지 않고, 환경별 카탈로그 파일에서 가져옵니다.
카탈로그에는 지표·라벨 **이름**만 두고 실제 라벨 값이나 조회 결과는 넣지 않습니다.

쿼리 템플릿은 Python `str.format` 문법을 사용합니다.
- `{selector}`: 실행 시 대상 매처(예: `k8s_namespace_name="otel-demo"`)
- `{range}`: 실행 시 범위 벡터 구간(예: `5m`)
- 그 밖의 `{이름}`: `labels`에 정의한 라벨 이름
- PromQL 중괄호는 `{{`, `}}`로 씁니다. 예: `k8s_node_cpu_usage{{{selector}}}`
"""

from __future__ import annotations

import re
import string
from collections.abc import Mapping
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from infra_agent.datasources.prometheus import (
    build_selector,
    is_valid_label_name,
    is_valid_metric_name,
)
from infra_agent.schemas import AgentName, DataSourceKind, TargetKind
from infra_agent.timeutil import parse_duration

RUNTIME_FIELDS = frozenset({"selector", "range"})
_ITEM_KEY_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")


class CatalogError(Exception):
    """카탈로그 파일 형식 또는 사용 오류."""


class EvidenceStatus(StrEnum):
    USER_CONFIRMED = "user_confirmed"
    """사용자가 지표 이름만 확인함."""
    DISCOVERED = "discovered"
    """탐색 명령으로 존재·라벨을 확인함."""
    VERIFIED = "verified"
    """의미·단위까지 검토해 분석에 사용해도 된다고 확인함."""


_STATUS_ORDER = {
    EvidenceStatus.USER_CONFIRMED: 0,
    EvidenceStatus.DISCOVERED: 1,
    EvidenceStatus.VERIFIED: 2,
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ItemEvidence(_Model):
    status: EvidenceStatus
    checked_at: date | None = None


def _template_fields(template: str) -> set[str]:
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise ValueError(f"쿼리 템플릿 형식 오류: {exc}") from exc
    fields = set()
    for _, name, spec, conv in parsed:
        if name is None:
            continue
        if not name.isidentifier() or spec or conv:
            raise ValueError(f"쿼리 템플릿 자리표시자 형식이 올바르지 않습니다: {{{name}}}")
        fields.add(name)
    return fields


class CatalogItem(_Model):
    agent: AgentName
    source: DataSourceKind
    description: str = Field(min_length=1)
    unit: str | None = None
    query: str = Field(min_length=1)
    requires_metrics: tuple[str, ...] = ()
    labels: dict[str, str] = Field(default_factory=dict)
    target_labels: dict[TargetKind, str] = Field(default_factory=dict)
    """대상 종류 → 이 항목의 지표에서 해당 대상을 가리키는 라벨 이름."""
    evidence: ItemEvidence
    caveats: tuple[str, ...] = ()

    @field_validator("agent")
    @classmethod
    def _not_coordinator(cls, value: AgentName) -> AgentName:
        if value is AgentName.COORDINATOR:
            raise ValueError("카탈로그 항목은 전문 에이전트에만 할당할 수 있습니다")
        return value

    @field_validator("requires_metrics")
    @classmethod
    def _metric_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        bad = [m for m in value if not is_valid_metric_name(m)]
        if bad:
            raise ValueError(f"지표 이름 형식이 올바르지 않습니다: {bad}")
        return value

    @model_validator(mode="after")
    def _check_template(self) -> Self:
        for key, label in self.labels.items():
            if key in RUNTIME_FIELDS:
                raise ValueError(f"labels 키에 예약어를 사용할 수 없습니다: {key}")
            if not is_valid_label_name(label):
                raise ValueError(f"라벨 이름 형식이 올바르지 않습니다: {label!r}")
        bad_targets = [v for v in self.target_labels.values() if not is_valid_label_name(v)]
        if bad_targets:
            raise ValueError(f"target_labels의 라벨 이름 형식이 올바르지 않습니다: {bad_targets}")
        fields = _template_fields(self.query)
        unknown = fields - RUNTIME_FIELDS - set(self.labels)
        if unknown:
            raise ValueError(f"정의되지 않은 자리표시자: {sorted(unknown)}")
        if self.source is DataSourceKind.PROMETHEUS and not self.requires_metrics:
            raise ValueError("Prometheus 항목에는 requires_metrics가 필요합니다")
        return self

    @property
    def uses_range(self) -> bool:
        return "range" in _template_fields(self.query)

    def render(self, selector: str = "", range: str | None = None) -> str:
        """실행할 조회식을 만듭니다. selector는 중괄호 없는 매처 목록입니다."""
        if "{" in selector or "}" in selector:
            raise CatalogError("selector에는 중괄호를 넣지 않습니다")
        values: dict[str, str] = dict(self.labels)
        values["selector"] = selector
        if self.uses_range:
            if range is None:
                raise CatalogError("이 항목에는 range 값이 필요합니다")
            parse_duration(range)
            values["range"] = range.strip()
        return self.query.format(**values)

    def selector_for(self, targets: Mapping[TargetKind, str]) -> tuple[str, list[TargetKind]]:
        """대상 값으로 selector를 만듭니다.

        반환: (selector, 이 항목이 지원하지 않아 필터링하지 못한 대상 종류 목록).
        지원하지 않는 대상은 조용히 무시하지 않고 호출자에게 알려 한계로 표시하게 합니다.
        """
        matchers: dict[str, str] = {}
        unsupported: list[TargetKind] = []
        for kind, value in targets.items():
            label = self.target_labels.get(kind)
            if label is None:
                unsupported.append(kind)
            else:
                matchers[label] = value
        return build_selector(matchers), unsupported

    def missing_metrics(self, available: set[str]) -> list[str]:
        return [m for m in self.requires_metrics if m not in available]


class Catalog(_Model):
    version: int = Field(ge=1, le=1)
    environment: str = Field(min_length=1)
    items: dict[str, CatalogItem]

    @field_validator("items")
    @classmethod
    def _keys(cls, value: dict[str, CatalogItem]) -> dict[str, CatalogItem]:
        bad = [k for k in value if not _ITEM_KEY_RE.match(k)]
        if bad:
            raise ValueError(f"항목 키는 'domain.name' 형식이어야 합니다: {bad}")
        return value

    def for_agent(self, agent: AgentName) -> dict[str, CatalogItem]:
        return {k: v for k, v in self.items.items() if v.agent is agent}

    def usable(
        self,
        available_metrics: set[str],
        min_status: EvidenceStatus = EvidenceStatus.DISCOVERED,
    ) -> dict[str, CatalogItem]:
        """필요 지표가 모두 있고 검증 상태가 min_status 이상인 항목."""
        threshold = _STATUS_ORDER[min_status]
        return {
            key: item
            for key, item in self.items.items()
            if _STATUS_ORDER[item.evidence.status] >= threshold
            and not item.missing_metrics(available_metrics)
        }


def load_catalog(path: str | Path) -> Catalog:
    file = Path(path)
    if not file.is_file():
        raise CatalogError(f"카탈로그 파일을 찾을 수 없습니다: {file}")
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CatalogError(f"카탈로그 YAML 구문 오류: {file}: {exc}") from exc
    try:
        return Catalog.model_validate(data)
    except ValidationError as exc:
        lines = [
            f"  - {'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}"
            for e in exc.errors()
        ]
        raise CatalogError(f"카탈로그 형식 오류: {file}\n" + "\n".join(lines)) from exc
