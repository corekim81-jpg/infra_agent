# 환경 및 설정 설계

> **상태: 일부 구현.** 설정 로딩(2절, #5), 조회 카탈로그 로더·탐색 명령·`live` 테스트(4·5절, #7)가 구현되었습니다. 실제 카탈로그 파일(`config/catalog/otel-demo.yaml`)은 아직 없고, 개발 서버에서 탐색·`live` 테스트를 실행한 결과도 아직 없습니다.
> 이 문서의 지표 목록은 **사용자가 개발 환경에서 조회를 확인한 지표 이름**이며, 라벨 구조·단위·의미·수집 범위는 탐색 단계에서 검증해야 합니다.
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

### 2.4 환경 변수

| 변수 | 용도 | 비밀 여부 | 상태 |
| --- | --- | --- | --- |
| `INFRA_AGENT_CONFIG` | 설정 파일 경로 | 아니오 | 구현됨 |
| `INFRA_AGENT_PROFILE` | 프로필 덮어쓰기 | 아니오 | 구현됨 |
| `INFRA_AGENT__<SECTION>__<KEY>` | 설정 항목 덮어쓰기. `__`로 중첩 구분, 대소문자 무관, 값은 YAML 스칼라로 해석(`true`, `15`, `null`). 예: `INFRA_AGENT__DATASOURCES__PROMETHEUS__URL` | 아니오 | 구현됨 |
| `token_env`로 지정한 변수 (예: `INFRA_AGENT_PROMETHEUS_TOKEN`) | 데이터 소스 인증이 필요해질 경우의 토큰 | 예 | 구현됨 (Bearer 헤더, 값은 마스킹 등록) |
| `INFRA_AGENT_KUBECONFIG` (`kubeconfig_env` 기본값) | 전용 읽기 계정 kubeconfig 파일 경로 | 파일 내용은 비밀 | 설정 필드만 구현 |
| `ANTHROPIC_API_KEY` | Claude Agent SDK 인증 (SDK가 직접 읽음, 프로그램은 값을 읽거나 기록하지 않음) | 예 | 6단계 |
| `INFRA_AGENT_LIVE_TESTS` | `1`일 때만 `live` 테스트 실행 | 아니오 | 구현됨 (`tests/conftest.py`) |

## 3. 데이터 소스별 초기 사용 범위

### 3.1 Prometheus — 사용자 확인 지표

아래는 조회가 확인된 지표 이름입니다. **라벨 이름·단위·집계 범위는 탐색 전까지 미검증**입니다.
OpenTelemetry 지표는 Prometheus로 변환될 때 단위 접미사나 라벨 이름이 바뀔 수 있으므로, 카탈로그는 탐색 결과로 확정합니다.

| 분야 | 지표 | 담당 에이전트 | 비고 |
| --- | --- | --- | --- |
| 노드 자원 | `k8s_node_cpu_usage`, `k8s_node_memory_working_set_bytes` | Server | k3d 노드 기준. 물리 서버 전체 값 아님 |
| 컨테이너 자원 | `container_memory_working_set_bytes` | Server | cAdvisor 계열로 추정, 출처 라벨 검증 필요 |
| 워크로드 상태 | `k8s_container_restarts` | Kubernetes | |
| 네트워크 | `hubble_drop_total`, `hubble_dns_queries_total` | Network | 드롭 사유·DNS 응답 코드 라벨 검증 필요 |
| PostgreSQL | `postgresql_backends`, `postgresql_connection_max`, `postgresql_deadlocks_total`, `postgresql_db_size_bytes` | DB | 별도 postgres_exporter 워크로드는 확인되지 않음 (수집 경로 검증 필요) |
| 앱 커넥션 풀 (accounting) | `db_client_connection_count`, `db_client_connection_max` | DB | |
| 앱 커넥션 풀 (product-catalog) | `db_sql_connection_open`, `db_sql_connection_max_open`, `db_sql_connection_wait_total` | DB | |
| DB 작업 지연 | `db_client_operation_duration_seconds_bucket` | DB | 히스토그램, 분위수 계산 |
| 호스트 계열 | `system_*` | (보류) | 수집 범위가 검증되기 전에는 물리 서버 전체 값으로 해석하지 않음 |

서비스 요청량·오류율·응답 시간 지표(OTel Demo의 HTTP/RPC 지표, spanmetrics 등)는 아직 확인 목록에 없으므로 탐색 단계에서 확인합니다.

### 3.2 Loki, Tempo

- Loki: 애플리케이션 로그. Kubernetes 이벤트가 OTel objects/events 수집을 통해 Loki에 저장되는지는 **검증 필요**입니다.
- Tempo: OTel Demo 서비스 트레이스. DB span(`db.system` 등 속성) 존재 여부와 속성 이름은 **검증 필요**입니다.

### 3.3 Kubernetes API

- 전용 ServiceAccount와 읽기 전용 ClusterRole(`get`, `list`, `watch`; `secrets` 제외)을 만든 뒤, 그 계정의 kubeconfig로만 연결합니다.
- **관리자 kubeconfig는 프로그램에 사용하지 않습니다.** 프로그램 시작 시 권한 점검(`SelfSubjectRulesReview` 등)으로 쓰기 권한이 있으면 경고하는 방안을 구현 단계에서 검토합니다.
- 읽기 계정 준비 전에는 Kubernetes Agent가 Prometheus의 k8s_cluster 계열 지표와 (확인된다면) Loki의 이벤트 로그로 동작합니다.

### 3.4 DB

- 초기 분석 대상: `otel-demo` 네임스페이스의 PostgreSQL 17.6.
- Valkey 9.0.1도 실행 중이나 **Valkey 지표 수집 여부는 미확인**입니다. 확인 전까지 분석 대상에서 제외하고 "확인 불가"로 표시합니다.
- PostgreSQL 직접 SQL 접속은 초기 필수 조건이 아닙니다.
- 미확인: MySQL, `pg_stat_statements`, 쿼리 실행 계획, 상세 잠금 그래프. 이 항목이 필요한 질문에는 한계와 필요한 수집 설정을 답변에 포함합니다.

## 4. 조회 카탈로그

### 4.1 목적

에이전트 코드에 PromQL·LogQL·TraceQL과 지표 이름을 고정하지 않고, **환경별 카탈로그 파일**에서 분석 항목별 조회식을 가져옵니다.

### 4.2 형식

> **구현됨 (#7):** 로더와 검증은 `src/infra_agent/catalog/models.py`입니다. 자리표시자는 `labels`의 키와 실행 시 값 `{selector}`, `{range}`만 허용하며, `selector`에 중괄호가 들어가면 거부합니다.

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

> **구현됨 (#7):** `src/infra_agent/discovery/`, 명령 `infra-agent discover`. 실제 개발 서버에서의 실행 결과는 아직 없습니다.

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

## 5. 테스트 구분

| 구분 | 데이터 | 실행 위치 | 조건 |
| --- | --- | --- | --- |
| 단위·계약 | 가상 응답(`tests/fixtures/synthetic/`) | CI, 로컬 | 항상 |
| 개발 서버 연동 (`live` 마커) | 개발 서버 실제 데이터(읽기 전용) | Windows 호스트 + 터널 | `INFRA_AGENT_LIVE_TESTS=1`, `dev-tunnel` 프로필 |

- CI는 `pytest -m "not live"`만 실행합니다.
- `live` 테스트: `tests/live/test_live_sources.py` (데이터 소스 준비 상태, Prometheus 기본 조회, 확인 지표 존재, 탐색 스모크). `INFRA_AGENT_CONFIG`가 없으면 건너뜁니다.
- 연동 테스트는 특정 값을 기대하지 않고 형식·존재·최신성을 검증합니다. 결과 원문은 저장소에 남기지 않습니다.
- 가상 fixture는 탐색 결과의 **구조**(지표·라벨 이름)를 본떠 만들되, 값은 가상임을 표시합니다(`synthetic: true`).

## 6. 사용자 측 준비 사항

| 항목 | 필요한 시점 |
| --- | --- |
| SSH 터널 실행 (1.1절 포트) | 연동 테스트, 탐색 |
| Kubernetes 전용 읽기 계정과 kubeconfig | Kubernetes API 연동 단계 |
| Claude Agent SDK 인증 (`ANTHROPIC_API_KEY`) | 모델 연동 단계 |
| 모델로 보낼 데이터 범위 결정 (`llm.data_policy`) | 실제 조회 데이터를 모델에 전달하기 전 |
