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
| Kubernetes API (선택, #35) | 예: `https://127.0.0.1:16443` — 로컬 포트는 사용자가 정하고, kubeconfig의 `server`에 씁니다 |

- 터널 명령과 원격 주소는 저장소에 기록하지 않습니다(공개 저장소). 개인 설정 또는 로컬 문서로 관리합니다.
- WSL, Docker, 클라우드 실행 환경에서는 `127.0.0.1`이 Windows 호스트의 터널을 가리키지 않을 수 있으므로 접속 경로를 별도로 확인합니다.
- 프로그램은 시작 시 각 주소의 연결 가능 여부를 점검하고, 실패하면 "터널 미실행 또는 경로 오류 가능"을 포함한 진단 메시지를 출력합니다(설계).

### 1.2 컨테이너·클러스터 내부 실행 (#39)

목표 실행 환경(Rocky Linux 9 컨테이너, Kubernetes 내부)용 파일입니다.

**개발 서버 확인 결과(#39, 2026-10-02, 사용자 실행):**
- 이미지 빌드(docker)와 k3d 클러스터로 이미지 가져오기 성공.
- 연결 점검 Job: Prometheus·Loki·Tempo·Kubernetes API 모두 정상. Kubernetes API는 Pod의 ServiceAccount 토큰으로 접속했고 권한 점검을 통과했습니다.
- 질문 Job: 클러스터 내부에서 질문 한 건이 끝까지 실행됐고(Kubernetes Agent 성공), Kubernetes API 근거 3개가 오류 없이 조회됐습니다.
- 처음에는 데이터 소스 세 곳이 시간 초과로 실패했습니다. 데이터 소스 네임스페이스에 들어오는 트래픽 기본 차단 정책이 있었기 때문이며, 조회 포트 허용 정책(`networkpolicy-example.yaml`)을 추가한 뒤 정상이 됐습니다.
- 확인하지 않은 것: 토큰 교체 후 재읽기(Job이 짧게 끝나 교체 시점에 도달하지 않음, 단위 테스트로만 검증), podman 빌드, 레지스트리 배포.

| 파일 | 내용 |
| --- | --- |
| [`deploy/Containerfile`](../deploy/Containerfile) | Rocky Linux 9(minimal) + Python 3.11. 비루트 사용자(10001), 진입점 `infra-agent`. 코드와 조회 카탈로그·평가 세트만 포함 |
| `.dockerignore` | `.env`, `config/local.yaml`, `*.kubeconfig`, `var/`, `.git` 등을 빌드 컨텍스트에서 제외 |
| [`deploy/k8s/config.yaml`](../deploy/k8s/config.yaml) | 설정 ConfigMap 예시 (`profile: in-cluster`, `kubernetes.auth: in_cluster`, 모델 없음). 데이터 소스 주소는 예시이므로 실제 서비스 주소로 바꿉니다 |
| [`deploy/k8s/job-check.yaml`](../deploy/k8s/job-check.yaml) | 연결 점검 Job (`infra-agent check`) |
| [`deploy/k8s/job-ask.yaml`](../deploy/k8s/job-ask.yaml) | 질문 한 건을 실행하는 Job 예시 (HTTP API 제공 전의 임시 실행 방법) |
| [`deploy/k8s/networkpolicy-example.yaml`](../deploy/k8s/networkpolicy-example.yaml) | 데이터 소스 네임스페이스가 들어오는 트래픽을 기본 차단할 때, infra-agent 네임스페이스에서 조회 포트로의 접속을 허용하는 NetworkPolicy 예시 |

- **Kubernetes API 인증:** `datasources.kubernetes.auth: in_cluster`이면 Pod에 마운트된 ServiceAccount 토큰·CA(`/var/run/secrets/kubernetes.io/serviceaccount`)와 `KUBERNETES_SERVICE_HOST`·`PORT`로 접속합니다. 토큰은 주기적으로 교체되므로 요청마다 파일 변경을 확인해 다시 읽습니다. kubeconfig 파일은 쓰지 않습니다.
- **계정:** Job은 3.5절의 전용 읽기 계정(`infra-agent-reader`)으로 실행합니다. 그 ServiceAccount는 토큰 자동 마운트가 꺼져 있고 Job의 Pod에서만 켭니다. 권한 점검(쓰기·실행·프록시·secrets 권한이 있으면 조회하지 않음)은 클러스터 밖 실행과 같습니다.
- **Pod 보안 설정:** 비루트, 권한 상승 금지, 읽기 전용 루트 파일시스템, capability 전부 제거. 쓰는 경로는 `var/`(emptyDir)뿐입니다.
- **모델:** 이미지에는 모델 SDK를 넣지 않았습니다. 클러스터 내부 실행은 모델 없이(`llm.provider: fake`) 규칙·코드 판정만 합니다. 운영 데이터의 외부 모델 전송 범위가 미확정이기 때문입니다(architecture.md 11절).

**실행 절차 (사용자 작업, k3d 기준)**

```bash
# 1) 이미지 빌드 (저장소 루트, docker 또는 podman)
docker build -f deploy/Containerfile -t infra-agent:dev .
# 2) k3d 클러스터에 이미지 넣기 (레지스트리를 쓰면 push 후 Job의 image를 바꿉니다)
k3d image import infra-agent:dev -c <클러스터 이름>
# 3) 읽기 계정(이미 적용했다면 생략), 설정, 점검 Job
kubectl apply -f deploy/rbac/infra-agent-reader.yaml
kubectl apply -f deploy/k8s/config.yaml        # 데이터 소스 주소를 실제 서비스로 바꾼 뒤
kubectl apply -f deploy/k8s/job-check.yaml
kubectl -n infra-agent logs job/infra-agent-check
```

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
| `in-cluster` | Kubernetes 내부 실행 (1.2절, #39) | 클러스터 서비스 DNS, Kubernetes API는 Pod의 ServiceAccount 토큰(`auth: in_cluster`) | 설정에 따름 (이미지에는 모델 SDK 없음 → `fake`) |

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
| `INFRA_AGENT_KUBECONFIG` (`kubeconfig_env` 기본값) | 전용 읽기 계정 kubeconfig 파일 경로 (ServiceAccount 토큰 형식만 허용) | 파일 내용은 비밀 | 구현됨 (#35, 토큰은 마스킹 등록) |
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
- **DNS 질의량:** 개발 환경 live 확인(2026-09-30) 결과 `hubble_dns_queries_total`의 5분 증가율이 0이었습니다. Hubble DNS 지표는 DNS 가시성(L7 DNS 프록시 정책)이 적용된 흐름만 집계하므로 실제 DNS 질의량으로 보지 않습니다. 필요하면 CiliumNetworkPolicy의 DNS 규칙(`toPorts.rules.dns`)으로 가시성을 켜야 합니다.
- **스트리밍 호출 지연:** 개발 환경에서는 flagd를 호출받는 서비스 간 호출 일부의 지연 p95가 히스토그램 상한(12.8초 이상)으로 꾸준히 나옵니다(2026-09-30~10-01 live). 기능 플래그 이벤트 스트림처럼 오래 열린 호출로 보이므로, `config/local.yaml`의 `analysis.streaming_services: [flagd]`로 지연 판정에서 뺄 수 있습니다(기본값은 비어 있음, #33).
- **Hubble 드롭 사유:** 개발 환경에서는 `UNSUPPORTED_L3_PROTOCOL`(IPv4·IPv6가 아닌 L3 패킷) 드롭이 출발·도착 라벨 없이 30분에 수십 회 꾸준히 발생합니다. 일반적으로 장애가 아니므로 `analysis.benign_drop_reasons` 기본값으로 정보 표시합니다.
- **서비스 지표 라벨 차이:** spanmetrics는 `service`, service graph는 `client`/`server`, 나머지는 `service_name`을 씁니다. 카탈로그의 `target_labels`가 이 차이를 흡수합니다.
- **값 의미 미검증:** `k8s_pod_phase`(1~5 값), `k8s_node_condition_*`(1/0/-1), spanmetrics `status_code`·`span_kind` 값, 커넥션 상태 라벨 값은 가정이며 카탈로그 caveats에 적었습니다.
- **단위:** `k8s_node_cpu_usage`, `container_cpu_usage`는 cores로 교차 검증됨(2026-09-29 live 테스트). `k8s_pod_cpu_usage`(cores 가정)와 spanmetrics 지연(seconds 가정)은 미검증.

### 3.4 Loki, Tempo

- **Loki 라벨:** `service_name`(18개), `service_namespace`, `k8s_namespace_name`, `k8s_deployment_name`, `k8s_cluster_name`, `deployment_environment_name`.
  **Kubernetes 이벤트가 Loki에 저장되는지는 아직 확인되지 않았습니다**(라벨 이름만으로는 판단 불가).
- **Service Agent의 Loki 사용(#23):** 카탈로그 `log.lines_total`·`log.error_lines`·`log.error_samples`(대상 라벨 `service_name`, `k8s_namespace_name`). 오류 판정은 본문 키워드(error, exception, fatal, panic, 단어 단위) 기준이며, 레벨 필드(`severity_text`·`detected_level`)가 INFO·DEBUG·TRACE인 줄은 제외합니다. 개발 서버 live 결과(2026-09-30): 로그가 있는 서비스 18개, 앱 로그 샘플에 trace_id가 있음(로그↔트레이스 연결 가능), OTLP 로그에 레벨 필드가 있음. 키워드만으로는 INFO 로그의 단어 일부(`...Error`)가 오류로 잡혔으나, 단어 단위 매칭과 레벨 제외를 적용한 뒤 live 재실행에서 조회식이 정상 동작하고 해당 오탐(product-reviews)이 0건이 됨을 확인했습니다. 두 레벨 필드 중 어느 쪽이 제외에 쓰였는지는 따로 확인하지 않았습니다.
- **호출·소비만 하는 서비스:** fraud-detection처럼 SERVER span 없이 CLIENT·CONSUMER span만 있는 서비스가 있습니다. Service Agent는 요청량·오류율 판정은 SERVER span 기준으로 하되, 로그·트레이스 상세 확인 대상은 span 종류와 관계없이 spanmetrics에 나타난 서비스로 봅니다.
- **스트리밍 호출의 현재 값 변동:** flagd EventStream 호출은 약 600초 유지된 뒤 오류 상태로 끝나는 트레이스로 기록됩니다(live에서 600001~600006ms 확인). span은 호출이 끝날 때 기록되므로, 현재(5분 rate) 오류율·지연은 최근 5분 안에 스트림이 끝났는지에 따라 나타났다 사라집니다(live 2026-09-30: 13:03 실행에서는 CLIENT 오류 100%와 12.8초 이상 지연, 13:27 실행에서는 둘 다 없음). 분석 구간의 오류 트레이스에는 계속 나타나므로, Service Agent는 로그·트레이스 상세 대상을 고를 때 오류 트레이스의 루트 서비스도 사용합니다.
- **계측되지 않은 호출 대상:** service graph의 `server`에는 postgresql처럼 spanmetrics에 없는(계측되지 않은) 대상이 나옵니다. live(2026-09-30)에서 accounting → postgresql 호출 실패율이 6.5%로 기준을 넘었을 때, 응답한 쪽(postgresql)의 로그·트레이스는 없으므로 Service Agent는 호출한 쪽(accounting)을 상세 확인합니다.
- **Tempo 루트 span 미수신 표시:** 오류 트레이스 검색 결과의 루트 서비스가 `<root span not yet received>`로 오는 트레이스가 있습니다(루트 span이 아직 수집되지 않음). 서비스 이름이 아니므로 상세 대상에서 제외합니다.
- **클러스터 객체 로그:** 앱 서비스가 아닌 로그 출처 하나가 `service_name` 라벨로 들어오며, 내용은 Kubernetes 이벤트가 아니라 Pod 객체 JSON 전체입니다(k8s 객체 수집으로 보임). Service Agent는 이를 서비스 상세 확인에서 제외합니다. Pod 상태(종료 사유 등)를 담고 있어 Kubernetes Agent의 종료 사유 확인 경로로 검토할 수 있습니다.
- **Tempo service graph 지연 히스토그램:** 최대 유한 버킷이 12.8초로, p95가 12.8초이면 실제 값은 그 이상입니다. flagd EventStream처럼 수 분간 열려 있는 스트리밍 호출이 여기에 해당합니다.
- **Service Agent의 Tempo 사용(#23):** `/api/search` TraceQL `{ resource.service.name = "<서비스>" && status = error }`(서비스 이름 형식 검사 후 리터럴로만 삽입). 검색 응답의 `traceID`는 앞자리 0을 뺀 16진수로 옵니다(live에서 31자리 값 확인). 로그의 trace_id(32자리)와 연결하려고 양쪽 모두 앞을 0으로 채운 32자리로 맞춥니다.
- **Tempo 태그:** resource 42개(`service.name`, `k8s.*` 등), span 149개(`db.system`, `db.statement`, `db.query.text`, `http.*`, `rpc.*` 등), event 16개(`exception.*` 등).
  `db.statement`·`db.query.text`에는 **SQL 원문**이 담기므로 모델 입력 데이터 정책(architecture.md 10.2절) 결정 시 함께 고려합니다.

### 3.5 Kubernetes API

- 전용 ServiceAccount와 읽기 전용 ClusterRole(`get`, `list`, `watch`; `secrets` 제외)을 만든 뒤, 그 계정의 토큰 kubeconfig로만 연결합니다. 매니페스트: [`deploy/rbac/infra-agent-reader.yaml`](../deploy/rbac/infra-agent-reader.yaml) (Pod·노드·네임스페이스·이벤트, apps 워크로드, Job·CronJob, HPA).
- **관리자 kubeconfig는 프로그램에 사용하지 않습니다.** 프로그램은 kubeconfig 사용자에 클라이언트 인증서·exec 플러그인·auth-provider·사용자 이름/비밀번호·가장(`as`) 설정이 있으면 거부합니다. k3d가 만드는 기본 kubeconfig는 클라이언트 인증서 형식이므로 쓸 수 없습니다.
- **권한 점검:** `check`와 질문 처리 때 `SelfSubjectRulesReview`로 권한을 확인합니다(`default` 네임스페이스, 질문에 네임스페이스 대상이 있으면 그 네임스페이스도. ClusterRole 권한 포함). 쓰기 동사, Pod 실행·프록시 하위 리소스(`pods/exec` 등, get 포함), secrets 읽기 권한이 보이거나 권한 목록을 확인하지 못하면 `check`는 `not_read_only`로 실패하고, Kubernetes Agent는 API를 조회하지 않습니다. 다른 네임스페이스에만 묶인 Role은 보이지 않으므로, 전용 계정에는 매니페스트의 ClusterRole 외 권한을 주지 않습니다.
- 계정을 설정하지 않으면(`enabled: false`) Kubernetes Agent는 Prometheus의 k8s_cluster 계열 지표로만 동작합니다(#21).
- 설정: `datasources.kubernetes`의 `enabled`, `kubeconfig_env`(기본 `INFRA_AGENT_KUBECONFIG`), `context`(기본 current-context), `timeout_seconds`(기본 15), `max_items`(목록 조회 최대 객체 수, 기본 2000. 넘으면 일부만 본 것으로 표시하고 "해당 없음"을 말하지 않음), `allow_insecure_tls`(기본 false).

**준비 절차 (사용자 작업, 개발 서버 기준)**

1~2단계는 **k3d가 실행 중인 서버**(관리자 kubectl이 있는 곳)에서, 3~4단계는 **프로그램을 실행하는 Windows 호스트**에서 합니다. 서버에 저장소가 없으면 `deploy/rbac/`의 두 파일만 복사하거나 내려받아 실행합니다. Windows에서 복사한 스크립트는 줄바꿈(CRLF)을 `sed -i 's/\r$//' make-reader-config.sh`로 정리합니다.

1. 클러스터 관리자 kubectl이 있는 곳(개발 서버)에서 매니페스트를 적용합니다.
   ```bash
   kubectl apply -f deploy/rbac/infra-agent-reader.yaml
   ```
2. 프로그램이 접속할 API 서버 주소로 토큰 kubeconfig를 만듭니다. Windows 호스트에서 SSH 터널로 접속한다면 터널의 로컬 주소를 씁니다(1.1절). 토큰은 기본 168시간 뒤 만료되므로 만료되면 다시 만듭니다.
   ```bash
   umask 077
   deploy/rbac/make-reader-config.sh https://127.0.0.1:16443 168h > infra-agent-reader.kubeconfig
   ```
   관리자 kubeconfig는 CA 인증서를 읽고 토큰을 발급하는 데만 쓰이며 결과 파일에는 들어가지 않습니다. 결과 파일(`*.kubeconfig`)은 `.gitignore` 대상이며 커밋하지 않습니다.
3. 결과 파일을 프로그램 실행 위치로 옮기고, SSH 터널에 API 서버 포트를 추가합니다(`-L <로컬 포트>:127.0.0.1:<API 서버 포트>`, 원격 주소는 저장소에 기록하지 않음). API 서버 포트는 서버에서 `kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}'`로 확인합니다.
4. 경로를 환경 변수로 지정하고 설정에서 켭니다.
   ```powershell
   $env:INFRA_AGENT_KUBECONFIG = "C:\path\to\infra-agent-reader.kubeconfig"
   # config/local.yaml: datasources.kubernetes.enabled: true
   infra-agent check --config config/local.yaml   # kubernetes: 정상 (버전 ...) 확인
   ```
5. API 서버 인증서에 접속 주소(예: `127.0.0.1`)가 포함되어야 TLS 검증이 됩니다. k3s·k3d 기본 인증서에는 `127.0.0.1`·`localhost`가 들어 있습니다. kubeconfig의 `insecure-skip-tls-verify`는 토큰이 노출될 수 있어 기본으로 거부하며, 개발 환경에서만 `allow_insecure_tls: true`로 허용할 수 있습니다(`check`와 답변 한계에 표시).

**API에서 쓰는 정보 (#35)**

| 조회 | 경로 | 근거 ID | 사용 |
| --- | --- | --- | --- |
| 권한 점검 | `POST /apis/authorization.k8s.io/v1/selfsubjectrulesreviews` | `k8s_api.permissions@current` | 쓰기·secrets 권한 감지 |
| Pod 상태 | `GET /api/v1/pods` (네임스페이스 대상이면 `/api/v1/namespaces/<ns>/pods`) | `k8s_api.pods@current` | Pending 사유, 컨테이너 대기 사유, 구간 내 비정상 종료 사유 |
| Warning 이벤트 | `GET /api/v1/events?fieldSelector=type=Warning` | `k8s_api.events@window` | 대상·사유별 이벤트 (마지막 관측 시각이 분석 구간 안) |

- Kubernetes 이벤트는 API 서버 보관 기간(기본 1시간)이 지나면 사라집니다. 구간 시작이 1시간보다 오래되었으면(긴 구간 또는 과거 구간) 답변 한계에 표시하고 "Warning 이벤트 없음"이라고 말하지 않습니다.
- Pod 상태는 조회 시점의 현재 상태이므로, 분석 구간 끝이 실제 현재 시각에서 `stale_after_seconds`보다 멀면 쓰지 않습니다.
- **개발 서버 live 결과(#35, 2026-10-02):** `check`에서 Kubernetes API 정상(k3s v1.35), `tests/live/test_live_kubernetes.py` 4건 통과.
  - 전용 읽기 계정은 서버에서 `kubectl auth can-i`로 Pod 생성·secrets 조회가 모두 거부됨을 확인했고, 프로그램의 권한 점검도 통과했습니다.
  - SSH 터널의 `127.0.0.1` 주소로 `certificate-authority-data` TLS 검증이 통과했습니다. k3d API 서버 포트는 클러스터를 다시 만들면 바뀔 수 있으므로 터널 설정을 함께 확인합니다.
  - 권한 점검·Pod 목록·Warning 이벤트 조회 3개 모두 오류 없이 실행됐고, Pod 목록은 `max_items` 안에서 잘리지 않았습니다.
  - 평소 상태에서는 Pending·대기·비정상 종료 Pod와 Warning 이벤트가 없었고, 답변도 "해당 대상 없음"·"없음"으로 일치했습니다.
  - **문제 상태 확인(테스트용 네임스페이스에 존재하지 않는 이미지의 Pod를 만들어 확인 후 삭제):** 지표 판정이 Pending Pod·not ready 컨테이너를 경고했고, API가 Pending 사유(컨테이너 대기 ImagePullBackOff와 메시지)를 정보로 덧붙였습니다. 같은 컨테이너의 대기 사실은 따로 만들어지지 않았고(중복 집계 없음), Warning 이벤트는 대상·사유별 누적 횟수와 마지막 시각으로 표시됐습니다. 이때 지표의 Pod phase 값 1이 API의 Pending과 일치함도 확인했습니다.
  - **아직 확인하지 못한 것:** 구간 내 비정상 종료 사유(OOMKilled 등)와 재시작을 반복하는 실행 중 Pod의 대기 사유(CrashLoopBackOff) 표시는 가상 데이터 테스트로만 검증했습니다.
  - 준비 절차의 kubectl 명령은 k3d가 실행 중인 서버에서, 프로그램은 Windows 호스트에서 실행했습니다(서버에 저장소가 없어도 두 파일만 받아 실행 가능).

### 3.6 DB 분석 범위

- 대상: `otel-demo` 네임스페이스의 PostgreSQL 17.6, Valkey 9.0.1(`redis_*`로 수집).
- PostgreSQL 직접 SQL 접속은 초기 필수 조건이 아닙니다.
- 확인되지 않은 항목: MySQL, `pg_stat_statements`, 쿼리 실행 계획, 잠금 대기·잠금 그래프. 이 항목이 필요한 질문에는 한계와 필요한 수집 설정을 답변에 포함합니다.
- **DB Agent live 결과(#25, 2026-09-30):** DB 질문·Service→DB 질문 live 2건 통과. PostgreSQL 연결 사용률 5~7%, 데드락·Valkey 퇴출·거부 0, 롤백 비율 0%, 버퍼 캐시 적중률 100%, accounting 풀 사용 중 연결 0%(상태 라벨로 유휴 연결이 구분되는 것으로 보임).
  - product-catalog `db_client_operation_duration_seconds` p95가 작업 종류와 관계없이 모두 4.75초였고 직전 구간과도 같았습니다. 같은 작업의 spanmetrics DB 호출 지연은 약 0.002초입니다. 4.75초는 0~5초 첫 구간 안을 직선 보간한 값(5초×0.95)과 같아, 히스토그램이 OTel 기본 경계(0, 5, 10, …, 밀리초용)를 초 단위 값에 쓰는 것으로 추정합니다. DB Agent는 이런 값을 판정하지 않고 한계와 버킷 설정 제안으로 답합니다. 1차 수정 후 live에서도 여전히 경고가 나와, 전체 시계열의 경계를 합치면 다른 서비스의 좁은 경계에 가려지는 것으로 보고 서비스별 경계로 판단하도록 바꿨습니다live에서 서비스별 경계를 확인했습니다(`test_db_latency_bucket_bounds`): product-catalog는 0, 5, 10, 25, … 10000(OTel 기본 경계, 15개), accounting은 0.001, 0.005, 0.01, … 10초(9개)였고, spanmetrics는 서비스 모두 0.002~16.384초(14개)입니다. 수정 후 4.75초는 경고 대신 한계로 표시됩니다.
  - product-catalog 커넥션 풀 사용률은 결과가 없었습니다(지표 최신성은 확인됨). 분모를 따로 조회해 최대 열린 연결 수가 0(제한 없음)임을 live에서 확인했습니다.

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

> **구현됨 (#9, DB 항목 보완 #25, Network 워크로드 흐름 #29):** [`config/catalog/otel-demo.yaml`](../config/catalog/otel-demo.yaml) — Prometheus 항목 58개(Server 13, Kubernetes 11, Network 10, DB·캐시 17, Service 7)와 Loki 항목 3개. `verified` 2개(`node.cpu_usage`, `container.cpu_usage`: cores 단위 교차 검증 통과), 나머지는 `discovered`입니다.
> #25에서 DB Agent용으로 바꾼 항목: 커넥션 풀 사용률 2개를 사용 중 연결 기준(상태 값 idle 제외)으로 변경, `db.pool_wait_rate`·`cache.valkey_evictions_rejections`를 구간 증가 수 항목(`db.pool_waits_increase`, `cache.valkey_evicted_increase`, `cache.valkey_rejected_increase`)으로 교체, `db.span_latency_p95` 추가(Service 항목과 같은 조회식), 풀 사용률 분모 확인용 `db.pool_max_open_product_catalog` 추가.
> #27에서 Network Agent용으로 발생 수 항목 5개를 구간 증가량으로 바꿨습니다(`network.drops_increase`, `network.hubble_lost_events_increase`, `network.node_interface_errors_increase`, `network.pod_network_errors_increase`, `network.container_packet_drops_increase`). 로컬 Prometheus 3.5(빈 데이터)에서 전체 Prometheus 항목 조회식 169건을 실행해 문법 오류가 없음을 확인했습니다.

- 항목마다 `target_labels`(대상 종류 → 라벨 이름)를 두어, 에이전트가 대상(네임스페이스·Pod·서비스 등)을 selector로 바꿀 때 사용합니다. 항목이 지원하지 않는 대상은 `selector_for()`가 따로 반환하므로 답변의 한계로 표시해야 합니다.
- 점검 명령:

```powershell
infra-agent catalog                 # 파일 형식 검증
infra-agent catalog --execute       # 각 조회를 Prometheus에 실행: 정상 / 결과 없음 / 지표 없음 / 오류
```

  "결과 없음"은 오류가 아닙니다(예: 재시작이 없으면 `k8s.container_restarts_increase`는 비어 있음). "지표 없음"과 "오류"가 있으면 종료 코드 1을 반환합니다.
- 조회식 문법은 로컬 Prometheus 3.5(빈 데이터)에서 확인했습니다: #9에서 53개 항목 × selector 유무 106건, #25에서 55개 항목 × selector 유무 × 구간(5m·1800s) 166건 모두 오류 없음.
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
| Kubernetes 전용 읽기 계정과 kubeconfig (3.5절 준비 절차) | Kubernetes API 연동 live 확인 (8b) |
| Claude Agent SDK 인증 (`ANTHROPIC_API_KEY` 또는 Claude Code 로그인) | 모델 연동 단계 (6단계) |
| 운영 환경의 모델 데이터 범위 결정 (`llm.data_policy`, 개발 환경은 `full`로 결정) | 운영 데이터를 모델에 전달하기 전 |
