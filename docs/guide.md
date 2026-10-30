현재 코드 기준으로는 **GCP VM 두 대를 만들어 연결하는 방식**이 맞습니다.

- **앱 VM**: API + PostgreSQL + AI·메일 결과 처리
- **Runner VM**: 검증할 프로그램을 내려받아 Docker로 A/B 실행

아래는 **공개 주문 샘플을 먼저 실행하는 MVP 배포 순서**입니다. 서버 이름·IP는 설명용 예시이고, 실제 생성된 값으로 바꾸면 됩니다.

```text
프론트엔드
    │ HTTPS
    ▼
앱 VM ── PostgreSQL
              ▲
              │ 내부망 연결
          Runner VM
              └─ 기준 A / 후보 B Docker 실행
```

**1. GCP 프로젝트와 결제 계정 준비**

GCP 콘솔에서 다음 순서로 진행합니다.

1. 프로젝트를 만들거나 기존 프로젝트를 선택합니다.
2. 결제 계정을 연결합니다.
3. **Compute Engine → VM 인스턴스**에 들어가 API를 활성화합니다.

이번 구성에서는 VM·디스크·외부 IP 비용이 발생하므로, 생성 화면의 예상 비용도 확인하세요. [VM 생성 안내](https://docs.cloud.google.com/compute/docs/instances/create-start-instance)

**2. VM 두 대 만들기**

처음 시작할 설정은 아래처럼 제안합니다. Runner 크기는 실제 빌드 메모리 사용량에 따라 조정하면 됩니다.

| 항목 | 앱 VM | Runner VM |
|---|---|---|
| 이름 | `ossguard-app` | `ossguard-runner` |
| 운영체제 | Ubuntu 24.04 LTS | Ubuntu 24.04 LTS |
| 머신 | `e2-medium` | `e2-standard-2` |
| 디스크 | 30GB | 50GB 이상 |
| 네트워크 태그 | `ossguard-app` | `ossguard-runner` |
| 역할 | API·DB·결과 Worker | Docker 검증 |

두 VM은 **같은 리전·VPC·서브넷**에 둡니다. 서울을 사용한다면 리전은 `asia-northeast3`입니다. 위 머신 선택은 MVP 시작값에 대한 제안입니다. [머신 유형](https://docs.cloud.google.com/compute/docs/general-purpose-machines), [리전 안내](https://docs.cloud.google.com/compute/docs/regions-zones)

처음에는 전용 VPC와 서브넷을 만드는 편이 설정을 파악하기 쉽습니다. 예를 들어 서브넷을 `10.20.0.0/24`로 만들고 다음 내부 IP를 지정합니다.

```text
앱 VM 내부 IP:     10.20.0.2
Runner 내부 IP:    10.20.0.3
```

샘플 실행에는 GCP 서비스 계정 권한이 필요하지 않습니다. Secret Manager를 사용하기 전까지는 VM에 서비스 계정을 연결하지 않아도 됩니다.

**3. 방화벽 열기**

GCP의 **VPC 네트워크 → 방화벽**에서 다음 인바운드 규칙을 만듭니다.

| 대상 | 허용 포트 | 접속 출발지 |
|---|---|---|
| 앱 VM | TCP 80, 443 | 인터넷 |
| 앱 VM | TCP 54329 | Runner 내부 IP 하나만, 예: `10.20.0.3/32` |
| 두 VM | TCP 22 | IAP SSH를 쓴다면 `35.235.240.0/20` |

DB와 API의 `8000` 포트는 인터넷에 직접 열지 않습니다. API는 나중에 HTTPS 프록시를 통해 접근합니다.

IAP SSH를 사용하려면 접속 계정에 필요한 IAP 터널 접근 권한도 있어야 합니다. [방화벽 설정](https://docs.cloud.google.com/firewall/docs/using-firewalls), [IAP SSH 설정](https://docs.cloud.google.com/iap/docs/using-tcp-forwarding)

**4. 두 VM에 Docker 설치하기**

두 VM에 각각 SSH로 접속해 **Docker Engine + Buildx + Compose 플러그인**을 설치합니다.

Ubuntu용 [Docker 공식 설치 문서의 `Install using the apt repository`](https://docs.docker.com/engine/install/ubuntu/#install-using-the-apt-repository) 순서대로 설치하면 세 구성요소를 함께 설치할 수 있습니다.

설치 후 확인합니다.

```bash
sudo docker run --rm hello-world
sudo docker buildx version
sudo docker compose version
```

Runner는 일반 사용자로 Docker를 실행할 수 있어야 합니다.

```bash
sudo usermod -aG docker "$(whoami)"
```

그다음 **SSH 연결을 끊고 다시 접속**한 뒤 확인합니다.

```bash
docker ps
```

`permission denied` 없이 목록이 나오면 됩니다. Docker 그룹에는 서버를 관리하는 사용자만 넣으세요.

**5. 두 VM에 백엔드 코드 받기**

두 VM 모두 같은 경로에 코드를 둡니다.

```bash
sudo apt update
sudo apt install -y git curl gh

sudo install -d \
  -o "$(id -un)" \
  -g "$(id -gn)" \
  /opt/ossguard
```

현재 Backend는 비공개 저장소이므로 GitHub 인증 후 복제합니다.

```bash
gh auth login

gh repo clone \
  cbu-hackathon-m-team/Backend \
  /opt/ossguard

cd /opt/ossguard
git switch main
```

이후 명령은 기본적으로 `/opt/ossguard`에서 실행합니다.

**6. 앱 VM의 환경변수 설정**

**앱 VM에서만** 실행합니다.

```bash
cd /opt/ossguard
cp .env.example .env
chmod 600 .env
nano .env
```

아래 항목을 실제 값으로 설정합니다.

```dotenv
POSTGRES_PASSWORD=여기에_DB_비밀번호

OSSGUARD_API_TOKEN=여기에_긴_팀_토큰

OSSGUARD_ALLOWED_REPOS=["https://github.com/cbu-hackathon-m-team/OSSGuard-Purchase-Demo"]

OSSGUARD_CORS_ORIGINS=["https://실제-프론트엔드-도메인"]

OSSGUARD_RESULT_BASE_URL=https://실제-프론트엔드-도메인/validations

OSSGUARD_AI_KEY=
OSSGUARD_AI_MODEL=
OSSGUARD_EMAIL_KEY=
OSSGUARD_EMAIL_FROM=
```

DB 비밀번호와 팀 토큰은 **서로 다른 값**을 사용합니다. 다음 명령을 각각 실행하면 URL에 넣기 쉬운 무작위 값을 만들 수 있습니다.

```bash
openssl rand -hex 32
```

AI·메일 항목은 처음에는 비워도 됩니다. 먼저 기본 A/B 검증을 성공시킨 뒤 추가하는 순서가 좋습니다.

**7. 앱 VM에서 DB 접속 범위와 자동 재시작 설정**

현재 `compose.yaml`의 DB는 로컬에서만 접속됩니다. 서버의 파일을 엽니다.

```bash
nano compose.yaml
```

`db`의 `ports`에 **앱 VM 내부 IP**를 추가합니다.

```yaml
ports:
  - "127.0.0.1:54329:5432"
  - "10.20.0.2:54329:5432"
```

그리고 `db`, `api`, `result-worker` 각각에 다음 항목을 추가합니다.

```yaml
restart: unless-stopped
```

예를 들어 API는 이렇게 됩니다.

```yaml
api:
  build: .
  restart: unless-stopped
  env_file: .env
```

나머지 기존 설정은 유지합니다. `migrate`는 한 번 실행하고 끝나는 서비스이므로 재시작 설정을 추가하지 않습니다.

이제 실행합니다.

```bash
docker compose up -d --build
docker compose ps
curl -f http://127.0.0.1:8000/health
```

`/health`가 HTTP 200을 반환하면 API가 올라온 것입니다.

오류가 있으면 다음 로그를 확인합니다.

```bash
docker compose logs --tail=100 migrate api result-worker
```

**8. Runner VM에 Python과 실행 환경 준비**

이번에는 **Runner VM에서만** 실행합니다.

현재 코드가 요구하는 uv 버전은 `0.9.26`입니다.

```bash
curl -LsSf https://astral.sh/uv/0.9.26/install.sh | sh
source "$HOME/.local/bin/env"

cd /opt/ossguard
uv python install 3.12.14
uv sync --locked --no-dev --python 3.12.14
```

uv는 설치 URL에서 버전을 지정할 수 있고 Python도 설치할 수 있습니다. [uv 설치](https://docs.astral.sh/uv/getting-started/installation/), [Python 설치](https://docs.astral.sh/uv/guides/install-python/)

Runner의 설정 파일을 만듭니다.

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

핵심 설정은 다음과 같습니다.

```dotenv
OSSGUARD_DATABASE_URL=postgresql+asyncpg://ossguard:앱_VM에서_정한_DB_비밀번호@10.20.0.2:54329/ossguard

OSSGUARD_RUNNER_ID=gcp-runner-1
OSSGUARD_WORK_DIR=/opt/ossguard/.work

OSSGUARD_ALLOWED_REPOS=["https://github.com/cbu-hackathon-m-team/OSSGuard-Purchase-Demo"]

OSSGUARD_AI_KEY=
OSSGUARD_EMAIL_KEY=
```

Runner의 DB 주소에는 **앱 VM 내부 IP**를 넣습니다. AI·메일 키는 Runner에 넣지 않습니다.

직접 실행해서 연결을 확인합니다.

```bash
uv run python -m ossguard.runner
```

다음 메시지가 나오면 준비된 것입니다.

```text
Runner ready: gcp-runner-1
```

확인 후 `Ctrl+C`로 종료합니다.

**9. Runner를 자동 실행 서비스로 등록**

Runner VM에서 아래 파일을 만듭니다. 명령 안의 사용자명과 PATH는 현재 SSH 사용자 기준으로 들어갑니다.

```bash
sudo tee /etc/systemd/system/ossguard-runner.service > /dev/null <<EOF
[Unit]
Description=OSSGuard Runner
Wants=network-online.target
After=network-online.target docker.service
Requires=docker.service

[Service]
Type=simple
User=$(id -un)
SupplementaryGroups=docker
WorkingDirectory=/opt/ossguard
Environment="PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=/opt/ossguard/.venv/bin/python -m ossguard.runner
Restart=on-failure
RestartSec=5
TimeoutStopSec=120

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now ossguard-runner
sudo systemctl status ossguard-runner
```

로그는 다음 명령으로 확인합니다.

```bash
sudo journalctl -u ossguard-runner -f
```

**10. 앱 VM에 도메인과 HTTPS 연결**

앱 VM의 외부 IP를 **고정 IP**로 지정하고, 도메인 DNS에 다음 A 레코드를 등록합니다.

```text
api.본인도메인 → 앱 VM의 고정 외부 IP
```

고정 IP는 GCP의 IP 주소 화면에서 예약하거나 기존 임시 IP를 고정 IP로 승격할 수 있습니다. [고정 IP 안내](https://docs.cloud.google.com/vpc/docs/reserve-static-external-ip-address)

앱 VM에 [Caddy 공식 문서의 Ubuntu 설치 절차](https://caddyserver.com/docs/install#debian-ubuntu-raspbian)대로 Caddy를 설치합니다.

설치 후 설정 파일을 엽니다.

```bash
sudo nano /etc/caddy/Caddyfile
```

내용을 다음처럼 설정합니다.

```caddyfile
api.본인도메인 {
    reverse_proxy 127.0.0.1:8000
}
```

적용합니다.

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

DNS가 앱 VM을 가리키고 80·443 포트가 열려 있어야 HTTPS 인증서 발급이 진행됩니다. [Caddy HTTPS 동작 조건](https://caddyserver.com/docs/automatic-https)

다음 주소를 확인합니다.

```text
https://api.본인도메인/health
https://api.본인도메인/docs
```

**11. 프론트엔드와 연결**

프론트엔드 배포 환경변수에 다음 값을 넣고 다시 빌드·배포합니다.

```dotenv
VITE_API_BASE_URL=https://api.본인도메인
```

앱 VM의 `OSSGUARD_CORS_ORIGINS`에는 이 프론트엔드의 **정확한 origin**이 들어 있어야 합니다. 경로나 마지막 `/`는 넣지 않습니다.

```dotenv
OSSGUARD_CORS_ORIGINS=["https://frontend.example.com"]
```

백엔드 `.env`를 변경했다면 컨테이너를 다시 생성해 반영합니다.

```bash
docker compose up -d --force-recreate api result-worker
```

대시보드의 팀 토큰 입력란에는 앱 VM에서 정한 `OSSGUARD_API_TOKEN`을 입력합니다.

**12. 실제 검증으로 마지막 확인**

대시보드에서 아래 순서로 진행합니다.

1. 공개 샘플 URL 등록
2. `main` 소스 확인
3. `pydantic` 목표 버전 `2.12.5` 선택
4. 업무 테스트 검토·승인
5. 검증 시작
6. 결과에서 배송 호출 `1회 → 0회` 확인
7. 목표를 `1.10.24`로 바꿔 동일 버전 대조군 통과 확인

접수 후 계속 대기 중이면 **Runner 서비스**, 완료됐는데 설명·메일만 처리되지 않으면 **결과 Worker** 로그를 확인하면 됩니다.

처음에는 위에서 검토한 샘플 저장소만 허용하세요. 다른 프로그램까지 실행 대상으로 받을 때는 [현재 GCP 운영 문서](C:/Project/cbu-hackathon/backend/docs/gcp.md)에 적힌 **빌드 컨테이너의 VM 메타데이터·VPC 접근 차단**도 호스트에 적용·시험해야 합니다. 이 부분은 현재 코드가 자동 설정하지 않습니다.

여기서는 배포 절차를 설명했으며, 실제 GCP 리소스 생성이나 서버 설정 변경은 하지 않았습니다.