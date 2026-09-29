"""탐색 대상 기준값.

`CONFIRMED_METRICS`는 사용자가 개발 환경에서 조회를 확인했다고 알려준 지표 이름입니다
(environment.md 3.1절). 탐색은 이 이름들이 실제로 있는지, 라벨·단위·최신성이 어떤지 확인합니다.
"""

from __future__ import annotations

from infra_agent.schemas import AgentName

CONFIRMED_METRICS: dict[str, AgentName] = {
    "k8s_node_cpu_usage": AgentName.SERVER,
    "k8s_node_memory_working_set_bytes": AgentName.SERVER,
    "container_memory_working_set_bytes": AgentName.SERVER,
    "k8s_container_restarts": AgentName.KUBERNETES,
    "hubble_drop_total": AgentName.NETWORK,
    "hubble_dns_queries_total": AgentName.NETWORK,
    "postgresql_backends": AgentName.DB,
    "postgresql_connection_max": AgentName.DB,
    "postgresql_deadlocks_total": AgentName.DB,
    "postgresql_db_size_bytes": AgentName.DB,
    "db_client_connection_count": AgentName.DB,
    "db_client_connection_max": AgentName.DB,
    "db_sql_connection_open": AgentName.DB,
    "db_sql_connection_max_open": AgentName.DB,
    "db_sql_connection_wait_total": AgentName.DB,
    "db_client_operation_duration_seconds_bucket": AgentName.DB,
}

INTEREST_FIRST_TOKENS: frozenset[str] = frozenset(
    {
        # 노드·컨테이너·Kubernetes
        "k8s",
        "container",
        "kube",
        "kubelet",
        "node",
        "system",
        "process",
        # 네트워크
        "hubble",
        "cilium",
        # DB·캐시
        "postgresql",
        "db",
        "valkey",
        "redis",
        # 서비스 요청·트레이스 파생 지표
        "http",
        "rpc",
        "traces",
        "calls",
        "duration",
        "span",
        "otelcol",
    }
)
"""상세 탐색할 지표 이름의 첫 토큰 (`_` 앞부분)."""

HISTOGRAM_COMPANION_SUFFIXES = ("_sum", "_count")

AVAILABILITY_OFFSETS = ("1h", "6h", "24h", "7d", "30d")
"""참조 지표로 과거 데이터 존재 여부를 확인할 시점 (현재 기준 이전)."""

MAX_LOKI_LABELS = 30
MAX_SAMPLE_VALUES = 5
SAMPLE_SERIES = 3
