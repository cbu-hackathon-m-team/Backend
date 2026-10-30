"""검증 실행기에서 쓰는 데이터 형식.

FastAPI 쪽(PUT /api/test-suites/{id}, POST /api/validations)에서 확정된 테스트 스위트를 받아
모조 입력으로 바꾸고, 각 환경에 넣은 결과를 담는다. 모조 환경 자체의 형태(컨테이너 구성, 초기화 API)는
아직 정해지지 않았으므로 환경은 주소만 가진 EnvTarget으로 추상화한다.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class Check(BaseModel):
    """기대 결과 비교식. 명세서에서 허용한 연산만 쓴다."""

    op: Literal["eq", "gte", "event_count", "event_order"]
    target: str  # 예: "response.status", "state.stock.SKU-1", "shipping.requested"
    value: Any


class TestCase(BaseModel):
    """테스트 케이스 하나 (예: T01 결제 완료, 재고 10, 주문 2)."""

    case_id: str
    name: str = ""
    input: dict[str, Any]  # 대상 앱에 보낼 요청 본문
    initial_state: dict[str, Any] = Field(default_factory=dict)  # 실행 전 맞출 재고·결제 등 초기 상태
    expect: list[Check] = Field(default_factory=list)


class TestSuite(BaseModel):
    """사용자가 검토·확정한 테스트 묶음."""

    id: str
    project_id: str
    version: int = 1
    confirmed: bool = False
    cases: list[TestCase]


class EnvTarget(BaseModel):
    """모조 입력을 받을 환경 하나 (기준 / 후보).

    주소는 모조 환경이 확정되면 실행 어댑터(start)가 채워 넘긴다.
    """

    env_id: str  # "base" | "cand"
    input_url: str  # 모조 입력을 전달할 주소 (예: 워크플로 webhook)
    reset_url: str | None = None  # 케이스별 초기 상태를 주입할 주소. 없으면 초기 상태가 있는 케이스는 실행하지 않는다.
    timeout_s: float = 15.0


class MockInput(BaseModel):
    """환경에 넣을 모조 입력 한 건. 확정 스위트에서 복사해 만들며 원본이 바뀌어도 영향받지 않는다."""

    run_id: str
    suite_id: str
    suite_version: int
    case_id: str
    input: dict[str, Any]
    initial_state: dict[str, Any] = Field(default_factory=dict)


class InjectionResult(BaseModel):
    """모조 입력을 한 환경에 넣은 결과. 판정은 하지 않고 관측한 사실만 담는다."""

    run_id: str
    env_id: str
    case_id: str
    delivered: bool  # 요청이 전달되고 응답을 받았는지 (응답 코드와 무관)
    failed_stage: Literal["seed", "input"] | None = None
    error: str | None = None
    response_status: int | None = None
    response_body: Any = None
    started_at: datetime
    elapsed_ms: float | None = None
