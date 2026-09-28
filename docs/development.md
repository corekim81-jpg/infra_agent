# 개발 가이드

> **상태: 초기 설정 전.** 현재 저장소에는 문서만 있으며 애플리케이션 코드, 의존성 정의, 테스트, CI가 없습니다.
> 아래의 도구·명령·디렉터리는 **제안**이며, 실제로 도입된 항목만 "도입됨"으로 표시합니다.
> 시스템 설계는 [architecture.md](architecture.md), 기능 범위는 [README.md](../README.md)를 기준으로 합니다.

## 1. 개발 환경

| 항목 | 제안 | 상태 |
| --- | --- | --- |
| 목표 실행 환경 | Rocky Linux 9 계열, 컨테이너, Kubernetes | 미구성 |
| Python | 3.11 이상 (Rocky Linux 9 AppStream의 `python3.11`/`python3.12` 사용 가능) | 미확정 |
| 패키지·가상환경 | `pyproject.toml` + `uv` 또는 `pip`/`venv` | 미도입 |
| 린트·포맷 | `ruff` | 미도입 |
| 타입 검사 | `mypy` | 미도입 |
| 테스트 | `pytest`, `pytest-asyncio` | 미도입 |

도입 후 예상 명령(현재 실행 불가):

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
ruff check . && ruff format --check .
mypy src
pytest -m "not integration"          # 단위·계약 테스트
pytest -m integration                # 실제 데이터 소스 연결 필요
```

위 명령이 실제로 동작하게 되면 이 절과 [CLAUDE.md](../CLAUDE.md)의 검증 명령을 함께 갱신합니다.

## 2. 설정과 비밀값

- 연결 주소, 인증정보, 모델 설정은 코드에 하드코딩하지 않습니다.
- 설정은 환경 변수(예: `INFRA_AGENT_` 접두어)와 설정 파일로 읽고, 비밀값은 환경 변수 또는 Kubernetes Secret 마운트로만 받습니다.
- 저장소에는 비밀값 없는 예시 파일(예: `.env.example`, `config/example.yaml`)만 커밋합니다. 실제 `.env`는 `.gitignore`에 포함합니다.
- 로그와 오류 메시지는 출력 전에 비밀값을 마스킹합니다.

## 3. 디렉터리 구조 (제안)

```text
infra_agent/
├─ README.md
├─ CLAUDE.md
├─ docs/
│  ├─ architecture.md
│  └─ development.md
├─ pyproject.toml
├─ config/
│  ├─ example.yaml              # 비밀값 없는 설정 예시
│  └─ query_catalog/            # 분석 항목별 PromQL/LogQL/TraceQL 후보
├─ src/infra_agent/
│  ├─ config/                   # 설정 로딩·검증
│  ├─ schemas/                  # AnalysisContext, AgentResult 등 공통 형식
│  ├─ llm/                      # 모델 제공자 어댑터
│  ├─ datasources/              # prometheus, loki, tempo, kubernetes, hubble, mcp
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
│  ├─ integration/              # 실제 데이터 소스 연결 (선택 실행)
│  ├─ eval/                     # 대표 운영 질문 답변 품질 평가
│  └─ fixtures/synthetic/       # 테스트용 가상 데이터만
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
| 연동 | 실제 Prometheus/Loki/Tempo/Kubernetes 연결, 가용성 점검 | 실제(읽기 전용) | `integration` 마커, 연결 설정이 있을 때만 |
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

각 단계는 별도 Issue로 관리합니다. 체크 표시는 해당 단계의 PR이 병합되고 검증이 끝난 뒤에만 합니다.

| 단계 | 내용 | 완료 조건 | 상태 |
| --- | --- | --- | --- |
| 0 | 개발 기반 문서 (이 문서, architecture, CLAUDE.md) | 문서 PR 병합 | 진행 중 |
| 1 | 프로젝트 골격: `pyproject.toml`, 설정 로딩, 공통 스키마, 린트·테스트 설정, 모델·조정 방식 결정 | 단위 테스트 실행 가능 | 계획 |
| 2 | Prometheus 읽기 전용 조회와 가용성 점검 | 계약 테스트 + 연동 테스트(환경 있을 때) | 계획 |
| 3 | 단일 에이전트(Server) 질문→조회→답변 흐름, CLI | "현재 서버 상태" 질문 end-to-end | 계획 |
| 4 | Coordinator와 실행기(병렬·순차, 타임아웃, 재시도, 예산, 부분 실패) | 실행 제어 단위 테스트 | 계획 |
| 5 | Kubernetes·Service(Loki/Tempo) 에이전트 | 대표 질문 평가 | 계획 |
| 6 | Network·DB 에이전트 (가용 데이터 기준, 부족 시 수집 설정 제안) | 대표 질문 평가 | 계획 |
| 7 | 근거 검증·답변 형식 고도화, 품질 평가 세트 | 평가 기준 충족 | 계획 |
| 8 | 컨테이너 이미지, Kubernetes 배포·RBAC, 운영 문서 | Rocky Linux 9 기반 이미지 실행 확인 | 계획 |

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
