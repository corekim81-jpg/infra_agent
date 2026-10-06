# 개발 가이드

> **상태: 13b단계 (#45) HTTP API 구현, 개발 서버에서 클러스터 내부 상시 실행 확인.** 패키지 구성·설정·스키마·마스킹·테스트·CI(#5), 데이터 소스 클라이언트·탐색·카탈로그 로더(#7), otel-demo 카탈로그(#9), 모델 없이 동작하는 Server Agent와 `ask` 명령(#11), 모델 계층(`llm`, 질문 해석·Server Agent 원인 후보, #15), 실행 계획·제한된 병렬 실행기(#19), Kubernetes Agent(#21), Service Agent와 Loki·Tempo 조회(#23), DB Agent(#25), Network Agent(#27), 분야 간 교차 분석(#29), Kubernetes API 읽기 전용 연동(#35: 전용 읽기 계정 kubeconfig, 권한 점검, Pod 상태·Warning 이벤트, RBAC 매니페스트)이 있습니다.
> Kubernetes API 연동은 개발 서버에서 live 확인했습니다(2026-10-02, 테스트용 Pod로 Pending 사유·Warning 이벤트 표시 확인). 구간 내 비정상 종료 사유 표시는 아직 확인하지 못했습니다. 대표 질문 품질 평가(12b)는 `eval` 명령과 개발 서버 실행 기록과 사람 검토(2026-10-01, 7/7 통과)가 있습니다. 검토에서 나온 후속 항목은 [evaluation.md](evaluation.md) 3절을 봅니다. 실제로 도입된 항목만 "도입됨"으로 표시합니다.
> 시스템 설계는 [architecture.md](architecture.md), 개발 환경·설정은 [environment.md](environment.md), 기능 범위는 [README.md](../README.md)를 기준으로 합니다.

## 1. 개발 환경

| 항목 | 제안 | 상태 |
| --- | --- | --- |
| 개발 실행 위치 | Windows 호스트 + SSH 터널로 개발 서버(OTel Demo, k3d) 연동 ([environment.md](environment.md) 1절) | 결정 |
| 목표 실행 환경 | Rocky Linux 9 계열, 컨테이너, Kubernetes | 미구성 |
| Python | 3.11 이상 (`requires-python >=3.11`). CI: Linux 3.11·3.12, Windows 3.11 | 도입됨 |
| 패키지·가상환경 | `pyproject.toml`(hatchling) + `pip`/`venv` | 도입됨 |
| 린트·포맷 | `ruff` (설정: `pyproject.toml`) | 도입됨 |
| 타입 검사 | `mypy --strict` + pydantic 플러그인 (대상: `src`) | 도입됨 |
| 테스트 | `pytest`, `pytest-asyncio` | 도입됨 |
| HTTP 목 | `respx`, `httpx.MockTransport`(가상 백엔드 `tests/fakes.py`) | 도입됨 |
| CI | GitHub Actions `.github/workflows/ci.yml` | 도입됨 |
| 모델 SDK | `claude-agent-sdk` (1차 어댑터, 선택 의존성 `pip install -e ".[llm]"`, [architecture.md](architecture.md) 10.1절) | 도입됨 (선택, CI는 미설치·가짜 SDK로 검증) |

검증 명령:

```bash
# Linux (Rocky Linux 9)
python3.11 -m venv .venv && source .venv/bin/activate
# Windows (PowerShell)
py -3.11 -m venv .venv; .venv\Scripts\Activate.ps1

python -m pip install -e ".[dev]"
python -m ruff check .
python -m ruff format --check .
python -m mypy                       # 대상: src (pyproject.toml)
python -m pytest -m "not live"       # 단위 테스트 (CI와 동일)

# 개발 서버 연동 테스트 (SSH 터널 필요, INFRA_AGENT_CONFIG에 설정 파일 경로 지정)
INFRA_AGENT_LIVE_TESTS=1 INFRA_AGENT_CONFIG=config/local.yaml python -m pytest -m live   # Linux
$env:INFRA_AGENT_LIVE_TESTS="1"; $env:INFRA_AGENT_CONFIG="config/local.yaml"; python -m pytest -m live  # Windows PowerShell
```

Linux(Python 3.11, 3.12)에서 `live`를 제외한 명령을 실행해 통과를 확인했습니다. Windows 실행은 CI(windows-latest)로 확인합니다. `live` 테스트(`tests/live/`)는 개발 서버 SSH 터널이 필요해 CI와 개발 에이전트 작업 환경에서는 실행하지 않고 사용자 환경에서 실행합니다.
명령이 바뀌면 이 절과 [CLAUDE.md](../CLAUDE.md)의 검증 명령을 함께 갱신합니다.

## 2. 설정과 비밀값

- 연결 주소, 인증정보, 모델 설정은 코드에 하드코딩하지 않습니다.
- 설정은 환경 변수(예: `INFRA_AGENT_` 접두어)와 설정 파일로 읽고, 비밀값은 환경 변수 또는 Kubernetes Secret 마운트로만 받습니다.
- 저장소에는 비밀값 없는 예시 파일(예: `.env.example`, `config/example.yaml`)만 커밋합니다. 실제 `.env`는 `.gitignore`에 포함합니다.
- 로그와 오류 메시지는 출력 전에 비밀값을 마스킹합니다.

## 3. 디렉터리 구조

현재 존재하는 항목: `pyproject.toml`, `config/example.yaml`, `config/catalog/otel-demo.yaml`, `config/eval/questions.yaml`, `src/infra_agent/{config,schemas,security,datasources,discovery,catalog,tools,agents,orchestration,answer,evaluation,llm}`, `cli.py`, `timeutil.py`, `tests/{unit,live}`, `tests/fakes.py`, `tests/expr_prom.py`, `tests/fixtures/synthetic`, `.github/workflows/ci.yml`. 나머지는 계획입니다. (`datasources/`에는 prometheus·loki·tempo와 공통 HTTP·연결 점검만 있습니다.)

```text
infra_agent/
├─ README.md
├─ CLAUDE.md
├─ docs/
│  ├─ architecture.md
│  ├─ development.md
│  └─ evaluation.md             # 대표 질문 평가 기준·실행 기록
├─ pyproject.toml
├─ config/
│  ├─ example.yaml              # 비밀값·내부 주소 없는 설정 예시
│  ├─ catalog/                  # 환경별 조회 카탈로그 (예: otel-demo.yaml)
│  └─ eval/questions.yaml       # 대표 질문 평가 세트
├─ src/infra_agent/
│  ├─ config/                   # 설정 로딩·검증
│  ├─ schemas/                  # AnalysisContext, AgentResult 등 공통 형식
│  ├─ llm/                      # 모델 제공자 어댑터
│  ├─ datasources/              # prometheus, loki, tempo, kubernetes, hubble, mcp
│  ├─ discovery/                # 지표·라벨·최신성 탐색 (카탈로그 후보 생성)
│  ├─ tools/                    # 읽기 전용 도구 정의, 에이전트별 허용 목록
│  ├─ units.py                  # 단위 표시 형식 (답변·모델 관측 데이터 공통)
│  ├─ analysis/                 # 기준 구간 비교, 임계값 판정 등 결정적 분석
│  ├─ agents/                   # base(인터페이스), common(수집·결과 정리), upstream(선행 결과), server, kubernetes, service, db, network
│  │  └─ prompts.py             # 역할별 지침
│  ├─ orchestration/            # 질문 해석, 실행 계획(plan), 실행기(executor), runner
│  ├─ answer/                   # 결과 종합, 분야 간 교차 확인(cross), 답변 렌더링
│  ├─ evaluation/               # 대표 질문 평가: 평가 세트, 결정적 평가 기준, 실행·보고서
│  └─ interfaces/               # cli, (이후) http api
├─ tests/
│  ├─ unit/
│  ├─ contract/                 # 데이터 소스 응답 형식 처리 (가상 응답)
│  ├─ live/                     # 개발 서버 실제 연동 (live 마커, 선택 실행)
│  └─ fixtures/synthetic/       # 테스트용 가상 데이터만
├─ var/                         # 탐색 보고서 등 로컬 산출물 (Git 제외)
└─ deploy/                      # Containerfile, Kubernetes 매니페스트, RBAC
```

## 4. 코딩 규칙

- 계층 경계를 지킵니다: 에이전트는 `tools` 인터페이스만 사용하고 데이터 소스 클라이언트를 직접 호출하지 않습니다. 모델 호출은 `llm` 어댑터를 통해서만 합니다.
- 계산(집계, 비교, 임계값 판정)은 Python 코드로 하고 모델에 맡기지 않습니다.
- 공통 형식은 `schemas`에 정의된 타입만 사용합니다. 에이전트 간 자유 형식 문자열 전달을 피합니다.
- I/O는 `async`로 작성하고 모든 외부 호출에 타임아웃을 지정합니다.
- 도구는 읽기 전용이어야 하며, 쓰기 성격의 API(POST로 상태를 변경하는 호출, Kubernetes 변경 동사)를 추가하지 않습니다.
- 조회된 외부 데이터는 모델 입력에서 데이터로 구분해 전달합니다.
- 공개 함수와 스키마에는 타입 힌트를 작성합니다.

## 5. 테스트와 품질 평가

| 종류 | 목적 | 데이터 | 실행 조건 |
| --- | --- | --- | --- |
| 단위 | 시간 범위 정규화, 에이전트 선택, 판정 로직, 예산·타임아웃·재시도, 마스킹 | 가상 | 항상 |
| 계약 | 데이터 소스 응답 파싱, 빈 결과·오류·지연 데이터 처리 | 가상 응답 fixture | 항상 |
| 개발 서버 연동 | 실제 Prometheus/Loki/Tempo(/Kubernetes) 연결, 가용성 점검, 탐색 | 실제(읽기 전용) | `live` 마커, `INFRA_AGENT_LIVE_TESTS=1`, SSH 터널 실행 중 ([environment.md](environment.md) 5절) |
| 품질 평가 | 대표 질문에 대한 에이전트 선택, 근거 일치, 금지 주장 여부 ([evaluation.md](evaluation.md)) | 가상(데이터 없는 Prometheus, `tests/unit/test_evaluation.py`) 또는 실제 환경(`infra-agent eval`) | 가상은 항상, 실제는 수동 |

### 5.1 가상 데이터와 실제 데이터 구분

- 가상 데이터는 `tests/fixtures/synthetic/`에만 두고 파일 또는 메타데이터에 `synthetic: true`를 명시합니다.
- 실제 운영 데이터(응답 원문, 로그, 호스트 정보)는 저장소에 커밋하지 않습니다.
- 연동 테스트와 품질 평가 결과를 기록할 때는 사용한 데이터가 가상인지 실제인지 표시합니다.

### 5.2 품질 평가 기준

평가 세트, 결정적 평가 기준, 사람 검토 항목, 실행 방법과 기록은 [evaluation.md](evaluation.md)를 기준으로 합니다.

## 6. 단계별 구현 계획

각 단계는 별도 Issue로 관리합니다. 상태는 해당 단계의 PR이 병합되고 완료 조건이 검증된 뒤에만 "완료"로 바꿉니다.

순서 원칙:

1. 환경마다 지표가 다르다는 위험을 먼저 줄이기 위해 **탐색과 조회 카탈로그**를 앞에 둡니다.
2. 에이전트 하나로 질문→조회→답변 흐름을 먼저 완성한 뒤 에이전트를 늘립니다.
3. 모델 없이 동작하는 결정적 분석을 먼저 만들어, 테스트와 기본 동작이 모델·데이터 정책 결정에 묶이지 않게 합니다.

| # | 작업 | 주요 산출물 | 완료 조건 | 선행 | 상태 |
| --- | --- | --- | --- | --- | --- |
| 0 | 개발 기반 문서 | CLAUDE.md, architecture.md, development.md | PR 병합 | – | 완료 (#1, PR #2) |
| 1 | 환경·연동 설계 반영 | environment.md, architecture.md 갱신 | PR 병합 | 0 | 완료 (#3, PR #4) |
| 2 | 프로젝트 골격 | `pyproject.toml`, 설정 로딩(프로필), 공통 스키마, 비밀값 마스킹, pytest·ruff·mypy, GitHub Actions CI | CI에서 `not live` 테스트·린트 통과 | 1 | 완료 (#5, PR #6) |
| 3 | Prometheus 조회와 탐색 | Prometheus 클라이언트, 가용성 점검, `discover` 명령, 카탈로그 로더 | 가상 응답 테스트 통과, 개발 서버 탐색 보고서 생성(live) | 2 | 완료 (#7, PR #8; 개발 서버 탐색·live 4건 통과 2026-09-29) |
| 4 | 조회 카탈로그 v1 (otel-demo) | `config/catalog/otel-demo.yaml` (노드·컨테이너·재시작·hubble·PostgreSQL·커넥션 풀) | 확인 지표의 라벨·단위 검토, `evidence.status` 갱신 | 3 | 완료 (#9, PR #10; 실행 점검 오류 0, live 7건 통과) |
| 5 | Server Agent (모델 없이) | k3d 노드·Pod·컨테이너 CPU·메모리 분석, 기준 구간 비교, 템플릿 답변, CLI | "현재 서버 상태", "30분 전 대비 증가" 질문에 근거·범위·한계 포함 답변 (가상 + live) | 4 | 완료 (#11, PR #12; 개발 서버 `ask` 실행·live 9건 통과 2026-09-29) |
| 6 | 모델 계층 | `LLMClient`, 가짜 모델, Claude Agent SDK 어댑터(내장 도구 비활성 검증), `data_policy` 강제, 질문 해석, 근거 검증 | 단위 테스트 통과, 내장 도구 차단 테스트 통과 | 5 | 완료 (#15, PR #16; 개발 서버 모델 live 3건 통과 2026-09-29. 관측 표시값 보완 #17) |
| 7 | 조정 계층 | 실행 계획 템플릿, 실행기(동시 실행, 타임아웃, 재시도, 예산, 부분 실패) | 가짜 에이전트로 병렬·순차·실패 시나리오 테스트 | 6 | 완료 (#19, PR #20; 개발 서버 live 12건 통과 2026-09-29) |
| 8 | Kubernetes Agent | Prometheus `k8s_*` 기반 재시작·상태 분석, 이벤트(수집 위치 확인 후) | "재시작·Pending Pod" 질문 답변 | 7 | 완료 (#21, PR #22; 개발 서버 live 14건 통과 2026-09-29. 이벤트는 수집 위치 미확인으로 제외, 8b에서 확인) |
| 8b | Kubernetes API 연동 | 읽기 전용 RBAC 매니페스트(`deploy/rbac/`), 토큰 kubeconfig만 허용하는 클라이언트, 권한 점검(`check`·질문 처리), Kubernetes Agent 전용 조회 도구(Pod 상태·Warning 이벤트) | 전용 계정으로만 조회, 쓰기 권한 감지 시 경고 | 8, 사용자 계정 준비 | 완료 (#35; 개발 서버 `check` 정상·live 4건 통과, 테스트용 Pod로 Pending 사유·Warning 이벤트 표시 확인 2026-10-02. 비정상 종료 사유 표시는 미확인) |
| 9 | Loki·Tempo와 Service Agent | 로그·트레이스 클라이언트, 요청량·오류율·지연 분석, 로그·트레이스 연결 | "오류 증가 시간대 로그·트레이스" 질문 답변 | 7 | 완료 (#23, PR #24; 개발 서버 live 4건 통과 2026-09-30, 병합 후 느린 트레이스 검색식 포함 재확인) |
| 10 | DB Agent | PostgreSQL 지표, 앱 커넥션 풀, DB 작업 지연, Tempo DB span(가용 시) | "커넥션 풀 부족·쿼리 지연" 질문 답변, 미확인 항목 한계 표시 | 9 | 완료 (#25, PR #26; 개발 서버 live 3건 통과 2026-09-30) |
| 11 | Network Agent | `hubble_*` 드롭·DNS 분석 | 가용 데이터 기준 답변, 부족 시 수집 설정 제안 | 7 | 완료 (#27, PR #28; 개발 서버 live 2건 통과 2026-09-30) |
| 12 | 분야 간 교차 분석 | Service 이상 대상 추출, Network 집중 확인(서비스 이상 최고 시점 기준), Coordinator 교차 확인(연결·미연결·미확인 구분, 동시 발생 원인 후보) | "네트워크인지 DB인지" 질문에 분야별 연결 결과 답변 (가상 + live) | 8–11 | 완료 (#29, PR #30; 개발 서버 live 3건 통과 2026-10-01) |
| 12b | 대표 질문 품질 평가 | 평가 세트(`config/eval/questions.yaml`), 결정적 평가 기준(`evaluation`), `eval` 명령, [evaluation.md](evaluation.md) | 대표 질문 7개 평가 기록 | 12 | 완료 (#31, PR #32; 개발 서버 평가 모델 사용·미사용 각 7/7 통과, 사람 검토 반영 2026-10-01) |
| 13a | 컨테이너·클러스터 내부 실행 | Containerfile(Rocky Linux 9), in-cluster 인증(ServiceAccount 토큰 재읽기), 설정 ConfigMap·연결 점검 Job·질문 Job 예시 | 클러스터 내부 읽기 전용 실행 확인 | 8b, 12 | 완료 (#39; 개발 서버에서 이미지 빌드, 클러스터 내부 연결 점검·질문 실행 확인 2026-10-02) |
| 13b | HTTP API·상시 실행 | FastAPI 기반 HTTP API(`infra-agent serve`: 토큰 인증, 동시 처리·질문 길이 제한), Deployment·Service | 클러스터 내부에서 질문 요청·응답 | 13a | 완료 (#45; 개발 서버에서 Deployment 배포, 인증 거부·질문 응답 확인 2026-10-06) |
| 13c | MCP 경로 | 데이터 접근 계층의 MCP 구현 | MCP로 같은 조회 수행 | 13a | 계획 |

8·9·11은 서로 독립적이므로 순서를 바꾸거나 병행할 수 있습니다. 실제 조회 데이터를 모델에 전달하는 동작(`data_policy`가 `none`이 아닌 경우)은 개발 환경(OTel Demo)에서만 `full`로 결정되었습니다(2026-09-29). 운영 환경의 정책은 결정 전까지 `none`을 사용합니다.

## 7. Issue·PR 절차

1. 관련 코드·문서와 기존 Issue·PR을 확인합니다. 같은 작업의 Issue가 있으면 재사용하고, 없으면 생성합니다(배경, 요구사항, 작업 범위, 완료 조건, 검증 방법).
2. 기본 브랜치(`main`)에서 작업 브랜치를 만듭니다. 이름 규칙: `<type>/<issue번호>-<요약>` (예: `feat/12-prometheus-client`, `docs/1-dev-foundation-docs`). 실행 환경이 별도 규칙을 요구하면 그 규칙을 따릅니다.
3. 작은 단위로 구현하고 관련 없는 변경을 섞지 않습니다.
4. 변경에 필요한 테스트·정적 검사를 실행합니다. 실행하지 못한 검증은 PR에 명시합니다.
5. 동작이나 사용법이 바뀌면 README.md와 관련 문서를 함께 수정합니다.
6. 커밋 메시지는 `<type>: <요약>` 형식을 권장합니다 (`feat`, `fix`, `docs`, `test`, `refactor`, `chore`).
7. `main`을 대상으로 PR을 만들고 해결한 문제, 주요 변경, 검증 결과, 제한사항을 작성하며 `Closes #번호`로 Issue를 연결합니다. 검증이 끝나지 않았으면 Draft PR로 만들고 차단 사유를 적습니다.
8. CI 결과와 리뷰 피드백을 확인하고 범위 안의 문제를 수정합니다.

`main` 직접 push, force push, PR 병합, 운영 배포, 브랜치 보호 해제는 별도 요청 없이 하지 않습니다.

## 8. 문제 해결

실제로 발생했거나 코드가 구분해 알려 주는 문제만 적습니다.

| 증상 (`check` 출력) | 원인 | 해결 |
| --- | --- | --- |
| `kubernetes: 연결 실패 [missing_credential]` | `INFRA_AGENT_KUBECONFIG`(또는 `kubeconfig_env`)가 설정되지 않음 | 전용 읽기 계정 kubeconfig 경로를 지정 (environment.md 3.5절) |
| `kubernetes: 연결 실패 [kubeconfig_error] … 허용하지 않는 인증 설정(client-certificate-data …)` | 관리자·개인 kubeconfig를 지정함 | `deploy/rbac/make-reader-config.sh`로 만든 토큰 kubeconfig 사용 |
| `kubernetes: 연결 실패 [http_401] 인증 실패` | 토큰 만료 또는 다른 클러스터의 토큰 | kubeconfig를 다시 만듦 |
| `kubernetes: 연결 실패 [http_403] 권한 없음` | ClusterRole·바인딩이 적용되지 않음 | `kubectl apply -f deploy/rbac/infra-agent-reader.yaml` |
| `kubernetes: 응답했지만 사용 불가 [not_read_only]` | 계정에 쓰기·Pod 실행·프록시·secrets 읽기 권한이 있거나 권한 목록을 확인하지 못함 | 전용 읽기 계정 사용. 이 상태에서는 질문 처리 때도 API를 조회하지 않음 |
| `kubernetes: 연결 실패 [kubeconfig_error] … insecure-skip-tls-verify` | kubeconfig가 서버 인증서 검증을 끔 | `certificate-authority-data` 사용 (`make-reader-config.sh`가 넣어 줌). 개발 환경에서만 `allow_insecure_tls: true` |
| `kubernetes: 연결 실패 [connect_error]` (TLS 오류 포함) | 터널 미실행, 주소 오류, 또는 서버 인증서에 접속 주소가 없음 | 터널·포트 확인, 인증서에 포함된 주소(`127.0.0.1`·`localhost` 등)로 접속 |
| `kubernetes: 연결 실패 [missing_credential] 클러스터 내부 실행이 아닙니다` | `auth: in_cluster`를 클러스터 밖에서 사용 | 클러스터 밖에서는 `auth: kubeconfig`(기본값) |
| `kubernetes: 연결 실패 [missing_credential] ServiceAccount 토큰을 읽지 못했습니다` | Pod에 토큰이 마운트되지 않음 | Pod의 `serviceAccountName`, `automountServiceAccountToken: true` 확인 |
| 클러스터 내부 실행에서 Prometheus·Loki·Tempo가 `[timeout]`, kubernetes만 정상 | 데이터 소스 네임스페이스의 NetworkPolicy가 infra-agent 네임스페이스의 접속을 차단 (기본 차단 정책) | `kubectl get networkpolicy -A`로 확인 후 조회 포트 허용 정책 추가 (`deploy/k8s/networkpolicy-example.yaml`) |
| port-forward 후 API가 404 등 엉뚱한 응답 | 로컬 포트를 다른 프로그램이 이미 사용 (port-forward 실패) | 비어 있는 로컬 포트 사용, `/healthz`가 `{"status":"ok"}`인지 먼저 확인 |
| Prometheus·Loki·Tempo `연결 실패 [connect_error]` + SSH 터널 힌트 | 터널 미실행, WSL·Docker에서 실행 | environment.md 1.1절 |
