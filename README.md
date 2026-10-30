# OSSGuard Backend

GitHub의 Python 프로그램에서 **선택한 의존성 하나를 임시 소스에서 실제 업데이트**하고,
기존 A와 후보 B를 Docker로 빌드·실행해 업무 결과와 외부 API 호출을 비교한다.
원격 저장소나 운영 서버는 수정하지 않는다. Node-RED 전용 코드가 없는 Python/uv 첫 구현이다.

## 실행

필수: Python **3.12.14**, uv **0.9.26**, Git, Linux 컨테이너를 실행하는 Docker + Buildx.
Windows 개발은 Docker Desktop, GCP Runner는 Linux 호스트 프로세스를 사용한다.

```powershell
cd C:\Project\cbu-hackathon\backend
uv python install 3.12.14
uv sync --locked --python 3.12.14
Copy-Item .env.example .env  # .env가 없을 때 한 번만. 기존 설정을 덮어쓰지 말 것
```

`.env`에서 `OSSGUARD_API_TOKEN`을 임의의 긴 팀 토큰으로 바꾸고,
`OSSGUARD_ALLOWED_REPOS`에는 기본 공개 시연 저장소 `https://github.com/cbu-hackathon-m-team/OSSGuard-Purchase-Demo`가 들어 있다.
다른 저장소를 검토해 추가할 수 있다. `[]`에서는 프로젝트 실행 대상 등록이 차단된다. 환경변수 목록은 JSON 배열이다.

```powershell
docker compose up -d db
uv run alembic upgrade head
uv run uvicorn ossguard.api:app --host 127.0.0.1 --port 8000
```

다른 터미널 두 개에서 각각 실행한다.

```powershell
uv run python -m ossguard.runner
uv run python -m ossguard.result_worker
```

[Swagger](http://127.0.0.1:8000/docs)의 **Authorize**에 `.env`의 팀 토큰을 입력한다.
`/health`와 API 설명 외 데이터 API는 Bearer 인증이 필요하다. 사용자별 계정/권한은 후속 범위다.
프론트엔드 개발 origin 기본값은 `http://localhost:3000`이며 `5173`도 허용한다. 결과 링크 기본값은 `http://localhost:3000/validations`다.

API·DB·결과 Worker를 함께 컨테이너로 실행하려면 `docker compose up -d --build`를 쓴다.
**Runner는 별도 호스트 터미널에서 실행**한다. API 컨테이너에 Docker 소켓을 마운트하지 않는다.
로컬 DB 포트는 `127.0.0.1:54329`, API 포트는 `127.0.0.1:8000`이다.
DB 데이터는 `ossguard_pgdata` 볼륨에 남는다. 종료는 `docker compose down`이며 `-v`를 붙이면 데이터가 삭제된다.

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

```powershell
uv run ruff check .
uv run ruff format --check .
uv run pytest -q -m "not integration"
uv run pytest -q -m integration
uv run alembic check
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
