# 모조 환경 (Mock Environment) 정리

검증 실행기가 테스트를 돌리는 대상인 **모조 환경**의 정의와, 이를 실행용 VM에 Docker 컨테이너로 올리는 방법을 정리한다.

---

## 1. 모조 환경이란

같은 주문 처리 업무를 두 상황으로 재현해 둔 격리된 실행 환경이다.

| 환경 | n8n 버전 | 워크플로 | 역할 |
| --- | --- | --- | --- |
| **기준(base)** | 기존 버전 | 정상 흐름 `order_ok.json` | 업데이트 전 정상 동작 기준 |
| **후보(cand)** | 업데이트 대상 버전 | 결함 흐름 `order_broken.json` | 업데이트 후 오류가 생긴 상황 |

각 환경은 **n8n 컨테이너 1개 + 모의 서비스 컨테이너 1개**로 구성한다.

- **n8n**: 실제 업무 흐름(주문 → 결제 확인 → 재고 차감 → 배송 요청)을 실행한다.
- **모의 서비스(mock)**: 결제·재고·배송 같은 외부 시스템을 대신하는 작은 FastAPI 서버다. n8n이 보내는 호출을 받아 **응답하고 기록**한다.

### 왜 모의 서비스가 필요한가

결함 흐름도 HTTP 응답은 200으로 정상이다. 오류는 "배송 요청이 실제로 나가지 않았다"는 사실로만 드러난다.
그래서 n8n 내부를 들여다보는 대신 **n8n이 바깥으로 보낸 호출을 모의 서비스가 기록**하고, 실행기는 그 기록을 판정 근거로 쓴다.

```
검증 실행기
  ① POST mock/__reset          케이스별 초기 상태 주입 (재고 10, 결제 완료 등)
  ② POST n8n/webhook/order     모조 입력 전달 (주문)
        └─ n8n → mock 결제 확인 → mock 재고 조회/차감 → mock 배송 요청
  ③ GET  mock/__records        실제로 들어온 호출 기록과 최종 상태 수집
  ④ 기대 결과와 비교 → 판정
```

---

## 2. 구성 요소

### 2-1. 컨테이너와 네트워크

```
┌──────────── net-a ────────────┐    ┌──────────── net-b ────────────┐
│ n8n-base  :5678 ──▶ mock-a    │    │ n8n-cand  :5679 ──▶ mock-b    │
│ (기존 버전, 정상 흐름)  :8001 │    │ (새 버전, 결함 흐름)    :8002 │
│                 별칭 "mock"   │    │                 별칭 "mock"   │
└───────────────────────────────┘    └───────────────────────────────┘
            ▲                                     ▲
            └──────── 검증 실행기 (호스트 포트로 접근) ────────┘
```

- 두 환경을 **서로 다른 Docker 네트워크**에 둔다. n8n-base는 mock-b에 닿을 수 없어서 기록이 섞이지 않는다.
- 각 네트워크 안에서 모의 서비스에 **같은 별칭 `mock`** 을 붙인다. 두 워크플로 모두 `http://mock:8000`을 호출하지만 각자 자기 모의 서비스로 간다. 그래서 워크플로 JSON을 환경마다 고칠 필요가 없다.
- 실행기는 VM의 호스트 포트(5678/5679/8001/8002)로 접근한다.

### 2-2. 워크플로

```
Webhook (POST /webhook/order, 응답 모드: 마지막 노드 종료 시)
 → HTTP GET  http://mock:8000/payments/{order_id}
 → IF  status == "paid"
 → HTTP GET  http://mock:8000/inventory/{sku}
 → IF  stock >= qty
 → HTTP POST http://mock:8000/inventory/decrement
 → HTTP POST http://mock:8000/shipping/requests     ← 결함 버전은 이 연결만 끊는다
```

- **정상 흐름**: n8n UI에서 위 흐름을 만들고 export한 JSON이다.
- **결함 흐름**: 정상 JSON을 복사해 `connections`에서 "재고 차감 → 배송 요청" 연결 하나만 지운 JSON이다. 노드 구성은 같고 연결 하나만 다르므로 흐름 diff가 명확하다.
- 응답 모드를 "마지막 노드 종료 시(`lastNode`)"로 두면 흐름이 끝난 뒤 응답하므로 실행기가 결과를 폴링할 필요가 없다. 결함 흐름은 재고 차감에서 끝나도 200을 돌려준다. 시연 장면과 같다.
- 두 파일은 **같은 워크플로 ID**를 쓴다. 컨테이너가 재시작되어 다시 import해도 같은 워크플로를 덮어써서 중복이 생기지 않는다.

### 2-3. 모의 서비스 API

| 메서드 | 경로 | 용도 | 기록되는 이벤트 |
| --- | --- | --- | --- |
| POST | `/__reset` | 기록 초기화 + 초기 상태(seed) 주입 | – |
| GET | `/__records` | 기록된 이벤트와 현재 상태 조회 | – |
| GET | `/payments/{order_id}` | 결제 상태 조회 | `payment.checked` |
| GET | `/inventory/{sku}` | 재고 조회 | – |
| POST | `/inventory/decrement` | 재고 차감 | `inventory.decremented` |
| POST | `/shipping/requests` | 배송 요청 | `shipping.requested` |

`/__` 로 시작하는 경로는 실행기 전용 제어 API이고, 나머지는 n8n이 호출하는 업무 API다.

---

## 3. 디렉터리 구조

```
mock-env/
├── docker-compose.yml
├── .env                    # n8n 이미지 버전, 워크플로 ID, 흐름 파일 선택
├── n8n/
│   └── entrypoint.sh       # 흐름 import → 활성화 → n8n 시작
├── flows/
│   ├── order_ok.json       # 정상 흐름
│   └── order_broken.json   # 결함 흐름 (배송 연결 제거)
└── mock_service/
    ├── Dockerfile
    ├── requirements.txt
    └── main.py
```

---

## 4. 파일 내용

### 4-1. `mock_service/main.py`

```python
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()
state: dict = {"stock": {}, "payments": {}}
records: list[dict] = []


def record(step_key: str, payload: dict) -> None:
    records.append({"sequence": len(records) + 1, "step_key": step_key, "payload": payload})


class Seed(BaseModel):
    stock: dict[str, int]
    payments: dict[str, str]  # order_id -> "paid" | "failed"


@app.post("/__reset")
def reset(seed: Seed):
    records.clear()
    state["stock"] = dict(seed.stock)
    state["payments"] = dict(seed.payments)
    return {"ok": True}


@app.get("/__records")
def get_records():
    return {"events": records, "state": state}


@app.get("/payments/{order_id}")
def payment(order_id: str):
    status = state["payments"].get(order_id, "failed")
    record("payment.checked", {"order_id": order_id, "status": status})
    return {"order_id": order_id, "status": status}


@app.get("/inventory/{sku}")
def inventory(sku: str):
    return {"sku": sku, "stock": state["stock"].get(sku, 0)}


@app.post("/inventory/decrement")
def decrement(body: dict):
    state["stock"][body["sku"]] -= body["qty"]
    record("inventory.decremented", body)
    return {"stock": state["stock"][body["sku"]]}


@app.post("/shipping/requests")
def shipping(body: dict):
    record("shipping.requested", body)
    return {"shipment_id": "SHP-" + body["order_id"]}
```

### 4-2. `mock_service/requirements.txt`

```
fastapi
uvicorn
```

### 4-3. `mock_service/Dockerfile`

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### 4-4. `n8n/entrypoint.sh`

```sh
#!/bin/sh
set -e

# 흐름 파일이 있으면 가져와서 활성화한다 (처음 흐름을 만들 때는 파일 없이 시작)
if [ -f /flows/order.json ]; then
  n8n import:workflow --input=/flows/order.json
  n8n update:workflow --id="$WORKFLOW_ID" --active=true
fi

exec n8n start
```

> ⚠️ n8n 버전에 따라 import 시 워크플로가 비활성화되거나 활성화 명령이 바뀔 수 있다(`update:workflow` 대신 다른 명령).
> 사용할 **두 버전 모두에서** 기동 후 `POST /webhook/order`가 404 없이 응답하는지 확인한다.

> ⚠️ Windows에서 작성한 `.sh` 파일은 줄바꿈이 CRLF라 컨테이너에서 `not found` 오류가 난다.
> 저장소에 `.gitattributes`로 `*.sh text eol=lf`를 지정하거나 에디터에서 LF로 저장한다.

### 4-5. `.env`

```dotenv
# 실제로 사용할 두 n8n 버전으로 고정한다 (latest 금지)
N8N_BASE_IMAGE=n8nio/n8n:<기존 버전>
N8N_CAND_IMAGE=n8nio/n8n:<업데이트 대상 버전>

# 두 흐름 파일에 공통으로 들어 있는 워크플로 ID
WORKFLOW_ID=<워크플로 ID>

# 후보 환경에 올릴 흐름 (대조군 실험 시 order_ok.json으로 변경)
CAND_FLOW=order_broken.json
```

### 4-6. `docker-compose.yml`

```yaml
x-n8n-common: &n8n-common
  entrypoint: ["/bin/sh", "/entrypoint.sh"]
  environment:
    WORKFLOW_ID: ${WORKFLOW_ID}
    N8N_SECURE_COOKIE: "false"               # VM IP로 http 접속해 UI를 쓸 때 필요
    N8N_DIAGNOSTICS_ENABLED: "false"
    N8N_VERSION_NOTIFICATIONS_ENABLED: "false"
    N8N_PERSONALIZATION_ENABLED: "false"
    GENERIC_TIMEZONE: Asia/Seoul
  restart: "no"
  labels:
    ossguard.role: mock-env                  # 정리(F13) 시 라벨로 잔여 자원 확인

services:
  # ── 기준 환경 ──────────────────────────────
  n8n-base:
    <<: *n8n-common
    image: ${N8N_BASE_IMAGE}
    ports: ["5678:5678"]
    volumes:
      - ./n8n/entrypoint.sh:/entrypoint.sh:ro
      - ./flows/order_ok.json:/flows/order.json:ro
    depends_on: [mock-a]
    networks: [net-a]

  mock-a:
    build: ./mock_service
    ports: ["8001:8000"]
    labels: { ossguard.role: mock-env }
    networks:
      net-a:
        aliases: [mock]

  # ── 후보 환경 ──────────────────────────────
  n8n-cand:
    <<: *n8n-common
    image: ${N8N_CAND_IMAGE}
    ports: ["5679:5678"]
    volumes:
      - ./n8n/entrypoint.sh:/entrypoint.sh:ro
      - ./flows/${CAND_FLOW}:/flows/order.json:ro
    depends_on: [mock-b]
    networks: [net-b]

  mock-b:
    build: ./mock_service
    ports: ["8002:8000"]
    labels: { ossguard.role: mock-env }
    networks:
      net-b:
        aliases: [mock]

networks:
  net-a: {}
  net-b: {}
```

포인트:

- **n8n 데이터 볼륨을 일부러 두지 않는다.** `docker compose down` 후 다시 올리면 n8n이 깨끗한 상태로 시작하고 흐름 파일만 다시 import된다. 실행마다 같은 초기 상태가 보장된다.
- 버전과 흐름 파일을 `.env`로 분리해 두었다. 후보 자동 업데이트(F23)는 `N8N_CAND_IMAGE`만 바꿔 `n8n-cand`를 다시 올리는 동작으로 구현할 수 있다.

---

## 5. 컨테이너로 올리는 순서

### 5-1. VM 준비 (최초 1회)

```bash
# Docker Engine + Compose 플러그인 설치 (Ubuntu 기준)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # 재로그인 후 sudo 없이 docker 사용

docker --version
docker compose version
```

방화벽은 5678 / 5679 / 8001 / 8002 포트를 **백엔드(FastAPI) 서버 IP에서만** 열어 둔다.

`mock-env/` 폴더를 VM으로 복사한다(git clone 또는 scp).

### 5-2. 정상 흐름 처음 만들기 (최초 1회)

흐름 JSON이 아직 없으므로 n8n UI에서 한 번 만들어 export한다.

```bash
cd mock-env
docker compose up -d --build n8n-base mock-a     # 흐름 파일 없이 기동 (import 건너뜀)
```

1. 브라우저에서 `http://<VM_IP>:5678` 접속 → 소유자 계정 생성(로컬 테스트용).
2. 2-2의 흐름을 만든다. 모든 HTTP Request 노드의 주소는 `http://mock:8000/...` 으로 쓴다.
3. 저장 후 URL에 보이는 워크플로 ID를 확인해 `.env`의 `WORKFLOW_ID`에 적는다.
4. CLI로 export 한다.

```bash
docker compose exec n8n-base n8n export:workflow --id=<WORKFLOW_ID> --output=/tmp/order_ok.json
docker compose cp n8n-base:/tmp/order_ok.json ./flows/order_ok.json
```

5. `order_ok.json`을 복사해 `order_broken.json`을 만들고, `connections` 안에서 재고 차감 노드 → 배송 요청 노드 연결 항목만 지운다.
6. `docker compose down`으로 정리한다.

### 5-3. 두 환경 기동

```bash
cd mock-env
docker compose up -d --build
docker compose ps
docker compose logs -f n8n-base n8n-cand     # import / 활성화 / 시작 로그 확인
```

### 5-4. 동작 확인 (수동 T01)

```bash
# 기준 환경
curl -X POST http://<VM_IP>:8001/__reset -H 'Content-Type: application/json' \
  -d '{"stock":{"SKU-1":10},"payments":{"ORD-T01":"paid"}}'
curl -X POST http://<VM_IP>:5678/webhook/order -H 'Content-Type: application/json' \
  -d '{"order_id":"ORD-T01","sku":"SKU-1","qty":2}'
curl http://<VM_IP>:8001/__records

# 후보 환경 (포트만 다름)
curl -X POST http://<VM_IP>:8002/__reset -H 'Content-Type: application/json' \
  -d '{"stock":{"SKU-1":10},"payments":{"ORD-T01":"paid"}}'
curl -X POST http://<VM_IP>:5679/webhook/order -H 'Content-Type: application/json' \
  -d '{"order_id":"ORD-T01","sku":"SKU-1","qty":2}'
curl http://<VM_IP>:8002/__records
```

기대 결과:

| 환경 | webhook 응답 | `state.stock.SKU-1` | `shipping.requested` 이벤트 |
| --- | --- | --- | --- |
| 기준 (mock-a) | 200 | 8 | 1건 |
| 후보 (mock-b) | 200 | 8 | **0건** |

### 5-5. 자주 쓰는 운영 명령

```bash
# 후보만 다른 버전으로 교체 (자동 업데이트 동작과 같음)
N8N_CAND_IMAGE=n8nio/n8n:<새 버전> docker compose up -d n8n-cand

# 대조군: 새 버전 + 정상 흐름
CAND_FLOW=order_ok.json docker compose up -d --force-recreate n8n-cand

# 전체 정리
docker compose down --remove-orphans

# 라벨로 잔여 컨테이너 확인
docker ps -a --filter label=ossguard.role=mock-env
```

---

## 6. 실행기와의 연결 정보

검증 실행기는 아래 주소만 알면 된다.

```python
ENVS = {
    "base": {"n8n": "http://<VM_IP>:5678", "mock": "http://<VM_IP>:8001"},
    "cand": {"n8n": "http://<VM_IP>:5679", "mock": "http://<VM_IP>:8002"},
}
```

- **준비 확인**: 기동 직후에는 흐름 활성화가 끝나지 않아 webhook이 404를 줄 수 있다. 실행기는 `GET /healthz`(n8n)와 `GET /__records`(mock)가 200을 줄 때까지 기다린 뒤, 첫 webhook 호출에서 404가 나면 짧게 재시도한다.
- **케이스 격리**: 케이스마다 먼저 `/__reset`을 호출해 재고·결제 상태와 기록을 초기화한다. 이전 케이스의 기록이 섞이지 않는다.

---

## 7. 주의 사항

- 이 결함은 **의도적으로 주입한 맞춤 흐름 결함**이다. n8n 원본의 실제 업데이트 버그를 발견했다고 표현하지 않는다.
- 대조군(새 버전 + 정상 흐름)이 통과해야 "버전 업데이트가 아니라 흐름 변경이 원인"이라고 설명할 수 있다.
- 기능명세서는 첫 대상을 Node-RED로 적어 두었다. n8n으로 진행한다면 팀 내에서 명세를 맞춘다. 구조(대상 앱 + 모의 서비스 + 네트워크 격리)는 Node-RED에서도 같다.
