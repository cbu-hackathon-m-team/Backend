# OSSGuard 실행 백엔드

최신 PDF 명세의 **Node-RED 검증 실행 담당 영역**을 구현한다. Python 3.12 / FastAPI / HTTPX / Docker SDK / PostgreSQL / SQLAlchemy / Alembic을 사용한다. Redis·Celery는 사용하지 않는다.

역할 경계와 연동 계약은 [실행백엔드 인수인계](docs/실행백엔드-인수인계.md), 실제 검증 범위는 [검증기록](docs/검증기록.md)을 참고한다.

## 1. Python 의존성

현재 PC에는 `.venv`가 준비돼 있다. 새 PC에서는 Python 3.12 이상으로 생성한다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt -e ".[dev]"
```

현재 PC의 기본 `python`은 Windows 실행 별칭이므로 `.venv\Scripts\python.exe`를 직접 사용한다. 가상환경 활성화나 PowerShell 실행 정책 변경은 필요 없다. Linux에서는 `.venv/bin/python`을 사용한다. lock 파일은 이번 Windows 설치 결과이며 pywin32는 Windows 전용 조건으로 제한했다. Linux 설치 자체는 별도 확인이 필요하다.

## 2. Docker와 DB 준비

Docker Desktop(Windows, Linux containers) 또는 Docker Engine + Compose v2(Linux)가 필요하다. 이 작업에서는 Docker를 시스템에 설치하지 않았다.

`.env.example`을 `.env`로 복사하고 로컬 개발용 DB 비밀번호를 지정한다. 다음 명령은 저장소 최상위에서 실행한다.

```powershell
docker compose --env-file .env -f infra/execution.compose.yaml up -d --wait
.\.venv\Scripts\python.exe scripts/prepare_runtime.py
```

두 번째 명령은 고정 digest의 Node-RED 이미지를 다운로드하고 팀 모의 서비스 이미지를 빌드한다. 검증 실행 중에는 임의 이미지를 받거나 빌드하지 않는다.

아래 스크립트로 `.env`의 POSTGRES_PASSWORD를 읽어 연결 주소를 설정한다. 비밀번호의 `@`, `#` 등은 자동으로 URL 인코딩하며 화면에 출력하지 않는다. POSTGRES_PASSWORD는 한 줄로 작성한다.

```powershell
. .\scripts\use-local-db.ps1
.\.venv\Scripts\python.exe -m alembic upgrade head
```

새 PowerShell 창에서는 위 연결 설정 스크립트를 다시 실행한다. Python은 `.env`를 자동으로 읽지 않는다. PostgreSQL 데이터는 Compose 영구 볼륨에 보관한다. DB 포트는 localhost에만 노출한다.

## 3. API 없이 실행

```powershell
# 같은 정상 흐름을 두 버전에 적용한 대조군
.\.venv\Scripts\python.exe -m ossguard.cli run --candidate normal

# 의도적인 배송 누락: T01/T02 회귀 의심, T03/T04 통과가 기대 결과
.\.venv\Scripts\python.exe -m ossguard.cli run --candidate missing_shipping

# 수정 후 새 실행
.\.venv\Scripts\python.exe -m ossguard.cli run --candidate fixed

# 위 명령에서 반환한 실행 ID로 과거 증거 조회
.\.venv\Scripts\python.exe -m ossguard.cli show RUN_UUID
```

검증 불통과 시 CLI 종료 코드는 1이다. 결함 샘플 실행의 종료 코드 1은 예상한 결과다. 현재 샘플은 실제 기업 데이터·공식 업데이트 버그가 아니다.

기본 출력은 실행 ID·상태·통과 여부·정리 상태·케이스별 판정의 짧은 요약이다. 출력에 표시되는 `All passed: True` 등은 입력할 명령이 아니다. 전체 JSON 증거가 필요하면 `python -m ossguard.cli --json show RUN_UUID`처럼 `--json`을 하위 명령 앞에 붙인다.

## 4. 실행 담당 API

```powershell
.\.venv\Scripts\python.exe -m uvicorn ossguard.api:app --host 127.0.0.1 --port 8000 --workers 1
```

- API 문서: `http://127.0.0.1:8000/docs`
- `POST /api/validations`: `fixtures/node-red/suite.json` 본문 → 202 + 실행 ID.
- `GET /api/validations/{id}`: 상태, 저장된 입력·기준·증거·비교·정리 결과.
- `GET /health/live`, `GET /health/ready`: 프로세스 및 DB 상태.

다른 터미널에서:

```powershell
$body = Get-Content fixtures/node-red/suite.json -Raw
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/validations -ContentType application/json -Body $body
```

현재 API는 단일 개발 워크스페이스의 내부 연동용이다. 다른 백엔드의 프로젝트·테스트 관리 API와 통합할 때는 저장된 확정 테스트에서 스냅샷을 만들어야 한다. 인증 없이 외부에 공개하지 않는다. 한 번에 한 검증만 허용하며 중복 요청은 409다.

## 5. 실패 복구

API 시작 시 이전 미완료 작업의 상태와 남은 자원을 복구한다. 별도로 재시도하려면 API를 종료한 뒤:

```powershell
.\.venv\Scripts\python.exe -m ossguard.cli recover
```

미완료 작업을 INTERRUPTED로 기록하고 로그 수집·정리를 수행한다. 정리 실패가 남은 작업은 PENDING으로 유지한다. 다른 API 워커가 실행 중이면 PostgreSQL 잠금 때문에 실행을 거부한다. 전체 Docker prune 명령은 사용하지 않는다.

## 6. 테스트

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests scripts migrations
```

기본 테스트는 SQLite, 가짜 어댑터, 로컬 모의 HTTP 서버, 모의 Docker SDK로 동작한다. 실제 Docker 통합 시험은 기본 실행에서 명시적으로 skip한다.

Docker·이미지·DB migration 준비 후 API를 종료하고:

```powershell
$env:OSSGUARD_DOCKER_TESTS = '1'
.\.venv\Scripts\python.exe -m pytest -q tests/test_docker_e2e.py
Remove-Item Env:OSSGUARD_DOCKER_TESTS
```

## 7. 독립 실패·복구 시험

실행 엔진 실패·복구 시험은 기존 API 서버를 유지한 채 별도 DB·프로세스로 실행할 수 있다.

```powershell
.\.venv\Scripts\python.exe scripts/verify_failure_recovery.py
```

[실패·복구 검증 결과와 범위](docs/실패복구-검증결과.md)를 참고한다.

## 8. Playwright 보조 실행

브라우저 UI가 완성되기 전에도 테스트 페이지로 브라우저 조작을 점검할 수 있다. 현재 PC는 설치된 Edge를 사용해 확인했다.

```powershell
$env:BROWSER_CHANNEL = 'msedge'
.\.venv\Scripts\python.exe tests/browser_smoke.py
```

자체 Chromium을 설치해 쓰려면:

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = "$PWD/.local/browsers"
.\.venv\Scripts\python.exe -m playwright install chromium --only-shell
Remove-Item Env:BROWSER_CHANNEL -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe tests/browser_smoke.py
```

`runner.cli`의 초기 HTTP·브라우저·SQLite 이력 도구도 유지한다. **새 PDF의 검증 결과는 `ossguard`의 PostgreSQL 저장 경로**를 사용한다.
