# 개발 가이드

> **상태: 4단계 진행 중.** 패키지 구성, 설정 로딩, 공통 스키마, 비밀값 마스킹, 테스트·CI(#5), 읽기 전용 데이터 소스 클라이언트·연결 점검·지표 탐색·카탈로그 로더(#7), otel-demo 조회 카탈로그·점검 명령(#9)이 있습니다.
> 에이전트·모델 연동·질문 응답은 아직 없습니다. 실제로 도입된 항목만 "도입됨"으로 표시합니다.
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
| 모델 SDK | `claude-agent-sdk` (1차 어댑터, [architecture.md](architecture.md) 10.1절) | 미도입 |

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

# 개발 서버 연동 테스트 (SSH 터널 필요, 3단계부터 테스트 추가 예정)
INFRA_AGENT_LIVE_TESTS=1 python -m pytest -m live        # Linux
$env:INFRA_AGENT_LIVE_TESTS="1"; python -m pytest -m live  # Windows PowerShell
```

Linux(Python 3.11, 3.12)에서 `live`를 제외한 명령을 실행해 통과를 확인했습니다. Windows 실행은 CI(windows-latest)로 확인합니다. 현재 `live` 테스트는 없습니다.
명령이 바뀌면 이 절과 [CLAUDE.md](../CLAUDE.md)의 검증 명령을 함께 갱신합니다.

## 2. 설정과 비밀값

- 연결 주소, 인증정보, 모델 설정은 코드에 하드코딩하지 않습니다.
- 설정은 환경 변수(예: `INFRA_AGENT_` 접두어)와 설정 파일로 읽고, 비밀값은 환경 변수 또는 Kubernetes Secret 마운트로만 받습니다.
- 저장소에는 비밀값 없는 예시 파일(예: `.env.example`, `config/example.yaml`)만 커밋합니다. 실제 `.env`는 `.gitignore`에 포함합니다.
- 로그와 오류 메시지는 출력 전에 비밀값을 마스킹합니다.

## 3. 디렉터리 구조

현재 존재하는 항목: `pyproject.toml`, `config/example.yaml`, `config/catalog/otel-demo.yaml`, `src/infra_agent/{config,schemas,security,datasources,discovery,catalog}`, `cli.py`, `timeutil.py`, `tests/{unit,live}`, `tests/fakes.py`, `tests/fixtures/synthetic`, `.github/workflows/ci.yml`. 나머지는 계획입니다. (`datasources/`에는 prometheus·loki·tempo와 공통 HTTP·연결 점검만 있습니다.)

```text
infra_agent/
├─ README.md
├─ CLAUDE.md
├─ docs/
│  ├─ architecture.md
│  └─ development.md
├─ pyproject.toml
├─ config/
│  ├─ example.yaml              # 비밀값·내부 주소 없는 설정 예시
│  └─ catalog/                  # 환경별 조회 카탈로그 (예: otel-demo.yaml)
├─ src/infra_agent/
│  ├─ config/                   # 설정 로딩·검증
│  ├─ schemas/                  # AnalysisContext, AgentResult 등 공통 형식
│  ├─ llm/                      # 모델 제공자 어댑터
│  ├─ datasources/              # prometheus, loki, tempo, kubernetes, hubble, mcp
│  ├─ discovery/                # 지표·라벨·최신성 탐색 (카탈로그 후보 생성)
│  ├─ tools/                    # 읽기 전용 도구 정의, 에이전트별 허용 목록
│  ├─ analysis/                 # 기준 구간 비교, 임계값 판정 등 결정적 분석
│  ├─ agents/                   # coordinator, server, network, db, service, kubernetes
│  │  └─ prompts/               # 역할별 지침
│  ├─ orchestration/            # 실행 계획(DAG), 실행기, 예산·타임아웃
│  ├─ answer/                   # 결과 종합, 근거 검증, 답변 렌더링
│  └─ interfaces/               # cli, (이후) http api
├─ tests/
│  ├─ unit/
│  ├─ contract/                 # 데이터 소스 응답 형식 처리 (가상 응답)
│  ├─ live/                     # 개발 서버 실제 연동 (live 마커, 선택 실행)
│  ├─ eval/                     # 대표 운영 질문 답변 품질 평가
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
| 품질 평가 | 대표 질문에 대한 에이전트 선택, 근거 일치, 금지 주장 여부 | 가상 시나리오 또는 실제 환경 | 수동 또는 별도 작업 |

### 5.1 가상 데이터와 실제 데이터 구분

- 가상 데이터는 `tests/fixtures/synthetic/`에만 두고 파일 또는 메타데이터에 `synthetic: true`를 명시합니다.
- 실제 운영 데이터(응답 원문, 로그, 호스트 정보)는 저장소에 커밋하지 않습니다.
- 연동 테스트와 품질 평가 결과를 기록할 때는 사용한 데이터가 가상인지 실제인지 표시합니다.

### 5.2 품질 평가 기준 (초안)

README.md의 대표 질문마다 다음을 확인합니다.

- 기대한 에이전트만 선택되었는가 (불필요한 확장 여부)
- 답변의 수치와 주장이 실제 조회 결과(근거 ID)와 일치하는가
- 데이터가 없거나 오래된 경우 "정상"이라고 답하지 않는가
- 동시 발생을 인과관계로 단정하지 않는가
- 시간 범위와 대상이 답변에 명시되고 조회에 반영되었는가
- 부분 실패 시 확인하지 못한 영역을 표시하는가
- 비밀값이 답변·로그에 없는가

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
| 4 | 조회 카탈로그 v1 (otel-demo) | `config/catalog/otel-demo.yaml` (노드·컨테이너·재시작·hubble·PostgreSQL·커넥션 풀) | 확인 지표의 라벨·단위 검토, `evidence.status` 갱신 | 3 | 진행 중 (#9) |
| 5 | Server Agent (모델 없이) | k3d 노드·Pod·컨테이너 CPU·메모리 분석, 기준 구간 비교, 템플릿 답변, CLI | "현재 서버 상태", "30분 전 대비 증가" 질문에 근거·범위·한계 포함 답변 (가상 + live) | 4 | 계획 |
| 6 | 모델 계층 | `LLMClient`, 가짜 모델, Claude Agent SDK 어댑터(내장 도구 비활성 검증), `data_policy` 강제, 질문 해석, 근거 검증 | 단위 테스트 통과, 내장 도구 차단 테스트 통과 | 5 | 계획 |
| 7 | 조정 계층 | 실행 계획 템플릿, 실행기(동시 실행, 타임아웃, 재시도, 예산, 부분 실패) | 가짜 에이전트로 병렬·순차·실패 시나리오 테스트 | 6 | 계획 |
| 8 | Kubernetes Agent | Prometheus `k8s_*` 기반 재시작·상태 분석, 이벤트(수집 위치 확인 후) | "재시작·Pending Pod" 질문 답변 | 7 | 계획 |
| 8b | Kubernetes API 연동 | 읽기 전용 RBAC 매니페스트, 권한 점검, `k8s_*` 도구 | 전용 계정으로만 조회, 쓰기 권한 감지 시 경고 | 8, 사용자 계정 준비 | 계획 |
| 9 | Loki·Tempo와 Service Agent | 로그·트레이스 클라이언트, 요청량·오류율·지연 분석, 로그·트레이스 연결 | "오류 증가 시간대 로그·트레이스" 질문 답변 | 7 | 계획 |
| 10 | DB Agent | PostgreSQL 지표, 앱 커넥션 풀, DB 작업 지연, Tempo DB span(가용 시) | "커넥션 풀 부족·쿼리 지연" 질문 답변, 미확인 항목 한계 표시 | 9 | 계획 |
| 11 | Network Agent | `hubble_*` 드롭·DNS 분석 | 가용 데이터 기준 답변, 부족 시 수집 설정 제안 | 7 | 계획 |
| 12 | 교차 분석과 품질 평가 | Service → (Network ∥ DB) 흐름, 결과 종합, README 대표 질문 평가 세트 | 대표 질문 7개 평가 기록 | 8–11 | 계획 |
| 13 | 배포·확장 | Containerfile(Rocky Linux 9), Kubernetes 배포·RBAC, MCP 경로, HTTP API | 클러스터 내부 읽기 전용 실행 확인 | 12 | 계획 |

8·9·11은 서로 독립적이므로 순서를 바꾸거나 병행할 수 있습니다. 실제 조회 데이터를 모델에 전달하는 동작(`data_policy`가 `none`이 아닌 경우)은 데이터 정책이 결정된 뒤에만 사용합니다.

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

구현 전이므로 아직 항목이 없습니다. 데이터 소스 연결 오류, 인증 오류, 데이터 지연 등 실제로 발생한 문제와 해결 방법을 구현 단계에서 추가합니다.
