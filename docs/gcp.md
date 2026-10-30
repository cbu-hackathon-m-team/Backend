# GCP 실행 설정

배포 자동화가 아닌, 두 VM에 이 백엔드를 실행하는 운영 절차다.
API·DB·메일/AI와 Git·Docker 실행을 분리하며 Kubernetes/Redis/Celery는 사용하지 않는다.

## 앱 VM

- Python 3.12.14, uv 0.9.26 설치 후 `uv sync --locked --no-dev`.
- PostgreSQL 사용자/DB를 만들고 `OSSGUARD_DATABASE_URL`을 asyncpg URL로 설정한다.
- PostgreSQL `listen_addresses`는 필요한 비공개 인터페이스에만 설정한다.
  `pg_hba.conf`는 Runner의 비공개 IP와 전용 DB 사용자만 허용한다. 가능하면 TLS를 사용한다.
- DB 포트 5432를 공인 인터넷에 열지 않는다. VPC firewall도 Runner 서비스 계정/태그 및 비공개 IP로 제한한다.
- `uv run alembic upgrade head` 후 API와 결과 Worker를 별도 systemd 서비스로 실행한다.
- API 앞에 HTTPS 리버스 프록시와 팀 접근 통제를 두고, 프론트엔드 origin을 명시한다.
  팀 Bearer 토큰은 사용자별 권한 체계가 아니다.
- AI·메일 자격증명은 이 VM의 결과 Worker에만 주고 Runner 설정 파일에는 넣지 않는다.

로컬 Compose DB를 그대로 쓸 경우 기본 포트 매핑이 loopback이라 다른 VM에서 접근되지 않는다.
GCP에서는 비공개 IP로 바인딩한 별도 배포 구성을 준비하고 위 DB/VPC 접근 제한을 함께 적용해야 한다.
공인 `0.0.0.0:5432` 포트를 편의상 열지 않는다.

## Runner VM

- Git, Python 3.12.14, uv 0.9.26, Docker Engine + Buildx를 설치한다.
- 전용 Linux 사용자로 실행하고 Docker 제어 권한은 그 사용자에게만 부여한다.
- `.env`의 `OSSGUARD_DATABASE_URL`은 앱 VM의 비공개 DB 주소로 설정한다.
- `OSSGUARD_RUNNER_ID`는 이 VM만의 고정 ID, `OSSGUARD_WORK_DIR`는 지속되는 절대 경로다.
- `OSSGUARD_ALLOWED_REPOS`는 API와 같은 검토한 공개 저장소 목록으로 설정한다.
- 시연 앱 비밀 전용 프로젝트의 필요한 secret들에만 Secret Accessor 권한을 준 서비스 계정을 사용한다.
  플랫폼 AI/메일/운영 DB 비밀에는 접근 권한을 주지 않는다.
- 운영 데이터베이스/결제/배송 자격증명을 입력하지 않는다. 런타임 앱은 내부 Mock만 사용한다.

Docker 런타임은 코드에서 internal 네트워크로 제한하지만 **빌드에는 PyPI/레지스트리 인터넷이 필요**하다.
빌드 컨테이너가 VM 메타데이터·VPC 서비스에 접근하지 못하도록 호스트 Docker egress firewall도 설정한다.
전용 Runner에서 Docker bridge/build 네트워크의 `169.254.169.254/32`, 플랫폼 DB 비공개 IP 및
필요 없는 VPC 대역으로의 전달을 차단하고 GitHub/PyPI/레지스트리 DNS·HTTPS만 필요한 범위에서 허용한다.
이 규칙은 VM의 iptables/nftables와 Docker backend에 맞춰 운영자가 적용·시험해야 하며 앱이 자동 변경하지 않는다.
Runner 호스트의 PostgreSQL/Secret Manager 연결과 빌드 컨테이너의 통신을 구분한다.

## 서비스 명령

각 서비스는 같은 checkout의 절대 경로를 WorkingDirectory로 쓰고 `.env`를 해당 위치에 둔다.
systemd의 EnvironmentFile에도 Python `.env`와 충돌하지 않는 같은 값을 설정할 수 있다.

```text
API:           /opt/ossguard/.venv/bin/uvicorn ossguard.api:app --host 127.0.0.1 --port 8000
Runner:        /opt/ossguard/.venv/bin/python -m ossguard.runner
Result Worker: /opt/ossguard/.venv/bin/python -m ossguard.result_worker
```

`Restart=on-failure`, `RestartSec=5`, 작업 파일 정리를 위한 충분한 종료 유예 시간을 둔다.
Runner ID/디렉터리를 공유하는 두 OS 프로세스는 파일 잠금으로 막고, 전역 Runner는 DB advisory lock으로 한 개만 실행한다.
통신 불명인 다른 VM 작업은 새 Runner가 lease 만료만으로 인수하지 않는다.

## 운영 확인

1. `/health`, DB private 연결, Alembic 버전을 확인한다.
2. 공개 시연 저장소를 등록하고 실제 SHA 고정·PyPI 버전 조회를 확인한다.
3. 업데이트 회귀와 변경 없는 대조군을 각각 실행한다.
4. 결과 DB에 응답·외부 호출·버전·diff가 남고 실행 자원이 정리되는지 확인한다.
5. 실제 AI 분석·Resend 발신 도메인과 수신함 도착을 확인한다.
6. `pg_dump` 백업을 별도 보관하고 임시 DB에 복원한다. 결과 보관 기간/저장량을 팀이 정한 뒤 삭제 정책을 운영한다.

이 저장소는 GCP 리소스를 생성하거나 방화벽/IAM을 자동 변경하지 않는다.
