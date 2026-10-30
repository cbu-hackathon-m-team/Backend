from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class AllowedOperator(str, Enum):
    EQ = "eq"
    GTE = "gte"
    EVENT_COUNT = "event_count"
    EVENT_ORDER = "event_order"


class VerdictType(str, Enum):
    PASS = "PASS"                                 # 규칙 충족 (통과)
    REGRESSION_SUSPECTED = "REGRESSION_SUSPECTED" # 기준 통과 + 후보 위반 (회귀 의심)
    BASE_CHECK_REQUIRED = "BASE_CHECK_REQUIRED"   # 기준 환경부터 위반 (기준 확인 필요)
    EXECUTION_ERROR = "EXECUTION_ERROR"           # 실행 오류
    UNVERIFIED = "UNVERIFIED"                     # 미검증


class ValidationState(str, Enum):
    PREPARING = "PREPARING"
    RUNNING_BASE = "RUNNING_BASE"
    RUNNING_CANDIDATE = "RUNNING_CANDIDATE"
    COMPARING = "COMPARING"
    ANALYZING = "ANALYZING"
    CLEANUP = "CLEANUP"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"


# --- F01: 프로젝트 설정 스키마 ---
class ProjectCreate(BaseModel):
    name: str = Field(default="주문-배송 Node-RED 업데이트 검증")
    target_type: str = Field(default="node-red")
    base_version: str = Field(default="nodered/node-red:3.0.2")
    candidate_version: str = Field(default="nodered/node-red:3.1.0")
    base_artifact_id: str = Field(default="flow_normal_v1")
    candidate_artifact_id: str = Field(
        default="flow_defect_missing_shipping",
        description="flow_normal_v1(정상 대조) | flow_defect_missing_shipping(배송 누락 결함) | flow_fixed_v2(수정 완료)",
    )


# --- F02: 업무 규칙 등록 스키마 ---
class LogicalStep(BaseModel):
    step_key: str = Field(..., example="shipping_request")
    label: str = Field(..., example="배송 요청")
    node_id: str = Field(..., example="node_shp_01")


class RuleUpdate(BaseModel):
    natural_language_rule: str = Field(
        default="결제가 완료되고 재고가 주문 수량 이상이면 수량만큼 재고를 차감한 뒤 배송을 1회 요청한다. 결제 실패 또는 재고 부족이면 차감·배송을 하지 않는다."
    )
    input_schema: Dict[str, str] = Field(
        default={"payment_status": "string", "stock": "positive_int", "order_qty": "positive_int"}
    )
    logical_steps: List[LogicalStep] = Field(
        default=[
            LogicalStep(step_key="order_received", label="주문 접수", node_id="node_ord_01"),
            LogicalStep(step_key="payment_check", label="결제 확인", node_id="node_pay_01"),
            LogicalStep(step_key="stock_deduct", label="재고 차감", node_id="node_stk_01"),
            LogicalStep(step_key="shipping_request", label="배송 요청", node_id="node_shp_01"),
        ]
    )


# --- F03 & F04: 테스트 케이스 및 기대 조건 스키마 ---
class ExpectationRule(BaseModel):
    operator: AllowedOperator
    target_field_or_step: str = Field(..., example="shipping_request")
    expected_value: Any = Field(..., example=1)


class TestCaseItem(BaseModel):
    case_id: str = Field(..., example="T01")
    description: str = Field(..., example="결제 완료, 재고 10, 주문 2 -> 재고 8 / 배송 요청 1회")
    inputs: Dict[str, Any] = Field(..., example={"payment_status": "COMPLETED", "stock": 10, "order_qty": 2})
    expectations: List[ExpectationRule]


class TestSuiteUpdate(BaseModel):
    cases: List[TestCaseItem]
    confirmed: bool = Field(default=True, description="사용자가 기대 결과를 최종 확정했는지 여부")


# --- F06: 실행 어댑터 공통 결과 계약 ---
class NormalizedEvent(BaseModel):
    step_key: str
    event_type: str
    payload: Dict[str, Any]
    sequence: int


class CommonExecutionResult(BaseModel):
    run_id: str
    env_id: str  # "base" 또는 "candidate"
    case_id: str
    response: Dict[str, Any]
    events: List[NormalizedEvent]
    logs: List[str]
    step_map: Dict[str, str]
    artifact_refs: Dict[str, str]
    execution_error: Optional[str] = None


# --- F05, F10, F11: 검증 시작 및 결과 조회 스키마 ---
class ValidationCreateRequest(BaseModel):
    project_id: str
    test_suite_id: str


class AIAnalysisResult(BaseModel):
    status: str = Field(default="SUCCESS", description="SUCCESS | FAILED")
    confirmed_facts: List[str] = []
    root_cause_candidates: List[str] = []
    verification_methods: List[str] = []
    error_message: Optional[str] = None