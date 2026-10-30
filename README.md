# OSSGuard Backend

Python 프로그램의 의존성을 업데이트했을 때 **업무 결과가 그대로 유지되는지** 확인하는 검증 서버입니다. 같은 소스 커밋의 기존 버전 A와 후보 버전 B를 Docker에서 빌드·실행하고, HTTP 응답과 외부 API 호출을 승인된 기대값에 맞춰 비교합니다.

원격 저장소나 운영 서버에 업데이트를 적용하지 않습니다. 현재는 팀이 검토한 공개 GitHub 저장소의 Python/uv 프로젝트를 지원하는 해커톤 MVP이며, Node-RED 전용 실행 기능이나 사용자별 계정·권한 체계는 포함하지 않습니다.

## 목차

- [구성과 역할](#구성과-역할)
- [준비할 도구](#준비할-도구)
- [빠른 시작](#빠른-시작)
- [환경변수](#환경변수)
- [검증할 프로그램 준비](#검증할-프로그램-준비)
- [사용자 실행 흐름과 API](#사용자-실행-흐름과-api)
- [실제 검증과 상태](#실제-검증과-상태)
- [AI, 이메일, 비밀값](#ai-이메일-비밀값)
- [테스트](#테스트)
- [GCP 배치](#gcp-배치)
- [문제 해결](#문제-해결)

## 구성과 역할

| 구성 요소 | 역할 |
| --- | --- |
| FastAPI | 팀 토큰 인증, 프로젝트·설정·테스트·실행 접수 및 조회 |
| PostgreSQL + SQLAlchemy | 프로젝트, 승인 테스트, 작업 큐, 실행 스냅샷과 증거 저장 |
| Alembic | DB 테이블 생성과 스키마 변경 |
| Runner | Git 소스 수집, 의존성 변경, Docker A/B 실행, 결과 판정 및 자원 정리 |
| 결과 Worker | 저장된 결과의 AI 설명과 이메일 처리 |
| WireMock | 실제 배송·결제 대신 검증용 HTTP 응답과 호출 기록 제공 |

```text
프론트엔드 / Swagger → API → PostgreSQL
                               ↑   ↑
                          Runner   결과 Worker
                             ↓       ↓
                      A/B 앱 + Mock  AI·이메일
```

Runner와 결과 Worker는 DB의 작업을 읽습니다. API만 실행하면 검증 작업은 접수되지만 실행되지 않습니다. Redis나 Celery는 사용하지 않습니다.

주요 소스는 다음과 같습니다.

```text
ossguard/
  api.py              HTTP API와 인증
  schemas.py          입력·출력 모델과 검증
  config.py           환경변수 설정
  db.py               DB 모델과 연결
  repository.py       공개 저장소 검사와 실행 규격 확인
  runner.py           검증 작업 처리
  docker_runner.py    컨테이너·네트워크 관리
  updater.py          의존성 업데이트와 변경 내역
  validator.py        HTTP·JSON·외부 호출 검증과 판정
  evidence.py         증거 크기 제한과 비밀값 마스킹
  ai.py               AI 초안과 원인 후보 설명
  result_worker.py    결과 분석과 이메일 처리
  secrets.py          GCP Secret Manager 참조 조회
migrations/           Alembic 마이그레이션
examples/purchase-app/ 주문·출고 시연 앱과 테스트
templates/          대상 앱의 표준 Dockerfile
tests/              단위·통합 테스트
```

## 준비할 도구

- **Python 3.12.14**, **uv 0.9.26**, Git
- **Docker + Buildx**: Linux 컨테이너를 실행할 수 있어야 합니다. Windows에서는 Docker Desktop의 Linux 컨테이너 모드를 사용합니다.
- PostgreSQL: 로컬에서는 포함된 Compose가 **PostgreSQL 17.6**을 실행하므로 별도 설치가 필요하지 않습니다.
- GitHub·PyPI·컨테이너 레지스트리와 통신할 수 있는 네트워크

Python 3.12.14와 uv 0.9.26은 CI와 대상 앱 Docker 템플릿에 맞춘 버전입니다. Python 패키지 의존성은 `uv.lock`으로 고정합니다.

```powershell
uv --version
git --version
docker version
docker buildx version
```

## 빠른 시작

아래는 **DB만 Docker로 띄우고 API·Runner·결과 Worker를 로컬 터미널에서 실행**하는 방법입니다. 모든 명령은 백엔드 저장소 루트에서 실행합니다. 이 프로젝트를 상위 폴더와 함께 받은 경우 먼저 `cd backend`로 이동하세요.

### 1. 의존성 설치와 환경 파일 준비

```powershell
uv python install 3.12.14
uv sync --locked --python 3.12.14
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

`.env`를 열어 `OSSGUARD_API_TOKEN`의 예시값을 자신만의 긴 임의 토큰으로 바꿉니다. 이 값은 프론트엔드의 **서버 접근 키**로 입력합니다. 계정 비밀번호나 AI API 키와는 다른 값입니다.

`OSSGUARD_ALLOWED_REPOS`에는 공개 [주문·출고 샘플](https://github.com/cbu-hackathon-m-team/OSSGuard-Purchase-Demo)이 들어 있습니다. 다른 저장소는 검토 후 JSON 배열에 추가하세요. 빈 목록 `[]`이면 실행 대상 등록이 차단됩니다.

처음에는 AI·이메일 키 없이 시작해도 됩니다. 저장소 테스트를 검토·승인하거나 수동 테스트를 작성해 실제 검증을 실행할 수 있습니다.

### 2. DB와 테이블 준비

```powershell
docker compose up -d db
docker compose ps db
uv run alembic upgrade head
```

DB의 기본 주소는 `127.0.0.1:54329`입니다. `POSTGRES_PASSWORD`를 변경했다면 로컬 프로세스가 사용하는 `OSSGUARD_DATABASE_URL`의 비밀번호도 맞춰야 합니다.

### 3. 세 프로세스 시작

터미널을 세 개 열고 각각 같은 백엔드 폴더에서 실행합니다.

**터미널 1 — API**

```powershell
uv run uvicorn ossguard.api:app --host 127.0.0.1 --port 8000
```

**터미널 2 — Runner**

```powershell
uv run python -m ossguard.runner
```

**터미널 3 — 결과 Worker**

```powershell
uv run python -m ossguard.result_worker
```

세 프로세스는 같은 DB를 사용해야 합니다. Runner와 결과 Worker는 중복 실행을 잠금으로 막습니다. 이미 실행 중인 프로세스가 있다면 추가로 켜지 마세요.

### 4. 연결 확인과 첫 검증

다른 터미널에서 다음 명령을 실행합니다.

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

정상이면 `status: ok`를 반환합니다. 이 요청은 DB 연결도 확인합니다.

1. [Swagger](http://127.0.0.1:8000/docs)를 열고 **Authorize**에 `.env`의 팀 토큰 값을 입력합니다. 입력란에는 `Bearer ` 접두어 없이 토큰만 넣습니다.
2. 프론트엔드를 사용할 경우 해당 저장소에서 `pnpm dev`를 실행하고 [서버 연결 설정](http://localhost:3000/dashboard/settings#server-connection)에 같은 토큰을 입력합니다. [프론트엔드 README](https://github.com/cbu-hackathon-m-team/Frontend/blob/main/README.md)를 참고하세요.
3. 공개 주문 샘플을 등록·검사하고 `pydantic` 목표 버전 `2.12.5`를 선택합니다.
4. 실행 설정과 가져온 테스트를 확인해 저장·승인한 뒤 검증을 시작합니다. `SHIPPING_BASE_URL`은 Runner가 모의 API 주소로 주입합니다.
5. 이 샘플은 호환성 차이를 재현하도록 만들어졌습니다. 정상 주문의 배송 호출이 1회에서 0회로 줄어드는 회귀가 예상됩니다. `1.10.24`를 목표로 선택하면 같은 버전의 대조 실행을 할 수 있습니다.

`/health`와 API 설명 외 데이터 API에는 Bearer 인증이 필요합니다. 프론트엔드 미리보기 로그인만으로 API 인증이 되지는 않습니다.

### 다른 실행 방식: API·결과 Worker도 Compose로 실행

로컬 Python API와 결과 Worker를 대신해 다음 명령을 사용할 수 있습니다.

```powershell
docker compose up -d --build
docker compose ps
```

Compose는 DB 준비 → 마이그레이션 → API·결과 Worker 순서로 시작합니다. **Runner는 Compose에 포함되지 않으므로 호스트 터미널에서 별도로 실행**해야 합니다. 호스트 Runner용 `uv sync`와 `.env`도 필요합니다. API 컨테이너에는 Docker 소켓을 연결하지 않습니다.

호스트 API와 Compose API를 동시에 켜면 포트 8000이 충돌합니다. 위 두 방식 중 하나를 선택하세요.

로컬 프로세스는 각 터미널에서 `Ctrl+C`로 종료합니다. Compose는 다음 명령으로 종료합니다.

```powershell
docker compose down
```

기본 DB 볼륨 `ossguard_pgdata`의 데이터는 남습니다. `docker compose down -v`는 데이터를 삭제하므로 초기화가 필요한 경우에만 사용하세요.

## 환경변수

설정은 [ossguard/config.py](ossguard/config.py)에서 읽습니다. 각 프로세스는 작업 디렉터리의 `.env`를 읽으며 OS 환경변수가 우선합니다. 설정 파일을 바꾼 뒤 해당 프로세스를 다시 시작하세요. 예시값은 [.env.example](.env.example)을 참고합니다.

| 변수 | 설명 |
| --- | --- |
| `OSSGUARD_DATABASE_URL` | `postgresql+asyncpg://...` 형식의 DB 주소. API·Runner·결과 Worker가 같은 DB를 사용해야 함 |
| `OSSGUARD_API_TOKEN` | API 데이터 요청을 인증하는 팀 토큰. 빈 값이면 인증이 필요한 API가 503을 반환 |
| `OSSGUARD_ALLOWED_REPOS` | 실행을 허용할 공개 GitHub URL의 JSON 배열 |
| `OSSGUARD_CORS_ORIGINS` | 브라우저 요청을 허용할 origin의 JSON 배열. 기본 예시는 `localhost:3000`, `localhost:5173` |
| `OSSGUARD_RUNNER_ID` | Runner의 고정 ID. 기본 `runner-1` |
| `OSSGUARD_WORK_DIR` | 임시 소스와 잠금 파일을 두는 작업 폴더. 기본 `.work` |
| `OSSGUARD_GITHUB_TOKEN` | 선택 사항. 플랫폼의 GitHub API 요청에 사용하며 대상 앱에는 전달하지 않음 |
| `OSSGUARD_AI_KEY`, `OSSGUARD_AI_MODEL` | 선택 사항. AI 테스트 초안과 결과 설명을 쓸 때 키와 모델을 함께 설정 |
| `OSSGUARD_EMAIL_KEY`, `OSSGUARD_EMAIL_FROM` | 선택 사항. Resend API 키와 발신 주소 |
| `OSSGUARD_RESULT_BASE_URL` | 이메일 결과 링크의 기본 경로. 기본 `http://localhost:3000/validations` |
| `OSSGUARD_SECRET_PROJECT_ID` | 대상 앱이 Secret Manager 참조를 사용할 때 허용할 전용 GCP 프로젝트 |
| `POSTGRES_PASSWORD` | Compose DB 비밀번호. 기본 `ossguard`. 기존 DB 볼륨의 사용자 비밀번호를 자동 변경하지는 않음 |

예를 들어 개발 서버를 `http://127.0.0.1:3000`에서 직접 API에 연결한다면 `localhost`와 다른 origin이므로 CORS 배열에 해당 주소를 추가해야 합니다. Vite 개발용 동일 origin 프록시를 쓰는 경우에는 브라우저가 별도 API origin을 호출하지 않습니다.

서버 토큰·AI·메일 키를 Git이나 프론트엔드 `VITE_*` 변수에 넣지 않습니다. 대상 앱의 비밀값은 아래의 고정 Secret Manager 참조를 사용합니다.

## 검증할 프로그램 준비

공개 [주문·출고 샘플](https://github.com/cbu-hackathon-m-team/OSSGuard-Purchase-Demo)의 `main`을 바로 사용할 수 있다. `pydantic` 목표 버전은 `2.12.5`, 동일 버전 대조군은 `1.10.24`다.
`examples/purchase-app/`의 내용을 다른 공개 GitHub 저장소 루트에 올려도 지원 규격에 맞는 시연 대상이 된다.
이 백엔드 저장소 자체를 대상 URL로 입력하면 루트 실행 규격이 없어 검사되지 않는다.
공개 저장소 생성·업로드는 자동으로 수행하지 않는다.

필수 파일:

```text
app.py                       # 0.0.0.0에서 HTTP 실행, 기본 포트 8000
pyproject.toml               # Python 3.12, 직접 런타임 PyPI 의존성
uv.lock
Dockerfile.ossguard          # templates/ 파일과 동일한 팀 표준 템플릿
ossguard.json                # 포트, health 경로, 환경변수 스키마
ossguard/tests.json          # HTTP 입력과 정상 기대값
ossguard/stubs/stubs.json    # Mock 응답 목록
```

현재는 root 단일 프로젝트, PyPI 공개 패키지, 동기 HTTP 업무만 지원한다.
Git/path 의존성·별도 index·workspace·submodule·심볼릭 링크·레포의 `.env` 파일은 지원하지 않는다.
임의 Dockerfile/Compose/테스트 스크립트 대신 제공한 규격을 사용한다.
대상 앱은 `app.py`에서 시작하도록 준비하고 별도 프레임워크가 필요하면 런타임 의존성에 선언한다.
패키지 설치가 업무 소스에 의존하는 로컬 Python 패키지 빌드는 아직 지원하지 않는다.

## 사용자 실행 흐름과 API

1. `POST /api/projects`로 `name`, `repository_url`, `ref`를 등록한다.
2. `POST /api/projects/{id}/inspect`로 커밋 SHA와 필수 파일을 확인한다. `suite_id`가 반환된다.
   API는 GitHub REST만 사용하며 clone/프로그램 실행은 하지 않는다. 같은 SHA 결과를 캐시한다.
3. `GET /api/projects/{id}/dependencies`로 **선언과 lock 버전**을 확인하고,
   `GET /api/projects/{id}/dependencies/{name}/versions`로 PyPI 배포 버전을 선택한다.
4. `PUT /api/projects/{id}/execution-profile`, `/environment-profile`, `/rules`로 입력한다.
   샘플의 `SHIPPING_BASE_URL`은 Runner가 Mock 주소를 넣으므로 사용자가 입력하지 않는다.
5. `GET /api/projects/{id}`의 테스트를 확인하고 `PUT /api/test-suites/{suite_id}`로
   수정한 `cases`, `stubs`, **`approved: true`**를 보낸다. 저장소에서 읽은 테스트도 승인 전에는 실행하지 않는다.
6. 필요하면 `/notification-settings`에 `{"enabled":true,"email":"본인@주소"}`를 저장한다.
7. `POST /api/validations`로 시작한다.

```json
{
  "project_id": "등록한 프로젝트 ID",
  "package": "pydantic",
  "target_version": "2.12.5",
  "suite_id": "승인한 테스트 ID",
  "idempotency_key": "매 실행마다 새로운 고유 키"
}
```

`202 {"id":"...","execution_status":"QUEUED"}`를 반환한다.
같은 키·입력 재요청은 같은 실행 ID를 반환하고, 같은 키·다른 입력은 409다.
실행마다 SHA·프로필 revision·승인 테스트·일반 환경값·고정 비밀 버전 참조·이메일을 스냅샷으로 남긴다.
이후 프로젝트 설정을 바꿔도 과거 검증 조건은 바뀌지 않는다.

- `GET /api/validations/{id}`: 단계, 판정, 오류, 설치 버전, 변경 증거, AI/정리/메일 상태.
- `GET /api/validations/{id}/evidence`: DB에 보관된 로그·diff·케이스별 A/B 응답과 Mock 저널.
- `GET /api/validations/{id}/notifications`: 접수 ID·시도 횟수·실패/전송 불명.
- `GET /api/validations?project_id=...`: 최근 실행 목록. 상태는 2초 정도 간격으로 polling하면 된다.
- `POST /api/projects/{id}/test-suites`: 수동 테스트 추가.
- `POST /api/projects/{id}/test-suites/generate`: AI 초안. 초안은 항상 미승인이다.

요청 스키마와 주요 응답 모델은 Swagger/OpenAPI에서 확인한다. 오류는 `error.code`, `error.message`
(입력 형식 오류는 `error.details`)로 반환한다. 실제 값이 필요한 예시는
`examples/purchase-app/ossguard/`의 JSON 파일을 참고한다.

## 실제 검증과 상태

`소스 SHA 수집 → 기존 빌드/실제 버전/기준 테스트 → uv add --no-sync → 소스 무변경 확인 → 후보 빌드/실제 버전/같은 테스트 → 판정·DB 보존 → 정리 → AI·메일`

`uv add`에는 대상 패키지·버전만 인자 배열로 전달하며 업무 소스를 자동 수정하지 않는다.
manifest/lock diff, 하위 의존성 변화, 이미지 ID, 실제 설치 목록을 기록한다.
컨테이너의 `importlib.metadata`로 확인한 버전과 목표가 다르면 `VERSION_MISMATCH`다.

| 상태 | 의미 |
| --- | --- |
| `PASS` | 기준과 후보가 모두 승인된 기대값을 만족 |
| `REGRESSION_SUSPECTED` | 기준은 통과하고 후보가 업무 기준 위반 |
| `BASELINE_INVALID` | 기존 버전부터 기준 위반. 후보는 실행하지 않음 |
| `NOT_EVALUATED` | 빌드/기동/관측 오류 등으로 판단할 증거 부족 |

HTTP 200만으로 통과하지 않는다. JSON `eq/gte`, 외부 호출의 메서드·경로·본문 식별자·횟수와
Mock 매칭/응답 상태를 확인한다. 케이스별 저널을 초기화하고 관측 장애는 실제 0건과 구분한다.
응답 전 외부 호출이 완료되는 앱을 대상으로 하며 큐·지연 작업은 관측 범위 밖이다.

작업은 PostgreSQL에 남고 한 번에 한 건 실행한다. 같은 VM/Runner ID의 프로세스가 재시작하면
소유한 컨테이너/네트워크/이미지와 작업 폴더를 정리한 뒤 `INTERRUPTED`로 마무리한다.
다른 VM의 RUNNING 작업은 heartbeat가 오래됐더라도 재실행하지 않는다. 해당 VM 복구·정리 확인이 필요하다.
Runner ID와 작업 디렉터리는 VM에서 안정적으로 유지해야 한다. OS 파일 잠금과 DB advisory lock을 함께 쓴다.

앱·Mock은 내부 전용 네트워크에 있고 외부로 요청할 수 없다. Docker의 internal 네트워크에서는
직접 호스트 포트 연결이 제공되지 않아, **플랫폼 소유 HTTP 중계 컨테이너**만 제어 네트워크와
내부 네트워크에 함께 붙인다. 중계는 `/app/`, `/mock/`의 고정 목적지만 전달하며 호스트 loopback 임시 포트를 사용한다.
실행 포트를 외부에 공개하지 않는다. API VM은 Runner 로컬 파일이나 이 포트에 접근할 필요가 없다.

최종 증거는 로그 tail 32 KiB, diff 파일당 64 KiB, 테스트 결과당 200 KB, 작업 증거 2 MB를 상한으로
DB에 저장한다. 잘린 자료는 `truncated`, 원본/보관 크기를 표시한다. 비밀값·인증 헤더는 마스킹한다.
정리는 해당 실행 라벨의 자원에만 적용하고 전역 `docker prune`을 실행하지 않는다.

## AI, 이메일, 비밀값

선택 기능을 사용할 때 `.env.example`을 참고해 아래 항목을 설정합니다. 기존 `.env` 전체를 덮어쓰지 말고 필요한 값만 수정하세요. 키는 프론트엔드 `VITE_*` 변수나 Git에 넣지 않는다.

- `OSSGUARD_AI_KEY`: [OpenAI API 키](https://platform.openai.com/api-keys).
  `OSSGUARD_AI_MODEL`에 사용할 모델을 명시합니다. 예시 모델은 `gpt-5-mini`이며 사용 계정의 접근 권한이 필요합니다.
- `OSSGUARD_EMAIL_KEY`: [Resend API 키](https://resend.com/api-keys).
  테스트 발신자로 `OSSGUARD_EMAIL_FROM=OSSGuard <onboarding@resend.dev>`를 설정할 수 있습니다. 이 주소는 **Resend 가입 이메일로만 테스트 발송**할 수 있다.
  프로젝트의 실행 설정에서 결과 이메일에 그 주소를 입력하고 저장한다.
  다른 수신자에게 보내려면 Resend에서 본인 도메인을 인증하고 `OSSGUARD_EMAIL_FROM`을 변경한다.
- `OSSGUARD_RESULT_BASE_URL`: 로컬은 `http://localhost:3000/validations`.
  다른 기기에서 열 메일에는 실제 접근 가능한 배포 주소가 필요하다.
- `OSSGUARD_SECRET_PROJECT_ID`: 주문·출고 샘플에서는 비워둔다.
  대상 앱이 비밀값을 요구할 때만 전용 GCP 프로젝트·Secret Manager API·비밀 버전을 준비하고,
  Runner에 해당 비밀의 읽기 권한과 Application Default Credentials를 설정한다.
  참조 형식은 `projects/프로젝트ID/secrets/비밀이름/versions/숫자`다.

키를 채운 다음 Compose의 환경변수를 반영하려면 `docker compose up -d --no-deps --force-recreate api result-worker`를 실행한다.
단순 `restart`는 변경된 `.env`를 반영하지 않는다. 이미 AI가 `SKIPPED`로 끝난 결과를 소급 처리하지 않으므로 새 검증으로 확인한다.

연동 참고: [모델 문서](https://developers.openai.com/api/docs/models/gpt-5-mini),
[Resend 테스트 발신 제한](https://resend.com/docs/knowledge-base/403-error-resend-dev-domain).

AI: `OSSGUARD_AI_KEY`, `OSSGUARD_AI_MODEL`을 설정한다.
OpenAI Chat Completions JSON mode를 사용하고 응답은 Pydantic으로 다시 검사한다.
사용 모델은 계정에서 사용 가능한 JSON mode 지원 모델을 명시한다.
AI가 없어도 수동/저장소 테스트로 검증할 수 있고, 결과 분석은 `SKIPPED`다.
AI 장애는 `FAILED`로 별도 기록하며 코드가 계산한 업무 판정을 변경하지 않는다.

메일: Resend HTTPS API의 `OSSGUARD_EMAIL_KEY`, 인증된 `OSSGUARD_EMAIL_FROM`,
실제 결과 화면의 `OSSGUARD_RESULT_BASE_URL`을 설정한다.
이벤트 고유 키·고정 payload·동일 idempotency key로 최대 3회 처리한다.
24시간 제공업체 보장보다 짧은 23시간 이내에만 재시도하고, 전송 여부를 확정하지 못하면 `UNKNOWN`으로 남긴다.
`ACCEPTED`는 제공업체 접수이며 수신함 도착을 뜻하지 않는다.
메일 키 미설정 시 검증은 유지되고 메일만 `FAILED/EMAIL_NOT_CONFIGURED`다.
이 경우 키 설정 후 무조건 자동 재발송하지 않으며 운영자가 제공업체 상태를 확인해야 한다.
메일에는 요약과 인증이 필요한 결과 링크만 넣고 토큰·비밀·원본 로그를 포함하지 않는다.

앱 비밀값은 사용자 폼에서 원문을 저장하지 않고 다음 참조만 받는다.

```json
{"values":{"REGION":"test"},"secret_refs":{"API_KEY":"projects/target-project/secrets/test-api-key/versions/1"}}
```

참조에는 `projects/PROJECT/secrets/NAME/versions/1` 형식을 사용한다.
`latest`는 입력 스냅샷 재현성을 위해 허용하지 않는다. `OSSGUARD_SECRET_PROJECT_ID`와 같은 프로젝트만 허용한다.
Runner의 GCP ADC 권한으로 실행 시 읽고 이미지/빌드 인자/AI/메일에 전달하지 않는다.
플랫폼 AI·메일 키는 앱 비밀 프로젝트와 분리한다. `GET environment-profile`은 참조와 일반값만 반환한다.

## 테스트

먼저 Docker 없이 실행 가능한 단위 테스트와 정적 검사를 확인하고, Docker가 준비된 환경에서 통합 테스트를 실행하세요. `uv sync --locked`는 개발용 검사 도구도 설치합니다.

```powershell
uv run ruff check .
uv run ruff format --check .
uv run pytest -q -m "not integration"
uv run pytest -q -m integration
uv run alembic check  # 로컬 DB 실행 및 마이그레이션 적용 후 확인
```

통합 테스트는 전용 임시 PostgreSQL 컨테이너에 실제 Alembic 마이그레이션을 적용한다.
로컬 샘플 소스로 실제 uv/Docker/WireMock A/B 파이프라인을 실행한다.
GitHub 사전 확인·소스 전송과 유료 AI·메일은 테스트 대역으로 검증한다.
샘플을 공개 저장소에 업로드한 뒤 GitHub 실제 입력, GCP 권한, 유료 API 및 실제 메일 수신은 별도 확인해야 한다.

## GCP 배치

앱 VM: API + PostgreSQL + 결과 Worker. Runner VM: Python + Git + uv 0.9.26 + Docker/Buildx.
현재 Compose는 로컬 실행용이다. [GCP 운영 설정](docs/gcp.md)에 VM 간 DB 연결과 서비스 설정을 정리했다.
기업 임의 코드의 적대적 다중 사용자 실행 서비스가 아니라 **팀이 검토한 저장소의 해커톤 MVP**다.

API 계약 참고: [uv CLI](https://docs.astral.sh/uv/reference/cli/),
[WireMock 저널](https://wiremock.org/docs/verifying/),
[OpenAI Chat API](https://developers.openai.com/api/reference/resources/chat),
[Resend 멱등 키](https://resend.com/docs/dashboard/emails/idempotency-keys).


## 문제 해결

| 증상 | 확인할 내용 |
| --- | --- |
| `docker version`에서 서버 연결 실패 | Docker Desktop/Engine 실행 여부와 Linux 컨테이너 모드를 확인하세요. |
| DB 연결 실패 또는 `/health` 503 | `docker compose ps db`, DB 로그, DB URL을 확인하세요. 로컬 기본 포트는 5432가 아니라 54329입니다. |
| 테이블이 없다는 오류 | 같은 `.env`와 DB를 사용해 `uv run alembic upgrade head`를 실행하세요. |
| `AUTH_NOT_CONFIGURED` | API 프로세스의 `OSSGUARD_API_TOKEN`이 비어 있습니다. 값을 설정하고 API를 다시 시작하세요. |
| `UNAUTHORIZED` | Swagger/프론트엔드에 입력한 토큰과 API 설정이 일치하는지 확인하세요. |
| `REPOSITORY_NOT_ALLOWED` | API와 Runner의 `OSSGUARD_ALLOWED_REPOS`에 검토한 공개 저장소 URL이 있는지 확인하세요. |
| `TESTS_NOT_APPROVED` | 선택한 테스트를 저장·승인한 뒤 다시 접수하세요. AI가 생성한 초안도 승인이 필요합니다. |
| 작업이 `QUEUED`에 머무름 | Runner가 실행 중인지, API와 같은 DB를 보는지 확인하세요. 기존 Runner의 잠금 오류도 확인하세요. |
| 기존 버전부터 실패 | 업데이트의 영향으로 결론 내리지 말고 기존 실행 환경과 테스트 기대값을 확인하세요. |
| AI가 `SKIPPED` 또는 `AI_NOT_CONFIGURED` | AI 키와 모델 설정을 확인하세요. 키 없이도 수동 테스트와 사실 기반 판정은 사용할 수 있습니다. |
| 결과 분석·메일만 진행되지 않음 | 결과 Worker 실행 여부와 DB 연결을 확인하세요. |
| 메일이 접수됐지만 도착하지 않음 | `ACCEPTED`는 제공업체 접수 상태입니다. 발신 도메인·수신자 제한·스팸함과 제공업체 상태를 별도로 확인하세요. |
| Compose에서 `.env` 수정이 반영되지 않음 | `docker compose up -d --no-deps --force-recreate api result-worker`로 해당 컨테이너를 다시 생성하세요. 호스트 Runner도 필요한 경우 재시작하세요. |
| 8000 포트 사용 중 | API를 호스트와 Compose 양쪽에서 동시에 실행하지 않았는지 확인하세요. |

```powershell
docker compose ps
docker compose logs --tail 100 db api result-worker
uv run alembic current
```

Runner를 강제 종료하거나 다른 VM의 작업이 중단된 경우에는 먼저 해당 실행의 자원 정리를 확인합니다. 다른 VM의 작업을 임의로 재접수하거나 전역 Docker 정리 명령으로 해결하지 않습니다. 운영 절차는 [GCP 운영 설정](docs/gcp.md)을 참고하세요.
