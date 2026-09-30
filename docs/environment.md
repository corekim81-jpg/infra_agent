# 환경 및 설정 설계

> **상태: 일부 구현.** 설정 로딩(2절, #5), 탐색 명령·카탈로그 로더·`live` 테스트(#7), otel-demo 조회 카탈로그와 점검 명령(4.4절, #9)이 있습니다.
> 3절의 지표 정보는 개발 서버 탐색 결과(2026-09-29)입니다. 라벨 **값**의 의미와 일부 단위는 아직 검증되지 않았습니다(3.3절).
> 시스템 구성은 [architecture.md](architecture.md), 개발 절차는 [development.md](development.md)를 기준으로 합니다.

## 1. 개발·테스트 환경

| 항목 | 내용 | 확인 상태 |
| --- | --- | --- |
| 대상 | 기존 OpenTelemetry Demo 환경 | 사용자 확인 |
| 서버 | Rocky Linux 9.8 개발 서버 1대 (사설망). 주소는 저장소에 기록하지 않고 로컬 설정에서 관리 | 사용자 확인 |
| 클러스터 | k3d Kubernetes, Ready 노드 3개 | 사용자 확인 |
| 관측 백엔드 | Prometheus, Loki, Tempo 실행 중, 조회 응답 확인 | 사용자 확인 |
| 수집 경로 | OpenTelemetry Collector의 kubeletstats, k8s_cluster, Kubernetes objects/events 수집, cAdvisor, Cilium/Hubble | 사용자 확인 (각 데이터가 어느 백엔드에 저장되는지는 검증 필요) |
| 미확인 | 별도 node_exporter, kube-state-metrics | 설치 확인되지 않음 → 해당 지표 이름을 가정하지 않음 |
| 신규 클러스터 | 초기 연동을 위해 kind 등 새 클러스터를 설치하지 않음 | 결정 |

### 1.1 실행 위치와 접속 경로

초기에는 Python 프로그램을 **Windows 호스트**에서 실행하고, SSH 터널로 개발 서버의 백엔드에 접속합니다.

| 데이터 소스 | 로컬 주소 (터널 실행 중에만 유효) |
| --- | --- |
| Prometheus | `http://127.0.0.1:19090` |
| Loki | `http://127.0.0.1:13100` |
| Tempo | `http://127.0.0.1:13200` |

- 터널 명령과 원격 주소는 저장소에 기록하지 않습니다(공개 저장소). 개인 설정 또는 로컬 문서로 관리합니다.
- WSL, Docker, 클라우드 실행 환경에서는 `127.0.0.1`이 Windows 호스트의 터널을 가리키지 않을 수 있으므로 접속 경로를 별도로 확인합니다.
- 프로그램은 시작 시 각 주소의 연결 가능 여부를 점검하고, 실패하면 "터널 미실행 또는 경로 오류 가능"을 포함한 진단 메시지를 출력합니다(설계).

## 2. 설정 구조

### 2.1 원칙

- 코드에는 주소·인증정보·모델 설정을 넣지 않습니다.
- 우선순위: 기본값 < 설정 파일(YAML) < 환경 변수.
- 비밀값은 설정 파일에 쓰지 않고 환경 변수 또는 파일 경로(예: kubeconfig 경로)로만 받습니다.
- 저장소에는 비밀값·내부 주소가 없는 예시만 커밋합니다. 실제 설정 파일(`config/local.yaml`)과 `.env`는 `.gitignore`에 포함합니다.

### 2.2 프로필

| 프로필 | 용도 | 데이터 소스 | 모델 |
| --- | --- | --- | --- |
| `ci` | CI, 단위·계약 테스트 | 가상 응답(HTTP 목) | 가짜 모델 |
| `dev-tunnel` | Windows 호스트 + SSH 터널로 개발 서버 연동 | 1.1절 로컬 주소 | 설정에 따름 |
| `in-cluster` | (향후) Kubernetes 내부 실행 | 클러스터 서비스 DNS | 설정에 따름 |

### 2.3 설정 파일

> **구현됨 (#5):** 설정 모델은 `src/infra_agent/config/settings.py`, 로더는 `src/infra_agent/config/loader.py`입니다.

- 예시 파일: [`config/example.yaml`](../config/example.yaml) (비밀값·내부 주소 없음). 이 파일을 `config/local.yaml`로 복사해 사용합니다(`config/local.yaml`은 Git 제외).
- 설정 확인: `infra-agent config --config config/local.yaml` — 적용될 설정을 검증하고 JSON으로 출력합니다. 오류가 있으면 항목 위치와 이유를 출력하고 종료 코드 2를 반환합니다.
- 검증 규칙(주요):
  - 정의되지 않은 키는 오류로 처리합니다(오타 방지).
  - `enabled: true`인 HTTP 데이터 소스는 `url`이 필요하며, URL에 사용자 정보(`user:pass@`)를 넣으면 거부합니다.
  - `token_env`, `kubeconfig_env`에는 비밀값이 아니라 **환경 변수 이름**(대문자·숫자·`_`)만 허용합니다.
  - `ci` 프로필에서는 `llm.provider: fake`만 허용합니다.
  - 제한 시간은 `tool_timeout_seconds <= agent_timeout_seconds <= request_timeout_seconds`여야 합니다.
  - `default_time_range`는 `30s`, `30m`, `1h`, `7d` 형식입니다.
- 설정 파일이 없으면 안전한 기본값(`ci` 프로필, 데이터 소스 비활성, `fake` 모델, `data_policy: none`)을 사용합니다.
- `llm` 섹션(#15): `provider`(`fake`=모델 없음, `claude_agent_sdk`), `model`(`null`이면 SDK 기본값), `data_policy`(`none`·`aggregated`·`full`, architecture.md 10.2절), `max_calls_per_request`(요청당 모델 호출 상한), `max_budget_usd_per_request`(SDK에 호출마다 전달하는 비용 상한). 예시 파일은 개발 환경 결정에 따라 `claude_agent_sdk`·`full`입니다. 운영 설정에는 그대로 쓰지 않습니다.
  - SDK 설치: `python -m pip install -e ".[llm]"`. SDK는 Claude Code CLI를 실행하므로 CLI 인증(`ANTHROPIC_API_KEY` 또는 Claude Code 로그인)이 필요합니다.
  - SDK가 없거나 CLI를 실행할 수 없으면 규칙 기반 경로로 계속하고 답변에 그 사실을 표시합니다. `ask --no-llm`으로 모델 없이 실행할 수 있습니다.
- `analysis` 섹션(#11): 판정 기준(사용률 경고 0.8·심각 0.9, 스로틀링 0.25, 직전 구간 대비 증가 50%와 최소 증가량 CPU 0.05 cores·메모리 100MiB, 데이터 지연 기준 300초, 표시 대상 수, 요청당 조회 상한 100(#21에서 60→100: 에이전트들이 공유하며 최신성·대상 존재 확인 조회 포함), Service 기준(#23: 오류율 경고 5%·심각 20%, p95 지연 경고 1초, 오류율 판정 최소 요청률 0.01건/초, 오류율 증가 2%p, 지연 증가 0.1초, 상세 서비스 3개, 최고 시점 전후 600초, 로그·트레이스 샘플 3건)). 값은 개발 환경용 제안 기본값이며 운영 환경에 맞게 조정해야 합니다.

### 2.4 환경 변수

| 변수 | 용도 | 비밀 여부 | 상태 |
| --- | --- | --- | --- |
| `INFRA_AGENT_CONFIG` | 설정 파일 경로 | 아니오 | 구현됨 |
| `INFRA_AGENT_PROFILE` | 프로필 덮어쓰기 | 아니오 | 구현됨 |
| `INFRA_AGENT__<SECTION>__<KEY>` | 설정 항목 덮어쓰기. `__`로 중첩 구분, 대소문자 무관, 값은 YAML 스칼라로 해석(`true`, `15`, `null`). 예: `INFRA_AGENT__DATASOURCES__PROMETHEUS__URL` | 아니오 | 구현됨 |
| `token_env`로 지정한 변수 (예: `INFRA_AGENT_PROMETHEUS_TOKEN`) | 데이터 소스 인증이 필요해질 경우의 토큰 | 예 | 구현됨 (Bearer 헤더, 값은 마스킹 등록) |
| `INFRA_AGENT_KUBECONFIG` (`kubeconfig_env` 기본값) | 전용 읽기 계정 kubeconfig 파일 경로 | 파일 내용은 비밀 | 설정 필드만 구현 |
| `ANTHROPIC_API_KEY` | Claude Agent SDK 인증 (SDK가 직접 읽음, 프로그램은 값을 읽거나 기록하지 않음). Claude Code 로그인으로 대체 가능 | 예 | 구현됨 (#15) |
| `INFRA_AGENT_LIVE_TESTS` | `1`일 때만 `live` 테스트 실행 | 아니오 | 구현됨 (`tests/conftest.py`) |

## 3. 데이터 소스별 사용 범위 (탐색 결과)

> **근거:** 개발 서버에서 `infra-agent discover`를 실행한 결과(2026-09-29)입니다. 지표·라벨 **이름**과 존재 여부만 기록하며, 라벨 값과 원본 보고서는 저장소에 두지 않습니다.
> 요약: Prometheus 3.13.1, Loki 3.7.2, Tempo v3.0.0 모두 정상. 전체 지표 2,074개 중 관심 지표 319개를 상세 확인했고 조회 오류는 0건입니다.
> 분석에 사용하는 조회식은 [`config/catalog/otel-demo.yaml`](../config/catalog/otel-demo.yaml)이 기준입니다(4절).

### 3.1 Prometheus 공통 사항

| 항목 | 탐색 결과 | 영향 |
| --- | --- | --- |
| 보존 기간 | `storageRetention` 1w, 참조 지표 기준 7일 전 데이터 있음·30일 전 없음 | 7일보다 긴 비교는 할 수 없음 |
| 최신성 | 대부분 최신 샘플이 30초 이내 | 최신성 점검 기준 설정 시 참고 |
| 메타데이터 | 상세 확인 지표 319개 모두 타입·단위 메타데이터 없음 (OTLP 수집으로 추정) | 단위는 이름 접미사로 판단하고, 이름에 단위가 없으면 카탈로그 caveats에 표시하고 교차 검증 |
| 사용자 확인 지표 | 초기 확인 목록 16개(`discovery/expectations.py`) 모두 존재 | – |
| `node_*`, `kube_*` | 사용하지 않음 (node_exporter·kube-state-metrics 미확인) | – |

### 3.2 분야별 사용 가능 지표

| 분야 | 지표 (계열) | 주요 대상 라벨 | 담당 |
| --- | --- | --- | --- |
| 노드 자원 | `k8s_node_{cpu_usage, memory_*, filesystem_*, network_*}`, `k8s_node_allocatable_*` | `k8s_node_name` | Server |
| Pod·컨테이너 자원 (kubeletstats) | `k8s_pod_*`, `container_{cpu_usage, memory_*, filesystem_*}` | `k8s_namespace_name`, `k8s_pod_name`, `k8s_container_name`, `service_name` | Server |
| 컨테이너 (cAdvisor) | `container_cpu_cfs_*`, `container_network_*`, `container_oom_events_total` | 위와 같음 (`service_name`은 수집 대상을 가리킬 수 있음) | Server·Network·Kubernetes |
| 클러스터 상태 (k8s_cluster) | `k8s_container_{restarts, ready, *_limit, *_request}`, `k8s_pod_phase`, `k8s_node_condition_*`, `k8s_{deployment,replicaset,statefulset,daemonset,job,hpa}_*`, `k8s_namespace_phase` | 리소스 이름·네임스페이스 | Kubernetes |
| Hubble | `hubble_{drop, flows_processed, tcp_flags, dns_queries, dns_responses, dns_response_types, lost_events}_total` | `source_*`, `destination_*`, `reason`, `verdict`, `flag` | Network |
| PostgreSQL | `postgresql_*` 27종 (연결 수·최대, 데드락, 커밋·롤백, 블록 hit/read, DB·테이블·인덱스 크기, bgwriter) | `postgresql_database_name`, `service_name=postgresql` | DB |
| 앱 커넥션 풀 | accounting: `db_client_connection_{count,max}` 등 Npgsql 지표 / product-catalog: `db_sql_connection_*` | `service_name`, 풀 이름 | DB |
| DB 작업 지연 | `db_client_operation_duration_seconds_bucket` (product-catalog) | `service_name`, `db_operation_name` | DB |
| Valkey | `redis_*` 29종 (클라이언트, 메모리, 퇴출·만료, 적중·실패, 거부 연결) | `service_name=valkey-cart` | DB (캐시) |
| 서비스 RED | `traces_spanmetrics_{calls_total, latency_bucket}` (Tempo metrics-generator) | `service`, `span_kind`, `status_code`, `http_route`, `db_system_name` | Service |
| 서비스 의존 관계 | `traces_service_graph_request_{total, failed_total, server_seconds_bucket, client_seconds_bucket}` | `client`, `server` | Service |
| 서비스별 계측 | `http_*`, `rpc_*` (서비스마다 지표 이름·단위가 다름: `_milliseconds` / `_seconds`) | `service_name` | Service (보조) |

### 3.3 주의할 점

- **`system_*` 지표:** `k8s_pod_name`·`telemetry_auto_version` 라벨이 붙어 있어 Pod 안의 자동 계측(런타임) 값으로 보입니다. **물리 서버 전체 값으로 사용하지 않으며** 카탈로그에서 제외했습니다. 물리 서버 성능 분석이 필요하면 호스트 수준 수집을 별도로 구성해야 합니다.
- **Hubble 지표의 `k8s_namespace_name`·`k8s_pod_name`:** 트래픽 대상이 아니라 수집 주체(cilium) Pod를 가리킵니다. 대상 필터는 `source_*`/`destination_*` 라벨을 씁니다.
- **DNS 오류율:** Hubble DNS 지표에 응답 코드(rcode) 라벨이 없어 판단할 수 없습니다.
- **서비스 지표 라벨 차이:** spanmetrics는 `service`, service graph는 `client`/`server`, 나머지는 `service_name`을 씁니다. 카탈로그의 `target_labels`가 이 차이를 흡수합니다.
- **값 의미 미검증:** `k8s_pod_phase`(1~5 값), `k8s_node_condition_*`(1/0/-1), spanmetrics `status_code`·`span_kind` 값, 커넥션 상태 라벨 값은 가정이며 카탈로그 caveats에 적었습니다.
- **단위:** `k8s_node_cpu_usage`, `container_cpu_usage`는 cores로 교차 검증됨(2026-09-29 live 테스트). `k8s_pod_cpu_usage`(cores 가정)와 spanmetrics 지연(seconds 가정)은 미검증.

### 3.4 Loki, Tempo

- **Loki 라벨:** `service_name`(18개), `service_namespace`, `k8s_namespace_name`, `k8s_deployment_name`, `k8s_cluster_name`, `deployment_environment_name`.
  **Kubernetes 이벤트가 Loki에 저장되는지는 아직 확인되지 않았습니다**(라벨 이름만으로는 판단 불가).
- **Service Agent의 Loki 사용(#23):** 카탈로그 `log.lines_total`·`log.error_lines`·`log.error_samples`(대상 라벨 `service_name`, `k8s_namespace_name`). 오류 판정은 본문 키워드(error, exception, fatal, panic) 기준이며, 로그 레벨 필드(`detected_level` 등) 사용은 검토 중입니다. 개발 서버 live 결과(2026-09-30): 로그가 있는 서비스 18개, 앱 로그 샘플에 trace_id가 있음(로그↔트레이스 연결 가능), OTLP 로그에 레벨 필드가 있음. 키워드만으로는 INFO 로그의 단어 일부(`...Error`)가 오류로 잡혀, 단어 단위 매칭과 INFO·DEBUG·TRACE 레벨 제외를 적용했습니다(레벨 필드 이름 `severity_text`·`detected_level` 중 실제 사용 필드는 live 재확인 필요).
- **클러스터 객체 로그:** 앱 서비스가 아닌 로그 출처 하나가 `service_name` 라벨로 들어오며, 내용은 Kubernetes 이벤트가 아니라 Pod 객체 JSON 전체입니다(k8s 객체 수집으로 보임). Service Agent는 이를 서비스 상세 확인에서 제외합니다. Pod 상태(종료 사유 등)를 담고 있어 Kubernetes Agent의 종료 사유 확인 경로로 검토할 수 있습니다.
- **Tempo service graph 지연 히스토그램:** 최대 유한 버킷이 12.8초로, p95가 12.8초이면 실제 값은 그 이상입니다. flagd EventStream처럼 수 분간 열려 있는 스트리밍 호출이 여기에 해당합니다.
- **Service Agent의 Tempo 사용(#23):** `/api/search` TraceQL `{ resource.service.name = "<서비스>" && status = error }`(서비스 이름 형식 검사 후 리터럴로만 삽입).
- **Tempo 태그:** resource 42개(`service.name`, `k8s.*` 등), span 149개(`db.system`, `db.statement`, `db.query.text`, `http.*`, `rpc.*` 등), event 16개(`exception.*` 등).
  `db.statement`·`db.query.text`에는 **SQL 원문**이 담기므로 모델 입력 데이터 정책(architecture.md 10.2절) 결정 시 함께 고려합니다.

### 3.5 Kubernetes API

- 전용 ServiceAccount와 읽기 전용 ClusterRole(`get`, `list`, `watch`; `secrets` 제외)을 만든 뒤, 그 계정의 kubeconfig로만 연결합니다.
- **관리자 kubeconfig는 프로그램에 사용하지 않습니다.** 프로그램 시작 시 권한 점검(`SelfSubjectRulesReview` 등)으로 쓰기 권한이 있으면 경고하는 방안을 구현 단계에서 검토합니다.
- 읽기 계정 준비 전에는 Kubernetes Agent가 Prometheus의 k8s_cluster 계열 지표로 동작합니다(구현됨, #21). Pending 사유·종료 사유 등 상세 정보는 API 연동 후 확인할 수 있습니다.

### 3.6 DB 분석 범위

- 대상: `otel-demo` 네임스페이스의 PostgreSQL 17.6, Valkey 9.0.1(`redis_*`로 수집).
- PostgreSQL 직접 SQL 접속은 초기 필수 조건이 아닙니다.
- 확인되지 않은 항목: MySQL, `pg_stat_statements`, 쿼리 실행 계획, 잠금 대기·잠금 그래프. 이 항목이 필요한 질문에는 한계와 필요한 수집 설정을 답변에 포함합니다.

## 4. 조회 카탈로그

### 4.1 목적

에이전트 코드에 PromQL·LogQL·TraceQL과 지표 이름을 고정하지 않고, **환경별 카탈로그 파일**에서 분석 항목별 조회식을 가져옵니다.

### 4.2 형식

> **구현됨 (#7, #9):** 로더와 검증은 `src/infra_agent/catalog/models.py`입니다. 자리표시자는 `labels`의 키와 실행 시 값 `{selector}`, `{range}`만 허용하며, `selector`에 중괄호가 들어가면 거부합니다. `target_labels`(#9)로 대상 종류별 라벨 이름을 지정합니다.

```yaml
# config/catalog/otel-demo.yaml
version: 1
environment: otel-demo-k3d
items:
  node.cpu_usage:
    agent: server
    source: prometheus
    description: k3d 노드 CPU 사용량
    unit: cores                       # 탐색으로 확인
    query: 'sum by ({node_label}) (k8s_node_cpu_usage{{{selector}}})'
    requires_metrics: [k8s_node_cpu_usage]
    labels:
      node_label: k8s_node_name       # 예시 값. 실제 라벨 이름은 탐색으로 확정
    evidence:
      status: user_confirmed          # user_confirmed | discovered | verified
      checked_at: null
    caveats:
      - k3d 노드 값이며 물리 서버 전체 자원이 아님
```

- `evidence.status`
  - `user_confirmed`: 사용자가 이름만 확인함
  - `discovered`: 탐색 명령으로 존재·라벨을 확인함
  - `verified`: 의미와 단위까지 검토해 분석에 사용해도 된다고 확인함
- 에이전트는 가용성 점검에서 `requires_metrics`가 실제로 존재하는 항목만 실행합니다. 없는 항목은 "확인 불가"로 기록합니다.
- 카탈로그에는 지표·라벨 **이름**만 두고, 실제 라벨 값(Pod 이름, 호스트 이름 등)이나 조회 결과는 넣지 않습니다.

### 4.3 탐색 절차

> **구현됨 (#7):** `src/infra_agent/discovery/`, 명령 `infra-agent discover`. 개발 서버에서 2026-09-29에 실행했습니다(결과 요약은 3절).

`infra-agent discover`는 읽기 전용 GET 요청만으로 다음을 수행합니다.

1. 활성화된 데이터 소스 연결 점검 (`/-/ready`, `/ready`, buildinfo)
2. Prometheus: 보존 설정(`runtimeinfo.storageRetention`), 전체 지표 이름과 계열(이름 첫 토큰) 분포, 메타데이터(타입·단위·설명)
3. 관심 지표 상세: 사용자 확인 지표 전부와 관심 계열(`k8s`, `container`, `system`, `hubble`, `postgresql`, `db`, `http`, `rpc` 등, `discovery/expectations.py`)에 대해
   - 라벨 이름(`/api/v1/labels?match[]=`), 시계열 수(`count`), 최신 샘플 경과 시간(`max(timestamp())`), 라벨 값 표본(`topk(3, …)`)
   - 히스토그램은 `_bucket`만 상세 조회하고 `_sum`/`_count`는 생략
4. 과거 데이터 존재 여부: 참조 지표로 1h·6h·24h·7d·30d 전 시점 확인
5. Loki 라벨 이름과 값 개수·표본(최대 30개 라벨), Tempo 태그 이름(범위별)
6. `var/discovery/discovery-<UTC시각>.{md,json}` 저장 (Git 제외)

부하 제한: 동시 요청 수는 `execution.max_concurrency`, 상세 지표 수는 `--max-metrics`(기본 400)로 제한합니다.
보고서 보호: 라벨 값의 IPv4 주소(127.x 제외)는 기본적으로 `<ip>`로 가리고, 비밀값 패턴도 마스킹합니다. `--no-values`로 라벨 값 표본을 아예 제외할 수 있습니다.

Windows 실행 예 (SSH 터널 실행 중, PowerShell):

```powershell
copy config\example.yaml config\local.yaml
$env:INFRA_AGENT_CONFIG = "config\local.yaml"
infra-agent check
infra-agent discover                       # 옵션: --lookback 1h --max-metrics 400 --include-prefix app --no-values
$env:INFRA_AGENT_LIVE_TESTS = "1"; python -m pytest -m live
```

사람이 보고서를 검토한 뒤 카탈로그(4단계)를 작성하고 `evidence.status`를 갱신해 커밋합니다. 보고서 자체는 커밋하지 않습니다.

### 4.4 otel-demo 카탈로그와 점검

> **구현됨 (#9):** [`config/catalog/otel-demo.yaml`](../config/catalog/otel-demo.yaml) — Prometheus 항목 53개(Server 13, Kubernetes 11, Network 8, DB·캐시 14, Service 7). `verified` 2개(`node.cpu_usage`, `container.cpu_usage`: cores 단위 교차 검증 통과), 나머지 51개는 `discovered`입니다.

- 항목마다 `target_labels`(대상 종류 → 라벨 이름)를 두어, 에이전트가 대상(네임스페이스·Pod·서비스 등)을 selector로 바꿀 때 사용합니다. 항목이 지원하지 않는 대상은 `selector_for()`가 따로 반환하므로 답변의 한계로 표시해야 합니다.
- 점검 명령:

```powershell
infra-agent catalog                 # 파일 형식 검증
infra-agent catalog --execute       # 각 조회를 Prometheus에 실행: 정상 / 결과 없음 / 지표 없음 / 오류
```

  "결과 없음"은 오류가 아닙니다(예: 재시작이 없으면 `k8s.container_restarts_increase`는 비어 있음). "지표 없음"과 "오류"가 있으면 종료 코드 1을 반환합니다.
- 조회식 문법은 로컬 Prometheus 3.5(빈 데이터)에서 53개 항목 × selector 유무 106건을 실행해 오류가 없음을 확인했습니다.
- **개발 서버 실행 점검(2026-09-29):** 정상 43, 결과 없음 10, 지표 없음 0, 오류 0.
  - 결과 없음 10건은 모두 "문제가 있을 때만 결과가 나오는" 조건형 조회입니다(`k8s.container_restarts_increase`, `k8s.container_oom_events`, `k8s.pod_phase`, `k8s.node_not_ready`, `k8s.node_pressure`, `k8s.deployment_unavailable`, `k8s.statefulset_unready`, `k8s.daemonset_unready`, `k8s.job_failed_pods`, `k8s.hpa_at_max`).
  - `network.tcp_flags_rate`가 워크로드 단위 집계에서 시계열 1.4만 개를 반환해 네임스페이스 단위로 줄였습니다.
  - live 테스트 7건 통과: 카탈로그 전체 조회 실행, `k8s_node_cpu_usage`·`container_cpu_usage`의 cores 단위 교차 검증(CPU 시간 증가율 대비 비율 0.5~2.0).
- **조건형 조회 해석 규칙(에이전트 구현 시 적용):** 결과가 비어 있으면 "조건에 해당하는 대상 없음"으로 판단할 수 있는 것은 필요한 지표가 존재하고 최신 데이터가 있을 때뿐입니다. 이를 확인하지 못했으면 "확인 불가"로 답합니다.
- `verified`로 올리는 기준: `--execute` 결과 확인 + caveats에 적은 단위·값 의미를 실제 값으로 검토.

## 5. 테스트 구분

| 구분 | 데이터 | 실행 위치 | 조건 |
| --- | --- | --- | --- |
| 단위·계약 | 가상 응답(`tests/fixtures/synthetic/`) | CI, 로컬 | 항상 |
| 개발 서버 연동 (`live` 마커) | 개발 서버 실제 데이터(읽기 전용) | Windows 호스트 + 터널 | `INFRA_AGENT_LIVE_TESTS=1`, `dev-tunnel` 프로필 |
| 실제 모델 호출 (`live` 마커, `test_live_llm.py`) | 질문 문장·정책에 따른 조회 결과 | 사용자 환경 | 위 조건 + `.[llm]` 설치, SDK 인증, `llm.provider: claude_agent_sdk`. 과금 발생 가능 |

- CI는 `pytest -m "not live"`만 실행합니다.
- `live` 테스트: `tests/live/test_live_sources.py` (데이터 소스 준비 상태, Prometheus 기본 조회, 확인 지표 존재, 탐색 스모크). `INFRA_AGENT_CONFIG`가 없으면 건너뜁니다.
- 연동 테스트는 특정 값을 기대하지 않고 형식·존재·최신성을 검증합니다. 결과 원문은 저장소에 남기지 않습니다.
- 가상 fixture는 탐색 결과의 **구조**(지표·라벨 이름)를 본떠 만들되, 값은 가상임을 표시합니다(`synthetic: true`).

## 6. 사용자 측 준비 사항

| 항목 | 필요한 시점 |
| --- | --- |
| SSH 터널 실행 (1.1절 포트) | 연동 테스트, 탐색 |
| Kubernetes 전용 읽기 계정과 kubeconfig | Kubernetes API 연동 단계 |
| Claude Agent SDK 인증 (`ANTHROPIC_API_KEY` 또는 Claude Code 로그인) | 모델 연동 단계 (6단계) |
| 운영 환경의 모델 데이터 범위 결정 (`llm.data_policy`, 개발 환경은 `full`로 결정) | 운영 데이터를 모델에 전달하기 전 |
