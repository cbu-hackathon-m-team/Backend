from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


class AllowedOperator(str, Enum):
    EQ = "eq"
    GTE = "gte"
    EVENT_COUNT = "event_count"
    EVENT_ORDER = "event_order"


class ValidationState(str, Enum):
    PREPARING = "PREPARING"
    RUNNING_BASE = "RUNNING_BASE"
    RUNNING_CANDIDATE = "RUNNING_CANDIDATE"
    COMPARING = "COMPARING"
    ANALYZING = "ANALYZING"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"


class VerdictType(str, Enum):
    PASS = "PASS"
    REGRESSION_SUSPECTED = "REGRESSION_SUSPECTED"
    BASE_CHECK_REQUIRED = "BASE_CHECK_REQUIRED"
    EXECUTION_ERROR = "EXECUTION_ERROR"
    UNVERIFIED = "UNVERIFIED"


class ExpectationRule(BaseModel):
    operator: AllowedOperator
    target_field_or_step: str
    expected_value: Any


class TestCaseItem(BaseModel):
    case_id: str
    description: str
    inputs: Dict[str, Any]
    expectations: List[ExpectationRule]


class ProjectCreate(BaseModel):
    name: str = Field(default="주문-결제-재고-배송 통합 검증")
    target_type: str = Field(default="NODE_RED")
    base_version: str = Field(default="3.0.2")
    candidate_version: str = Field(default="3.1.0")
    base_artifact_id: str = Field(default="flow_base_v1")
    candidate_artifact_id: str = Field(default="flow_defect_missing_shipping")


class RuleUpdate(BaseModel):
    model_config = ConfigDict(extra="allow")

    business_rules: List[str] = Field(
        default=[
            "결제 완료 및 재고 충분 시 재고 차감 후 배송 요청 1회 발생",
            "결제 실패 또는 재고 부족 시 재고 유지 및 배송 요청 0회",
        ]
    )
    target_fields: List[str] = Field(default=["remaining_stock", "http_status"])
    logical_steps: List[str] = Field(
        default=["order_received", "payment_check", "stock_deduct", "shipping_request"]
    )


class TestSuiteUpdate(BaseModel):
    confirmed: bool = Field(default=True)
    cases: List[TestCaseItem] = Field(
        default=[
            TestCaseItem(
                case_id="T01",
                description="결제 완료, 재고 10, 주문 2 -> 재고 8 / 배송 요청 1회",
                inputs={"payment_status": "COMPLETED", "stock": 10, "order_qty": 2},
                expectations=[
                    ExpectationRule(operator=AllowedOperator.EQ, target_field_or_step="remaining_stock", expected_value=8),
                    ExpectationRule(operator=AllowedOperator.EVENT_COUNT, target_field_or_step="shipping_request", expected_value=1),
                ],
            ),
            TestCaseItem(
                case_id="T02",
                description="결제 완료, 재고 2, 주문 2 (경계 조건) -> 재고 0 / 배송 요청 1회",
                inputs={"payment_status": "COMPLETED", "stock": 2, "order_qty": 2},
                expectations=[
                    ExpectationRule(operator=AllowedOperator.EQ, target_field_or_step="remaining_stock", expected_value=0),
                    ExpectationRule(operator=AllowedOperator.EVENT_COUNT, target_field_or_step="shipping_request", expected_value=1),
                ],
            ),
            TestCaseItem(
                case_id="T03",
                description="결제 실패, 재고 10, 주문 2 -> 재고 10 유지 / 배송 요청 0회",
                inputs={"payment_status": "FAILED", "stock": 10, "order_qty": 2},
                expectations=[
                    ExpectationRule(operator=AllowedOperator.EQ, target_field_or_step="remaining_stock", expected_value=10),
                    ExpectationRule(operator=AllowedOperator.EVENT_COUNT, target_field_or_step="shipping_request", expected_value=0),
                ],
            ),
            TestCaseItem(
                case_id="T04",
                description="결제 완료, 재고 1, 주문 2 (재고 부족) -> 재고 1 유지 / 배송 요청 0회",
                inputs={"payment_status": "COMPLETED", "stock": 1, "order_qty": 2},
                expectations=[
                    ExpectationRule(operator=AllowedOperator.EQ, target_field_or_step="remaining_stock", expected_value=1),
                    ExpectationRule(operator=AllowedOperator.EVENT_COUNT, target_field_or_step="shipping_request", expected_value=0),
                ],
            ),
        ]
    )


class ValidationCreateRequest(BaseModel):
    project_id: str = Field(default="proj_xxxxx")
    test_suite_id: str = Field(default="suite_yyyyy")


class NormalizedEvent(BaseModel):
    step_key: str
    event_type: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    sequence: int


class CommonExecutionResult(BaseModel):
    run_id: str
    env_id: str
    case_id: str
    response: Dict[str, Any] = Field(default_factory=dict)
    events: List[NormalizedEvent] = Field(default_factory=list)
    logs: List[str] = Field(default_factory=list)
    step_map: Dict[str, str] = Field(default_factory=dict)
    artifact_refs: Dict[str, str] = Field(default_factory=dict)
    execution_error: bool = False


class AIAnalysisResult(BaseModel):
    status: str
    confirmed_facts: List[str] = Field(default_factory=list)
    root_cause_candidates: List[str] = Field(default_factory=list)
    verification_methods: List[str] = Field(default_factory=list)
    error_message: Optional[str] = None