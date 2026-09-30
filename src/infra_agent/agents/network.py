"""Network Agent — 패킷 드롭·흐름 판정·네트워크 오류 분석 (모델 없이 결정적 판정).

데이터
- 카탈로그에서 agent=network로 지정된 항목만 조회합니다(조회 도구가 강제).
  Cilium/Hubble 지표(`hubble_*`), kubeletstats(`k8s_node_network_*`, `k8s_pod_network_*`),
  cAdvisor(`container_network_*`)를 씁니다. Hubble Relay 직접 조회는 하지 않습니다.
- Hubble 지표의 `k8s_*` 라벨은 수집 주체(cilium) Pod이므로, 트래픽 대상은 `source_*`·`destination_*`
  라벨로 표시합니다.
- DNS 응답 코드(rcode)와 네트워크 지연(RTT)은 수집되지 않아 DNS 오류율·네트워크 지연은 판단하지
  않습니다.

판정 (기준값은 `analysis` 설정)
- 구간 내 발생 수(0.5 이상이면 발생): Hubble 패킷 드롭(사유별), 노드 인터페이스 오류,
  Pod 네트워크 오류, 컨테이너 패킷 드롭
- 흐름 판정 비율: 네임스페이스 쌍별 DROPPED·ERROR 판정 흐름 비율(`flow_drop_ratio_warning`,
  흐름이 `min_request_rate`보다 적은 쌍은 제외)
- 현재 값(판정 없음): TCP RST 패킷, DNS 질의량
- `benign_drop_reasons`에 있는 드롭 사유(기본 UNSUPPORTED_L3_PROTOCOL)는 발생 수를 그대로 보이되
  경고가 아닌 정보로 표시합니다.
- DNS 질의가 0이면 Hubble DNS 가시성 미적용 가능성을 한계에 적습니다(실제 질의 0으로 단정하지 않음).
- Hubble 이벤트 유실이 있으면 Hubble 기반 결과가 불완전하다고 한계에 적습니다.
- 결과가 없거나 오래된 데이터로는 "정상"이라고 판단하지 않습니다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from infra_agent.agents.base import context_targets
from infra_agent.agents.common import (
    Collector,
    fetch_evidence,
    finish,
    is_fresh,
    with_explanation,
)
from infra_agent.agents.explain import AgentExplainer
from infra_agent.agents.formatting import count_text, fmt_value, window_text
from infra_agent.config.settings import AnalysisConfig
from infra_agent.schemas import (
    AgentName,
    AgentResult,
    AgentTask,
    AnalysisContext,
    Finding,
    FindingKind,
    JudgementBasis,
    Severity,
    TargetKind,
    TargetRef,
    ToolResult,
    ToolStatus,
)
from infra_agent.tools import CatalogQueryTool, QueryMode, value_rows

SCOPE_NOTE = (
    "Network 분석은 Cilium/Hubble·kubeletstats·cAdvisor 지표 기준입니다. DNS 응답 코드와 "
    "네트워크 지연(RTT)은 수집되지 않아 DNS 오류율·네트워크 지연은 판단하지 않습니다."
)
COUNT_NOTE = "구간 내 발생 수(드롭·오류)는 Prometheus increase()의 추정값입니다."
DROP_REASON_NOTE = (
    "Hubble 드롭 사유 중에는 정책 거부처럼 의도된 차단도 있어, 드롭이 곧 장애를 뜻하지는 않습니다"
    "(사유와 출발·도착을 함께 확인)."
)
HUBBLE_FILTER_NOTE = (
    "Hubble 항목(드롭·흐름 판정·TCP RST·DNS)의 네임스페이스·워크로드 필터는 도착(destination) "
    "기준이라, 요청 대상에서 나가는 트래픽은 포함되지 않음"
)
HUBBLE_KEYS = frozenset(
    {
        "network.drops_increase",
        "network.flows_by_verdict",
        "network.tcp_flags_rate",
        "network.dns_query_rate",
    }
)
UNKNOWN_PEER = "외부·미확인"
DNS_ZERO_NOTE = (
    "network.dns_query_rate: DNS 질의가 0건/초로 집계됨. Hubble DNS 지표는 DNS 가시성(L7 DNS "
    "프록시 정책)이 적용된 흐름만 집계하므로, 실제 DNS 질의가 없다는 뜻이 아닐 수 있음"
)
BAD_VERDICTS = frozenset({"DROPPED", "ERROR"})
"""문제로 보는 흐름 판정 값 (Hubble verdict)."""


@dataclass(frozen=True)
class CountCheck:
    key: str
    label: str


COUNT_CHECKS: tuple[CountCheck, ...] = (
    CountCheck("network.drops_increase", "Hubble 패킷 드롭"),
    CountCheck("network.node_interface_errors_increase", "노드 네트워크 인터페이스 오류"),
    CountCheck("network.pod_network_errors_increase", "Pod 네트워크 오류"),
    CountCheck("network.container_packet_drops_increase", "컨테이너 패킷 드롭"),
)


def _peer(labels: Mapping[str, str], side: str) -> str:
    namespace = labels.get(f"{side}_namespace", "")
    workload = labels.get(f"{side}_workload", "")
    if namespace and workload:
        return f"{namespace}/{workload}"
    return namespace or workload or UNKNOWN_PEER


def network_entity(labels: Mapping[str, str], flow: bool = False) -> TargetRef:
    """결과 라벨로 대상 이름을 만듭니다 (흐름 출발 → 도착, 노드 인터페이스, Pod).

    `flow=True`(Hubble 항목)이면 출발·도착 라벨이 없어도 흐름으로 봅니다. Prometheus는 빈 값
    라벨을 결과에서 빼므로, 출발·도착을 모르는 드롭은 `reason` 라벨만 남습니다.
    """
    if flow or any(k.startswith(("source_", "destination_")) for k in labels):
        has_workload = labels.get("source_workload") or labels.get("destination_workload")
        kind = TargetKind.WORKLOAD if has_workload else TargetKind.NAMESPACE
        name = f"{_peer(labels, 'source')} → {_peer(labels, 'destination')}"
    elif labels.get("k8s_pod_name"):
        kind = TargetKind.POD
        name = "/".join(p for p in (labels.get("k8s_namespace_name"), labels["k8s_pod_name"]) if p)
    elif labels.get("k8s_node_name"):
        kind = TargetKind.NODE
        name = labels["k8s_node_name"]
        if labels.get("interface"):
            name += f" {labels['interface']}"
    else:
        kind, name = TargetKind.HOST, str(dict(labels))
    if labels.get("direction"):
        name += f" ({labels['direction']})"
    used = {
        k: v
        for k, v in labels.items()
        if k
        in (
            "source_namespace",
            "source_workload",
            "destination_namespace",
            "destination_workload",
            "k8s_node_name",
            "interface",
            "k8s_namespace_name",
            "k8s_pod_name",
            "direction",
        )
    }
    return TargetRef(kind=kind, name=name, labels=used)


class NetworkAgent:
    name = AgentName.NETWORK

    def __init__(
        self,
        tool: CatalogQueryTool,
        analysis: AnalysisConfig,
        explainer: AgentExplainer | None = None,
    ) -> None:
        self._tool = tool
        self._cfg = analysis
        self._explainer = explainer
        self._scope_suffix = ""

    async def run(
        self,
        task: AgentTask,
        ctx: AnalysisContext,
        upstream: Mapping[str, AgentResult] | None = None,
    ) -> AgentResult:
        """`upstream`(Service Agent 결과)은 아직 쓰지 않습니다(교차 분석은 12단계)."""
        targets = context_targets(ctx)
        col = Collector()
        col.limit(SCOPE_NOTE)
        # Hubble 항목의 네임스페이스·워크로드 필터는 도착(destination) 기준입니다.
        hubble_filtered = bool({TargetKind.NAMESPACE, TargetKind.WORKLOAD} & set(targets))
        self._scope_suffix = " (도착 기준)" if hubble_filtered else ""
        if hubble_filtered:
            col.limit(HUBBLE_FILTER_NOTE)
        await self._lost_events(ctx, targets, col)
        for check in COUNT_CHECKS:
            await self._count(check, ctx, targets, col)
        await self._verdicts(ctx, targets, col)
        await self._tcp_rst(ctx, targets, col)
        await self._dns(ctx, targets, col)
        result = finish(task, self.name, col)
        return await with_explanation(result, self._explainer, ctx)

    # ------------------------------------------------------------------ 공통

    async def _fetch(
        self,
        key: str,
        ctx: AnalysisContext,
        mode: QueryMode,
        targets: Mapping[TargetKind, str],
        col: Collector,
    ) -> tuple[ToolResult, list[tuple[dict[str, str], float]]] | None:
        """조회해 근거와 결과 행을 돌려줍니다.

        조회하지 못했거나 결과가 없으면 사유를 한계에 적고 None을 돌려줍니다(정상으로 보지 않음).
        """
        result = await fetch_evidence(self._tool, self._cfg, key, ctx, mode, targets, col)
        if result is None:
            return None
        rows = value_rows(result)
        if result.status is ToolStatus.EMPTY or not rows:
            await self._no_data(key, ctx, targets, col)
            return None
        return result, rows

    async def _no_data(
        self, key: str, ctx: AnalysisContext, targets: Mapping[TargetKind, str], col: Collector
    ) -> None:
        if targets:
            coverage = await self._tool.coverage(key, targets, ctx.time_range.end)
            if coverage == 0:
                names = ", ".join(f"{k.value}={v}" for k, v in targets.items())
                col.limit(f"{key}: 요청 대상({names})에 해당하는 시계열이 없어 판단하지 않음")
                return
        col.limit(f"{key}: 결과가 없어 판단하지 않음")

    def _suffix_for(self, key: str) -> str:
        """Hubble 항목에만 대상 필터 기준 표시를 붙입니다."""
        return self._scope_suffix if key in HUBBLE_KEYS else ""

    def _fact(
        self,
        statement: str,
        result: ToolResult,
        severity: Severity = Severity.INFO,
        target: TargetRef | None = None,
        basis: JudgementBasis = JudgementBasis.STATE,
    ) -> Finding:
        return Finding(
            kind=FindingKind.FACT,
            statement=statement,
            severity=severity,
            targets=(target,) if target else (),
            evidence_ids=(result.evidence_id,),
            basis=basis,
        )

    # ------------------------------------------------------------------ 판정

    async def _lost_events(
        self, ctx: AnalysisContext, targets: Mapping[TargetKind, str], col: Collector
    ) -> None:
        """Hubble 이벤트 유실: 분석 대상 이상이 아니라 관측 품질 문제이므로 정보와 한계로 표시."""
        key = "network.hubble_lost_events_increase"
        # 관측 품질 점검이므로 요청 대상과 관계없이 전체를 봅니다(대상 필터를 적용할 수 없는 항목).
        fetched = await self._fetch(key, ctx, QueryMode.WINDOW, {}, col)
        if fetched is None:
            return
        result, rows = fetched
        scope = window_text(ctx)
        lost = sum(v for _, v in rows)
        if lost >= 0.5:
            sources = sorted({labels.get("source") or "?" for labels, v in rows if v > 0})
            col.findings.append(
                self._fact(
                    f"Hubble 이벤트 유실 ({scope}): {count_text(lost)} (유실 위치: "
                    f"{', '.join(sources)})",
                    result,
                )
            )
            col.limit(
                "Hubble 이벤트가 유실되어 Hubble 기반 드롭·흐름·DNS 결과에 빠진 흐름이 있을 수 있음"
            )
        elif is_fresh(result, self._cfg):
            col.findings.append(self._fact(f"Hubble 이벤트 유실 ({scope}): 없음", result))

    async def _count(
        self,
        check: CountCheck,
        ctx: AnalysisContext,
        targets: Mapping[TargetKind, str],
        col: Collector,
    ) -> None:
        fetched = await self._fetch(check.key, ctx, QueryMode.WINDOW, targets, col)
        if fetched is None:
            return
        result, rows = fetched
        scope = window_text(ctx)
        flow = check.key in HUBBLE_KEYS
        benign_reasons = {r.upper() for r in self._cfg.benign_drop_reasons}

        def is_benign(labels: Mapping[str, str]) -> bool:
            return check.key == "network.drops_increase" and (
                labels.get("reason", "").upper() in benign_reasons
            )

        # increase() 추정 오차로 생기는 아주 작은 값은 발생으로 보지 않음.
        # 경고 대상(비장애성 사유가 아닌 것)을 먼저 보입니다.
        happened = sorted((r for r in rows if r[1] >= 0.5), key=lambda r: (is_benign(r[0]), -r[1]))
        benign_seen: set[str] = set()
        for labels, value in happened[: self._cfg.top_n]:
            target = network_entity(labels, flow=flow)
            benign = is_benign(labels)
            reason = ""
            if labels.get("reason"):
                note = ", 비장애성 사유로 설정되어 정보로 표시" if benign else ""
                reason = f" (사유 {labels['reason']}{note})"
            if benign:
                benign_seen.add(labels["reason"])
            col.findings.append(
                self._fact(
                    f"{check.label} ({scope}){self._suffix_for(check.key)}: {target.name} "
                    f"{count_text(value)}{reason}",
                    result,
                    Severity.INFO if benign else Severity.WARNING,
                    target,
                )
            )
        if benign_seen:
            col.limit(
                f"{check.key}: 드롭 사유 {', '.join(sorted(benign_seen))}는 "
                "analysis.benign_drop_reasons 설정에 따라 경고가 아닌 정보로 표시함"
            )
        if happened:
            col.limit(COUNT_NOTE)
            if check.key == "network.drops_increase":
                col.limit(DROP_REASON_NOTE)
            if len(happened) > self._cfg.top_n:
                col.limit(
                    f"{check.key}: 발생 대상 {len(happened)}개 중 상위 {self._cfg.top_n}개만 표시"
                )
            return
        if not is_fresh(result, self._cfg):
            col.limit(f"{check.key}: 데이터 최신성을 확인하지 못해 발생 여부를 판단하지 않음")
            return
        col.findings.append(
            self._fact(
                f"{check.label} ({scope}){self._suffix_for(check.key)}: 발생 없음 "
                f"(대상 {len(rows)}개)",
                result,
            )
        )

    async def _verdicts(
        self, ctx: AnalysisContext, targets: Mapping[TargetKind, str], col: Collector
    ) -> None:
        """네임스페이스 쌍별 DROPPED·ERROR 판정 흐름 비율."""
        key = "network.flows_by_verdict"
        fetched = await self._fetch(key, ctx, QueryMode.CURRENT, targets, col)
        if fetched is None:
            return
        result, rows = fetched
        totals: dict[tuple[str, str], float] = {}
        bad: dict[tuple[str, str], float] = {}
        pair_labels: dict[tuple[str, str], dict[str, str]] = {}
        verdicts: set[str] = set()
        for labels, value in rows:
            pair = (labels.get("source_namespace", ""), labels.get("destination_namespace", ""))
            pair_labels.setdefault(
                pair,
                {"source_namespace": pair[0], "destination_namespace": pair[1]},
            )
            verdict = labels.get("verdict", "").upper()
            verdicts.add(verdict)
            totals[pair] = totals.get(pair, 0.0) + value
            if verdict in BAD_VERDICTS:
                bad[pair] = bad.get(pair, 0.0) + value
        active = {p: t for p, t in totals.items() if t >= self._cfg.min_request_rate}
        quiet = [p for p in totals if p not in active]
        if quiet:
            with_bad = sum(1 for p in quiet if bad.get(p, 0.0) > 0)
            col.limit(
                f"{key}: 흐름이 {self._cfg.min_request_rate:g}건/초보다 적은 네임스페이스 쌍 "
                f"{len(quiet)}개는 비율을 판정하지 않음"
                + (f" (그중 드롭·오류 판정이 있는 쌍 {with_bad}개)" if with_bad else "")
            )
        if not active:
            col.limit(
                f"{key}: 흐름이 기준({self._cfg.min_request_rate:g}건/초)보다 적어 판단하지 않음"
            )
            return
        ratios = {p: bad.get(p, 0.0) / t for p, t in active.items()}
        warn = self._cfg.flow_drop_ratio_warning
        exceeded = sorted(((p, r) for p, r in ratios.items() if r >= warn), key=lambda x: -x[1])
        for pair, ratio in exceeded[: self._cfg.top_n]:
            target = network_entity(pair_labels[pair], flow=True)
            col.findings.append(
                self._fact(
                    f"흐름 드롭·오류 판정 비율(현재, 5분): {target.name} "
                    f"{fmt_value(ratio, 'ratio')} (드롭·오류 {bad.get(pair, 0.0):.3g}건/초, "
                    f"기준 {fmt_value(warn, 'ratio')} 이상)",
                    result,
                    Severity.WARNING,
                    target,
                    JudgementBasis.THRESHOLD,
                )
            )
        if exceeded or not is_fresh(result, self._cfg):
            return
        worst, worst_ratio = max(ratios.items(), key=lambda kv: kv[1])
        worst_name = network_entity(pair_labels[worst], flow=True).name
        worst_text = (
            "드롭·오류 판정 없음"
            if worst_ratio == 0
            else f"최대 {worst_name} {fmt_value(worst_ratio, 'ratio')}"
        )
        col.findings.append(
            self._fact(
                f"흐름 드롭·오류 판정 비율(현재, 5분){self._scope_suffix}: "
                f"네임스페이스 쌍 {len(ratios)}개 모두 기준"
                f"({fmt_value(warn, 'ratio')}) 미만, {worst_text} "
                f"(확인한 판정 값: {', '.join(sorted(v for v in verdicts if v))})",
                result,
                basis=JudgementBasis.THRESHOLD,
            )
        )

    async def _tcp_rst(
        self, ctx: AnalysisContext, targets: Mapping[TargetKind, str], col: Collector
    ) -> None:
        """TCP RST 패킷(연결 강제 종료) 현재 값. 판정 기준은 두지 않습니다."""
        key = "network.tcp_flags_rate"
        fetched = await self._fetch(key, ctx, QueryMode.CURRENT, targets, col)
        if fetched is None:
            return
        result, rows = fetched
        flags = sorted({labels.get("flag", "") for labels, _ in rows if labels.get("flag")})
        rst = sorted(
            (
                (labels, v)
                for labels, v in rows
                if labels.get("flag", "").upper() == "RST" and v > 0
            ),
            key=lambda r: -r[1],
        )
        if rst:
            parts = [
                f"{network_entity(labels, flow=True).name} {v:.3g}건/초"
                for labels, v in rst[: self._cfg.top_n]
            ]
            col.findings.append(
                self._fact("TCP RST 패킷(현재, 5분, 판정 기준 미적용): " + ", ".join(parts), result)
            )
        elif not flags:
            col.limit(f"{key}: 결과에 TCP 플래그 값이 없어 RST 여부를 판단하지 않음")
        elif is_fresh(result, self._cfg):
            col.findings.append(
                self._fact(
                    f"TCP RST 패킷(현재, 5분){self._scope_suffix}: 없음 (확인한 플래그: "
                    + ", ".join(flags)
                    + ")",
                    result,
                )
            )

    async def _dns(
        self, ctx: AnalysisContext, targets: Mapping[TargetKind, str], col: Collector
    ) -> None:
        key = "network.dns_query_rate"
        fetched = await self._fetch(key, ctx, QueryMode.CURRENT, targets, col)
        if fetched is None:
            return
        result, rows = fetched
        total = sum(v for _, v in rows)
        by_source: dict[str, float] = {}
        for labels, value in rows:
            name = labels.get("source_namespace") or UNKNOWN_PEER
            by_source[name] = by_source.get(name, 0.0) + value
        if total <= 0:
            # 수집 범위 문제일 수 있으므로 "DNS 질의 없음"을 사실로 내세우지 않습니다.
            col.limit(DNS_ZERO_NOTE)
            return
        top = sorted(by_source.items(), key=lambda x: -x[1])[: self._cfg.top_n]
        col.findings.append(
            self._fact(
                f"DNS 질의(현재, 5분){self._scope_suffix}: 전체 {total:.3g}건/초, "
                "출발 네임스페이스별 " + ", ".join(f"{n} {v:.3g}건/초" for n, v in top),
                result,
            )
        )
