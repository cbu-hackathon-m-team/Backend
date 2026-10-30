"""검증 실행기에서 쓰는 데이터 형식.

FastAPI 쪽(PUT /api/test-suites/{id}, POST /api/validations)에서 확정된 테스트 스위트를 받아
모조 입력으로 바꾸고, 각 환경에 넣은 결과를 담는다. 모조 환경 자체의 형태(컨테이너 구성, 초기화 API)는
아직 정해지지 않았으므로 환경은 주소만 가진 EnvTarget으로 추상화한다.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class Check(BaseModel):
    """기대 결과 비교식. 명세서에서 허용한 연산만 쓴다.

    - eq / gte    : target은 값 경로 ("response.status", "response.body.x", "state.stock.SKU-1")
    - event_count : target은 이벤트의 step_key, value는 기대 건수
    - event_order : value는 step_key 목록. 관측 이벤트 중 목록에 있는 것만 순서대로 뽑아 정확히 일치해야 한다
    """

    op: Literal["eq", "gte", "event_count", "event_order"]
    target: str = "*"
    value: Any

    @model_validator(mode="after")
    def _check_value(self):
        if self.op == "gte" and not _is_number(self.value):
            raise ValueError("gte value must be a number")
        if self.op == "event_count" and not (isinstance(self.value, int) and not isinstance(self.value, bool) and self.value >= 0):
            raise ValueError("event_count value must be a non-negative integer")
        if self.op == "event_order" and not (
            isinstance(self.value, list) and self.value and all(isinstance(v, str) for v in self.value)
        ):
            raise ValueError("event_order value must be a non-empty list of step keys")
        return self


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


class Event(BaseModel):
    """실제 처리 기록 한 건 (공통 결과 형식의 events 항목)."""

    step_key: str  # 논리 단계 이름 (예: "shipping.requested")
    event_type: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    sequence: int


class Observation(BaseModel):
    """한 환경에서 한 케이스를 실행해 관측한 결과. 판정 엔진의 입력이다."""

    env_id: str
    case_id: str
    delivered: bool
    error: str | None = None
    response_status: int | None = None
    response_body: Any = None
    events: list[Event] = Field(default_factory=list)
    state: dict[str, Any] = Field(default_factory=dict)  # 실행 후 상태 (예: 재고)

    @classmethod
    def from_injection(
        cls, result: InjectionResult, events: list[Event] | None = None, state: dict[str, Any] | None = None
    ) -> "Observation":
        """주입 결과에 수집 단계에서 얻은 이벤트·상태를 붙인다."""
        return cls(
            env_id=result.env_id,
            case_id=result.case_id,
            delivered=result.delivered,
            error=result.error,
            response_status=result.response_status,
            response_body=result.response_body,
            events=events or [],
            state=state or {},
        )


class CheckResult(BaseModel):
    check: Check
    passed: bool
    actual: Any = None
    reason: str | None = None


class EnvVerdict(BaseModel):
    """한 환경에서 한 케이스가 확정 기준을 만족했는지."""

    env_id: str
    status: Literal["pass", "fail", "not_run"]
    checks: list[CheckResult] = Field(default_factory=list)
    reason: str | None = None


class Difference(BaseModel):
    """기준·후보 결과의 단순 차이. 규칙 위반 여부와 별개로 표시한다."""

    path: str
    base: Any
    cand: Any


CaseVerdictKind = Literal["pass", "regression_suspected", "baseline_needs_check", "unverified"]


class CaseVerdict(BaseModel):
    case_id: str
    verdict: CaseVerdictKind
    base: EnvVerdict
    cand: EnvVerdict
    differences: list[Difference] = Field(default_factory=list)


class ValidationVerdict(BaseModel):
    judge_version: str
    overall: Literal["pass", "regression_suspected", "baseline_needs_check", "incomplete"]
    counts: dict[str, int]
    cases: list[CaseVerdict]
