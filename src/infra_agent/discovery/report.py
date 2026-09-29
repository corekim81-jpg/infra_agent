"""탐색 보고서 모델과 출력.

보고서는 **실제 개발 환경 데이터**를 담으므로 `var/discovery/`(Git 제외)에 저장합니다.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from infra_agent.datasources.probe import SourceStatus


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MetricDetail(_Model):
    name: str
    confirmed: bool = False
    type: str | None = None
    unit: str | None = None
    help: str | None = None
    label_names: list[str] = Field(default_factory=list)
    series_count: int | None = None
    latest_age_seconds: float | None = None
    sample_labels: list[dict[str, str]] = Field(default_factory=list)
    error: str | None = None


class PrometheusDiscovery(_Model):
    status: SourceStatus
    storage_retention: str | None = None
    total_metrics: int = 0
    families: dict[str, int] = Field(default_factory=dict)
    confirmed_present: dict[str, bool] = Field(default_factory=dict)
    detailed_count: int = 0
    omitted_count: int = 0
    histogram_companions_skipped: int = 0
    reference_metric: str | None = None
    availability: dict[str, bool | None] = Field(default_factory=dict)
    metrics: list[MetricDetail] = Field(default_factory=list)


class LabelValues(_Model):
    count: int
    samples: list[str] = Field(default_factory=list)


class LokiDiscovery(_Model):
    status: SourceStatus
    label_names: list[str] = Field(default_factory=list)
    label_values: dict[str, LabelValues] = Field(default_factory=dict)
    error: str | None = None


class TempoDiscovery(_Model):
    status: SourceStatus
    tag_names: dict[str, list[str]] = Field(default_factory=dict)
    error: str | None = None


class DiscoveryReport(_Model):
    generated_at: datetime
    tool_version: str
    profile: str
    synthetic: bool = False
    lookback_seconds: int
    mask_ips: bool
    include_values: bool
    prometheus: PrometheusDiscovery | None = None
    loki: LokiDiscovery | None = None
    tempo: TempoDiscovery | None = None
    warnings: list[str] = Field(default_factory=list)


def _age(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 120:
        return f"{seconds:.0f}s"
    if seconds < 7200:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def _dash(value: object | None) -> str:
    return "-" if value is None else str(value)


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(report: DiscoveryReport) -> str:
    lines = [
        "# 지표 탐색 보고서",
        "",
        "> 실제 개발 환경에서 수집한 결과입니다. **저장소에 커밋하지 마세요.**",
        "",
        f"- 생성 시각(UTC): {report.generated_at.isoformat()}",
        f"- 도구 버전: {report.tool_version}, 프로필: {report.profile}",
        f"- 조회 구간: 최근 {report.lookback_seconds // 60}분",
        f"- IP 마스킹: {'예' if report.mask_ips else '아니오'}, "
        f"라벨 값 표본: {'포함' if report.include_values else '제외'}",
        "",
    ]
    if report.warnings:
        lines += ["## 경고", ""] + [f"- {_cell(w)}" for w in report.warnings] + [""]

    lines += [
        "## 데이터 소스 상태",
        "",
        "| 소스 | 상태 | 버전 | 지연(ms) | 오류 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for section in (report.prometheus, report.loki, report.tempo):
        if section is None:
            continue
        st = section.status
        state = (
            "비활성"
            if not st.enabled
            else ("정상" if st.ready else ("응답(준비 안됨)" if st.reachable else "실패"))
        )
        lines.append(
            f"| {st.name} | {state} | {st.version or '-'} | {_dash(st.latency_ms)} "
            f"| {_cell(st.error or '')} {_cell(st.hint or '')} |"
        )
    lines.append("")

    prom = report.prometheus
    if prom is not None and prom.status.reachable:
        lines += [
            "## Prometheus",
            "",
            f"- 전체 지표 수: {prom.total_metrics}",
            f"- 상세 탐색: {prom.detailed_count}개 (생략 {prom.omitted_count}개, "
            f"히스토그램 _sum/_count 생략 {prom.histogram_companions_skipped}개)",
            f"- 저장 보존 설정(storageRetention): {prom.storage_retention or '확인 불가'}",
            f"- 과거 데이터 존재 (참조 지표 `{prom.reference_metric or '-'}`): "
            + (
                ", ".join(
                    f"{k} 전={'있음' if v else ('없음' if v is False else '확인 실패')}"
                    for k, v in prom.availability.items()
                )
                or "-"
            ),
            "",
            "### 사용자 확인 지표 존재 여부",
            "",
            "| 지표 | 존재 |",
            "| --- | --- |",
        ]
        lines += [
            f"| `{n}` | {'예' if ok else '**아니오**'} |"
            for n, ok in prom.confirmed_present.items()
        ]
        lines += [
            "",
            "### 지표 계열 (이름 첫 토큰 기준, 상위 40)",
            "",
            "| 계열 | 지표 수 |",
            "| --- | --- |",
        ]
        top = sorted(prom.families.items(), key=lambda kv: (-kv[1], kv[0]))[:40]
        lines += [f"| {k} | {v} |" for k, v in top]
        lines += [
            "",
            "### 상세 지표",
            "",
            "| 지표 | 확인 | 타입 | 단위 | 시계열 | 최신 샘플 경과 | 라벨 | 오류 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for m in prom.metrics:
            labels = ", ".join(x for x in m.label_names if x != "__name__")
            lines.append(
                f"| `{m.name}` | {'✓' if m.confirmed else ''} | {m.type or '-'} "
                f"| {_cell(m.unit or '-')} | {_dash(m.series_count)} "
                f"| {_age(m.latest_age_seconds)} "
                f"| {_cell(labels)} | {_cell(m.error or '')} |"
            )
        lines.append("")

    if report.loki is not None and report.loki.status.reachable:
        lines += ["## Loki 라벨", "", "| 라벨 | 값 개수 | 표본 |", "| --- | --- | --- |"]
        for name in report.loki.label_names:
            lv = report.loki.label_values.get(name)
            if lv is None:
                lines.append(f"| {name} | - | - |")
            else:
                lines.append(f"| {name} | {lv.count} | {_cell(', '.join(lv.samples))} |")
        if report.loki.error:
            lines.append(f"\n오류: {_cell(report.loki.error)}")
        lines.append("")

    if report.tempo is not None and report.tempo.status.reachable:
        lines += ["## Tempo 태그", ""]
        for scope, tags in report.tempo.tag_names.items():
            lines.append(f"- **{scope}** ({len(tags)}): {_cell(', '.join(tags))}")
        if report.tempo.error:
            lines.append(f"\n오류: {_cell(report.tempo.error)}")
        lines.append("")
    return "\n".join(lines)


def write_report(report: DiscoveryReport, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = report.generated_at.strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"discovery-{stamp}.json"
    md_path = out_dir / f"discovery-{stamp}.md"
    json_path.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path
